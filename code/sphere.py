#!/usr/bin/env python3
#
# sphere.py — a Phong-shaded sphere drawn in horizontal lines, as a
# light-paint plot with a preview.
#
# The sphere is seen square on (orthographic, viewer on +z). A spotlight on
# the viewer's side, up and to the left by default, shines on it: its cone
# has a soft edge, so the pool of light fades into the ambient level before
# the sphere's own terminator does. Shading is Phong's, but for a softened
# terminator (--soft): ambient + diffuse in
# the sphere's colour, plus a specular highlight that is white but for a tint
# of the sphere's colour at its centre (see TINT).
#
# Each horizontal line is the sphere's chord at that height, cut into --seg mm
# pieces; a piece takes the mean shade over its own length.
#
# Usage:
#   ./sphere.py                                  # 120 mm radius, 6 mm pitch
#   ./sphere.py --radius 100 --pitch 3 --colour 255,120,30
#   ./sphere.py --light=-2,2,4 --aim=-0.3,0.3 --cone 8,13   # negative values need the =
# Writes NAME.plot and NAME_preview.png (--out NAME, default sphere).
#
# The plot draws the lines top to bottom, alternating direction, changing
# colour mid-stroke. It draws fast (--speed, 17000 mm/min by default), which
# takes three things, all as rate_test.py found them:
#   * Each line runs up to speed and slows down again with the LED dark,
#     outside the sphere, so the pen crosses the sphere at constant speed and
#     the ends of the chords are no brighter than their middles.
#   * GRBL only holds a feed it can stop from within the moves it has queued,
#     so the feed limits how short --seg can be.
#   * The light changes --lag ms after its command: every colour command is
#     moved upstream by that much travel.
# Colours are full range: set the exposure with paint's brightness.

import argparse
import math
import sys

import numpy as np

from photo_hlines import PLOT_X, PLOT_Y, preview
from rate_test import ACCEL, feed_for
import outdirs as OD

SUBSAMPLES = 4                  # shade samples averaged along one piece
TINT = 0.2                      # the weakest channel stays this far below the
                                # strongest, so no shade is ever grey: the
                                # carrier shows grey (R == G == B) on its WHITE
                                # element, not its RGB ones. Wide enough to
                                # survive plotter.pl's brightness scaling down
                                # to 2%.


def unit(v):
    v = np.asarray(v, dtype=float)
    return v / np.linalg.norm(v)


