#!/usr/bin/env python3
#
# harmonograph3d.py — the harmonograph as a lit 3D curve casting a shadow on a
# floor: a light-painting piece for the plotter, and its preview.
#
# A third pendulum gives the figure height, so it becomes a space curve,
# seen from above and a little in front (VIEW_TILT). From that:
#
#   shading      the curve is a thin tube lit by one light (LIGHT): its
#                brightness follows the angle between the curve and the light
#                (Kajiya-Kay: diffuse sqrt(1 - (T.L)^2), plus a highlight
#                towards white), and it is darker where another strand stands
#                between it and the light (self-shadow).
#   crossings    light painting only adds, so nothing can be hidden after the
#                fact: where two strands cross on the picture, the one behind
#                is not drawn (LED dark for a few mm either side, like a knot
#                diagram). This is the depth cue, and it stops the crossings
#                summing to white.
#   shadow       the curve projected along the light onto a floor below it,
#                drawn as a second, dim path, with gaps where the curve
#                passes in front of it.
#
# One description, two outputs, as harmonograph.py:
#   harmonograph3d.plot      what the plotter draws: shadow first, then the
#                            curve, each one stroke; dynamic brightness on, so
#                            the light per mm stays even and `paint -ed=` sets it
#   harmonograph3d_ref.png   the preview, rendered additively, as the camera sums
#
# Brightness changes are held HOLD_MM apart (~25 ms at the feed): the carrier
# was clean down to ~13 ms between colours (rate test 2026-10-06).

import argparse
import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter
import outdirs as OD

BED = (393.0, 262.0)      # mm
MARGIN_MM = 15.0
PPM = 8.0                 # preview pixels per mm
REF_MARGIN = 5.0

FEED = 18300              # mm/min: harmonograph_blue's peak was 100% of this
SEG_MM = 2.0              # polyline segment length
HOLD_MM = 8.0             # least travel between brightness changes
MIN_RUN_MM = 4.0          # lit or dark stretches shorter than this are merged

# Pendulums: (amplitude, frequency, phase, decay). X and Y as harmonograph.py;
# Z is the new one, slow, so height changes along the loops but not within one.
X_TERMS = [(1.00, 2.000, 0.00, 0.007),
           (0.50, 5.007, 0.90, 0.011)]
Y_TERMS = [(1.00, 3.000, 0.30, 0.007),
           (0.45, 4.004, 0.00, 0.011)]
Z_TERMS = [(1.00, 1.003, 0.60, 0.006),
           (0.35, 6.010, 0.00, 0.011)]
T_END = 85.0
T_STEP = 0.004

ASPECT = 1.45             # the figure's width : depth before the view
Z_HEIGHT = 0.55           # height range, as a share of the depth
FLOOR_GAP = 0.15          # floor below the lowest point, same units
VIEW_TILT = 28.0          # degrees: 0 = straight down, more shows the floor more
LIGHT = (-0.40, 0.30, 0.85)   # towards the light: from the left, behind, above

TUBE_MM = 1.6             # the drawn line's radius, for occlusion and shadows
GAP_MM = 2.6              # a strand behind another is dark within this of it
DEPTH_MM = 2.0            # ... if it is at least this much further away
ARC_SKIP_MM = 8.0         # neighbours along the curve never hide each other
CROSS_DEG = 25.0          # nor do strands running side by side (the ribbon's
                          # loops): only those crossing at more than this

# Colours at full drive; `paint` scales them all by the brightness.
LIT = np.array([0, 0, 255.0])         # a strand fully facing the light
DARK_SIDE = 0.03                     # ambient: a strand edge-on to it, or shadowed
HIGHLIGHT = np.array([110, 150, 255.0])  # what the highlight blends towards
DIFFUSE_POWER = 2.5                   # >1 deepens the fall-off from lit to dark side
SPEC_POWER = 6 
SPEC = 0.18
SHADOW = np.array([0, 0, 35.0])      # the floor shadow
LEVELS = 16                          # brightness steps


def pendulums(terms, t):
    return sum(a * np.sin(f * t + p) * np.exp(-d * t) for a, f, p, d in terms)


