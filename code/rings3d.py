#!/usr/bin/env python3
#
# rings3d.py — the rainbow rings in 3D: the plane of the rings tipped back
# about its horizontal axis so the bottom comes towards the camera, seen in
# perspective, and lit by a spotlight to the right of the rings and towards
# the observer. Writes rings3d.plot and rings3d_preview.png.
#
# Each ring is taken as a glowing tube. A line has one brightness at each
# point, so a point of a ring is shaded as the tube's side that faces the
# camera (the tube normal nearest the view direction): diffuse light from the
# spot and a specular glint where the tube's normal bisects the spot and the
# camera (Blinn-Phong): in the ring's own colour, like coloured metal, or with
# --glint white whitening it, like gloss. Their sum is raised to GAMMA
# (the finished picture's tone curve would otherwise lift the shadows and
# flatten the spot), over a small linear AMBIENT floor. The spot is a
# point light with a soft-edged cone aimed at the centre of the rings, its
# light falling off with the square of the distance (1 at the centre).
#
# The colour changes along each ring (`colour` between `drawto`s), in
# LEVELS steps of each light term so it changes only every few millimetres.
#
# The rings are fitted to the bed. Tipped 60 degrees their tops crowd to half
# their spacing, so there are fewer rings than in rainbow_rings.py, far enough
# apart that the 8 mm APA-106's lines stay separate where they crowd most.
#
# Colours at full drive (the brightest glint); the level from paint:
#   ./paint rings3d.plot -ed=2 -raw -grid=4      UNPLUG THE SOLENOID
#
# Behind the 4-stop ND (2026-11-02), 16x the ED at a lower speed:
#   ./rings3d.py --speed 3000 --ed 32
#   ./paint rings3d.plot -ed=32 -raw -grid=64
#
#   ./rings3d.py --tilt 60 --rings 7 --camera 320

import argparse
import colorsys
import math
import outdirs as OD

BED = (393.0, 262.0)      # mm
MARGIN_MM = 15.0          # keep the figure clear of the limits
FEED = 25000              # mm/min; lower with --speed when the LED is behind an ND filter
SEGMENTS = 180            # per ring: 2 degrees
HUE_LAST = 320.0          # degrees: the innermost ring's hue (0 = red, outermost)
RED = 0.7                 # the red die looks too bright beside the others (as rainbow_rings --red 0.7)
WHITE = (RED, 1.0, 1.0)   # the glint's white: equal drive looks pink on this LED, so red cut as above
CAMERA = 320.0            # outer radii x 100 from the rings' centre to the eye: nearer, stronger perspective
AMBIENT = 0.03            # LED drive in the dark, linear (the picture's tone curve lifts it a lot)
DIFFUSE = 0.75
SPECULAR = 0.25           # DIFFUSE + SPECULAR = 1: full drive at most
GAMMA = 2.0               # light -> LED drive: undoes the picture's tone curve, so the shading looks as modelled
SHINE = 24                # Blinn-Phong exponent: the glint's tightness
CONE = (12.0, 40.0)       # degrees off the spot's axis: full light inside, none outside
LEVELS = 20               # steps of each light term
LINE = 8.0                # mm, the LED's line width (preview, and the spacing check)


def norm(v):
    m = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / m for c in v)


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def perp(v, t):
    """v less its component along the unit vector t."""
    k = dot(v, t)
    return tuple(x - k * y for x, y in zip(v, t))


def smoothstep(e0, e1, x):
    t = max(0.0, min(1.0, (x - e0) / (e1 - e0)))
    return t * t * (3 - 2 * t)


