#!/usr/bin/env python3
#
# undistort.py — plumb-line lens calibration and barrel-distortion correction
# for the light-painting camera, using the plotter's own lines as the target.
#
# The plotter holds a line straight to its step size, so any bow in a
# photographed line is the lens. Calibration traces every line in a photo of
# cal_grid.plot and searches for the radial model that makes them all straight.
#
# Model (maps a measured/distorted point to its corrected position):
#   t = r / R,  R = half-diagonal,  r = distance from (cx, cy)
#   scale = 1 + k1*t^2 + k2*t^4
#   corrected = centre + (measured - centre) * scale
#
# Usage:
#   ./undistort.py calibrate pics/IMG_0090.JPG [--out lens_cal.json]
#   ./undistort.py apply pics/IMG_0090.JPG [...] [--cal lens_cal.json]
#   ./undistort.py rectify pics/IMG_0090_undist.JPG [...] [--ppm 8]
#
# rectify: perspective correction on top of the lens correction. The lens model
# leaves a small rotation and keystone (the camera is never quite square to the
# bed). For a v1/v2-style frame the two blue rails and the outer two horizontals
# form a trapezoid whose corners sit at known plotter coordinates, so a
# homography from those four corners puts the whole frame into plot space at a
# fixed pixels-per-mm: +x right, +y up, every stroke horizontal, mm read off
# directly. Works either way up (the trapezoid's wide end declares which).
#
# Needs numpy and pillow only — no OpenCV, no scipy.

import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

DEFAULT_CAL = 'lens_cal.json'


# ---------------------------------------------------------------------------
# Tracing the lines out of a photograph
# ---------------------------------------------------------------------------

def load_luma(path):
    """Max-channel luminance — 'is the LED lit here', independent of hue."""
    return np.asarray(Image.open(path).convert('RGB')).astype(np.float32).max(axis=2)


def ridge_seeds(vec, thr):
    """Centres of the bright runs in a 1-D slice."""
    idx = np.flatnonzero(vec > thr)
    if len(idx) == 0:
        return []
    out, start = [], 0
    for k in list(np.flatnonzero(np.diff(idx) > 1)) + [len(idx) - 1]:
        seg = idx[start:k + 1]
        if len(seg) >= 2:
            out.append(int(round(seg.mean())))
        start = k + 1
    return out


def track_ridge(a, x0, y0, thr, win=6):
    """Follow one ridge outward from (x0, y0), steering to the local maximum.

    The window is deliberately narrow: at a grid crossing the perpendicular
    line is brighter, and a wide search would let the track jump onto it.
    """
    H, W = a.shape
    pts = []
    for step in (-1, 1):
        y, x = y0, x0
        while 0 <= x < W:
            lo, hi = max(0, y - win), min(H, y + win + 1)
            seg = a[lo:hi, x]
            k = int(np.argmax(seg))
            if seg[k] <= thr:
                break
            y = lo + k
            pts.append((x, y))
            x += step
    pts.sort()
    return np.array(pts, dtype=np.float64)


def trace_lines(lum, axis, min_frac=0.35, trim=0.10):
    """Trace every line running along `axis` ('h' or 'v').

    Seeds from several slices and keeps the best harvest, so a slice that
    happens to land along a perpendicular line does not spoil the run.
    Tracks shorter than min_frac of the frame are dropped — that is what
    rejects the perpendicular lines, which die within a column or two.
    """
    a = lum if axis == 'h' else lum.T
    H, W = a.shape
    peak = np.percentile(a, 99.9)
    seed_thr, follow_thr = peak * 0.55, peak * 0.22

    best = []
    for frac in (0.50, 0.42, 0.58, 0.34):
        s = int(W * frac)
        tracks = [track_ridge(a, s, y, follow_thr) for y in ridge_seeds(a[:, s], seed_thr)]
        tracks = [t for t in tracks if len(t) >= min_frac * W]
        if len(tracks) > len(best):
            best = tracks

    out = []
    for t in best:
        n = len(t)
        c = t[int(n * trim):int(n * (1 - trim))]      # drop the end blobs
        if len(c) < min_frac * W:
            continue
        # back to image coordinates
        out.append(c if axis == 'h' else c[:, ::-1].copy())
    return out


# ---------------------------------------------------------------------------
# The distortion model
# ---------------------------------------------------------------------------

