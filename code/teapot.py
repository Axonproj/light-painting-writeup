#!/usr/bin/env python3
#
# teapot.py — the Utah teapot, Phong-shaded, drawn in horizontal lines, as a
# light-paint plot with a preview.
#
# The teapot is Martin Newell's (1975), from his own patch data: ten bicubic
# Bezier patches, six of them turned into the four quadrants of the body and
# four mirrored for the two sides of the handle and the spout, 32 in all. It
# is seen from the side and a little above (--yaw, --tilt), orthographically,
# spout to the right, lit by sphere.py's spotlight and shaded by sphere.py's
# code. It also casts shadows on itself: the knob on the lid, the spout and
# the handle on the body.
#
# There is no formula for where a line meets a teapot, so the patches are
# rendered first — every patch sampled densely and the nearest sample kept at
# each pixel — and the lines are read off that picture. A line can cross the
# teapot in several pieces (the handle, the gap inside it, the body, the
# spout): the pen crosses the gaps at speed with the LED dark.
#
# Usage:
#   ./teapot.py                                  # as wide as the bed allows
#   ./teapot.py --pitch 4 --colour 255,120,30
#   ./teapot.py --yaw=-35 --tilt 30 --shadow 0   # negative values need the =
# The picture of 2026-10-10, the teapot in a pool of light on an orange floor
# (paint it at brightness 100; --level sets the exposure):
#   ./teapot.py --pitch 3 --seg 3 --speed 6000 --level 0.605 --width 270 \
#       --canvas 358,256 --shift=-40,26 --colour 25,80,255 --light=-2,4,3 \
#       --cone 8.5,13 --aim=-0.15,0 --ambient 0.2 --soft 0.3 --shininess 35 \
#       --fill=4,1.2,0.2,0.65 --floor 255,45,0
# At 8500 mm/min the same plot stalled here and there (bright dots): a move
# and a colour every 21 ms is more than the link to GRBL reliably carries.
# Writes NAME.plot and NAME_preview.png (--out NAME, default teapot).
#
# Drawn as sphere.py draws: fast, with a dark run-up at each end of every
# line, which is what limits the teapot's width (see sphere.py).

import argparse
import copy
import math
import sys

import numpy as np
from PIL import Image, ImageFilter

from photo_hlines import PLOT_X, PLOT_Y, preview
from rate_test import ACCEL
from sphere import common_args, parse_common, run_up, shade, smoothstep, triple, unit, write_plot
import outdirs as OD

PX = 3                          # render pixels per mm
SAMPLES = 700                   # samples each way across a patch: more than
                                # its widest span in pixels, so no gaps
SHADOW_PX = 1024                # the light's own view of the teapot, for shadows
SHADOW_NEAR, SHADOW_FAR = 0.03, 0.12    # a point is shadowed by what stands
                                # this far (in half-widths) nearer the light:
                                # nothing at NEAR, fully at FAR. The margin
                                # keeps a surface from shadowing itself where
                                # it turns away, which the shading does better.
MIN_MM = 2.0                    # shorter lit pieces are dropped, shorter gaps bridged
HOLD = 0.03                     # colour change not worth a command, as a fraction
FLOOR_MIN = 20.0                # the floor is drawn where it is at least this bright (of 255)
MARGIN = 3.0                    # mm kept clear at the ends of the run-ups

