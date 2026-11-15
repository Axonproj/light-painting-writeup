#!/usr/bin/env python3
#
# sunset.py — a red sun against a blue sky, drawn in vertical lines of light.
# Writes plots/sunset.plot and previews/sunset_preview.png (--out NAME).
#
# The sky shades from light blue at the top to dark blue at the bottom. The
# sun is a disc that shades the other way, bright red at its bottom to dark
# blue at its top. Every line runs the full height of the picture; on every
# other line the sun is shifted up by --offset mm and the sky's shading by
# --sky-offset mm, so the sun's rim is stepped and the shading alternates,
# while the top and bottom edges stay straight.
#
# Each line is cut into --seg mm pieces; a piece takes the mean colour over
# its own length (the colour changes in steps, as the plotter would draw it).
# The preview draws as the light pen does: a soft line --stroke mm wide
# (Gaussian across), overlapping lines adding up, on black.
#
#   py/sunset.py -open                      # -open shows the preview
#   ./paint sunset.plot -ed=30 -nodry -raw -av=22 -grid=100
#
# The plot: dynamic brightness, the lines drawn alternately up and down, the
# picture centred on the bed. Colours, from the ED wedges of 2026-11-05
# (bare 8 mm APA-106, f/22, ISO 100, 3000 mm/min; led_matrix_apa106_f22.json):
#   * the camera's response grows as ED^1.5 and the picture is shown in sRGB
#     (light^(1/2.2)), so drive = value^GAMMA (2.2/1.5) makes the shading come
#     out as previewed;
#   * red and blue are stronger than green: BALANCE scales them (0.72, 1, 0.68);
#   * then every colour is scaled so the strongest channel in the picture is at
#     full drive, and the exposure is set by paint's -ed.
# All three colours reached the sensor's limit at an ED of about 4.7 in the
# wedges; the ND8 on the lens (since 2026-11-06) moves that to about 38 at
# f/22, so the default --ed 30 leaves the brightest colours just short of it.

#   py/sunset.py --pitch 5 --offset 2.5 --sun-r 45 --sun-y 70

import argparse
import subprocess
import sys

import numpy as np
from PIL import Image

import outdirs as OD

PX = 4                                     # preview pixels per mm
BED_W, BED_H = 393.0, 262.0                # plot area, mm
GAMMA = 2.2 / 1.5                          # colour value -> LED drive: the camera's light grows as
                                           # drive^1.5, and the finished picture shows light^(1/2.2) (sRGB),
                                           # so drive = value^(2.2/1.5) shows the value as previewed.
                                           # 0.67 (rings3d's) made the light linear in the value, which
                                           # lifted every dark colour (sunset_b60_001, 2026-11-11)
BALANCE = (0.72, 1.0, 0.68)                # red, green, blue: equal-looking channels on this LED


def lerp(a, b, t):
    a, b = np.asarray(a, float), np.asarray(b, float)
    return a + (b - a) * np.clip(t, 0, 1)[..., None]


def colour_at(x, y, a, shift=0.0, sky_shift=0.0):
    """Colour (0-255 RGB) of the picture at x, y mm (y up from the bottom), with
    the sun moved up `shift` mm and the sky's shading up `sky_shift` mm. Each is
    held at its edge colour where the shift runs past the picture's top or
    bottom, so every line still spans the full height."""
    ysky = np.clip(y - sky_shift, 0, a.height)
    sky = lerp(a.sky_bottom, a.sky_top, ysky / a.height)
    ys = np.clip(y - shift, 0, a.height)
    dx, dy = x - a.sun_x, ys - a.sun_y
    inside = dx * dx + dy * dy <= a.sun_r * a.sun_r
    t = (ys - (a.sun_y - a.sun_r)) / (2 * a.sun_r)       # 0 at the sun's bottom, 1 at its top
    sun = lerp(a.sun_bottom, a.sun_top, t)
    return np.where(inside[..., None], sun, sky)


