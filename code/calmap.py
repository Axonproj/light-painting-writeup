#!/usr/bin/env python3
#
# calmap.py — photo-to-plot mapping from a photograph of cal_colour.plot.
#
# One general transform instead of a lens model plus a four-corner homography:
# every grid intersection in the photo is a known plotter coordinate, so the
# mapping is fitted from ~224 correspondences. It absorbs lens distortion,
# perspective and camera pose in one step, and therefore also any geometric
# error of the machine — which is what you want when photo coordinates are
# meant to be COMMANDED coordinates, and not what you want for judging the
# machine's accuracy (use undistort.py's grid calibration for that).
#
# Model, fitted in the plot -> photo direction so a warp can sample directly:
#   photo = H(plot)  +  TPS(plot)
# H is a homography (DLT, all points), TPS a regularised thin-plate spline on
# the residuals. Reported RMS is the reprojection error in photo pixels.
#
# Identification: lines carry a repeating hue sequence (see cal_colour.py).
# Lines are found by brightness alone, so their ORDER comes from geometry; each
# whole line then gets one colour (median between crossings, hue centres
# refined on the frame), and the colour sequence only has to fix the offset
# into the pattern and which way up the camera is. A misread line costs one
# vote, not the fit, and lines lost off the frame edge are allowed for.
#
# Usage:
#   ./calmap.py fit remote_pics/<stamp>.jpeg [--out cal_map.json] [--check]
#   ./calmap.py apply remote_pics/<frame>.jpeg [...] [--map cal_map.json]
#
# fit --check also writes <stamp>_check.jpeg: the photo warped into plot space
# with the reference grid overlaid in thin white, so a bad fit is visible.

import argparse
import json
import os
import sys
import time

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cal_colour as CC                      # the pattern: geometry + hue sequence
import picmeta
from undistort import sample_bilinear

DEFAULT_MAP = 'cal_map.json'


# ---------------------------------------------------------------------------
# Line finding — geometry first, colour second
# ---------------------------------------------------------------------------
# Lines are found by BRIGHTNESS alone, then each whole line is given one colour.
# Classifying every pixel by hue and tracking per colour fails on a bright LED:
# a line's bloom has a different hue from its core (blue fringes read cyan), a
# crossing of two colours makes a blob of a third, and clipped secondaries go
# pale enough to drop out of a saturation threshold. Geometry has none of
# those problems, and a median over a whole line's length outvotes them.

LETTER = {'red': 'R', 'green': 'G', 'blue': 'B', 'yellow': 'Y', 'magenta': 'M', 'cyan': 'C'}


def hsv(img):
    """Hue (deg), saturation, value from an RGB float array (..., 3)."""
    r, g, b = img[..., 0], img[..., 1], img[..., 2]
    mx, mn = img.max(axis=-1), img.min(axis=-1)
    d = mx - mn
    m = d > 1e-6
    rc = np.where(m, (mx - r) / np.where(m, d, 1), 0)
    gc = np.where(m, (mx - g) / np.where(m, d, 1), 0)
    bc = np.where(m, (mx - b) / np.where(m, d, 1), 0)
    h = np.where(mx == r, bc - gc, np.where(mx == g, 2 + rc - bc, 4 + gc - rc))
    h = (h * 60.0) % 360.0
    s = np.where(mx > 1e-6, d / np.where(mx > 1e-6, mx, 1), 0)
    return h, s, mx


def hue_dist(a, b):
    d = np.abs(a - b) % 360.0
    return np.minimum(d, 360.0 - d)


def profile_peaks(prof, min_sep, frac=0.25):
    """Local maxima of a 1-D profile above frac*max, at least min_sep apart."""
    k = np.ones(21) / 21.0
    p = np.convolve(prof, k, mode='same')
    thr = frac * p.max()
    idx = [i for i in range(1, len(p) - 1) if p[i] > thr and p[i] >= p[i - 1] and p[i] >= p[i + 1]]
    out = []
    for i in sorted(idx, key=lambda i: -p[i]):
        if all(abs(i - j) >= min_sep for j in out):
            out.append(i)
    return sorted(out)