def correct_points(pts, k1, k2, cx, cy, R):
    d = pts - np.array([cx, cy])
    t2 = (d[:, 0] ** 2 + d[:, 1] ** 2) / (R * R)
    s = 1.0 + k1 * t2 + k2 * t2 * t2
    return np.array([cx, cy]) + d * s[:, None]


def straightness(pts):
    """Sum of squared perpendicular deviations from the best-fit line."""
    c = pts - pts.mean(axis=0)
    cov = c.T @ c
    w, _ = np.linalg.eigh(cov)
    return float(w[0])                      # smaller eigenvalue = off-line energy


def cost(params, tracks, R, W, H):
    k1, k2, ux, uy = params
    cx, cy = W / 2 + ux * W, H / 2 + uy * H
    total = sum(straightness(correct_points(t, k1, k2, cx, cy, R)) for t in tracks)
    npts = sum(len(t) for t in tracks)
    return total / max(npts, 1)


# ---------------------------------------------------------------------------
# Warm start: curvature of a line varies linearly with its distance from the
# optical centre, so a quadratic fit per line gives k1 and the centre directly.
# ---------------------------------------------------------------------------

def warm_start(tracks, axis):
    rows = []
    for t in tracks:
        u, v = (t[:, 0], t[:, 1]) if axis == 'h' else (t[:, 1], t[:, 0])
        a = np.polyfit(u, v, 2)[0]
        rows.append((v.mean(), a))
    if len(rows) < 3:
        return None, None
    r = np.array(rows)
    slope, intercept = np.polyfit(r[:, 0], r[:, 1], 1)
    if abs(slope) < 1e-12:
        return None, None
    return slope, -intercept / slope       # (distortion coeff in px^-2, centre)


# ---------------------------------------------------------------------------
# Nelder-Mead (no scipy in this environment)
# ---------------------------------------------------------------------------

