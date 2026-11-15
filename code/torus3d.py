#!/usr/bin/env python3
#
# torus3d.py — a torus bent out of its plane (a saddle-shaped ring), its tube
# drawn as a set of strands running round it, lit and shaded, over its shadow
# on a floor: a light-painting piece for the plotter, and its preview.
#
# The tube is a real surface here, so each strand has a surface normal and is
# shaded the way a surface is, not like a hair:
#
#   shading      Lambert (n.L) with a little ambient and a soft highlight; and
#                scaled by how squarely the strand faces the camera (n.V),
#                because strands crowd together on the picture towards the
#                tube's edges and would otherwise sum to a bright rim.
#   hidden       a strand is dark wherever the tube is in front of it: on the
#                tube's far side, and where the ring passes in front of itself.
#                Light painting only adds, so what is hidden must not be drawn.
#   self-shadow  dark where another part of the ring stands between it and
#                the light.
#   shadow       every strand projected along the light onto a floor below,
#                dim, dark where the ring is in front of it.
#
# Writes torus3d.plot (shadow strands, then the tube's; each strand one closed
# stroke, dynamic brightness on, so `paint -ed=` sets the level) and
# torus3d_ref.png, the preview, summed as the camera sums.

import argparse
import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from harmonograph3d import hold, merge_short, resample, at, unit
import outdirs as OD

BED = (393.0, 262.0)
MARGIN_MM = 15.0
PPM = 8.0
REF_MARGIN = 5.0

FEED = 18300
SEG_MM = 2.0

# The shape, in units of the ring's radius.
TUBE = 0.24               # tube radius
BEND = 0.0                # height of the saddle: z = BEND sin(2 phi)
TWIST = 0                 # turns each strand makes round the tube per circuit
STRANDS = 32              # round the tube (about half are on the far side)
FLOOR_GAP = 0.12          # floor below the lowest point of the tube
VIEW_TILT = 35.0          # degrees from straight down
RING_TILT = 30.0          # the ring tipped away from the observer, degrees
CAM_DIST = 2.4            # eye to the ring's centre, in ring radii: near, so strong perspective
LIGHT = (-0.90, -0.15, 0.40)  # towards the light: low, from the left, a little in front
FILL = (0.75, -0.35, 0.55)    # a second, weaker light from the right, for the dark side
FILL_STRENGTH = 0.4
RING_SAMPLES = 1440

AMBIENT = 0.04
LIT = np.array([0, 0, 255.0])
GREEN_FROM = 1.0          # share of full drive where green starts to join the blue (1: never)
GREEN_TOP = 0.0           # green at full drive, as a share of the blue's 255


def lit(f):
    """Colours for shades f (0-1 of full drive): blue, warming towards cyan
    above GREEN_FROM, green reaching GREEN_TOP x 255 at full."""
    f = np.asarray(f, float)
    g = np.clip((f - GREEN_FROM) / max(1e-9, 1.0 - GREEN_FROM), 0, 1) * GREEN_TOP
    return LIT * f[:, None] + np.array([0, 255.0, 0]) * (f * g)[:, None]
WHITE = np.array([255.0, 255.0, 255.0])   # --white: grey, which the SK6812 draws in its W element alone
# Specular (Blinn-Phong): SPEC x (n.h)^SPEC_POWER from the main light, h halfway
# between the light and the eye; SPEC against the diffuse's 1. A higher power
# makes the highlight smaller and sharper.
SPEC, SPEC_POWER = 0.0, 16      # 0: matt, no highlight
FACING_MIN = 0.3          # n.V scaling never below this (the silhouette stays visible)
SHADOW = np.array([0, 0, 30.0])
OUTLINE = 0.6             # the tube's outlines (where it turns away from the eye), as a share of full drive
SHADOW_LINES = 0          # the floor shadow is a band of this many parallel lines
OVERLAP_MM = 2.0          # a ring lit all the way round runs on this far past its start
OUTLINE_MIN_MM = 15.0     # visible outline pieces shorter than this are not drawn
MARGIN_HIDE_MM = 0.0      # a strand behind the tube is dark this far beyond its edge
# Brightness levels a constant ratio apart, as the eye sees them: even steps of
# 16 (harmonograph3d's) were 2x and 1.5x jumps on the dark side, bands.
LEVEL_RATIO = 1.12        # each level this much brighter than the one below
LEVEL_LOWEST = 8          # dimmest level (of 255); below it, dark


