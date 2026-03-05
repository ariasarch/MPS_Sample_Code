"""
MPS Synthetic Degradation
==========================
Loads ground truth arrays and composites them into degraded raw video
mimicking real miniscope recordings. Feed output AVIs into MPS to validate
pipeline recovery.

Degradation model:
  frame[t] = BASELINE
            + neural_signal      (footprints × fluorescence × PEAK_DF_F × BASELINE)
            + neuropil_haze      (NEUROPIL_RATIO × blurred mean signal)
            + spatial_vignette   (slow tophat background MPS will correct)
            + gaussian_noise     (N(0, NOISE_SIGMA) per pixel)
            + motion             (slow AR(1) drift + sparse large transients)

All constants are matched to MPS_Data_Generation.py.

Usage:
  Set GT_DIR below, then:
    run_sample()  — quick 30s preview as clip_000.avi
    run_full()    — full parallel run, all clips
"""

import json, os, shutil, subprocess, threading
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter

# =============================================================================
# CONFIG  — edit these
# =============================================================================
GT_DIR = r""   # e.g. r"C:\Users\you\Desktop\MPS Sample Data"
SEED   = 42

# =============================================================================
# CONSTANTS  — must match MPS_Data_Generation.py
# =============================================================================
BASELINE       = 800.0
NOISE_SIGMA    = 20.0
PEAK_DF_F      = 2.0
NEUROPIL_RATIO = 0.15
H, W           = 600, 600
FPS            = 10

# Additional raw-data degradations (not present in clean renderer)
NEUROPIL_BLUR    = 35     # px — Gaussian blur for neuropil haze
TOPHAT_BG_AMP    = 150.0  # ADU — peak-to-peak slow spatial vignette
TOPHAT_BG_BLUR   = 80     # px — spatial smoothing of vignette
MOTION_MAX_PX    = 3.0    # px — max slow drift amplitude
MOTION_DRIFT_TAU = 500    # frames — drift autocorrelation time constant
TRANSIENT_RATE   = 0.001  # probability per frame of a sudden motion transient
TRANSIENT_MAX_PX = 12.0   # px — max transient jump magnitude
TRANSIENT_DECAY  = 8      # frames — exponential decay back to drift baseline

# Output: 8-bit AVI (maps 12-bit sensor range 0–4095 → 0–255)
SENSOR_MAX  = 4095.0
BASELINE_U8 = int(np.clip(BASELINE / SENSOR_MAX * 255.0, 0, 255))
CHUNK_FRAMES = 500