def unit(v):
    return v / np.linalg.norm(v, axis=-1, keepdims=True)


def thinned(terms, k):
    """The slow parts sped up k times: each frequency's offset from its whole
    number, and the decay. Run for T_END / k, the figure keeps its shape and
    its ribbons their width, with 1/k as many strands, k times as far apart."""
    return [(a, round(f) + (f - round(f)) * k, p, d * k) for a, f, p, d in terms]


def build(k=1.0):
    t = np.arange(0.0, T_END / k, T_STEP)
    P = np.stack([pendulums(thinned(X_TERMS, k), t), pendulums(thinned(Y_TERMS, k), t),
                  pendulums(thinned(Z_TERMS, k), t)], 1)
    # Normalise: x and y to the bed's shape (a harmonograph has no aspect of its
    # own to keep), z to a share of the depth.
    for k, half in ((0, ASPECT), (1, 1.0), (2, Z_HEIGHT)):
        lo, hi = P[:, k].min(), P[:, k].max()
        P[:, k] = (P[:, k] - (lo + hi) / 2) / (hi - lo) * 2 * half
    L = unit(np.array(LIGHT))
    zf = P[:, 2].min() - FLOOR_GAP
    S = P - np.outer((P[:, 2] - zf) / L[2], L)            # shadow on the floor

    # Orthographic view, tilted about x: screen (u, v), depth towards the viewer.
    a = math.radians(VIEW_TILT)
    V = np.array([0.0, -math.sin(a), math.cos(a)])        # towards the viewer

    def view(Q):
        u = Q[:, 0]
        v = Q[:, 1] * math.cos(a) + Q[:, 2] * math.sin(a)
        return np.stack([u, v], 1), Q @ V

    p2, pd = view(P)
    s2, sd = view(S)
    both = np.vstack([p2, s2])
    lo, hi = both.min(0), both.max(0)
    scale = min((BED[0] - 2 * MARGIN_MM) / (hi[0] - lo[0]),
                (BED[1] - 2 * MARGIN_MM) / (hi[1] - lo[1]))
    centre = (lo + hi) / 2

    def to_bed(q2):
        return (q2 - centre) * scale + np.array(BED) / 2

    return dict(P=P * scale, S=S * scale, p2=to_bed(p2), s2=to_bed(s2),
                pd=pd * scale, sd=sd * scale, L=L, V=V)


def resample(xy, step):
    """Fractional indices into xy, evenly spaced along its length."""
    d = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(xy, axis=0).T))])
    targets = np.arange(0, d[-1], step)
    return np.interp(targets, d, np.arange(len(xy))), targets


def at(arr, fi):
    i = np.minimum(fi.astype(int), len(arr) - 2)
    f = (fi - i)[:, None] if arr.ndim == 2 else fi - i
    return arr[i] * (1 - f) + arr[i + 1] * f


def screen_dir(xy):
    return unit(np.gradient(xy, axis=0))


def hidden(xy, depth, arc, oxy, odepth, oarc, same):
    """True where a point of (xy, depth) has a point of the other set in front
    of it within GAP_MM on the picture. same: the other set is the same curve,
    so its near neighbours along the arc don't count."""
    out = np.zeros(len(xy), bool)
    td, otd = screen_dir(xy), screen_dir(oxy)
    cos_max = math.cos(math.radians(CROSS_DEG))
    for s in range(0, len(xy), 400):
        e = min(s + 400, len(xy))
        d2 = ((xy[s:e, None, :] - oxy[None, :, :]) ** 2).sum(-1)
        m = (d2 < GAP_MM ** 2) & (odepth[None, :] - depth[s:e, None] > DEPTH_MM)
        if same:
            m &= np.abs(arc[s:e, None] - oarc[None, :]) > ARC_SKIP_MM
            m &= np.abs(td[s:e] @ otd.T) < cos_max
        out[s:e] = m.any(1)
    return out