def levels(lowest):
    """LEVEL_RATIO apart from `lowest` up, and 255. Near the bottom the
    rounding makes some steps whole numbers apart rather than the ratio."""
    return sorted({255} | {int(round(lowest * LEVEL_RATIO ** k))
                           for k in range(int(math.log(255 / lowest, LEVEL_RATIO)) + 1)})


LEVELS = levels(LEVEL_LOWEST)


def quantise(rgb):
    """Each colour to the nearest level (in ratio), keeping its hue."""
    out = []
    lv = np.array(LEVELS, float)
    for row in rgb:
        v = float(max(row))
        if v < LEVEL_LOWEST / math.sqrt(LEVEL_RATIO):
            out.append((0, 0, 0))
            continue
        q = lv[np.abs(np.log(lv / v)).argmin()]
        out.append(tuple(int(min(255, round(c * q / v))) for c in row))
    return out


def ring(phi):
    """Centre line, unit tangent, and a frame (N outward, B) round it."""
    c = np.stack([np.cos(phi), np.sin(phi), BEND * np.sin(2 * phi)], 1)
    dc = np.stack([-np.sin(phi), np.cos(phi), 2 * BEND * np.cos(2 * phi)], 1)
    T = unit(dc)
    out = np.stack([np.cos(phi), np.sin(phi), 0 * phi], 1)
    N = unit(out - (out * T).sum(1, keepdims=True) * T)
    B = np.cross(T, N)
    return c, T, N, B


def drop_short(vis, n):
    """Lit runs shorter than n points go dark."""
    vis = vis.copy()
    i = 0
    while i < len(vis):
        j = i
        while j < len(vis) and vis[j] == vis[i]:
            j += 1
        if vis[i] and j - i < n:
            vis[i:j] = False
        i = j
    return vis


def seam(xy, val, vis):
    """Where a closed stroke starts and ends. A stroke starts and stops
    dark-ish (the LED lights a few ms late), so the seam goes in the middle of
    the longest stretch that is hidden anyway; a ring that is lit all the way
    round is closed and runs on OVERLAP_MM past its start instead."""
    n = len(vis)
    if (~vis).any():
        best, run, start = (0, 0), 0, 0
        for i in range(2 * n):                 # twice round: a dark run may wrap
            if not vis[i % n]:
                if run == 0:
                    start = i
                run += 1
                if run > best[0] and run <= n:
                    best = (run, start)
            else:
                run = 0
        k = (best[1] + best[0] // 2) % n
        idx = np.r_[k:n, 0:k, k]               # round, and back to the start
    else:
        idx = np.r_[0:n, 0:int(round(OVERLAP_MM / SEG_MM)) + 1]
    return xy[idx], val[idx], vis[idx]


def rotate_x(deg):
    t = math.radians(deg)
    return np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]])