def nelder_mead(f, x0, step=0.02, iters=400, tol=1e-12):
    n = len(x0)
    sim = [np.array(x0, float)]
    for i in range(n):
        p = np.array(x0, float)
        p[i] += step if p[i] == 0 else step * (1 + abs(p[i]))
        sim.append(p)
    val = [f(p) for p in sim]
    for _ in range(iters):
        order = np.argsort(val)
        sim = [sim[i] for i in order]
        val = [val[i] for i in order]
        if abs(val[-1] - val[0]) < tol * (abs(val[0]) + tol):
            break
        cen = np.mean(sim[:-1], axis=0)
        xr = cen + (cen - sim[-1]);         fr = f(xr)
        if fr < val[0]:
            xe = cen + 2 * (cen - sim[-1]); fe = f(xe)
            sim[-1], val[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < val[-2]:
            sim[-1], val[-1] = xr, fr
        else:
            xc = cen + 0.5 * (sim[-1] - cen); fc = f(xc)
            if fc < val[-1]:
                sim[-1], val[-1] = xc, fc
            else:
                for i in range(1, n + 1):
                    sim[i] = sim[0] + 0.5 * (sim[i] - sim[0])
                    val[i] = f(sim[i])
    i = int(np.argmin(val))
    return sim[i], val[i]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_calibrate(args):
    lum = load_luma(args.image)
    H, W = lum.shape
    R = 0.5 * np.hypot(W, H)

    htracks = trace_lines(lum, 'h')
    vtracks = trace_lines(lum, 'v')
    tracks = htracks + vtracks
    print(f"{args.image}: {W}x{H}")
    print(f"traced {len(htracks)} horizontal, {len(vtracks)} vertical lines")
    if len(tracks) < 4:
        sys.exit("error: too few lines traced — check exposure and framing")
    if not vtracks:
        print("WARNING: no vertical lines — cx cannot be determined, holding it\n"
              "         at the frame centre. Shoot cal_grid.plot for a full fit.")

    kh, cy0 = warm_start(htracks, 'h')
    kv, cx0 = warm_start(vtracks, 'v') if vtracks else (None, None)
    kd = np.mean([k for k in (kh, kv) if k is not None]) if (kh or kv) else 0.0
    k1_0 = -kd * R * R
    cx0 = cx0 if cx0 is not None else W / 2
    cy0 = cy0 if cy0 is not None else H / 2
    print(f"\nwarm start: k1={k1_0:+.4f}  centre=({cx0:.0f}, {cy0:.0f})")
    if kh is not None and kv is not None:
        print(f"  k1 from horizontals {-kh * R * R:+.4f}, "
              f"from verticals {-kv * R * R:+.4f}  (these should agree)")

    x0 = [k1_0, 0.0, (cx0 - W / 2) / W, (cy0 - H / 2) / H]
    free_cx = bool(vtracks)

    def f(p):
        q = list(p)
        if not free_cx:
            q[2] = x0[2]
        return cost(q, tracks, R, W, H)

    before = cost([0, 0, 0, 0], tracks, R, W, H)
    best, after = nelder_mead(f, x0)
    if not free_cx:
        best[2] = x0[2]

    k1, k2, ux, uy = best
    cx, cy = W / 2 + ux * W, H / 2 + uy * H
    print(f"\nfitted: k1={k1:+.5f}  k2={k2:+.5f}  centre=({cx:.0f}, {cy:.0f})")
    print(f"RMS deviation from straight: {np.sqrt(before):.2f} px "
          f"-> {np.sqrt(after):.2f} px")

    off = np.hypot(cx - W / 2, cy - H / 2)
    print(f"optical centre is {off:.0f} px from the frame centre "
          f"({100 * off / R:.1f}% of the half-diagonal)")
    if off > 0.05 * R:
        print("  NOTE: that is a large offset. A real bow in the machine and an\n"
              "  offset optical centre are indistinguishable from one photo. To\n"
              "  separate them, reshoot the grid with the camera rotated 180 deg:\n"
              "  the lens component stays put in the frame, a machine bow flips.")

    cal = dict(k1=k1, k2=k2, cx=cx, cy=cy, R=R, width=W, height=H,
               source=os.path.basename(args.image),
               rms_before=float(np.sqrt(before)), rms_after=float(np.sqrt(after)))
    with open(args.out, 'w') as fh:
        json.dump(cal, fh, indent=2)
    print(f"\nwrote {args.out}")


def build_map(cal, W, H, zoom):
    """Source coordinates for every output pixel (inverts the radial model)."""
    k1, k2, cx, cy, R = cal['k1'], cal['k2'], cal['cx'], cal['cy'], cal['R']
    yo, xo = np.mgrid[0:H, 0:W].astype(np.float32)
    dx = (xo - cx) / zoom
    dy = (yo - cy) / zoom
    U = np.sqrt(dx * dx + dy * dy) / R          # corrected radius, normalised

    t = U.copy()                                 # Newton solve U = t + k1 t^3 + k2 t^5
    for _ in range(6):
        f = t + k1 * t ** 3 + k2 * t ** 5 - U
        fp = 1 + 3 * k1 * t ** 2 + 5 * k2 * t ** 4
        t = t - f / np.where(np.abs(fp) < 1e-6, 1e-6, fp)

    ratio = np.where(U > 1e-9, t / np.where(U > 1e-9, U, 1.0), 1.0)
    return cx + dx * ratio, cy + dy * ratio


def sample_bilinear(img, xs, ys, dtype=np.uint8):
    H, W = img.shape[:2]
    x0 = np.floor(xs).astype(np.int32)
    y0 = np.floor(ys).astype(np.int32)
    wx = (xs - x0)[..., None]
    wy = (ys - y0)[..., None]
    valid = (x0 >= 0) & (x0 < W - 1) & (y0 >= 0) & (y0 < H - 1)
    x0c, y0c = np.clip(x0, 0, W - 2), np.clip(y0, 0, H - 2)
    out = ((img[y0c,     x0c]     * (1 - wx) + img[y0c,     x0c + 1] * wx) * (1 - wy) +
           (img[y0c + 1, x0c]     * (1 - wx) + img[y0c + 1, x0c + 1] * wx) * wy)
    return np.where(valid[..., None], out, 0).astype(dtype)


def cmd_apply(args):
    with open(args.cal) as fh:
        cal = json.load(fh)
    for path in args.images:
        img = np.asarray(Image.open(path).convert('RGB')).astype(np.float32)
        H, W = img.shape[:2]
        if (W, H) != (cal['width'], cal['height']):
            print(f"skip {path}: {W}x{H} does not match the calibration "
                  f"{cal['width']}x{cal['height']}")
            continue
        xs, ys = build_map(cal, W, H, args.zoom)
        root, ext = os.path.splitext(path)
        out = f"{root}{args.suffix}{ext}"
        Image.fromarray(sample_bilinear(img, xs, ys)).save(out, quality=95)
        print(f"{path} -> {out}")


# ---------------------------------------------------------------------------
# Perspective rectification from the rails
# ---------------------------------------------------------------------------

# Plotter-mm corners of the trapezoid formed by the rails and the outer two
# horizontals, for v1.plot and v2.plot alike (v2's full-height rails pass
# through the same points at y=257 and y=5). Order: wide-end left, wide-end
# right, narrow-end right, narrow-end left.
TRAPEZOID_MM = [(5.0, 257.0), (388.0, 257.0), (293.0, 5.0), (100.0, 5.0)]
BED_MM = (393.0, 262.0)


def runs_1d(mask):
    """Centres of the True runs in a 1-D boolean array (runs of 3+ samples)."""
    idx = np.flatnonzero(mask)
    if len(idx) == 0:
        return []
    out, start = [], 0
    for k in list(np.flatnonzero(np.diff(idx) > 2)) + [len(idx) - 1]:
        seg = idx[start:k + 1]
        if len(seg) >= 3:
            out.append(seg.mean())
        start = k + 1
    return out


def robust_polyfit(x, y, rounds=3, k=3.0):
    """Straight-line fit with sigma clipping on the residual MAD."""
    x, y = np.asarray(x, float), np.asarray(y, float)
    keep = np.ones(len(x), bool)
    for _ in range(rounds):
        a, b = np.polyfit(x[keep], y[keep], 1)
        r = y - (a * x + b)
        mad = np.median(np.abs(r[keep] - np.median(r[keep]))) + 1e-9
        keep = np.abs(r) < k * 1.4826 * mad
    return a, b, int(keep.sum())


def fit_rails(img):
    """The two blue rails, each as x = c*y + d.

    Per row, the centres of blue-dominant runs; the rail's saturated core reads
    white so only its fringes are blue, which is why run centres rather than raw
    pixels are fitted. Split at the frame centre (the rails converge but never
    cross it), then sigma-clip.
    """
    R, G, B = img[..., 0], img[..., 1], img[..., 2]
    blue = (B > 120) & (B > R + 40) & (B > G + 40)
    H, W = blue.shape
    pts = [[], []]
    for y in range(0, H, 2):
        for xc in runs_1d(blue[y]):
            pts[0 if xc < W / 2 else 1].append((y, xc))
    rails = []
    for side in pts:
        if len(side) < 50:
            sys.exit("rectify: too few blue rail samples on one side")
        ys, xs = np.array(side).T
        c, d, n = robust_polyfit(ys, xs)
        rails.append((c, d))
    return rails


def fit_outer_lines(lum):
    """Top and bottom horizontal strokes, each as y = a*x + b.

    Per column in the central band of the frame (clear of the rails), the
    centres of bright runs; the first and last are the outer strokes.
    """
    H, W = lum.shape
    thr = 0.5 * np.percentile(lum, 99.9)
    tops, bots = [], []
    for x in range(int(W * 0.35), int(W * 0.65), 2):
        r = runs_1d(lum[:, x] > thr)
        if len(r) >= 2:
            tops.append((x, r[0]))
            bots.append((x, r[-1]))
    if len(tops) < 50:
        sys.exit("rectify: could not find the horizontal strokes")
    out = []
    for pts in (tops, bots):
        xs, ys = np.array(pts).T
        a, b, n = robust_polyfit(xs, ys)
        out.append((a, b))
    return out, len(tops)


def intersect(line, rail):
    """y = a x + b  with  x = c y + d."""
    a, b = line
    c, d = rail
    y = (a * d + b) / (1.0 - a * c)
    return c * y + d, y


def homography(src, dst):
    """DLT: 3x3 H with dst ~ H . src, from four point pairs."""
    A = []
    for (x, y), (u, v) in zip(src, dst):
        A.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        A.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.array(A, dtype=np.float64))
    Hm = vt[-1].reshape(3, 3)
    return Hm / Hm[2, 2]