def track(w, r0, band=40, step_band=35, max_gap=80):
    """Follow one line running along axis 1 near row r0, as the weighted
    centroid per column within a band around the previous position. A column
    where the band is bright end to end is a crossing (the perpendicular line
    fills it), whose centroid only echoes the band's centre — skip those.
    Returns (col, row) points."""
    H, W = w.shape
    pts = []
    x0 = W // 2
    for direction in (-1, 1):
        y, x, gap = float(r0), x0, 0
        first = True
        while 0 <= x < W:
            b = band if first else step_band
            lo, hi = int(max(0, y - b)), int(min(H, y + b + 1))
            col = w[lo:hi, x]
            pk = col.max() if len(col) else 0
            if pk > 0 and max(col[0], col[-1]) < 0.5 * pk:
                y = lo + (col * np.arange(len(col))).sum() / col.sum()
                pts.append((x, y))
                gap = 0
                first = False
            else:
                gap += 1
                if gap > max_gap and not first:
                    break
            x += direction
    pts.sort()
    return np.array(pts, float).reshape(-1, 2)


def fit_curve(pts, deg=3, rounds=3):
    """Polynomial y(x) with sigma clipping; returns coefficients."""
    x, y = pts[:, 0], pts[:, 1]
    keep = np.ones(len(x), bool)
    for _ in range(rounds):
        c = np.polyfit(x[keep], y[keep], deg)
        r = y - np.polyval(c, x)
        mad = np.median(np.abs(r[keep])) + 1e-6
        keep = np.abs(r) < 4 * 1.4826 * mad
    return np.polyfit(x[keep], y[keep], deg)


def lift(img, gamma):
    """Each pixel's brightness raised to `gamma` (0..1 scale), its hue and
    saturation kept: dim lines come up towards bright ones. The SK6812 draws
    the grid's green and cyan many times brighter than its blue and red; at
    grid brightness 20 its blue and red lines fell under the line finder's
    threshold (20% of the brightest) and the fit failed (2026-11-12). 0.5 found
    every line; 0.7 lost one vertical, 0.85 four."""
    if gamma >= 1:
        return img
    m = img.max(axis=2, keepdims=True)
    return img / np.maximum(m, 1e-6) * 255.0 * (np.clip(m, 0, 255) / 255.0) ** gamma


def brightness(img):
    """Max channel with the dark background removed: a weight for tracking.
    Max, not sum, so a pure primary weighs as much as a secondary."""
    v = img.max(axis=2)
    thr = 0.2 * np.percentile(v, 99.9)
    return np.clip(v - thr, 0, None), thr


def find_geometry(w, axis):
    """Every line along `axis` ('h': y(x) curves, 'v': x(y)), colour-blind,
    ordered by image position. Peaks come from a CENTRAL strip, where the lens
    bows the lines least; the median across the strip ignores the
    perpendicular lines, which cover only a fraction of its width.
    Returns list of (position, coeffs, tracked points (along, across))."""
    if axis == 'v':
        w = w.T
    H, W = w.shape
    strip = w[:, int(W * 0.35):int(W * 0.65)]
    peaks = profile_peaks(np.median(strip, axis=1), min_sep=60)
    out = []
    for k, r0 in enumerate(peaks):
        # The tracking band and the gap it may jump scale with the spacing of
        # the lines: the 8 mm LED's lines are ~80 px wide on the 700D, wider
        # than the old fixed band (+-35 px), and a dim line's crossings with
        # bright ones run past the old 80 px gap; either lost a line, and one
        # lost in the middle shifted every line after it in the pattern
        # (held-out 22 px; with these 4 px, 2026-11-06).
        gaps = [abs(r0 - peaks[j]) for j in (k - 1, k + 1) if 0 <= j < len(peaks)]
        sp = min(gaps) if gaps else 200
        b = int(max(35, min(110, 0.28 * sp)))
        pts = track(w, r0, band=max(b, 40), step_band=b, max_gap=max(80, int(sp)))
        if len(pts) < 0.3 * W:
            continue
        out.append((float(np.median(pts[:, 1])), fit_curve(pts), pts))
    return out


def crossing(ch, cv, W, H):
    """Where horizontal y(x) = ch and vertical x(y) = cv meet, photo px."""
    x, y = W / 2.0, H / 2.0
    for _ in range(20):
        x = np.polyval(cv, y)
        y = np.polyval(ch, x)
    return x, y