def lines(a):
    """[(x, [(y0, y1, rgb), ...]), ...] — each line's pieces, bottom to top."""
    out = []
    n = int(a.width // a.pitch) + 1
    x0 = (a.width - (n - 1) * a.pitch) / 2
    for i in range(n):
        x = x0 + i * a.pitch
        shift = a.offset if i % 2 else 0.0
        sky_shift = a.sky_offset if i % 2 else 0.0
        pieces = []
        y = 0.0
        while y < a.height - 1e-9:
            y1 = min(y + a.seg, a.height)
            ys = np.linspace(y, y1, 8)
            rgb = colour_at(np.full_like(ys, x), ys, a, shift, sky_shift).mean(axis=0)
            pieces.append((y, y1, rgb))
            y = y1
        out.append((x, pieces))
    return out


def preview(a, path):
    margin = 10
    w = int((a.width + 2 * margin) * PX)
    h = int((a.height + 2 * margin) * PX)
    img = np.zeros((h, w, 3))
    sigma = a.stroke / 2.355 * PX                          # stroke = full width at half maximum
    xs = np.arange(w)
    for x, pieces in lines(a):
        cx = (x + margin) * PX
        across = np.exp(-0.5 * ((xs - cx) / sigma) ** 2)  # the line's profile across
        cols = across > 1e-3
        for y0, y1, rgb in pieces:
            r0 = int(round(h - (y1 + margin) * PX))       # image rows run top down
            r1 = int(round(h - (y0 + margin) * PX))
            img[r0:r1, cols] += across[cols][None, :, None] * rgb[None, None, :]
        # soft round ends: a half-disc of fall-off above and below
        for yend, sgn in ((pieces[-1][1], -1), (pieces[0][0], 1)):
            rgb = pieces[-1][2] if sgn < 0 else pieces[0][2]
            r = int(round(h - (yend + margin) * PX))      # the bottom piece stops just above this row
            for k in range(0 if sgn > 0 else 1, int(3 * sigma)):
                rr = r + sgn * k
                if 0 <= rr < h:
                    img[rr, cols] += (across[cols] * np.exp(-0.5 * (k / sigma) ** 2))[:, None] * rgb
    img = np.clip(img, 0, 255).astype(np.uint8)
    Image.fromarray(img).save(path)
    return path


def drive(rgb):
    """Colour value (0-255) -> LED drive before the picture-wide scaling."""
    v = (np.asarray(rgb, float) / 255.0) ** GAMMA
    return v * np.asarray(BALANCE)


def write_plot(a, path, cmdline):
    ls = lines(a)
    top = max(drive(rgb).max() for _, ps in ls for _, _, rgb in ps)
    if a.centre:
        cx, cy = triple(a.centre)[:2]
    else:
        cx, cy = BED_W / 2, BED_H / 2
    ox, oy = cx - a.width / 2, cy - a.height / 2
    if ox < 0 or oy < 0 or ox + a.width > BED_W or oy + a.height > BED_H:
        raise SystemExit("picture leaves the bed (%g x %g mm)" % (BED_W, BED_H))
    out = ["# %s — generated by sunset.py, do not edit." % path.split("/")[-1],
           "# " + cmdline,
           "# %d vertical lines %g mm apart, %g x %g mm, x %.1f-%.1f, y %.1f-%.1f on the bed." % (len(ls), a.pitch, a.width, a.height, ox, ox + a.width, oy, oy + a.height),
           "# Colours at full drive in the strongest channel; the level from paint:",
           "#   ./paint %s -ed=%g -nodry -raw -av=22 -grid=100" % (path.split("/")[-1], a.ed),
           "# Dynamic brightness on. UNPLUG THE SOLENOID (see `help`).",
           "",
           "home",
           "status          # check WPos:393.000,0.000 before plotting",
           "",
           "lightpen on",
           "speed %d" % a.speed,
           "dynamic on",
           ""]
    last = None
    for i, (x, pieces) in enumerate(ls):
        if i % 2:
            pieces = [(y1, y0, rgb) for y0, y1, rgb in reversed(pieces)]   # drawn downwards
        for k, (ya, yb, rgb) in enumerate(pieces):
            c = tuple(int(round(v)) for v in np.clip(drive(rgb) / top * 255, 0, 255))
            if c != last:
                out.append("colour %d %d %d" % c)
                last = c
            if k == 0:
                out.append("moveto %.2f %.2f" % (ox + x, oy + ya))
            out.append("drawto %.2f %.2f" % (ox + x, oy + yb))
    out += ["", "dynamic off", "penup", "colour off", "home            # park at the home corner when done", ""]
    open(path, "w").write("\n".join(out))
    return path, len(ls), sum(1 for l in out if l.startswith("colour "))


def triple(s):
    return tuple(float(v) for v in s.split(','))


def main():
    ap = argparse.ArgumentParser(description="A red sun against a blue sky, in vertical lines: plot and preview.")
    ap.add_argument("--width", type=float, default=240.0, help="picture width, mm")
    ap.add_argument("--height", type=float, default=180.0, help="picture height, mm")
    ap.add_argument("--pitch", type=float, default=6.0, help="line spacing, mm")
    ap.add_argument("--offset", type=float, default=3.0, help="every other line shifted up, mm")
    ap.add_argument("--sky-offset", type=float, help="every other line's sky shading shifted up, mm "
                    "(default: --offset; the sky shades over the full height, so it needs "
                    "more than the sun to show)")
    ap.add_argument("--seg", type=float, default=3.0, help="colour step along a line, mm")
    ap.add_argument("--stroke", type=float, default=5.0, help="preview line width (half maximum), mm")
    ap.add_argument("--sky-top", default="110,180,255", help="sky colour at the top, R,G,B")
    ap.add_argument("--sky-bottom", default="0,15,80", help="sky colour at the bottom, R,G,B")
    ap.add_argument("--sun-bottom", default="255,0,0", help="sun colour at its bottom, R,G,B")
    ap.add_argument("--sun-top", default="0,10,90", help="sun colour at its top, R,G,B")
    ap.add_argument("--sun-r", type=float, default=50.0, help="sun radius, mm")
    ap.add_argument("--sun-x", type=float, help="sun centre from the left, mm (default: middle)")
    ap.add_argument("--sun-y", type=float, default=65.0, help="sun centre from the bottom, mm")
    ap.add_argument("--centre", help="picture centre on the bed X,Y mm (default: the bed's middle; "
                    "the camera sees about x 16-350, so 185,130 fills its view)")
    ap.add_argument("--speed", type=int, default=3000, help="drawing feed, mm/min (the ED wedges' speed)")
    ap.add_argument("--ed", type=float, default=30.0, help="energy density to suggest for paint -ed")
    ap.add_argument("--out", default="sunset", help="output name")
    ap.add_argument("-open", "--open", action="store_true", help="open the preview when it is written")
    a = ap.parse_args()
    for k in ("sky_top", "sky_bottom", "sun_bottom", "sun_top"):
        setattr(a, k, triple(getattr(a, k)))
    if a.sky_offset is None:
        a.sky_offset = a.offset
    if a.sun_x is None:
        a.sun_x = a.width / 2
    plot, n, ncol = write_plot(a, OD.plot(a.out + ".plot"), "py/sunset.py " + " ".join(sys.argv[1:]))
    print("%s: %d lines, %d colours; brightness at -ed=%g: %.0f%%"
          % (plot, n, ncol, a.ed, a.ed * a.speed / 1000))
    path = preview(a, OD.preview(a.out + "_preview.png"))
    print(path)
    if a.open:
        subprocess.run(["open", path])


if __name__ == "__main__":
    main()