# Newell's control points, x y z with z up, the spout along +x.
CP = np.array([[float(v) for v in p.split()] for p in """
    1.4 0 2.4, 1.4 -0.784 2.4, 0.784 -1.4 2.4, 0 -1.4 2.4, 1.3375 0 2.53125,
    1.3375 -0.749 2.53125, 0.749 -1.3375 2.53125, 0 -1.3375 2.53125, 1.4375 0 2.53125,
    1.4375 -0.805 2.53125, 0.805 -1.4375 2.53125, 0 -1.4375 2.53125, 1.5 0 2.4,
    1.5 -0.84 2.4, 0.84 -1.5 2.4, 0 -1.5 2.4, 1.75 0 1.875, 1.75 -0.98 1.875,
    0.98 -1.75 1.875, 0 -1.75 1.875, 2 0 1.35, 2 -1.12 1.35, 1.12 -2 1.35, 0 -2 1.35,
    2 0 0.9, 2 -1.12 0.9, 1.12 -2 0.9, 0 -2 0.9, 2 0 0.45, 2 -1.12 0.45, 1.12 -2 0.45,
    0 -2 0.45, 1.5 0 0.225, 1.5 -0.84 0.225, 0.84 -1.5 0.225, 0 -1.5 0.225, 1.5 0 0.15,
    1.5 -0.84 0.15, 0.84 -1.5 0.15, 0 -1.5 0.15, 0 0 3.15, 0 -0.002 3.15, 0.002 0 3.15,
    0.8 0 3.15, 0.8 -0.45 3.15, 0.45 -0.8 3.15, 0 -0.8 3.15, 0 0 2.85, 0.2 0 2.7,
    0.2 -0.112 2.7, 0.112 -0.2 2.7, 0 -0.2 2.7, 0.4 0 2.55, 0.4 -0.224 2.55, 0.224 -0.4 2.55,
    0 -0.4 2.55, 1.3 0 2.55, 1.3 -0.728 2.55, 0.728 -1.3 2.55, 0 -1.3 2.55, 1.3 0 2.4,
    1.3 -0.728 2.4, 0.728 -1.3 2.4, 0 -1.3 2.4, 0 0 0, 0 -1.425 0, 0.798 -1.425 0,
    1.425 -0.798 0, 1.425 0 0, 0 -1.5 0.075, 0.84 -1.5 0.075, 1.5 -0.84 0.075, 1.5 0 0.075,
    -1.6 0 2.025, -1.6 -0.3 2.025, -1.5 -0.3 2.25, -1.5 0 2.25, -2.3 0 2.025,
    -2.3 -0.3 2.025, -2.5 -0.3 2.25, -2.5 0 2.25, -2.7 0 2.025, -2.7 -0.3 2.025,
    -3 -0.3 2.25, -3 0 2.25, -2.7 0 1.8, -2.7 -0.3 1.8, -3 -0.3 1.8, -3 0 1.8, -2.7 0 1.575,
    -2.7 -0.3 1.575, -3 -0.3 1.35, -3 0 1.35, -2.5 0 1.125, -2.5 -0.3 1.125,
    -2.65 -0.3 0.9375, -2.65 0 0.9375, -2 0 0.9, -2 -0.3 0.9, -1.9 -0.3 0.6, -1.9 0 0.6,
    1.7 0 1.425, 1.7 -0.66 1.425, 1.7 -0.66 0.6, 1.7 0 0.6, 2.6 0 1.425, 2.6 -0.66 1.425,
    3.1 -0.66 0.825, 3.1 0 0.825, 2.3 0 2.1, 2.3 -0.25 2.1, 2.4 -0.25 2.025, 2.4 0 2.025,
    2.7 0 2.4, 2.7 -0.25 2.4, 3.3 -0.25 2.4, 3.3 0 2.4, 2.8 0 2.475, 2.8 -0.25 2.475,
    3.525 -0.25 2.49375, 3.525 0 2.49375, 2.9 0 2.475, 2.9 -0.15 2.475, 3.45 -0.15 2.5125,
    3.45 0 2.5125, 2.8 0 2.4, 2.8 -0.15 2.4, 3.2 -0.15 2.4, 3.2 0 2.4
""".replace("\n", " ").split(",")])

# The ten patches, as control point numbers, and how many of them (the first
# six) are turned into all four quadrants; the rest are mirrored in y.
PATCHES = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],                        # rim
    [12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27],              # body
    [24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39],
    [40, 41, 42, 40, 43, 44, 45, 46, 47, 47, 47, 47, 48, 49, 50, 51],              # lid
    [48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63],
    [64, 64, 64, 64, 65, 66, 67, 68, 69, 70, 71, 72, 39, 38, 37, 36],              # bottom
    [73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86, 87, 88],              # handle
    [85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100],
    [101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116],  # spout
    [113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128],
]
TURNED = 6