def make(strands=STRANDS):
    phi = np.linspace(0, 2 * math.pi, RING_SAMPLES, endpoint=False)
    c, T, N, B = ring(phi)
    # Tip the ring away from the observer: its far side drops, its near side
    # rises, so it is seen more edge-on.
    R = rotate_x(-RING_TILT)
    c, N, B = c @ R.T, N @ R.T, B @ R.T
    L = unit(np.array(LIGHT))
    L2 = unit(np.array(FILL))

    # A camera CAM_DIST ring radii from the ring's centre, above and in front
    # (VIEW_TILT from straight down), looking at the centre: perspective.
    a = math.radians(VIEW_TILT)
    E = CAM_DIST * np.array([0.0, -math.sin(a), math.cos(a)])
    fwd = -unit(E)
    right = np.array([1.0, 0, 0])
    up = np.cross(right, fwd)

    def view(Q):
        """Picture position (before scaling to the bed) and distance from the eye."""
        q = Q - E
        dist = q @ fwd
        return np.stack([q @ right / dist, q @ up / dist], -1), dist

    thetas = 2 * math.pi * np.arange(strands) / strands
    tube, normal = [], []
    for th in thetas:
        th = (th + TWIST * phi)[:, None]
        n = np.cos(th) * N + np.sin(th) * B
        tube.append(c + TUBE * n)
        normal.append(n)
    # Outlines: round each section, the two points where the surface turns
    # away from the eye, n.(E - P) = 0 with P = c + r n, so
    # n.(E - c) = r: a cos t + b sin t = r.
    edges = []
    if OUTLINE:
        w = E - c
        a_, b_ = (N * w).sum(1), (B * w).sum(1)
        base = np.arctan2(b_, a_)
        half = np.arccos(np.clip(TUBE / np.hypot(a_, b_), -1, 1))
        for t in (base + half, base - half):
            n = np.cos(t)[:, None] * N + np.sin(t)[:, None] * B
            edges.append((c + TUBE * n, n))

    allxy = np.vstack([view(P)[0] for P in tube])
    lo, hi = allxy.min(0), allxy.max(0)
    S = min((BED[0] - 2 * MARGIN_MM) / (hi[0] - lo[0]), (BED[1] - 2 * MARGIN_MM) / (hi[1] - lo[1]))
    centre = (lo + hi) / 2

    def bed(xy):
        return (xy - centre) * S + np.array(BED) / 2

    c2u, cdist = view(c)
    c2 = bed(c2u)
    r_pic = TUBE * S / cdist             # each section's half-width on the picture, mm

    def covered(P, margin=MARGIN_HIDE_MM, lift=0.004):
        """Where the ray from the eye to P meets the tube before P (by more
        than lift ring radii). The tube is the union of balls of its radius
        along the centre line; margin (mm on the picture) widens them for the
        test of whether the ray passes, so strands stop a little short of an
        edge that hides them."""
        out = np.zeros(len(P), bool)
        Cw = c - E
        for s0 in range(0, len(P), 300):
            e = min(s0 + 300, len(P))
            q = P[s0:e] - E
            dist = np.linalg.norm(q, axis=1)
            u = q / dist[:, None]
            tc = u @ Cw.T                                   # along the ray to each ball's centre
            perp2 = np.maximum((Cw ** 2).sum(1)[None] - tc ** 2, 0)
            widen = margin / S * cdist[None]                # mm on the picture, as ring radii there
            near = perp2 < (TUBE + widen) ** 2
            hit = tc - np.sqrt(np.maximum(TUBE ** 2 - perp2, 0))
            out[s0:e] = (near & (hit < dist[:, None] - lift) & (tc > 0)).any(1)
        return out

    def shadowed(P):
        """1 in the light, 0 where another part of the ring blocks it (soft)."""
        lit = np.ones(len(P))
        for s0 in range(0, len(P), 300):
            e = min(s0 + 300, len(P))
            D = c[None] - P[s0:e, None]
            along = D @ L
            perp = np.sqrt(np.maximum((D ** 2).sum(-1) - along ** 2, 0))
            m = along > 1.5 * TUBE
            f = np.where(m, np.clip((perp - TUBE) / (0.4 * TUBE), 0, 1), 1)
            lit[s0:e] = f.min(1)
        return lit

    def trace(P, n):
        """A closed curve on the tube: picture points every SEG_MM, with the
        3D point, normal, and distance from the eye at each."""
        P, n = (np.vstack([q, q[:1]]) for q in (P, n))
        xyu, _ = view(P)
        xy = bed(xyu)
        fi, _ = resample(xy, SEG_MM)
        Pm, nm, xym = at(P, fi), unit(at(n, fi)), at(xy, fi)
        return Pm, nm, xym, (Pm - E) @ fwd

    strokes, raw = [], []
    stats = dict(hidden=0.0, n=0)
    for P, n in zip(tube, normal):
        Pm, nm, xym, dist = trace(P, n)
        vis = merge_short(~covered(Pm), SEG_MM)
        Vp = unit(E - Pm)                        # towards the eye, from each point
        sh = shadowed(Pm)
        lam = np.clip(nm @ L, 0, 1) * sh + FILL_STRENGTH * np.clip(nm @ L2, 0, 1)
        spec = 0.0
        if SPEC:
            h = unit(L[None] + Vp)
            spec = SPEC * np.clip((nm * h).sum(1), 0, 1) ** SPEC_POWER * (nm @ L > 0) * sh
        facing = np.clip((nm * Vp).sum(1), FACING_MIN, 1)
        raw.append((xym, (AMBIENT + (1 - AMBIENT) * lam + spec) * facing, vis))
        stats['hidden'] += (~vis).sum()
        stats['n'] += len(vis)
    for P, n in edges:
        Pm, nm, xym, dist = trace(P, n)
        # On the edge itself the neighbouring sections graze it: only a part
        # of the ring clearly in front hides an outline.
        vis = ~covered(Pm, margin=0.0, lift=0.05 * TUBE)
        # Scraps of edge between two hidden stretches (where the outline
        # grazes the tube's own far side) are left dark.
        vis = drop_short(vis, int(OUTLINE_MIN_MM / SEG_MM))
        lam = np.clip(nm @ L, 0, 1) * shadowed(Pm) + FILL_STRENGTH * np.clip(nm @ L2, 0, 1)
        xym, lam, vis = seam(xym, np.minimum(lam, 1), vis)
        strokes.append((xym, hold(quantise(lit(OUTLINE * (0.4 + 0.6 * lam))), vis, SEG_MM)))
    # The best-lit visible point at full drive.
    peak = max(k[v].max() for _, k, v in raw if v.any())
    for xym, k, vis in raw:
        xym, k, vis = seam(xym, k, vis)
        strokes.append((xym, hold(quantise(lit(k / peak)), vis, SEG_MM)))
    stats['hidden'] /= stats['n']
    return [], strokes, stats