def smoothstep(lo, hi, v):
    t = np.clip((v - lo) / (hi - lo), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def shade(p, n, a, lit=1.0):
    """Phong shade, as float RGB 0-255, of surface points p with unit normals n
    (arrays of x,y,z, +z towards the viewer, in the units of a.light_pos).
    lit scales the spotlight point by point: 0 where something shadows it."""
    to_light = a.light_pos - p
    to_light = to_light / np.linalg.norm(to_light, axis=-1, keepdims=True)
    # Spotlight: full inside the inner cone, nothing outside the outer one.
    off_axis = np.degrees(np.arccos(np.minimum(1.0, (-to_light * a.axis).sum(-1))))
    spot = (1 - smoothstep(a.cone[0], a.cone[1], off_axis)) * lit
    n_l = (n * to_light).sum(-1)
    # Lambert's cosine stops dead at the terminator. A light with some size
    # reaches round it: the cosine is carried --soft past the terminator and
    # squared, so the shade eases down to the ambient level instead.
    diffuse = np.maximum((n_l + a.soft) / (1 + a.soft), 0.0) ** (2 if a.soft else 1)
    mirror_z = 2 * n_l * n[..., 2] - to_light[..., 2]   # light reflected about the normal
    specular = np.where(n_l > 0, np.maximum(mirror_z, 0.0), 0.0) ** a.shininess
    rgb = ((a.ambient + a.diffuse * spot * diffuse)[..., None] * a.rgb
           + (a.specular * spot * specular * 255)[..., None])
    rgb = np.minimum(rgb, 255.0)
    weakest = np.argmin(rgb, axis=-1)[..., None]
    capped = np.minimum(np.take_along_axis(rgb, weakest, -1),
                        (1 - TINT) * rgb.max(-1, keepdims=True))
    np.put_along_axis(rgb, weakest, capped, -1)
    return rgb


def sample(a):
    """Return [(y_mm, [(x0, x1, (r, g, b)), ...]), ...] in mm from the lower left
    of the sphere's bounding square, y up."""
    r = a.radius
    n_lines = int((2 * r - 0.01) // a.pitch) + 1
    y_first = r - (n_lines - 1) * a.pitch / 2           # centre the lines
    lines = []
    for i in range(n_lines):
        y = y_first + i * a.pitch
        half = math.sqrt(max(0.0, r * r - (y - r) ** 2))    # half the chord
        if half < a.seg / 2:
            continue
        n_segs = max(1, int(round(2 * half / a.seg)))
        segs = []
        for k in range(n_segs):
            x0 = r - half + 2 * half * k / n_segs
            x1 = r - half + 2 * half * (k + 1) / n_segs
            xs = (x0 + (x1 - x0) * (np.arange(SUBSAMPLES) + 0.5) / SUBSAMPLES - r) / r
            ys = np.full(SUBSAMPLES, (y - r) / r)
            p = np.stack([xs, ys, np.sqrt(np.maximum(0.0, 1 - xs * xs - ys * ys))], axis=-1)
            mean = shade(p, p, a).mean(axis=0)          # on a unit sphere, point = normal
            segs.append((x0, x1, tuple(int(round(v)) for v in mean)))
        lines.append((y, segs))
    return lines


def run_up(a):
    """Dark travel (mm) at each end of a line: the distance to reach the feed,
    and a tenth more."""
    v = a.speed / 60.0
    return 1.1 * v * v / (2 * ACCEL)


def write_plot(path, cmdline, a, lines, origin, what):
    """Write the plot. A piece coloured (0, 0, 0) is a gap: crossed at speed
    with the LED dark."""
    ox, oy = origin
    run = run_up(a)
    lead = a.speed / 60.0 * a.lag / 1000                # mm the pen travels in the lag
    with open(path, "w") as f:
        f.write("# %s — generated by %s. Do not edit.\n" % (path, cmdline))
        f.write("# %s; %d lines %g mm apart, colour steps of %g mm.\n"
                % (what, len(lines), a.pitch, a.seg))
        f.write("# Dark run-up of %.1f mm at each end of every line; colour commands lead "
                "by %g ms (%.2f mm).\n" % (run, a.lag, lead))
        f.write("\nhome\nstatus          # check WPos:393.000,0.000 before plotting\n")
        f.write("\nlightpen on\nspeed %d\n" % a.speed)
        for n, (y, segs) in enumerate(reversed(lines)):
            if n % 2:                                   # every other line right to left
                segs = [(x1, x0, rgb) for x0, x1, rgb in reversed(segs)]
            f.write("\n# y=%.2f\n" % y)
            way = -1 if n % 2 else 1
            start, end = ox + segs[0][0], ox + segs[-1][1]
            f.write("colour 0 0 0\nmoveto %.2f %.2f\n" % (start - way * run, oy + y))
            f.write("drawto %.2f %.2f\n" % (start - way * lead, oy + y))
            last = None
            for x0, x1, rgb in segs:
                if rgb != last:
                    f.write("colour %d %d %d\n" % rgb)
                    last = rgb
                f.write("drawto %.2f %.2f\n" % (ox + x1 - way * lead, oy + y))
            f.write("colour 0 0 0\ndrawto %.2f %.2f\n" % (end + way * run, oy + y))
        f.write("\npenup\ncolour off\nhome            # park at the home corner when done\n")


def triple(s):
    return tuple(float(v) for v in s.split(","))


def common_args(ap, what, unit_name):
    """The options a shaded object in horizontal lines takes, whatever it is."""
    ap.add_argument("--pitch", type=float, default=6.0, help="line spacing, mm")
    ap.add_argument("--seg", type=float, default=6.0, help="colour step along a line, mm")
    ap.add_argument("--colour", default="40,120,255", help="%s colour R,G,B" % what)
    ap.add_argument("--light", default="-2,2,4", help="spotlight position X,Y,Z in %s "
                    "from the centre (+x right, +y up, +z towards the viewer)" % unit_name)
    ap.add_argument("--aim", default="-0.3,0.3", help="where on the %s's face the spotlight "
                    "points: X,Y in %s" % (what, unit_name))
    ap.add_argument("--cone", default="8,13", help="spotlight cone INNER,OUTER half-angles in "
                    "degrees: full light inside INNER, none outside OUTER")
    ap.add_argument("--soft", type=float, default=0.5, help="how far the light reaches round "
                    "the terminator, as a cosine (0 = Lambert's hard edge)")
    ap.add_argument("--ambient", type=float, default=0.06)
    ap.add_argument("--diffuse", type=float, default=0.85)
    ap.add_argument("--specular", type=float, default=0.9)
    ap.add_argument("--shininess", type=float, default=30.0, help="Phong exponent")
    ap.add_argument("--stroke", type=float, default=1.5, help="preview stroke width, mm")
    ap.add_argument("--speed", type=int, default=17000, help="drawing feed, mm/min")
    ap.add_argument("--lag", type=float, default=6.5,
                    help="colour lag to compensate, ms (0 = none)")
    ap.add_argument("--centre", help="%s centre X,Y in mm (default: middle of the bed)" % what)
    ap.add_argument("--out", default=what, help="output name")


def parse_common(ap):
    a = ap.parse_args()
    a.rgb = np.array(triple(a.colour))
    a.light_pos = np.array(triple(a.light))
    a.cone = triple(a.cone)
    if a.light_pos[2] <= 1:
        sys.exit("the spotlight must be on the viewer's side (Z > 1)")
    if a.speed > feed_for(a.seg):
        sys.exit("GRBL will not hold %d mm/min through %g mm colour steps (limit %d): "
                 "lower --speed or raise --seg" % (a.speed, a.seg, feed_for(a.seg)))
    return a


def main():
    ap = argparse.ArgumentParser(description="A Phong-shaded sphere in horizontal lines; preview the plot.")
    ap.add_argument("--radius", type=float, default=120.0, help="sphere radius, mm")
    common_args(ap, "sphere", "sphere radii")
    a = parse_common(ap)
    ax, ay = triple(a.aim)
    if ax * ax + ay * ay >= 1:
        sys.exit("--aim must be on the sphere's face (inside the unit circle)")
    a.axis = unit(np.array([ax, ay, math.sqrt(1 - ax * ax - ay * ay)]) - a.light_pos)

    cx, cy = triple(a.centre) if a.centre else (PLOT_X / 2, PLOT_Y / 2)
    origin = (cx - a.radius, cy - a.radius)
    if (origin[0] < 0 or origin[1] < 0 or cx + a.radius > PLOT_X or cy + a.radius > PLOT_Y):
        sys.exit("a %g mm sphere at %g,%g does not fit the %g x %g mm bed"
                 % (a.radius, cx, cy, PLOT_X, PLOT_Y))

    run = run_up(a)
    if origin[0] - run < 0 or cx + a.radius + run > PLOT_X:
        sys.exit("no room for the %.0f mm run-up each side at %d mm/min: lower --speed "
                 "or --radius" % (run, a.speed))

    lines = sample(a)
    cmdline = " ".join(["sphere.py"] + sys.argv[1:])
    write_plot(OD.plot(a.out + ".plot"), cmdline, a, lines, origin,
               "Sphere, radius %g mm, centre %g,%g" % (a.radius, cx, cy))
    out = OD.preview(a.out + "_preview.png")
    preview(2 * a.radius, 2 * a.radius, lines, a.stroke, out)
    length = sum(segs[-1][1] - segs[0][0] for _, segs in lines)
    travel = length + len(lines) * 2 * run
    secs = travel / (a.speed / 60.0) + len(lines) * (a.speed / 60.0) / ACCEL
    print("%s.plot: %.1f m of lit line at %d mm/min, about %.0f s drawing; a colour step every %.0f ms"
          % (a.out, length / 1000, a.speed, secs, a.seg / a.speed * 60000))
    print("%s: sphere radius %g mm, %d lines at %g mm" % (out, a.radius, len(lines), a.pitch))


if __name__ == "__main__":
    main()