def main():
    ap = argparse.ArgumentParser(description="Rainbow rings tipped back in 3D, lit by a spotlight.")
    ap.add_argument("--rings", type=int, default=7)
    ap.add_argument("--inner", type=float, default=0.12, help="innermost radius, as a fraction of the outermost")
    ap.add_argument("--tilt", type=float, default=60.0, help="degrees the bottom comes towards the camera")
    ap.add_argument("--spot", default="1.6 0 1.4",
                    help='"X Y Z" of the spotlight, in outer radii from the centre (x right, z to the viewer)')
    ap.add_argument("--camera", type=float, default=CAMERA,
                    help="distance from the centre to the eye, outer radii x 100: smaller is stronger perspective")
    ap.add_argument("--speed", type=int, default=FEED,
                    help="mm/min: lower it to reach a higher ED (the 4-stop ND wants ~3000)")
    ap.add_argument("--ed", type=float, default=2.0, help="the -ed to suggest to paint")
    ap.add_argument("--glint", choices=("colour", "white"), default="colour",
                    help="the glint in the ring's own colour (metallic) or whitening it (glossy)")
    ap.add_argument("--red", type=float, default=RED,
                    help="scale on the red channel (0.7 for the A570; the 700D sees red more weakly, so "
                         "yellow went lime at 0.7, 2026-11-05)")
    ap.add_argument("--blue", type=float, default=1.0,
                    help="scale on the blue channel: on the 700D blue records ~3x green and ~2x red at equal "
                         "drive, so its glints capped the exposure and the others never reached full "
                         "(2026-11-05: --blue 0.3 --red 0.67 balances them)")
    ap.add_argument("--gamma", type=float, default=GAMMA,
                    help="light -> LED drive exponent: 2 darkens the mid-tones for the A570's JPEG tone "
                         "curve; the raw route renders linearly, so lower (1.4) brightens the rings' bodies "
                         "against their glints (2026-11-05)")
    ap.add_argument("--specular", type=float, default=SPECULAR,
                    help="the glint's share of full drive (the diffuse light gets the rest): higher makes "
                         "the glint stand out from the lit ring")
    ap.add_argument("--shine", type=float, default=SHINE,
                    help="Blinn-Phong exponent: higher makes the glint smaller and tighter")
    ap.add_argument("--white-power", type=float, default=1.0,
                    help="with --glint white: >1 keeps the white to the glint's peak and its sheen in the "
                         "ring's colour (November's ice-white cores with deep colour round them)")
    ap.add_argument("--levels", type=int, default=LEVELS,
                    help="steps of each light term: 20 changes colour only every few mm; with the LED near "
                         "full drive (behind an ND) 100 shades smoothly (the last step into the dark side "
                         "was abrupt at 20, 2026-11-06)")
    ap.add_argument("--out", default=OD.plot("rings3d.plot"))
    ap.add_argument("--preview", default=OD.preview("rings3d_preview.png"))
    a = ap.parse_args()
    if not 0 < a.specular < 1:
        raise SystemExit("--specular is between 0 and 1")
    specular, diffuse, shine = a.specular, 1.0 - a.specular, a.shine

    R = 100.0                                         # model outer radius; fitted to the bed after
    th = math.radians(a.tilt)
    eye = (0.0, 0.0, a.camera)
    spot = tuple(float(v) * R for v in a.spot.split())
    axis = norm(sub((0.0, 0.0, 0.0), spot))
    cos_in, cos_out = math.cos(math.radians(CONE[0])), math.cos(math.radians(CONE[1]))

    def tip(x, y):
        """A point of the rings' plane, tipped: y < 0 (the bottom) comes to +z."""
        return (x, y * math.cos(th), -y * math.sin(th))

    def project(p):
        f = a.camera / (a.camera - p[2])
        return (p[0] * f, p[1] * f)

    rings = []
    for i in range(a.rings):
        r = R * (1 - (1 - a.inner) * i / (a.rings - 1))
        hue = HUE_LAST * i / (a.rings - 1)
        rgb = colorsys.hsv_to_rgb(hue / 360.0, 1.0, 1.0)
        rgb = (rgb[0] * a.red, rgb[1], rgb[2] * a.blue)
        pts = []
        for k in range(SEGMENTS + 1):
            t = math.pi + 2 * math.pi * k / SEGMENTS          # anticlockwise from the left
            p = tip(r * math.cos(t), r * math.sin(t))
            tan = norm(tip(-math.sin(t), math.cos(t)))
            to_eye = norm(sub(eye, p))
            to_spot = sub(spot, p)
            dist = math.sqrt(dot(to_spot, to_spot))
            L = norm(to_spot)
            n = norm(perp(to_eye, tan))                       # the tube's side facing the camera
            H = norm(tuple(x + y for x, y in zip(L, to_eye)))
            nh = norm(perp(H, tan))                           # its normal nearest the half vector
            cone = smoothstep(cos_out, cos_in, dot(tuple(-c for c in L), axis))
            fall = min(1.0, (math.sqrt(dot(spot, spot)) / dist) ** 2)
            light = cone * fall
            diff = round(max(0.0, dot(n, L)) * light * a.levels) / a.levels
            spec = round(max(0.0, dot(nh, H)) ** shine * light * a.levels) / a.levels
            t = diffuse * diff + specular * spec                    # the light, 0..1
            w = specular * spec / t if t and a.glint == 'white' else 0.0   # how much of it is the white glint
            w *= spec ** (a.white_power - 1)                        # >1: white only at the glint's peak
            level = AMBIENT + (1 - AMBIENT) * t ** a.gamma
            c = tuple(round(255 * level * ((1 - w) * ch + w * wh)) for ch, wh in zip(rgb, (a.red, 1.0, a.blue)))
            pts.append((project(p), c))
        rings.append((r, hue, pts))

    # Fit to the bed.
    xs = [q[0] for _, _, pts in rings for q, _ in pts]
    ys = [q[1] for _, _, pts in rings for q, _ in pts]
    s = min((BED[0] - 2 * MARGIN_MM) / (max(xs) - min(xs)), (BED[1] - 2 * MARGIN_MM) / (max(ys) - min(ys)))
    ox = BED[0] / 2 - s * (max(xs) + min(xs)) / 2
    oy = BED[1] / 2 - s * (max(ys) + min(ys)) / 2
    fit = lambda q: (ox + s * q[0], oy + s * q[1])

    # Where the rings crowd most: the top, straight up from the centre.
    tops = sorted(fit(pts[SEGMENTS * 3 // 4][0])[1] for _, _, pts in rings)
    gap = min(b - c for b, c in zip(tops[1:], tops))

    out = [
        "# %s — generated by rings3d.py, do not edit." % a.out,
        "# %d rainbow rings, their plane tipped %g degrees (bottom towards the camera), in perspective"
        " (eye at %g),"
        % (a.rings, a.tilt, a.camera),
        "# lit by a spotlight to the right and towards the observer (%s outer radii)." % a.spot,
        "# Rings %.1f mm apart where they crowd most (the top); the LED's line is ~%g mm." % (gap, LINE),
        "# Colours at full drive at the brightest glint; the level from paint:",
        "#   ./paint %s -ed=%g -raw" % (a.out, a.ed),
        "# Dynamic brightness on. UNPLUG THE SOLENOID (see `help`).",
        "",
        "home",
        "status          # check WPos:393.000,0.000 before plotting",
        "",
        "lightpen on",
        "speed %d" % a.speed,
        "dynamic on",
    ]
    changes = 0
    for r, hue, pts in rings:
        (x, y), c = pts[0]
        x, y = fit((x, y))
        out += ["", "# radius %.2f (model), hue %.0f" % (r, hue),
                "colour %d %d %d" % c,                    # before the moveto: preloaded on the air move
                "moveto %.2f %.2f" % (x, y)]
        last = c
        for (q, c0), (q1, _) in zip(pts, pts[1:]):
            if c0 != last:                                # the segment's colour is its start point's
                out.append("colour %d %d %d" % c0)
                last = c0
                changes += 1
            x, y = fit(q1)
            if not (MARGIN_MM - 1e-6 <= x <= BED[0] - MARGIN_MM + 1e-6 and
                    MARGIN_MM - 1e-6 <= y <= BED[1] - MARGIN_MM + 1e-6):
                raise SystemExit("leaves the bed at %.1f %.1f" % (x, y))
            out.append("drawto %.2f %.2f" % (x, y))
    out += ["", "dynamic off", "penup", "colour off",
            "home            # park at the home corner when done", ""]
    with open(a.out, "w") as f:
        f.write("\n".join(out))
    n_mm = sum(math.dist(fit(p[0]), fit(q[0])) for _, _, pts in rings for p, q in zip(pts, pts[1:]))
    print("%s: %d rings, %.0f mm, %d colour changes along them, %.1f mm apart at the top"
          % (a.out, a.rings, n_mm, changes, gap))
    if gap < LINE * 1.1:
        print("  WARNING: the tops are closer than the LED's line: fewer --rings or a smaller --tilt")

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    k = 4                                                 # px per mm
    im = Image.new("RGB", (int(BED[0] * k), int(BED[1] * k)), (6, 6, 10))
    dr = ImageDraw.Draw(im)
    P = lambda q: (q[0] * k, (BED[1] - q[1]) * k)
    for _, _, pts in rings:
        for (q, c), (q1, _) in zip(pts, pts[1:]):
            dr.line([P(fit(q)), P(fit(q1))], fill=c, width=int(LINE * k * 0.8))
    im.save(a.preview)
    print(a.preview)


main()