def length(xy):
    return float(np.hypot(*np.diff(xy, axis=0).T).sum())


def write_plot(path, floor, tube, feed, led=None):
    total = sum(length(s[0]) for s in floor + tube)
    changes = 0
    with open(path, 'w') as f:
        f.write(f"# {path} — generated by torus3d.py, do not edit.\n")
        f.write(f"# A saddle-bent torus in {len(tube)} shaded strands over its shadow: "
                f"{total:.0f} mm, ~{total / (feed / 60.0):.0f}s at {feed} mm/min.\n")
        f.write("# Shading in the colours (full drive = 255), dynamic brightness on;\n"
                "# shoot with `paint torus3d.plot -ed=X`. UNPLUG THE SOLENOID.\n\n")
        f.write("home\nstatus          # check WPos:393.000,0.000 before plotting\n\n")
        if led:
            f.write(f"led type {led}     # the carrier's LED, set before the first colour\n\n")
        f.write(f"lightpen on\nspeed {feed}\ndynamic on\n")
        for name, strokes in (("shadow", floor), ("tube", tube)):
            for k, (pts, cols) in enumerate(strokes):
                f.write(f"\n# {name} strand {k + 1}\n")
                last = None
                for i, ((x, y), c) in enumerate(zip(pts, cols)):
                    if c != last:
                        f.write("colour %d %d %d\n" % c)
                        last = c
                        changes += 1
                    f.write(f"{'moveto' if i == 0 else 'drawto'} {x:.2f} {y:.2f}\n")
        f.write("\ndynamic off\npenup\ncolour off\n"
                "home            # park at the home corner when done\n")
    print(f"wrote {path}: {total:.0f} mm, {changes} colour changes, "
          f"~{total / (feed / 60.0):.0f}s at full feed")