def model_patches():
    """The 32 patches, each a 4x4 grid of control points."""
    out = []
    for i, numbers in enumerate(PATCHES):
        c = CP[numbers].reshape(4, 4, 3)
        if i < TURNED:
            for quarter in range(4):
                out.append(c)
                c = c[..., [1, 0, 2]] * (-1, 1, 1)      # a quarter turn about z
        else:
            out += [c, c * (1, -1, 1)]
    return out


def bernstein(n):
    """The cubic Bezier weights, and their derivatives, at n points over 0..1."""
    t = np.linspace(0, 1, n)[:, None]
    s = 1 - t
    b = np.hstack([s ** 3, 3 * t * s ** 2, 3 * t ** 2 * s, t ** 3])
    d = np.hstack([-3 * s ** 2, 3 * s ** 2 - 6 * t * s, 6 * t * s - 3 * t ** 2, 3 * t ** 2])
    return b, d


def view_matrix(yaw, tilt):
    """Model (z up) to view (x right, y up, z towards the viewer): turned yaw
    degrees about the vertical, then tipped tilt degrees towards the viewer."""
    a, t = math.radians(yaw), math.radians(tilt)
    stand = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]])
    turn = np.array([[math.cos(a), 0, math.sin(a)], [0, 1, 0], [-math.sin(a), 0, math.cos(a)]])
    tip = np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]])
    return tip @ turn @ stand


def nearest(buf, where, depth):
    """For each cell of flat buffer buf, the index of the sample with the
    greatest depth among those that land in it (where), or -1."""
    order = np.argsort(depth)
    chosen = np.full(buf, -1)
    chosen[where[order]] = order                         # the last written wins
    return chosen


def render(a):
    """Render the teapot: see render_points."""
    m = view_matrix(a.yaw, a.tilt)
    patches = model_patches()
    b, d = bernstein(SAMPLES)

    def surface(c):
        p = np.einsum("ui,vj,ijk->uvk", b, b, c).reshape(-1, 3)
        du = np.einsum("ui,vj,ijk->uvk", d, b, c).reshape(-1, 3)
        dv = np.einsum("ui,vj,ijk->uvk", b, d, c).reshape(-1, 3)
        n = np.cross(du, dv)
        size = np.linalg.norm(n, axis=1, keepdims=True)
        # Where a patch closes to a point (the top of the lid, the middle of
        # the bottom) the cross product vanishes: the normal there is the axis.
        flat = size[:, 0] < 1e-9
        n = np.where(flat[:, None], np.where(p[:, 2:3] > 1.5, (0, 0, 1.0), (0, 0, -1.0)),
                     n / np.maximum(size, 1e-12))
        return p @ m.T, n @ m.T, None

    hull = np.vstack([np.einsum("ui,vj,ijk->uvk", *([bernstein(24)[0]] * 2), c).reshape(-1, 3)
                      for c in patches]) @ m.T
    return render_points(a, m, hull, (surface(c) for c in patches), m @ (0, 0, 0.0), "teapot")