def cmd_rectify(args):
    margin, ppm = args.margin, args.ppm
    for path in args.images:
        img = np.asarray(Image.open(path).convert('RGB')).astype(np.float32)
        lum = img.max(axis=2)
        rails = fit_rails(img)
        (top, bot), n = fit_outer_lines(lum)
        corners = [intersect(top, rails[0]), intersect(top, rails[1]),
                   intersect(bot, rails[1]), intersect(bot, rails[0])]
        w_top = corners[1][0] - corners[0][0]
        w_bot = corners[2][0] - corners[3][0]
        wide_at_top = w_top > w_bot
        # A 180-degree camera rotation maps the wide end to the image bottom and
        # swaps left for right; it is a rotation, not a mirror, so the order of
        # the corners round the trapezoid is preserved.
        mm = TRAPEZOID_MM if wide_at_top else TRAPEZOID_MM[2:] + TRAPEZOID_MM[:2]
        print(f"{path}: outer strokes fitted on {n} columns; trapezoid wide end at the "
              f"{'top' if wide_at_top else 'bottom'} of the frame")
        for (x, y), (mx, my) in zip(corners, mm):
            print(f"  image ({x:7.1f}, {y:7.1f})  ->  plot ({mx:5.1f}, {my:5.1f}) mm")

        Hm = homography(corners, mm)              # image px -> plot mm
        Hinv = np.linalg.inv(Hm)                  # plot mm -> image px
        OW = int(round((BED_MM[0] + 2 * margin) * ppm))
        OH = int(round((BED_MM[1] + 2 * margin) * ppm))
        vo, uo = np.mgrid[0:OH, 0:OW].astype(np.float64)
        mx = uo / ppm - margin
        my = BED_MM[1] + margin - vo / ppm        # row 0 is the top of the bed
        den = Hinv[2, 0] * mx + Hinv[2, 1] * my + Hinv[2, 2]
        xs = (Hinv[0, 0] * mx + Hinv[0, 1] * my + Hinv[0, 2]) / den
        ys = (Hinv[1, 0] * mx + Hinv[1, 1] * my + Hinv[1, 2]) / den
        out_img = sample_bilinear(img, xs.astype(np.float32), ys.astype(np.float32))

        # Residual check: the rails should now be exactly where the plot put them.
        R, G, B = [out_img[..., i].astype(np.float32) for i in range(3)]
        blue = (B > 150) & (B > R + 60) & (B > G + 60)
        ry, rx = np.nonzero(blue)
        ok = len(rx) > 100
        if ok:
            # rail A: x = 5 + (257 - y) * 95/252 ... in output pixels; measure the
            # mean |dx| of left-half blue pixels against that line, in mm.
            y_mm = BED_MM[1] + margin - ry / ppm
            x_mm = rx / ppm - margin
            left = x_mm < BED_MM[0] / 2
            pred_a = 5.0 + (257.0 - y_mm[left]) * (95.0 / 252.0)
            pred_b = 388.0 - (257.0 - y_mm[~left]) * (95.0 / 252.0)
            res = np.concatenate([x_mm[left] - pred_a, x_mm[~left] - pred_b])
            print(f"  rails after rectification: mean offset {res.mean():+.2f} mm, "
                  f"spread {res.std():.2f} mm (the rail's own width is ~{np.median(np.abs(res))*2:.1f} mm)")
        root, ext = os.path.splitext(path)
        out = f"{root}{args.suffix}{ext}"
        Image.fromarray(out_img).save(out, quality=95)
        print(f"  {OW}x{OH} px at {ppm} px/mm, {margin} mm margin -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)

    c = sub.add_parser('calibrate', help='fit the lens model to a grid photo')
    c.add_argument('image')
    c.add_argument('--out', default=DEFAULT_CAL)
    c.set_defaults(func=cmd_calibrate)

    a = sub.add_parser('apply', help='correct photographs')
    a.add_argument('images', nargs='+')
    a.add_argument('--cal', default=DEFAULT_CAL)
    a.add_argument('--suffix', default='_undist')
    a.add_argument('--zoom', type=float, default=1.0,
                   help='below 1.0 keeps more of the original frame, at the '
                        'cost of black edges (default 1.0: full frame, cropped)')
    a.set_defaults(func=cmd_apply)

    r = sub.add_parser('rectify', help='perspective-correct a lens-corrected '
                       'v1/v2 frame into plot coordinates using the rails')
    r.add_argument('images', nargs='+')
    r.add_argument('--ppm', type=float, default=8.0, help='output pixels per mm')
    r.add_argument('--margin', type=float, default=5.0,
                   help='mm of frame kept beyond the bed on each side')
    r.add_argument('--suffix', default='_rect')
    r.set_defaults(func=cmd_rectify)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