# =============================================================================
# HELPERS
# =============================================================================
def _find_ffmpeg():
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg: return ffmpeg
    for c in [r"C:\ffmpeg\bin\ffmpeg.exe",
              r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
              r"C:\Users\ariAccount\ffmpeg\bin\ffmpeg.exe",
              r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"]:
        if os.path.exists(c): return c
    raise RuntimeError("ffmpeg not found. Add ffmpeg/bin to your PATH.")


def make_tophat_background(rng):
    """Slow spatial vignette — what MPS tophat correction removes."""
    bg = gaussian_filter(rng.standard_normal((H, W)).astype(np.float32), sigma=TOPHAT_BG_BLUR)
    bg -= bg.min(); bg /= bg.max() + 1e-9; bg *= TOPHAT_BG_AMP
    return bg


def make_motion_trace(T, rng):
    """
    Motion trace (T, 2) [dy, dx] combining:
      - Slow AR(1) drift  (MOTION_DRIFT_TAU, max ±MOTION_MAX_PX)
      - Sparse transients (TRANSIENT_RATE per frame, max ±TRANSIENT_MAX_PX,
                           decaying over TRANSIENT_DECAY frames)
    """
    alpha = 1.0 - 1.0 / MOTION_DRIFT_TAU
    drift = np.zeros((T, 2), dtype=np.float32)
    innov = rng.standard_normal((T, 2)).astype(np.float32) * (MOTION_MAX_PX * 0.1)
    for t in range(1, T):
        drift[t] = alpha * drift[t-1] + innov[t]
    drift = np.clip(drift, -MOTION_MAX_PX, MOTION_MAX_PX)

    decay      = np.exp(-np.arange(TRANSIENT_DECAY) / (TRANSIENT_DECAY / 3)).astype(np.float32)
    transients = np.zeros((T, 2), dtype=np.float32)
    t_frames   = np.where(rng.random(T) < TRANSIENT_RATE)[0]
    for tf in t_frames:
        jump  = rng.uniform(-TRANSIENT_MAX_PX, TRANSIENT_MAX_PX, 2).astype(np.float32)
        t_end = min(tf + TRANSIENT_DECAY, T); n = t_end - tf
        transients[tf:t_end] += jump * decay[:n, np.newaxis]

    print(f"  motion: {len(t_frames)} transients | "
          f"drift max={np.abs(drift).max():.2f}px | combined max={np.abs(drift+transients).max():.2f}px")
    return drift + transients


def apply_motion(frame, dy, dx):
    """Integer-pixel roll shift; fills wrapped edges with baseline value."""
    idy, idx = int(round(dy)), int(round(dx))
    if idy == 0 and idx == 0: return frame
    f = np.roll(np.roll(frame, idy, axis=0), idx, axis=1)
    if idy > 0:  f[:idy, :]  = BASELINE_U8
    elif idy < 0: f[idy:, :]  = BASELINE_U8
    if idx > 0:  f[:, :idx]  = BASELINE_U8
    elif idx < 0: f[:, idx:]  = BASELINE_U8
    return f


# =============================================================================
# CORE: LOAD & PRECOMPUTE  (called once, shared across clips)
# =============================================================================
def _load(gt_root, rng):
    print("Loading ground truth arrays...")
    fp   = np.load(str(gt_root / "ground_truth_footprints.npy")).astype(np.float32)
    fl   = np.load(str(gt_root / "ground_truth_fluorescence.npy")).astype(np.float32)
    N, T = fl.shape
    print(f"  footprints: {fp.shape}  fluorescence: {fl.shape}  ({T/FPS/3600:.2f}h)")

    fp_flat      = fp.reshape(N, H * W)
    neuropil_map = gaussian_filter(fp.mean(0), sigma=NEUROPIL_BLUR).astype(np.float32)
    neuropil_map /= neuropil_map.max() + 1e-9
    tophat_bg    = make_tophat_background(rng)
    motion       = make_motion_trace(T, rng)
    mean_signal  = (fp.sum(axis=(1, 2)) @ fl) / (H * W)   # (T,)

    return dict(fp_flat=fp_flat, fluorescence=fl, neuropil_map=neuropil_map,
                tophat_bg=tophat_bg, motion=motion, mean_signal=mean_signal, T_full=T)


# =============================================================================
# CORE: DEGRADE ONE CLIP
# =============================================================================
def _degrade_clip(clip_idx, t_start, t_end, static, out_dir, seed, ffmpeg):
    rng      = np.random.default_rng(seed)
    T_clip   = t_end - t_start
    out_path = out_dir / f"clip_{clip_idx:03d}.avi"

    print(f"  [clip {clip_idx:03d}]  frames {t_start}–{t_end}  ({T_clip/FPS:.0f}s)  → {out_path.name}")

    cmd = [ffmpeg, "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}",
           "-r", str(FPS), "-i", "pipe:0", "-c:v", "ffv1", "-pix_fmt", "gray", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    errs = []
    threading.Thread(target=lambda: [errs.append(l) for l in proc.stderr], daemon=True).start()

    n_chunks = (T_clip + CHUNK_FRAMES - 1) // CHUNK_FRAMES
    for ci in range(n_chunks):
        c0 = t_start + ci * CHUNK_FRAMES
        c1 = min(c0 + CHUNK_FRAMES, t_end)
        ct = c1 - c0

        fluor_c  = static["fluorescence"][:, c0:c1].T                        # (ct, N)
        neural   = (fluor_c @ static["fp_flat"]).reshape(ct, H, W) * PEAK_DF_F * BASELINE
        np_scale = static["mean_signal"][c0:c1] * NEUROPIL_RATIO
        neuropil = static["neuropil_map"][np.newaxis] * np_scale[:, np.newaxis, np.newaxis]
        frames   = (BASELINE + neural + neuropil + static["tophat_bg"][np.newaxis]
                    + rng.standard_normal((ct, H, W)).astype(np.float32) * NOISE_SIGMA)
        frames_u8 = np.clip(frames / SENSOR_MAX * 255.0, 0, 255).astype(np.uint8)

        for i in range(ct):
            dy, dx = float(static["motion"][c0+i, 0]), float(static["motion"][c0+i, 1])
            proc.stdin.write(apply_motion(frames_u8[i], dy, dx).tobytes())

        if (ci + 1) % max(1, n_chunks // 4) == 0 or ci == n_chunks - 1:
            print(f"  [clip {clip_idx:03d}]  {(c1-t_start)/T_clip*100:.0f}%")

    proc.stdin.close(); proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"clip {clip_idx:03d} failed:\n{b''.join(errs).decode(errors='replace')}")
    print(f"  [clip {clip_idx:03d}]  ✓  {os.path.getsize(out_path)/1e6:.0f} MB")
    return str(out_path)


def _clip_schedule(T_full, clip_seconds=120):
    frames = clip_seconds * FPS
    return [(i, i*frames, min((i+1)*frames, T_full))
            for i in range((T_full + frames - 1) // frames)]


# =============================================================================
# PUBLIC API
# =============================================================================
def run_sample(gt_dir=None, seconds=30, seed=None):
    """Quick preview: degrades the first `seconds` into degraded_clips/clip_000.avi."""
    if not (gt_dir or GT_DIR): raise ValueError("Set GT_DIR at the top of this script.")
    gt_root = Path(gt_dir or GT_DIR)
    out_dir = gt_root / "degraded_clips"; out_dir.mkdir(exist_ok=True)
    rng     = np.random.default_rng(seed or SEED)
    static  = _load(gt_root, rng)
    _degrade_clip(0, 0, seconds * FPS, static, out_dir, SEED, _find_ffmpeg())
    print(f"\nPreview done — check degraded_clips/clip_000.avi before running run_full().")


def run_full(gt_dir=None, seed=None, workers=4, clip_seconds=120):
    """
    Full parallel run — splits into clip_seconds clips, processes `workers` at a time.
    3h @ 120s/clip = 90 clips. workers=4 keeps ~4 GB RAM usage.
    Saves ground_truth_motion.npy alongside the clips.
    """
    import concurrent.futures
    if not (gt_dir or GT_DIR): raise ValueError("Set GT_DIR at the top of this script.")
    gt_root = Path(gt_dir or GT_DIR)
    out_dir = gt_root / "degraded_clips"; out_dir.mkdir(exist_ok=True)
    rng     = np.random.default_rng(seed or SEED)
    ffmpeg  = _find_ffmpeg()
    static  = _load(gt_root, rng)
    clips   = _clip_schedule(static["T_full"], clip_seconds)

    print(f"\n{len(clips)} clips planned ({clip_seconds}s each, {workers} workers)...")
    results, failed = {}, []

    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_degrade_clip, ci, t0, t1, static, out_dir,
                        SEED + ci * 1000, ffmpeg): ci
            for ci, t0, t1 in clips
        }
        for future in concurrent.futures.as_completed(futures):
            ci = futures[future]
            try:
                results[ci] = future.result()
                print(f"  clip_{ci:03d} done  ({len(results)}/{len(clips)} complete)")
            except Exception as e:
                print(f"  [ERROR] clip_{ci:03d}: {e}"); failed.append(ci)

    np.save(str(gt_root / "ground_truth_motion.npy"), static["motion"])
    print(f"\n{len(results)}/{len(clips)} clips OK  |  {len(failed)} failed")
    if failed: print(f"  Failed: {sorted(failed)}")
    print(f"  Output: {out_dir}")
    return results


if __name__ == "__main__":
    run_full()