def write_ref(path, strokes, gain=1.0):
    """The preview, built as the camera sums light: lines about as wide as the
    LED's trace, a halo, and over-exposure spilling towards white (a sensor
    channel at its ceiling no longer holds the colour)."""
    W = int(round((BED[0] + 2 * REF_MARGIN) * PPM))
    H = int(round((BED[1] + 2 * REF_MARGIN) * PPM))

    def px(p):
        return ((p[0] + REF_MARGIN) * PPM, (BED[1] + REF_MARGIN - p[1]) * PPM)

    layers = {}
    for pts, cols in strokes:
        for i in range(1, len(pts)):
            c = cols[i]
            if c == (0, 0, 0):
                continue
            m = layers.setdefault(c, Image.new('L', (W, H), 0))
            ImageDraw.Draw(m).line([px(pts[i - 1]), px(pts[i])], fill=255, width=int(PPM * 1.2))
    acc = np.zeros((H, W, 3))
    for c, mask in layers.items():
        core = np.asarray(mask.filter(ImageFilter.GaussianBlur(PPM * 0.4)), float) / 255
        glow = np.asarray(mask.filter(ImageFilter.GaussianBlur(PPM * 1.5)), float) / 255 * 0.6
        acc += (core + glow)[..., None] * np.array(c, float)[None, None] * gain
    over = np.clip(acc.max(-1, keepdims=True) - 255, 0, None)   # spill past the ceiling
    out = np.clip(acc + over * 0.8, 0, 255).astype(np.uint8)
    Image.fromarray(out).save(path)
    print(f"wrote {path}")


def main():
    global GREEN_FROM, GREEN_TOP, SPEC, SPEC_POWER, LIT, OUTLINE, LEVEL_LOWEST, LEVELS
    ap = argparse.ArgumentParser(description='A saddle-bent torus in shaded strands, over its shadow.')
    ap.add_argument('--plot', default=OD.plot('torus3d.plot'))
    ap.add_argument('--ref', default=OD.preview('torus3d_ref.png'))
    ap.add_argument('--feed', type=int, default=FEED)
    ap.add_argument('--strands', type=int, default=STRANDS)
    ap.add_argument('--gain', type=float, default=1.0, help='preview brightness')
    ap.add_argument('--led', help='LED type the plot is for (apa106, sk6812): '
                    'writes `led type` into the plot, so the carrier is set for it')
    ap.add_argument('--green-from', type=float, default=GREEN_FROM,
                    help='share of full drive where green starts to join the blue (default 1: never)')
    ap.add_argument('--green', type=float, default=GREEN_TOP,
                    help='green at full drive, as a share of the blue (e.g. 0.4)')
    ap.add_argument('--spec', type=float, default=SPEC,
                    help='specular highlight strength against the diffuse 1 (default 0: matt)')
    ap.add_argument('--spec-power', type=float, default=SPEC_POWER,
                    help='specular exponent: higher, a smaller, sharper highlight (default 16)')
    ap.add_argument('--white', action='store_true',
                    help='draw in white (grey: the SK6812 W element), to be coloured afterwards; '
                         'no green mixing')
    ap.add_argument('--colour', help='the colour at full drive, "R,G,B" (default 0,0,255: blue); '
                    'no green mixing, e.g. 0,255,0 for green alone')
    ap.add_argument('--outline', type=float, default=OUTLINE,
                    help="the tube's outlines, as a share of full drive (default %(default)s; 0: none)")
    ap.add_argument('--lowest', type=int, default=LEVEL_LOWEST,
                    help='dimmest LED level drawn, of 255; anything dimmer is left dark '
                         '(default %(default)s; the SK6812 records cleanly down to 5 at ED 10.9)')
    args = ap.parse_args()
    GREEN_FROM, GREEN_TOP = args.green_from, args.green
    LEVEL_LOWEST, LEVELS = args.lowest, levels(args.lowest)
    OUTLINE = args.outline
    SPEC, SPEC_POWER = args.spec, args.spec_power
    if args.white:
        LIT, GREEN_TOP = WHITE, 0.0
    elif args.colour:
        LIT, GREEN_TOP = np.array([float(v) for v in args.colour.split(',')]), 0.0
    floor, tube, stats = make(args.strands)
    print(f"tube strands dark behind the tube: {stats['hidden']:.0%}")
    write_plot(args.plot, floor, tube, args.feed, args.led)
    write_ref(args.ref, floor + tube, args.gain)


if __name__ == '__main__':
    main()