def confine(geo_h, geo_v, W, H, rounds=3):
    """Refit every line from only the points between the two outermost lines
    crossing it. In the pattern each line stops at the outer perpendiculars,
    but the tracker runs on past its ends into the glow and stray stroke ends
    beyond, and the cubic bent to suit them: the outer verticals (13 and
    388 mm) mapped up to 5 mm off, and a border drawn 4 mm outside them landed
    anywhere from 390 to 397 mm (2026-11-12). Both directions are refitted
    together from the previous round's curves, so a bent outer line
    straightens as the lines it confines do. Found lines only: where the
    pattern's outer line is off the frame, the span ends at the outermost line
    found, which still covers every crossing the map uses."""
    for _ in range(rounds):
        if len(geo_h) < 2 or len(geo_v) < 2:
            return geo_h, geo_v
        new_h, new_v = [], []
        for pos, c, pts in geo_h:
            a = crossing(c, geo_v[0][1], W, H)[0]
            b = crossing(c, geo_v[-1][1], W, H)[0]
            keep = pts[(pts[:, 0] >= min(a, b)) & (pts[:, 0] <= max(a, b))]
            new_h.append((pos, fit_curve(keep) if len(keep) > 0.5 * len(pts) else c, pts))
        for pos, c, pts in geo_v:
            a = crossing(geo_h[0][1], c, W, H)[1]
            b = crossing(geo_h[-1][1], c, W, H)[1]
            keep = pts[(pts[:, 0] >= min(a, b)) & (pts[:, 0] <= max(a, b))]
            new_v.append((pos, fit_curve(keep) if len(keep) > 0.5 * len(pts) else c, pts))
        geo_h, geo_v = new_h, new_v
    return geo_h, geo_v


def line_colour(img, thr, coeffs, axis, across):
    """Median colour of one line, sampled only BETWEEN crossings (`across` =
    image positions of the perpendicular lines), from bright pixels near its
    core. Clipped pixels are dropped when enough remain: a channel stuck at 255
    has lost the ratio that sets the hue. Returns (hue, saturation)."""
    H, W = img.shape[:2]
    mids = [(a + b) / 2.0 for a, b in zip(across, across[1:])]
    samples = []
    for t in mids:
        if axis == 'h':
            x, y = int(round(t)), int(round(np.polyval(coeffs, t)))
            win = img[max(0, y - 5):y + 6, max(0, x - 15):x + 16]
        else:
            y, x = int(round(t)), int(round(np.polyval(coeffs, t)))
            win = img[max(0, y - 15):y + 16, max(0, x - 5):x + 6]
        samples.append(win.reshape(-1, 3))
    s = np.concatenate(samples) if samples else np.zeros((0, 3))
    s = s[s.max(axis=1) > thr]
    if len(s) == 0:
        return float('nan'), 0.0
    unclipped = s[(s < 250).all(axis=1)]
    if len(unclipped) >= 0.2 * len(s) and len(unclipped) >= 30:
        s = unclipped
    h, sat, _ = hsv(np.median(s, axis=0)[None, :])
    return float(h[0]), float(sat[0])


# ---------------------------------------------------------------------------
# Identification via the colour sequence
# ---------------------------------------------------------------------------

def expected_indices():
    """(colour_index, coordinate mm) for the horizontals (bottom->top) and the
    verticals (left->right), from cal_colour's ordering."""
    L = CC.lines()
    hs = [(ci, p0[1]) for name, ci, p0, p1 in L if name.startswith('h')]
    vs = [(ci, p0[0]) for name, ci, p0, p1 in L if name.startswith('v')]
    return hs, vs


# Hue-wheel ORDER of the six classes. Their exact hues vary with the LED; the
# order around the wheel does not.
WHEEL = ['red', 'yellow', 'green', 'cyan', 'blue', 'magenta']


def wheel_order_score(centres):
    """How many classes have the expected next class after them, going round
    the wheel by measured hue (0-6). The wrong orientation groups the same
    lines (a reversed periodic sequence has the same partition) but under
    shuffled names, which breaks the order."""
    names = [n for n, _ in CC.HUES]
    by_hue = sorted(centres, key=lambda c: centres[c])
    got = [names[c] for c in by_hue]
    score = 0
    for i, n in enumerate(got):
        nxt = got[(i + 1) % len(got)]
        if n in WHEEL and WHEEL[(WHEEL.index(n) + 1) % len(WHEEL)] == nxt:
            score += 1
    return score


def circ_mean(deg):
    a = np.deg2rad(np.asarray(deg, float))
    return float(np.rad2deg(np.arctan2(np.sin(a).mean(), np.cos(a).mean())) % 360)