def render_points(a, m, hull, surfaces, base, what):
    """Render an object given as points on its surface. Returns (img, mask):
    float RGB 0-255 and coverage, row 0 at the BOTTOM, PX pixels per mm, the
    object a.width mm wide. Points are in half-widths of the object from its
    centre, as sphere.py's are in radii, so the spotlight's options mean the
    same.
    m is the view matrix; hull, some points that span the object; surfaces
    yields (points, unit normals, colours) for each part of it, dense enough
    to leave no pixel out, colours being None for --colour; base is a point of
    the floor. All but the colours are in view coordinates."""
    # Frame it: centre the view's bounding box, half its width = 1.
    lo, hi = hull.min(axis=0), hull.max(axis=0)
    centre, half = (lo + hi) / 2, (hi[0] - lo[0]) / 2
    top = (hi[1] - lo[1]) / 2 / half                    # half the height, in half-widths
    mm = a.width / 2                                    # mm per half-width
    # The picture can be larger than the teapot (--canvas), which stays in
    # its middle: reach is half the picture's size, in half-widths.
    reach_x, reach_y = 1.0, top
    if a.canvas:
        reach_x, reach_y = (v / 2 / mm for v in a.canvas)
        if reach_x < 1 or reach_y < top:
            sys.exit("--canvas is smaller than the %s (%g x %.0f mm)"
                     % (what, a.width, 2 * top * mm))
    width_px = int(round(2 * reach_x * mm * PX)) + 1
    height_px = int(round(2 * reach_y * mm * PX)) + 1
    # --shift moves the object off the middle: left and below are how far the
    # picture reaches from its centre those ways, in half-widths.
    left, below = reach_x + a.shift[0] / mm, reach_y + a.shift[1] / mm

    # The light's view, for the shadows: a perspective one from the spotlight.
    towards = unit(-a.light_pos)
    e1 = unit(np.cross(towards, (0, 1, 0)))
    e2 = np.cross(e1, towards)
    from_light = (hull - centre) / half - a.light_pos
    spread = 1.05 * max(np.abs(from_light @ e / (from_light @ towards)).max() for e in (e1, e2))

    def light_cell(q):
        r = q - a.light_pos
        along = r @ towards
        col = np.clip(((r @ e1) / along / spread + 1) / 2 * (SHADOW_PX - 1), 0, SHADOW_PX - 1)
        row = np.clip(((r @ e2) / along / spread + 1) / 2 * (SHADOW_PX - 1), 0, SHADOW_PX - 1)
        return np.rint(row).astype(int) * SHADOW_PX + np.rint(col).astype(int), along

    depth = np.full(width_px * height_px, -np.inf)
    pos = np.zeros((width_px * height_px, 3))
    nrm = np.zeros((width_px * height_px, 3))
    tint = np.tile(a.rgb, (width_px * height_px, 1))    # the surface's own colour
    first_lit = np.full(SHADOW_PX * SHADOW_PX, np.inf)  # distance of what the light sees
    for p, n, rgb in surfaces:
        q = (p - centre) / half
        n = np.where(n[:, 2:3] < 0, -n, n)              # seen from either side
        col = np.rint((q[:, 0] + left) * mm * PX).astype(int)
        row = np.rint((q[:, 1] + below) * mm * PX).astype(int)
        inside = (col >= 0) & (col < width_px) & (row >= 0) & (row < height_px)
        if not inside.all():
            sys.exit("--shift puts the %s outside the picture" % what)
        chosen = nearest(depth.size, row * width_px + col, q[:, 2])
        hit = chosen >= 0
        hit[hit] = q[chosen[hit], 2] > depth[hit]
        depth[hit], pos[hit], nrm[hit] = q[chosen[hit], 2], q[chosen[hit]], n[chosen[hit]]
        if rgb is not None:
            tint[hit] = rgb[chosen[hit]]
        cell, along = light_cell(q)
        chosen = nearest(first_lit.size, cell, -along)
        hit = chosen >= 0
        first_lit[hit] = np.minimum(first_lit[hit], along[chosen[hit]])

    shape = (height_px, width_px)
    mask = (depth > -np.inf).reshape(shape)
    pos, nrm = pos.reshape(shape + (3,)), nrm.reshape(shape + (3,))
    a = copy.copy(a)
    a.rgb = tint.reshape(shape + (3,))

    # The floor the object stands on (--floor): wherever the object is not,
    # the view ray meets the plane of its base.
    solid = mask
    if a.floor:
        up = m @ (0, 0, 1.0)
        base = (base - centre) / half
        qx = (np.arange(width_px) / (mm * PX) - left)[None, :] + np.zeros(shape)
        qy = (np.arange(height_px) / (mm * PX) - below)[:, None] + np.zeros(shape)
        qz = base[2] - (up[0] * (qx - base[0]) + up[1] * (qy - base[1])) / up[2]
        pos = np.where(mask[..., None], pos, np.stack([qx, qy, qz], axis=-1))
        nrm = np.where(mask[..., None], nrm, up)
        solid = np.ones(shape, dtype=bool)

    # Shadows: how much nearer the light is the first thing it sees that way?
    # A stray gap in the light's view would let light through: take the
    # nearest of each cell and its neighbours.
    seen = first_lit.reshape(SHADOW_PX, SHADOW_PX)
    padded = np.pad(seen, 1, constant_values=np.inf)
    seen = np.min([padded[i:i + SHADOW_PX, j:j + SHADOW_PX] for i in range(3) for j in range(3)],
                  axis=0)
    cell, along = light_cell(pos.reshape(-1, 3))
    behind = (along - seen.ravel()[cell]).reshape(shape)
    shadow = np.where(solid, smoothstep(SHADOW_NEAR, SHADOW_FAR, behind), 0.0)
    soft = Image.fromarray((shadow * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(PX))
    lit = 1 - a.shadow * np.asarray(soft) / 255.0

    # Aim the spotlight at the surface under --aim.
    ax, ay = triple(a.aim)
    col, row = int(round((ax + left) * mm * PX)), int(round((ay + below) * mm * PX))
    if not (0 <= col < width_px and 0 <= row < height_px and mask[row, col]):
        sys.exit("--aim %g,%g is not on the %s" % (ax, ay, what))
    a.axis = unit(pos[row, col] - a.light_pos)

    img = shade(pos, nrm, a, lit)
    if a.fill:
        # A second, weaker light with no cone and no shadows, to lift the
        # side the spotlight leaves dark.
        x, y, z, strength = (float(v) for v in a.fill.split(","))
        b = copy.copy(a)
        b.light_pos, b.cone, b.ambient = np.array([x, y, z]), (180.0, 181.0), 0.0
        b.diffuse, b.specular = a.diffuse * strength, a.specular * strength
        img = np.minimum(img + shade(pos, nrm, b), 255.0)
    if a.floor:
        # The floor takes the spotlight only: beyond its pool it is black, and
        # is not drawn where it is too dark to show.
        b = copy.copy(a)
        b.rgb, b.ambient, b.specular = np.array(triple(a.floor)), 0.0, 0.0
        ground = shade(pos, nrm, b, lit)
        if a.checker:
            # Tiles: every other square of the floor in a second colour. The
            # squares are laid out in the model's own x and y, so they turn
            # with the object.
            size, r, g, bl = (float(v) for v in a.checker.split(","))
            on_floor = (pos * half + centre) @ m / (size * half)
            other = (np.floor(on_floor[..., 0]) + np.floor(on_floor[..., 1])) % 2 == 1
            b.rgb = np.array([r, g, bl])
            ground = np.where(other[..., None], shade(pos, nrm, b, lit), ground)
        img = np.where(mask[..., None], img, ground)
        mask = mask | (ground.max(axis=-1) >= FLOOR_MIN)
    return np.where(mask[..., None], img, 0.0), mask


def runs(cover):
    """The lit stretches of one row of coverage, as (first, past-the-last)
    pixel pairs: gaps under MIN_MM bridged, then stretches under it dropped."""
    edges = np.flatnonzero(np.diff(np.concatenate([[0], cover.astype(np.int8), [0]])))
    out = []
    for x0, x1 in zip(edges[::2], edges[1::2]):
        if out and x0 - out[-1][1] < MIN_MM * PX:
            out[-1][1] = x1
        else:
            out.append([x0, x1])
    return [(x0, x1) for x0, x1 in out if x1 - x0 >= MIN_MM * PX]


def tone(img, a):
    """The picture as LED drive. The camera's picture is the light itself put
    through the display's tone curve, which lifts the dark shades: undo it
    (--gamma), keeping each colour's hue, and scale by --level."""
    peak = img.max(axis=-1, keepdims=True) / 255.0
    return img * a.level * peak ** (a.gamma - 1)


def untone(rgb, a):
    """A drive colour as the camera will show it, for the preview."""
    v = np.asarray(rgb, dtype=float) / a.level
    peak = max(v.max() / 255.0, 1e-9)
    return tuple(int(round(min(255.0, c * peak ** (1 / a.gamma - 1)))) for c in v)


def led_levels(rgb, carry, last=None):
    """Whole LED levels for one piece of a line, and the error to carry into
    the next piece (last is the piece before's levels): the dark shades are only a few levels, and carrying the
    rounding error along the line (the LED's spot is wider than a piece)
    keeps their average right. Never grey: the carrier would show that on its
    white element."""
    want = rgb + carry
    level = np.clip(np.floor(want + 0.5), 0, 255)
    if level[0] == level[1] == level[2] and level[0] > 0:
        level[np.argmin(want)] -= 1
    # A colour command for every piece starves GRBL of moves, and the pen
    # stalls in bright blobs: keep the last colour while it is within HOLD of
    # the one wanted. The error still carries, so the average stays right.
    if last is not None and (np.abs(level - last) <= np.floor(HOLD * np.asarray(last))).all():
        level = np.asarray(last, dtype=float)
    return tuple(int(v) for v in level), want - level


def sample(img, mask, a):
    """Return [(y_mm, [(x0, x1, (r, g, b)), ...]), ...] in mm from the lower left
    of the render, y up. Pieces coloured (0, 0, 0) are the gaps in a line."""
    height = (img.shape[0] - 1) / PX
    n_lines = int(height // a.pitch) + 1
    y_first = (height - (n_lines - 1) * a.pitch) / 2    # centre the lines
    lines = []
    for i in range(n_lines):
        y = y_first + i * a.pitch
        row = int(round(y * PX))
        band = slice(max(row - 1, 0), row + 2)          # 1 mm of the picture
        weight = mask[band].sum(axis=0)
        colour = (img[band] * mask[band][..., None]).sum(axis=0) / np.maximum(weight, 1)[:, None]
        segs = []
        for x0, x1 in runs(mask[row]):
            if segs:
                segs.append((segs[-1][1], x0 / PX, (0, 0, 0)))
            n_segs = max(1, int(round((x1 - x0) / PX / a.seg)))
            cuts = np.rint(np.linspace(x0, x1, n_segs + 1)).astype(int)
            carry, level = np.zeros(3), None            # dither: see led_levels
            for c0, c1 in zip(cuts[:-1], cuts[1:]):
                lit = weight[c0:c1] > 0
                rgb = colour[c0:c1][lit].mean(axis=0) if lit.any() else np.zeros(3)
                level, carry = led_levels(rgb, carry, level)
                if segs and segs[-1][2] == level and c0 > x0:
                    segs[-1] = (segs[-1][0], c1 / PX, level)    # one move, not two
                else:
                    segs.append((c0 / PX, c1 / PX, level))
        if segs:
            lines.append((y, segs))
    return height, lines


def picture_args(ap, what, width_help):
    """The options of a shaded object in horizontal lines on a floor."""
    ap.add_argument("--width", type=float, help="%s, mm (default: all the run-ups leave "
                    "of the bed)" % width_help)
    ap.add_argument("--tilt", type=float, default=25.0, help="degrees the viewer looks down")
    ap.add_argument("--shadow", type=float, default=1.0, help="depth of the shadows the %s "
                    "casts on itself: 0 none, 1 full" % what)
    ap.add_argument("--floor", help="colour R,G,B of a floor under the %s, lit by the "
                    "spotlight alone: its pool of light, and the %s's shadow in it" % (what, what))
    ap.add_argument("--checker", help="tile the floor: SIZE,R,G,B, squares SIZE half-widths "
                    "of the %s across, every other one in this colour" % what)
    ap.add_argument("--canvas", help="picture size W,H in mm, the %s in its middle "
                    "(default: the %s's own size); room for the floor" % (what, what))
    ap.add_argument("--shift", default="0,0", help="move the %s X,Y mm off the middle "
                    "of the picture (right and up)" % what)
    ap.add_argument("--fill", help="a second light, X,Y,Z,STRENGTH: no cone, no shadows, "
                    "STRENGTH times the spotlight")
    ap.add_argument("--gamma", type=float, default=2.2, help="tone curve of the finished "
                    "picture to undo in the LED drive (1 = drive the shades as they are)")
    ap.add_argument("--level", type=float, default=1.0, help="LED drive scale, 0-1: set the "
                    "exposure here and paint at brightness 100, which keeps every LED level")
    common_args(ap, what, "half-widths of the %s" % what)


def make(a, what, title, render_it, script):
    """Render (render_it(a) -> img, mask), then write the plot and its preview."""
    run = run_up(a)
    room = PLOT_X - 2 * (run + MARGIN)
    if a.width is None:
        a.width = float(int(room))
    if a.width > room:
        sys.exit("no room for the %.0f mm run-up each side at %d mm/min: the %s can be "
                 "%.0f mm wide at most, or lower --speed" % (run, a.speed, what, room))

    a.canvas = triple(a.canvas) if a.canvas else None
    a.shift = triple(a.shift)
    img, mask = render_it(a)
    height, lines = sample(tone(img, a), mask, a)
    across = (img.shape[1] - 1) / PX                    # the picture's width, mm
    cx, cy = triple(a.centre) if a.centre else (PLOT_X / 2, PLOT_Y / 2)
    origin = (cx - across / 2, cy - height / 2)
    if (origin[0] - run < 0 or origin[0] + across + run > PLOT_X
            or origin[1] < 0 or origin[1] + height > PLOT_Y):
        sys.exit("a %g x %.0f mm picture at %g,%g, with its run-ups, does not fit the %g x %g mm bed"
                 % (across, height, cx, cy, PLOT_X, PLOT_Y))

    cmdline = " ".join([script] + sys.argv[1:])
    write_plot(OD.plot(a.out + ".plot"), cmdline, a, lines, origin,
               "%s, %g x %.0f mm, centre %g,%g" % (title, a.width, height, cx, cy))
    out = OD.preview(a.out + "_preview.png")
    preview(height, across, [(y, [(x0, x1, untone(rgb, a)) for x0, x1, rgb in segs])
                              for y, segs in lines], a.stroke, out)
    lit = sum(x1 - x0 for _, segs in lines for x0, x1, rgb in segs if rgb != (0, 0, 0))
    travel = sum(segs[-1][1] - segs[0][0] + 2 * run for _, segs in lines)
    secs = travel / (a.speed / 60.0) + len(lines) * (a.speed / 60.0) / ACCEL
    print("%s.plot: %.1f m of lit line at %d mm/min, about %.0f s drawing; a colour step every %.0f ms"
          % (a.out, lit / 1000, a.speed, secs, a.seg / a.speed * 60000))
    print("%s: %s %g x %.0f mm, %d lines at %g mm" % (out, what, a.width, height, len(lines), a.pitch))


def main():
    ap = argparse.ArgumentParser(description="The Utah teapot, Phong-shaded, in horizontal "
                                 "lines; preview the plot.")
    ap.add_argument("--yaw", type=float, default=-20.0, help="degrees turned about the vertical "
                    "(negative brings the spout towards the viewer)")
    picture_args(ap, "teapot", "teapot width, handle to spout")
    # The sphere's cone would leave the spout in the dark: wider, aimed at the body.
    ap.set_defaults(aim="-0.2,0.15", cone="10,17")
    make(parse_common(ap), "teapot", "Utah teapot", render, "teapot.py")


if __name__ == "__main__":
    main()
