"""
MPS Synthetic Data Generator
=============================
Generates ground-truth calcium imaging data:
  - ground_truth_fluorescence.npy
  - ground_truth_footprints.npy
  - ground_truth_centers.npy
  - ground_truth_radii.npy
  - ground_truth_metadata.json
  - ground_truth_visualization.png
  - ground_truth_clean.avi  (30s clean reference)

Usage:
  Set OUTPUT_DIR below, then: python MPS_Data_Generation.py
"""

import json, os, shutil, subprocess, threading, warnings
import matplotlib; matplotlib.use('Agg')
import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
from scipy.ndimage import zoom
warnings.filterwarnings('ignore')

# =============================================================================
# CONFIG  — edit these
# =============================================================================
OUTPUT_DIR = r""   # e.g. r"C:\Users\you\Desktop\MPS Sample Data"
SEED       = 42

FPS             = 10
DURATION_H      = 3
N_FRAMES        = FPS * 3600 * DURATION_H   # 108,000 frames
H, W            = 600, 600

# Neuron placement
N_NEURONS    = 100
MIN_RADIUS   = 4    # px
MAX_RADIUS   = 8    # px
MIN_SPACING  = 25   # px center-to-center
BORDER_MARGIN = 10  # px from FOV edge
ROI_CENTER   = (W // 2, H // 2)
ROI_RADIUS   = 170  # px — core seed zone
ROI_OVERFLOW = 25   # px — edge spillover for organic boundary

# Photometry
BASELINE       = 800.0
NOISE_SIGMA    = 20.0
PEAK_DF_F      = 2.0
NEUROPIL_RATIO = 0.15

# GCaMP8f AR(2) spike model @ 10 fps
AR1, AR2 = 0.90, -0.10

CLEAN_AVI_DURATION_S = 30


# =============================================================================
# 1. NEURON PLACEMENT
# =============================================================================
def place_neurons(n, rng):
    cx0, cy0 = ROI_CENTER
    centers, radii, attempts = [], [], 0

    while len(centers) < n and attempts < 5_000_000:
        attempts += 1
        filled = len(centers) / n
        max_r  = ROI_RADIUS + (ROI_OVERFLOW if filled >= 0.85 else 0) - MAX_RADIUS
        angle  = rng.uniform(0, 2 * np.pi)
        dist   = max_r * np.sqrt(rng.uniform(0, 1))
        x, y   = int(round(cx0 + dist * np.cos(angle))), int(round(cy0 + dist * np.sin(angle)))
        r      = int(rng.integers(MIN_RADIUS, MAX_RADIUS + 1))

        if not (BORDER_MARGIN <= x - r and x + r <= W - BORDER_MARGIN): continue
        if not (BORDER_MARGIN <= y - r and y + r <= H - BORDER_MARGIN): continue
        if all(np.hypot(x - px, y - py) >= MIN_SPACING for (px, py) in centers):
            centers.append((x, y)); radii.append(r)

    if len(centers) < n:
        raise RuntimeError(f"Placed only {len(centers)}/{n} neurons. Adjust spacing/ROI.")
    print(f"  Placed {len(centers)} neurons ({attempts:,} attempts)")
    return np.array(centers, dtype=np.int32), np.array(radii, dtype=np.int32)


# =============================================================================
# 2. FLUORESCENCE  (AR(2) GCaMP8f model, 3 activity tiers)
# =============================================================================
def generate_fluorescence(n_neurons, n_frames, fps, rng):
    tiers = rng.choice(['high', 'sparse', 'low'], size=n_neurons, p=[0.30, 0.40, 0.30])
    rate_ranges = {'high': (0.3, 1.0), 'sparse': (0.05, 0.15), 'low': (0.01, 0.04)}
    amp_ranges  = {'high': (0.6, 1.0), 'sparse': (0.4, 0.9),   'low': (0.2, 0.6)}

    fluor = np.zeros((n_neurons, n_frames), dtype=np.float32)
    for i in range(n_neurons):
        tier    = tiers[i]
        p_spike = rng.uniform(*rate_ranges[tier]) / fps
        spikes  = (rng.random(n_frames) < p_spike).astype(np.float32)
        spikes *= rng.uniform(*amp_ranges[tier], size=n_frames)

        y = np.zeros(n_frames, dtype=np.float64)
        for t in range(2, n_frames):
            y[t] = max(0.0, AR1 * y[t-1] + AR2 * y[t-2] + spikes[t])
        fluor[i] = y.astype(np.float32)

    generate_fluorescence.tiers = tiers
    return fluor


# =============================================================================
# 3. SPATIAL FOOTPRINTS  (elongated, irregular — calibrated from real LHb data)
# =============================================================================
def make_footprints(centers, radii, h, w, rng):
    yy, xx = np.mgrid[0:h, 0:w]
    fp = np.zeros((len(centers), h, w), dtype=np.float32)

    for i, ((cx, cy), r) in enumerate(zip(centers, radii)):
        rl = np.random.default_rng(rng.integers(0, 2**32 - 1))
        axis_ratio  = float(np.clip(np.exp(rl.normal(np.log(2.15), 0.35)), 1.3, 4.0))
        orientation = float(rl.uniform(0, np.pi))
        cos_a, sin_a = np.cos(orientation), np.sin(orientation)
        semi_maj = r * np.sqrt(axis_ratio)
        semi_min = r / np.sqrt(axis_ratio)

        pad = int(np.ceil(semi_maj * 1.4 + 4))
        x0, x1 = max(0, cx - pad), min(w, cx + pad + 1)
        y0, y1 = max(0, cy - pad), min(h, cy + pad + 1)
        lx = (xx[y0:y1, x0:x1] - cx).astype(np.float64)
        ly = (yy[y0:y1, x0:x1] - cy).astype(np.float64)

        lx_r =  cos_a * lx + sin_a * ly
        ly_r = -sin_a * lx + cos_a * ly
        r_norm = np.sqrt((lx_r / semi_maj)**2 + (ly_r / semi_min)**2)

        theta  = np.arctan2(ly_r, lx_r)
        wobble = sum(
            rl.uniform(0.08, 0.18) * np.sin(int(rl.integers(2, 5)) * theta + rl.uniform(0, 2*np.pi))
            for _ in range(int(rl.integers(2, 4)))
        )
        fp[i, y0:y1, x0:x1] = np.clip((r_norm * (1.0 - wobble)) <= 1.0, 0.0, 1.0)

    return fp


# =============================================================================
# 4. VISUALIZATION
# =============================================================================
def visualize_ground_truth(centers, radii, footprints, fluorescence, output_dir):
    tiers    = generate_fluorescence.tiers
    tier_col = {'high': '#ff6b6b', 'sparse': '#ffe66d', 'low': '#4ecdc4'}
    dark     = '#111111'

    fig = plt.figure(figsize=(22, 17), facecolor='#0a0a0a')
    gs  = gridspec.GridSpec(3, 3, figure=fig, hspace=0.45, wspace=0.35)

    ax = fig.add_subplot(gs[0, 0])
    ax.imshow(footprints.mean(0), cmap='hot', origin='upper')
    for i, ((cx, cy), r) in enumerate(zip(centers, radii)):
        ax.add_patch(plt.Circle((cx, cy), r, fill=False, color=tier_col[tiers[i]], lw=0.6, alpha=0.8))
        if i % 10 == 0: ax.text(cx, cy, str(i), color='white', fontsize=4, ha='center', va='center')
    ax.add_patch(plt.Circle(ROI_CENTER, ROI_RADIUS, fill=False, edgecolor='white', lw=1, ls='--', alpha=0.5))
    ax.set_title(f'Mean Projection — {len(centers)} Neurons\n(red=high, yellow=sparse, teal=low)', color='white', fontsize=10)
    ax.axis('off')

    vf  = min(6000, N_FRAMES)
    F_v = fluorescence[:, :vf]
    F_n = (F_v - F_v.min(1, keepdims=True)) / (F_v.max(1, keepdims=True) - F_v.min(1, keepdims=True) + 1e-6)
    ax  = fig.add_subplot(gs[0, 1:])
    ax.imshow(F_n, aspect='auto', cmap='inferno', origin='upper',
              extent=[0, vf/FPS/60, len(centers), 0])
    ax.set_xlabel('Time (min)', color='white'); ax.set_ylabel('Neuron #', color='white')
    ax.set_title('Activity Heatmap — First 10 min', color='white', fontsize=11)
    ax.tick_params(colors='white'); ax.set_facecolor(dark)

    ax  = fig.add_subplot(gs[1, :])
    vf2 = min(18000, N_FRAMES)
    t2  = np.arange(vf2) / FPS / 60
    for off, tier in enumerate(['high', 'sparse', 'low']):
        idxs = [i for i, t in enumerate(tiers) if t == tier]
        if not idxs: continue
        nid = idxs[len(idxs)//2]; tr = fluorescence[nid, :vf2]; mx = tr.max()
        ax.plot(t2, (tr/mx if mx > 0 else tr) + off*1.3,
                color=tier_col[tier], lw=0.7, alpha=0.9, label=f'{tier} (n={nid})')
    ax.set_xlabel('Time (min)', color='white'); ax.set_ylabel('dF/F (offset)', color='white')
    ax.set_title('Example Traces — One Per Tier, 30 min', color='white', fontsize=11)
    ax.legend(loc='upper right', fontsize=9, facecolor='#1a1a1a', labelcolor='white')
    ax.tick_params(colors='white'); ax.set_facecolor(dark)

    for ax, data, title, color, xlabel in [
        (fig.add_subplot(gs[2, 0]), radii,  'Neuron Size Distribution',    '#4ecdc4', 'Radius (px)'),
        (fig.add_subplot(gs[2, 2]), fluorescence.mean(1), 'Mean Activity Per Neuron', None, 'Neuron #'),
    ]:
        if color:
            ax.hist(data, bins=range(MIN_RADIUS, MAX_RADIUS+2), color=color, edgecolor='white', lw=0.5, alpha=0.8)
        else:
            ax.bar(range(len(centers)), data, color=[tier_col[tiers[i]] for i in range(len(centers))], alpha=0.8, width=1.0)
        ax.set_xlabel(xlabel, color='white'); ax.set_ylabel('Count' if color else 'Mean dF/F', color='white')
        ax.set_title(title, color='white', fontsize=11)
        ax.tick_params(colors='white'); ax.set_facecolor(dark)

    ax = fig.add_subplot(gs[2, 1])
    nn = [min(np.hypot(cx-ox, cy-oy) for j,(ox,oy) in enumerate(centers) if j!=i)
          for i,(cx,cy) in enumerate(centers)]
    ax.hist(nn, bins=20, color='#ffe66d', edgecolor='white', lw=0.5, alpha=0.8)
    ax.axvline(MIN_SPACING, color='red', lw=1.5, ls='--', label=f'Min ({MIN_SPACING} px)')
    ax.set_xlabel('Nearest Neighbor Distance (px)', color='white'); ax.set_ylabel('Count', color='white')
    ax.set_title('Nearest Neighbor Spacing', color='white', fontsize=11)
    ax.legend(fontsize=8, facecolor='#1a1a1a', labelcolor='white')
    ax.tick_params(colors='white'); ax.set_facecolor(dark)

    for a in fig.axes:
        for sp in ['top','right']: a.spines[sp].set_visible(False)
        for sp in ['bottom','left']: a.spines[sp].set_color('gray')
        a.set_facecolor(dark)
    fig.suptitle('MPS Synthetic Ground Truth — 100 Neurons (GCaMP8f AR2, 3 Tiers), 3h @ 10 fps',
                 color='white', fontsize=13, y=0.99)
    out = os.path.join(output_dir, 'ground_truth_visualization.png')
    plt.savefig(out, dpi=150, bbox_inches='tight', facecolor='#0a0a0a')
    plt.close()
    print(f"  Saved: {out}")


# =============================================================================
# 5. CLEAN AVI  (noiseless, no motion — ground-truth reference)
# =============================================================================
def _find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg: return ffmpeg
    for c in [r"C:\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe"]:
        if os.path.exists(c): return c
    raise RuntimeError("ffmpeg not found. Add ffmpeg/bin to your PATH.")


def render_clean_avi(footprints, fluorescence, output_dir, duration_s=30):
    n_frames   = FPS * duration_s
    out_path   = os.path.join(output_dir, "ground_truth_clean.avi")
    ffmpeg     = _find_ffmpeg()
    fp_flat    = footprints.reshape(N_NEURONS, -1)
    signal_vol = (fp_flat.T @ (fluorescence[:, :n_frames] * PEAK_DF_F * BASELINE)).astype(np.float32)

    cmd = [ffmpeg, "-y", "-f", "rawvideo", "-vcodec", "rawvideo",
           "-s", f"{W}x{H}", "-pix_fmt", "gray16le", "-r", str(FPS), "-i", "pipe:0",
           "-vcodec", "rawvideo", "-pix_fmt", "gray16le", out_path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    errs = []
    threading.Thread(target=lambda: [errs.append(l) for l in proc.stderr], daemon=True).start()

    for t in range(n_frames):
        frame = np.clip(BASELINE + signal_vol[:, t].reshape(H, W), 0, 65535).astype(np.uint16)
        proc.stdin.write(frame.tobytes())
        if (t + 1) % (FPS * 5) == 0: print(f"    frame {t+1}/{n_frames}")

    proc.stdin.close(); proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{b''.join(errs).decode(errors='replace')}")
    print(f"  Saved: {out_path}  ({os.path.getsize(out_path)/1e6:.1f} MB)")


# =============================================================================
# MAIN
# =============================================================================
def main():
    if not OUTPUT_DIR:
        raise ValueError("Set OUTPUT_DIR at the top of this script before running.")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rng = np.random.default_rng(SEED)

    print("\n[1/6] Placing neurons...")
    centers, radii = place_neurons(N_NEURONS, rng)

    print(f"\n[2/6] Generating fluorescence ({N_FRAMES:,} frames)...")
    fluorescence = generate_fluorescence(N_NEURONS, N_FRAMES, FPS, rng)
    print(f"  dF/F range: [{fluorescence.min():.3f}, {fluorescence.max():.3f}]  "
          f"({fluorescence.nbytes/1e6:.0f} MB)")

    print("\n[3/6] Computing spatial footprints...")
    footprints = make_footprints(centers, radii, H, W, rng)

    print("\n[4/6] Saving ground truth arrays...")
    np.save(os.path.join(OUTPUT_DIR, 'ground_truth_fluorescence.npy'), fluorescence)
    np.save(os.path.join(OUTPUT_DIR, 'ground_truth_footprints.npy'),   footprints)
    np.save(os.path.join(OUTPUT_DIR, 'ground_truth_centers.npy'),      centers)
    np.save(os.path.join(OUTPUT_DIR, 'ground_truth_radii.npy'),        radii)

    tiers = generate_fluorescence.tiers
    json.dump({
        "n_neurons": N_NEURONS, "n_frames": N_FRAMES, "fps": FPS,
        "height": H, "width": W, "baseline_adu": BASELINE,
        "noise_sigma": NOISE_SIGMA, "peak_adu": BASELINE * PEAK_DF_F,
        "min_spacing_px": MIN_SPACING, "ar_coefficients": {"AR1": AR1, "AR2": AR2},
        "gcamp": "GCaMP8f", "tau_rise_s": 0.05, "tau_decay_s": 0.35,
        "centers": centers.tolist(), "radii": radii.tolist(),
        "activity_tiers": tiers.tolist(),
        "activity_groups": {t: [i for i,x in enumerate(tiers) if x==t]
                            for t in ['high', 'sparse', 'low']},
    }, open(os.path.join(OUTPUT_DIR, 'ground_truth_metadata.json'), 'w'), indent=2)
    print("  Metadata saved.")

    print("\n[5/6] Generating visualization...")
    visualize_ground_truth(centers, radii, footprints, fluorescence, OUTPUT_DIR)

    print(f"\n[6/6] Rendering clean AVI ({CLEAN_AVI_DURATION_S}s)...")
    render_clean_avi(footprints, fluorescence, OUTPUT_DIR, CLEAN_AVI_DURATION_S)

    total = sum(os.path.getsize(os.path.join(OUTPUT_DIR, f))
                for f in os.listdir(OUTPUT_DIR)
                if os.path.isfile(os.path.join(OUTPUT_DIR, f)))
    print(f"\nDone — {total/1e6:.1f} MB total in: {OUTPUT_DIR}")


if __name__ == "__main__":
    main()