def align(hues_h, hues_v, exp_h, exp_v):
    """Every way the found lines could sit in the pattern: two orientations
    times the offsets that lines lost off the frame edge allow (found lines are
    contiguous — geometry fixes their ORDER). Each hypothesis assigns every line
    a colour class; it is scored by how tightly each class's lines agree in hue
    (RMS from their own circular mean). The right alignment gives six tight
    clusters, a wrong one mixes hues within a class. No nominal hues are used:
    an LED change moves them all (the APA-106's magenta sat nearer nominal blue
    than its own blue did). Returns hypotheses, best first, as dicts."""
    out = []
    nh, nv = len(hues_h), len(hues_v)
    if nh > len(exp_h) or nv > len(exp_v) or nh == 0 or nv == 0:
        return out
    hues = np.r_[hues_h, hues_v]
    for flip_h, flip_v, label in ((True, False, 'upright'), (False, True, 'rotated 180')):
        sh = [c for c, _ in exp_h][::-1] if flip_h else [c for c, _ in exp_h]
        sv = [c for c, _ in exp_v][::-1] if flip_v else [c for c, _ in exp_v]
        for oh in range(len(sh) - nh + 1):
            for ov in range(len(sv) - nv + 1):
                cls = np.array(sh[oh:oh + nh] + sv[ov:ov + nv])
                ok = ~np.isnan(hues)
                centres = {}
                for c in set(cls[ok].tolist()):
                    centres[c] = circ_mean(hues[ok & (cls == c)])
                resid = np.array([hue_dist(h, centres[c]) if ok[i] else 0.0
                                  for i, (h, c) in enumerate(zip(hues, cls))])
                # a line is misread if another class's centre is nearer its hue
                misread = [i for i, (h, c) in enumerate(zip(hues, cls)) if ok[i] and
                           min(centres, key=lambda k: hue_dist(h, centres[k])) != c]
                out.append(dict(label=label, flip_h=flip_h, flip_v=flip_v, oh=oh, ov=ov,
                                cls_h=cls[:nh], cls_v=cls[nh:], centres=centres,
                                rms=float(np.sqrt((resid[ok] ** 2).mean())), misread=misread,
                                order=wheel_order_score(centres)))
    if not out:
        return out
    # Tight clusters first (the right partition into lines-of-one-colour), then
    # the hue-wheel order to name them — which is what fixes the orientation.
    tight = 1.5 * min(d['rms'] for d in out) + 2.0
    out.sort(key=lambda d: (d['rms'] > tight, -d['order'], d['rms']))
    return out


def letters(cls):
    names = [n for n, _ in CC.HUES]
    return ' '.join(LETTER[names[c]] if c >= 0 else '?' for c in cls)


def intersections(hlines, vlines, W, H):
    """hlines: [(coeffs y(x), y_mm)], vlines: [(coeffs x(y), x_mm)] -> arrays
    of photo px (N,2) and plot mm (N,2)."""
    px, mm = [], []
    for ch, ymm in hlines:
        for cv, xmm in vlines:
            x, y = W / 2.0, H / 2.0
            for _ in range(20):
                x = np.polyval(cv, y)
                y = np.polyval(ch, x)
            if 0 <= x < W and 0 <= y < H:
                px.append((x, y))
                mm.append((xmm, ymm))
    return np.array(px), np.array(mm)


# ---------------------------------------------------------------------------
# The mapping: homography + thin-plate spline, plot mm -> photo px
# ---------------------------------------------------------------------------

def homography_lsq(src, dst):
    def norm(p):
        c = p.mean(axis=0)
        s = np.sqrt(2) / np.sqrt(((p - c) ** 2).sum(axis=1)).mean()
        T = np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])
        return T, (p - c) * s
    Ts, s = norm(src)
    Td, d = norm(dst)
    A = []
    for (x, y), (u, v) in zip(s, d):
        A.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        A.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, _, vt = np.linalg.svd(np.array(A))
    Hn = vt[-1].reshape(3, 3)
    Hm = np.linalg.inv(Td) @ Hn @ Ts
    return Hm / Hm[2, 2]


def apply_h(Hm, p):
    q = np.c_[p, np.ones(len(p))] @ Hm.T
    return q[:, :2] / q[:, 2:3]


def tps_kernel(r2):
    return np.where(r2 > 0, 0.5 * r2 * np.log(np.where(r2 > 0, r2, 1)), 0.0)