def self_shadow(P, arc, L):
    """1 where nothing stands between a point and the light, falling to 0 in
    another strand's shadow (soft over a tube's width)."""
    lit = np.ones(len(P))
    for s in range(0, len(P), 400):
        e = min(s + 400, len(P))
        D = P[None, :, :] - P[s:e, None, :]                 # to every other point
        along = D @ L
        perp = np.sqrt(np.maximum((D ** 2).sum(-1) - along ** 2, 0))
        m = (along > 2 * TUBE_MM) & (np.abs(arc[s:e, None] - arc[None, :]) > ARC_SKIP_MM)
        f = np.where(m, np.clip((perp - TUBE_MM) / (2 * TUBE_MM), 0, 1), 1)
        lit[s:e] = f.min(1)
    return lit


def merge_short(vis, step):
    """Lit or dark runs shorter than MIN_RUN_MM take their neighbours' state."""
    vis = vis.copy()
    n = max(1, int(round(MIN_RUN_MM / step)))
    i = 0
    while i < len(vis):
        j = i
        while j < len(vis) and vis[j] == vis[i]:
            j += 1
        if j - i < n and i > 0:
            vis[i:j] = vis[i - 1]
        i = j
    return vis


def quantise(rgb):
    peak = 255.0
    q = np.round(rgb / peak * LEVELS) / LEVELS * peak
    return [tuple(int(min(255, max(0, c))) for c in row) for row in q]


def hold(cols, vis, step):
    """Brightness held HOLD_MM between changes; lit/dark switches are kept
    exact (they are the crossings), and no brightness change comes within
    MIN_RUN_MM before one."""
    vis = list(vis)
    n = int(round(HOLD_MM / step))
    near = int(round(MIN_RUN_MM / step))
    next_switch = [len(vis)] * len(vis)          # index of the next lit/dark switch
    for i in range(len(vis) - 2, -1, -1):
        next_switch[i] = i + 1 if vis[i + 1] != vis[i] else next_switch[i + 1]
    out, last, since = [], None, 1e9
    for i, (c, v) in enumerate(zip(cols, vis)):
        want = c if v else (0, 0, 0)
        switched = last is None or v != (last != (0, 0, 0))
        if switched or (v and want != last and since >= n and next_switch[i] - i >= near):
            if want != last:
                since = 0
            last = want
        out.append(last)
        since += 1
    return out


def make(k=1.0):
    g = build(k)
    fi_p, arc_p = resample(g['p2'], SEG_MM)
    fi_s, arc_s = resample(g['s2'], SEG_MM)
    P, p2, pd = at(g['P'], fi_p), at(g['p2'], fi_p), at(g['pd'], fi_p)
    s2, sd = at(g['s2'], fi_s), at(g['sd'], fi_s)

    # Shading: tangent against the light and the view (hair shading).
    T = unit(np.gradient(P, axis=0))
    tl, tv = T @ g['L'], T @ g['V']
    sl, sv = np.sqrt(1 - tl ** 2), np.sqrt(1 - tv ** 2)
    diffuse = sl ** DIFFUSE_POWER
    spec = np.clip(sl * sv - tl * tv, 0, 1) ** SPEC_POWER
    shade = self_shadow(P, arc_p, g['L'])
    k = DARK_SIDE + (1 - DARK_SIDE) * diffuse * shade
    rgb = LIT * k[:, None]
    rgb = rgb + (HIGHLIGHT - rgb) * (SPEC * spec * shade)[:, None]

    vis_p = merge_short(~hidden(p2, pd, arc_p, p2, pd, arc_p, True), SEG_MM)
    vis_s = merge_short(~hidden(s2, sd, arc_s, p2, pd, arc_p, False), SEG_MM)
    # The shadow fades with the height that casts it: far from the floor, soft.
    h = at(g['P'], fi_s)[:, 2] - at(g['S'], fi_s)[:, 2]
    fade = 1.0 - 0.5 * (h - h.min()) / max(h.max() - h.min(), 1e-9)
    srgb = SHADOW * fade[:, None]

    curve = (p2, hold(quantise(rgb), vis_p, SEG_MM), arc_p[-1])
    shadow = (s2, hold(quantise(srgb), vis_s, SEG_MM), arc_s[-1])
    stats = dict(hidden_curve=1 - vis_p.mean(), hidden_shadow=1 - vis_s.mean(),
                 self_shadowed=(shade < 0.5).mean())
    return curve, shadow, stats