def tps_fit(ctrl, values, lam):
    """Regularised TPS: values (N,2) as a function of ctrl (N,2)."""
    n = len(ctrl)
    d2 = ((ctrl[:, None, :] - ctrl[None, :, :]) ** 2).sum(axis=2)
    K = tps_kernel(d2) + lam * np.eye(n)
    P = np.c_[np.ones(n), ctrl]
    A = np.zeros((n + 3, n + 3))
    A[:n, :n], A[:n, n:], A[n:, :n] = K, P, P.T
    rhs = np.zeros((n + 3, 2))
    rhs[:n] = values
    sol = np.linalg.solve(A, rhs)
    return sol[:n], sol[n:]


def tps_eval(ctrl, wts, aff, p):
    d2 = ((p[:, None, :] - ctrl[None, :, :]) ** 2).sum(axis=2)
    return tps_kernel(d2) @ wts + np.c_[np.ones(len(p)), p] @ aff


def map_mm_to_px(m, p):
    Hm = np.array(m['H'])
    q = apply_h(Hm, p)
    if m.get('tps'):
        t = m['tps']
        q = q + tps_eval(np.array(t['ctrl']), np.array(t['w']), np.array(t['a']), p)
    return q


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def warp_to_plot(img, m, ppm, margin, dtype=np.uint8):
    OW = int(round((CC.BED[0] + 2 * margin) * ppm))
    OH = int(round((CC.BED[1] + 2 * margin) * ppm))
    vo, uo = np.mgrid[0:OH, 0:OW]
    mm = np.c_[(uo.ravel() / ppm - margin), (CC.BED[1] + margin - vo.ravel() / ppm)]
    # TPS over a 7M-point grid is heavy; evaluate on a coarse lattice and interpolate.
    step = 8
    gv, gu = np.mgrid[0:OH + step:step, 0:OW + step:step]
    gmm = np.c_[(gu.ravel() / ppm - margin), (CC.BED[1] + margin - gv.ravel() / ppm)]
    gpx = map_mm_to_px(m, gmm).reshape(gv.shape + (2,))
    from numpy import interp
    # bilinear upsample of the lattice
    def up(a):
        y = np.arange(0, OH); x = np.arange(0, OW)
        fy = y / step; fx = x / step
        iy = np.clip(np.floor(fy).astype(int), 0, a.shape[0] - 2)
        ix = np.clip(np.floor(fx).astype(int), 0, a.shape[1] - 2)
        wy = (fy - iy)[:, None]; wx = (fx - ix)[None, :]
        return ((a[iy][:, ix] * (1 - wx) + a[iy][:, ix + 1] * wx) * (1 - wy) +
                (a[iy + 1][:, ix] * (1 - wx) + a[iy + 1][:, ix + 1] * wx) * wy)
    xs, ys = up(gpx[..., 0]), up(gpx[..., 1])
    return sample_bilinear(img, xs.astype(np.float32), ys.astype(np.float32), dtype), OW, OH