def write_plot(path, curve, shadow, feed):
    total = curve[2] + shadow[2]
    changes = 0
    with open(path, 'w') as f:
        f.write(f"# {path} — generated by harmonograph3d.py, do not edit.\n")
        f.write(f"# The harmonograph as a lit 3D curve over its shadow: shadow "
                f"{shadow[2]:.0f} mm, curve {curve[2]:.0f} mm,\n")
        f.write(f"# about {total / (feed / 60.0):.0f}s of drawing at {feed} mm/min "
                f"(more: the pen rarely reaches it).\n")
        f.write("# Shading in the colours (full drive = 255), dynamic brightness on;\n"
                "# shoot with `paint harmonograph3d.plot -ed=X`. UNPLUG THE SOLENOID.\n\n")
        f.write("home\nstatus          # check WPos:393.000,0.000 before plotting\n\n")
        f.write(f"lightpen on\nspeed {feed}\ndynamic on\n")
        for name, (pts, cols, length) in (("shadow", shadow), ("curve", curve)):
            f.write(f"\n# {name}\n")
            last = None
            for i, ((x, y), c) in enumerate(zip(pts, cols)):
                if c != last:
                    f.write("colour %d %d %d\n" % c)
                    last = c
                    changes += 1
                f.write(f"{'moveto' if i == 0 else 'drawto'} {x:.2f} {y:.2f}\n")
        f.write("\ndynamic off\npenup\ncolour off\n"
                "home            # park at the home corner when done\n")
    n = len(curve[0]) + len(shadow[0])
    print(f"wrote {path}: {n} points, {total:.0f} mm, {changes} colour changes, "
          f"~{total / (feed / 60.0):.0f}s at full feed")


def write_ref(path, curve, shadow):
    """Additive preview: each brightness drawn as a core plus a halo, summed."""
    W = int(round((BED[0] + 2 * REF_MARGIN) * PPM))
    H = int(round((BED[1] + 2 * REF_MARGIN) * PPM))

    def px(p):
        return ((p[0] + REF_MARGIN) * PPM, (BED[1] + REF_MARGIN - p[1]) * PPM)

    layers = {}                      # colour -> mask of every segment in it
    for pts, cols, _ in (shadow, curve):
        for i in range(1, len(pts)):
            c = cols[i]
            if c == (0, 0, 0):
                continue
            m = layers.get(c)
            if m is None:
                m = layers[c] = Image.new('L', (W, H), 0)
            ImageDraw.Draw(m).line([px(pts[i - 1]), px(pts[i])], fill=255, width=3)
    acc = Image.new('RGB', (W, H), (0, 0, 0))
    for c, mask in layers.items():
        core = Image.merge('RGB', [mask.point(lambda v, k=k: v * k // 255) for k in c])
        glow = core.filter(ImageFilter.GaussianBlur(5)).point(lambda v: int(v * 0.8))
        acc = ImageChops.add(ImageChops.add(acc, core), glow)
    acc.save(path)
    print(f"wrote {path}: {W}x{H} px at {PPM:g} px/mm")


def main():
    ap = argparse.ArgumentParser(description='The harmonograph as a lit 3D curve casting a shadow.')
    ap.add_argument('--plot', default=OD.plot('harmonograph3d.plot'))
    ap.add_argument('--ref', default=OD.preview('harmonograph3d_ref.png'))
    ap.add_argument('--feed', type=int, default=FEED, help='mm/min')
    ap.add_argument('--strands', type=float, default=1.0,
                    help='share of the strands to draw, spread wider apart (0.5: half, twice the spacing)')
    args = ap.parse_args()
    curve, shadow, stats = make(1.0 / args.strands)
    print("hidden behind a nearer strand: curve {hidden_curve:.0%}, shadow {hidden_shadow:.0%}; "
          "curve in its own shadow: {self_shadowed:.0%}".format(**stats))
    write_plot(args.plot, curve, shadow, args.feed)
    write_ref(args.ref, curve, shadow)


if __name__ == '__main__':
    main()