def cmd_fit(args):
    img = lift(np.asarray(Image.open(args.image).convert('RGB')).astype(np.float32), args.lift)
    H, W = img.shape[:2]
    w, thr = brightness(img)
    geo_h, geo_v = confine(find_geometry(w, 'h'), find_geometry(w, 'v'), W, H)
    exp_h, exp_v = expected_indices()
    print(f"{args.image}: {W}x{H}; found {len(geo_h)} horizontals (pattern has {len(exp_h)}), "
          f"{len(geo_v)} verticals (pattern has {len(exp_v)})")

    pos_h = [p for p, _, _ in geo_h]
    pos_v = [p for p, _, _ in geo_v]
    hv = [line_colour(img, thr, c, 'h', pos_v) for _, c, _ in geo_h]
    vv = [line_colour(img, thr, c, 'v', pos_h) for _, c, _ in geo_v]
    weak = [f"{'h' if i < len(hv) else 'v'}{i if i < len(hv) else i - len(hv)}"
            for i, (h, s) in enumerate(hv + vv) if s < 0.3]
    if weak:
        print(f"  pale lines (saturation < 0.3, colour unreliable — over-exposed?): {' '.join(weak)}")

    # Orientation: plot y increases upwards, image rows increase downwards, so
    # the unrotated camera sees plot order REVERSED in row order. align() tries
    # both, at every offset the line counts allow.
    hyps = align([h for h, _ in hv], [h for h, _ in vv], exp_h, exp_v)
    if not hyps:
        sys.exit("fit failed: more lines than the pattern has — stray light in the frame?")
    if args.offset:
        # Told where the lines sit (say the bed's left edge is in frame, so the
        # first vertical seen is the pattern's first): keep only that alignment.
        oh, ov = (int(v) for v in args.offset.split(','))
        hyps = [h for h in hyps if h['oh'] == oh and h['ov'] == ov]
        if not hyps:
            sys.exit(f"fit failed: no alignment at offset h{oh} v{ov}")
    best = hyps[0]
    names = [n for n, _ in CC.HUES]
    centres = {names[c]: round(h, 1) for c, h in sorted(best['centres'].items())}
    print(f"hue centres on this frame: {centres}")
    print(f"  horizontals, image top->bottom: {letters(best['cls_h'])}")
    print(f"  verticals, image left->right:   {letters(best['cls_v'])}")
    for h in hyps[:3]:
        print(f"  {h['label']:11s} offset h{h['oh']} v{h['ov']}: hue spread RMS {h['rms']:5.1f} deg, "
              f"wheel order {h['order']}/6, {len(h['misread'])} line(s) nearer another colour")
    # The right alignment should be tight AND clearly better than the next.
    # Misread lines are outvoted by geometry, but if many disagree the frame is
    # not telling us which alignment is right — refuse rather than fit a map
    # shifted by a whole grid step.
    n = len(hv) + len(vv)
    if len(best['misread']) > max(2, n // 5) and args.offset:
        print(f"  {len(best['misread'])}/{n} lines nearer another colour; fitting anyway at the --offset given")
    elif len(best['misread']) > max(2, n // 5):
        sys.exit(f"fit failed: {len(best['misread'])}/{n} lines don't match the colour sequence — "
                 f"check exposure (clipped lines go pale) and that the frame is cal_colour.plot")
    if best['order'] < 5:
        sys.exit(f"fit failed: the colours are out of hue-wheel order ({best['order']}/6) — "
                 f"not cal_colour.plot, or the exposure has washed the hues together")
    if len(hyps) > 1 and hyps[1]['order'] == best['order'] and hyps[1]['rms'] < 1.5 * best['rms'] + 2.0:
        sys.exit(f"fit failed: two alignments fit equally well ({best['label']} and {hyps[1]['label']}) — "
                 f"too few lines in frame to tell where they sit in the pattern")
    label, flip_h, flip_v, oh, ov = best['label'], best['flip_h'], best['flip_v'], best['oh'], best['ov']

    seq_h = [mm for _, mm in exp_h][::-1] if flip_h else [mm for _, mm in exp_h]
    seq_v = [mm for _, mm in exp_v][::-1] if flip_v else [mm for _, mm in exp_v]
    hl = [(c, seq_h[oh + i]) for i, (_, c, _) in enumerate(geo_h)]
    vl = [(c, seq_v[ov + i]) for i, (_, c, _) in enumerate(geo_v)]
    px, mm = intersections(hl, vl, W, H)
    Hm = homography_lsq(mm, px)
    rms_h = float(np.sqrt((((apply_h(Hm, mm) - px) ** 2).sum(axis=1)).mean()))

    resid = px - apply_h(Hm, mm)
    wts, aff = tps_fit(mm, resid, args.lam)
    fitted = apply_h(Hm, mm) + tps_eval(mm, wts, aff, mm)
    r = np.sqrt(((fitted - px) ** 2).sum(axis=1))
    print(f"\norientation: {label}")
    print(f"homography only : RMS {rms_h:.2f} px, max {np.sqrt((resid**2).sum(axis=1)).max():.2f} px")
    print(f"with TPS (lam={args.lam:g}): RMS {np.sqrt((r**2).mean()):.2f} px, max {r.max():.2f} px")
    # leave-one-out style sanity: refit without about 1/8 of the points, test on
    # them. Interior points only, scattered: every 8th in order (as this was
    # until 2026-11-05) is the same two columns of every row, one of them the
    # bed's edge, so half the test was the map extrapolating past its last
    # column, which no picture asks of it (every edge point is fitted).
    idx = np.arange(len(mm))
    edge = ((mm[:, 0] == mm[:, 0].min()) | (mm[:, 0] == mm[:, 0].max()) |
            (mm[:, 1] == mm[:, 1].min()) | (mm[:, 1] == mm[:, 1].max()))
    test = (idx % 7 == 0) & ~edge
    w2, a2 = tps_fit(mm[~test], resid[~test], args.lam)
    r2 = np.sqrt(((apply_h(Hm, mm[test]) + tps_eval(mm[~test], w2, a2, mm[test]) - px[test]) ** 2).sum(axis=1))
    print(f"held-out {int(test.sum())} interior points: RMS {np.sqrt((r2**2).mean()):.2f} px, max {r2.max():.2f} px")

    m = dict(source=os.path.basename(args.image), width=W, height=H, orientation=label,
             hue_centres=centres, n_points=int(len(mm)), lam=args.lam,
             rms_homography_px=float(rms_h), rms_px=float(np.sqrt((r**2).mean())),
             rms_heldout_px=float(np.sqrt((r2**2).mean())),
             H=Hm.tolist(), tps=dict(ctrl=mm.tolist(), w=wts.tolist(), a=aff.tolist()))
    with open(args.out, 'w') as fh:
        json.dump(m, fh)
    print(f"wrote {args.out}")

    if args.check:
        out, OW, OH = warp_to_plot(img, m, args.ppm, args.margin)
        im = Image.fromarray(out)
        d = ImageDraw.Draw(im)
        def P(p):
            return ((p[0] + args.margin) * args.ppm, (CC.BED[1] + args.margin - p[1]) * args.ppm)
        for name, ci, p0, p1 in CC.lines():
            d.line([P(p0), P(p1)], fill=(255, 255, 255), width=1)
        root, ext = os.path.splitext(args.image)
        path = f"{root}_check{ext}"
        im.save(path, quality=95)
        print(f"check image (warped photo + reference grid in white): {path}")


def cmd_apply(args):
    with open(args.map) as fh:
        m = json.load(fh)
    for path in args.images:
        src = Image.open(path)
        icc = src.info.get('icc_profile')       # a P3 picture (raw_paint --colour p3) stays P3
        img = np.asarray(src.convert('RGB')).astype(np.float32)
        H, W = img.shape[:2]
        if (W, H) != (m['width'], m['height']):
            print(f"skip {path}: {W}x{H} does not match the map's {m['width']}x{m['height']}")
            continue
        out, OW, OH = warp_to_plot(img, m, args.ppm, args.margin)
        root, ext = os.path.splitext(path)
        dst = f"{root}{args.suffix}{ext}"
        # A note in the picture (see picmeta.py) goes on into the corrected one.
        note = picmeta.embedded(path)
        if note:
            fitted = time.strftime('%Y-%m-%d %H:%M', time.localtime(os.path.getmtime(args.map)))
            note = picmeta.add(note, f"map: {os.path.basename(args.map)} (fitted {fitted}), "
                                     f"{args.ppm:g} px/mm, {args.margin:g} mm margin")
        extra = {'icc_profile': icc} if icc else {}
        Image.fromarray(out).save(dst, quality=95, pnginfo=picmeta.pnginfo(note), **extra)
        print(f"{path} -> {dst}  ({OW}x{OH} px, {args.ppm:g} px/mm, {args.margin:g} mm margin)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)
    f = sub.add_parser('fit', help='fit the mapping from a photo of cal_colour.plot')
    f.add_argument('image')
    f.add_argument('--out', default=DEFAULT_MAP)
    f.add_argument('--lam', type=float, default=1000.0, help='TPS regularisation (px^2 units)')
    f.add_argument('--check', action='store_true', help='write <image>_check with the grid overlaid')
    f.add_argument('--lift', type=float, default=0.5,
                   help='brightness exponent before line finding, hue kept (1 = off; 0.5 brings dim lines up)')
    f.add_argument('--ppm', type=float, default=CC.PPM)
    f.add_argument('--margin', type=float, default=CC.MARGIN)
    f.add_argument('--offset', metavar='OH,OV',
                   help='the alignment to use when the frame cannot tell (the fit lists them as hN vN)')
    f.set_defaults(func=cmd_fit)
    a = sub.add_parser('apply', help='warp photographs into plot space')
    a.add_argument('images', nargs='+')
    a.add_argument('--map', default=DEFAULT_MAP)
    a.add_argument('--suffix', default='_plot')
    a.add_argument('--ppm', type=float, default=CC.PPM)
    a.add_argument('--margin', type=float, default=CC.MARGIN)
    a.set_defaults(func=cmd_apply)
    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
