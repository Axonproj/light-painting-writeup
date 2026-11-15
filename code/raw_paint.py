#!/usr/bin/env python3
#
# raw_paint.py — the processing half of `paint -raw`: dark-frame subtraction
# and calibration done on the camera's raw data instead of its JPEGs: the
# A570's DNG (CHDK) or the EOS 700D's CR2.
#
# Why: the A570's JPEG processing clips stroke centres to white (100% of stroke
# pixels had a channel at 255 at brightness 3) while the sensor itself had
# almost all of them in range (0.6% of photosites at its ceiling); its tone
# curve also crushes the faint wall halos to black, and its "linear" values are
# only approximately undone. The raw data is linear: subtraction is exact, the
# colour survives to the stroke centre, and the halos are kept.
#
# usage: raw_paint.py LIT DARK [GRID] [--keep 99.9] [--white sensor] [--tiff]
#   stems without extension (each needs STEM.dng or STEM.cr2). Writes:
#     LIT_rawsub.png       lit - dark, rendered (display-ready, camera framing)
#     GRID_rawgrid.png     the grid frame developed for calmap (if GRID given)
#     LIT_rawsub_plot.png  corrected into plot space with cal_map_raw.json
#     LIT_rawsub_plot.tif  the same picture in 16 bits, for printing (--tiff)
#   --keep P: the brightest (100-P)% of the subtracted image renders as white.
#   --white sensor: white is the sensor's ceiling instead, whatever the frame
#     holds. The default stretches a dim frame to look as bright as a strong
#     one; this does not, so two frames (calibration charts) can be compared.
#   --colour p3: develop into Display P3 instead of sRGB (the Mac's screen):
#     the LED's violet, green and red lie outside sRGB, which clamps them —
#     a violet stroke's green goes to 0 and a white glint in it starts with a
#     hard edge. P3 holds more of them. The profile is embedded in the PNGs and
#     the TIFF; a viewer that ignores profiles shows the colours too pale.
#   --tiff: the PNG has 256 levels per channel, which can band in the dark
#     fall-off of a glow when printed large. The TIFF is rendered from the same
#     linear data with the same black and white points, so it is the PNG's
#     picture with 65536 levels: uncompressed, sRGB (profile embedded), and
#     tagged 80 px/cm, which prints the plot at its real size.
#
# Raw frames are framed slightly differently from the JPEGs (more of the
# sensor), so the map is fitted on a raw-developed grid: cal_map_raw.json,
# separate from the JPEG route's cal_map.json.

import argparse
import json
import os
import struct
import subprocess
import sys

import numpy as np
import rawpy
from numpy.lib.stride_tricks import sliding_window_view
from PIL import Image, ImageCms

import calmap
import cal_colour as CC
import picmeta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # the project folder (the scripts are in py/)
RAW_MAP = os.path.join(ROOT, 'cal_map_raw.json')
DEVELOP_BRIGHT = 0.25     # develop_linear's scale: the sensor's ceiling in its output

# Where each camera's raw frame stands against the saved JPEG. The A570's
# JPEGs are turned 180 by remote_snap; the 700D's by EOS_ROTATE (the same
# variable remote_snap reads, clockwise, default 180). The raw is never turned
# in the camera, so it is turned here to match.
# The 700D's CR2 header says white 15360, but at ISO 100 every channel stops
# at 13583 (fov_b20_001, 2026-11-05: 150k photosites piled there; anything
# above is a hot pixel). Developed against 15360 a clipped stroke would sit at
# 88% and tint; against 13583 it is the ceiling, as on the A570. Shot at
# ISO 100 only (remote_snap's default); another ISO may stop elsewhere.
EOS_SATURATION = 13583


# LibRaw's standard ways of converting photosites at the sensor's ceiling
# (--highlights): 'clip' whitens them, as an in-camera JPEG does (the A570's
# November 'ice white' glints were blue and green clipped this way); 'keep'
# (Ignore) keeps whatever colour the unclipped channels give; 'blend' mixes
# the two for a gradual white-to-colour edge. Default: keep for the 700D,
# clip for the A570.
# --colour: the space the raw is developed into. Display P3 has sRGB's
# white point and tone curve (to_srgb() encodes both); only the primaries differ.
COLOURS = {'srgb': rawpy.ColorSpace.sRGB, 'p3': rawpy.ColorSpace.P3D65}
P3_ICC = '/System/Library/ColorSync/Profiles/Display P3.icc'


def icc_bytes(colour):
    if colour == 'p3':
        with open(P3_ICC, 'rb') as fh:
            return fh.read()
    return ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()


HIGHLIGHTS = {'clip': rawpy.HighlightMode.Clip, 'keep': rawpy.HighlightMode.Ignore,
              'blend': rawpy.HighlightMode.Blend}


def raw_file(stem):
    """(path, kind) of a stem's raw frame: 'dng' (A570) or 'cr2' (700D)."""
    for ext, kind in (('.dng', 'dng'), ('.cr2', 'cr2'), ('.CR2', 'cr2'), ('.DNG', 'dng')):
        if os.path.exists(stem + ext):
            return stem + ext, kind
    sys.exit(f"raw_paint: no {stem}.dng or {stem}.cr2")


def rotation(kind):
    """Clockwise degrees to turn this kind of raw frame to its JPEG."""
    if kind == 'dng':
        return 180
    deg = int(os.environ.get('EOS_ROTATE', '180'))
    if deg not in (0, 90, 180, 270):
        sys.exit(f"raw_paint: EOS_ROTATE={deg}: 0, 90, 180 or 270")
    return deg


def saturation(r, kind):
    """The raw value at which the sensor really stops."""
    return EOS_SATURATION if kind == 'cr2' else r.white_level


def median5(a):
    """5x5 median, in strips to bound memory."""
    p = np.pad(a, 2, mode='edge')
    out = np.empty_like(a)
    for r in range(0, a.shape[0], 256):
        w = sliding_window_view(p[r:r + 260], (5, 5))
        out[r:r + 256] = np.median(w.reshape(w.shape[0], w.shape[1], 25), axis=2)
    return out


def same_colour_median(raw):
    """Each photosite's 5x5 median over its own Bayer colour (neighbours 2 apart)."""
    f = raw.astype(np.float32)
    med = np.empty_like(f)
    for dy in (0, 1):
        for dx in (0, 1):
            med[dy::2, dx::2] = median5(f[dy::2, dx::2])
    return f, med


def exposure(stem):
    """(seconds, ISO) of a stem's raw frame, from its EXIF; None for either
    that cannot be trusted (the A570's DNGs cap the time at 15 s)."""
    path, kind = raw_file(stem)
    try:
        out = subprocess.run(['exiftool', '-s3', '-n', '-ExposureTime', '-ISO', path],
                             capture_output=True, text=True, timeout=20).stdout.split()
        t, iso = float(out[0]), float(out[1])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None, None
    return (t if kind == 'cr2' or t < 15 else None), iso


def dark_scale(lit, dark, given):
    """How much of the dark frame to subtract. A dark frame from the library
    (`paint -darklib`) may be longer than the lit one: the sensor's own glow
    and dark current grow with time (LibRaw has already taken off the fixed
    black level), so it is scaled by lit time / dark time. 1 when the two
    match within 3%, or when the times are not known. Returns (scale, words)."""
    if given != 'auto':
        k = float(given)
        return k, f"scaled x{k:.3f} (given)"
    tl, il = exposure(lit)
    td, id_ = exposure(dark)
    if il and id_ and abs(il - id_) > 0.01 * il:
        print(f"raw_paint: WARNING — the dark frame is ISO {id_:g}, the lit one ISO {il:g}")
    if not tl or not td:
        return 1.0, "times unknown, not scaled"
    k = tl / td
    if abs(k - 1) < 0.03:
        return 1.0, f"{td:g} s, as the lit frame"
    return k, f"{td:g} s, scaled x{k:.3f} to the lit frame's {tl:g} s"


def hot_pixels(stem, sigmas=15):
    """Hot photosites, found in the DARK frame: far above their same-colour
    neighbours (sigmas x the frame's grain). On a bright stroke the lit frame's
    photosite is already at the ceiling, so subtracting the dark frame's hot
    excess left a hole; repairing them in both frames stops that."""
    with rawpy.imread(raw_file(stem)[0]) as r:
        f, med = same_colour_median(r.raw_image)
    dev = f - med
    grain = 1.4826 * np.median(np.abs(dev))
    return dev > sigmas * grain


def clipped_channels(r, sat):
    """Per pixel of the visible frame and per colour (R, G, B): True where a
    photosite of that colour in the pixel's 2x2 Bayer cell is at the sensor's
    ceiling. Returns an HxWx3 bool array."""
    raw = r.raw_image_visible
    cols = r.raw_colors_visible            # 0 R, 1 G, 2 B, 3 G2
    clip = raw >= sat - 2
    H, W = raw.shape
    h, w = H // 2 * 2, W // 2 * 2
    full = np.zeros((H, W, 3), bool)
    for c, sel in enumerate((cols == 0, (cols == 1) | (cols == 3), cols == 2)):
        cell = (clip & sel)[:h, :w].reshape(h // 2, 2, w // 2, 2).any(axis=(1, 3))
        full[:h, :w, c] = np.repeat(np.repeat(cell, 2, axis=0), 2, axis=1)
    return full


def develop_linear(stem, hot=None, highlights=None, colour='srgb'):
    """Linear RGB, 0..1-ish, camera white balance, sRGB primaries, no tone
    curve, turned like remote_snap's JPEGs (see rotation()). Developed at 1/4
    so the output never clips; the scale cancels in the rendering. Photosites
    in `hot` are replaced by their same-colour median before demosaicing."""
    path, kind = raw_file(stem)
    with rawpy.imread(path) as r:
        sat = saturation(r, kind)
        raw = r.raw_image_visible
        at_ceiling = float(np.mean(raw >= sat - 2))
        ignore = (HIGHLIGHTS[highlights] if highlights else
                  rawpy.HighlightMode.Ignore if kind == 'cr2'
                  else rawpy.HighlightMode.Clip) == rawpy.HighlightMode.Ignore
        clipped = clipped_channels(r, sat) if ignore else None
        if hot is not None:
            full = r.raw_image                # writable; postprocess reads it
            _, med = same_colour_median(full)
            full[hot] = med[hot].astype(full.dtype)
        rgb = r.postprocess(gamma=(1, 1), no_auto_bright=True, bright=DEVELOP_BRIGHT,
                            output_bps=16, use_camera_wb=True,
                            output_color=COLOURS[colour],
                            # Clip cuts every channel at the sensor's ceiling AFTER white
                            # balance: on the 700D (blue x1.55, red x2.1) a blue glint at 90%
                            # of the ceiling became 140% and was cut flat — 17k pixels in
                            # rings3d_b13p5_001 with nothing clipped on the sensor (2026-11-05):
                            # pale, hard-edged highlights. Ignore keeps them; the 1/4 develop
                            # scale leaves room. The A570 keeps Clip (its pictures unchanged).
                            highlight_mode=(rawpy.HighlightMode.Ignore if ignore else
                                            HIGHLIGHTS[highlights] if highlights else
                                            rawpy.HighlightMode.Clip), user_flip=0,
                            user_sat=sat,
                            # LibRaw otherwise lowers the white to a frame's own
                            # brightest value when that is near it: a dark frame
                            # and a lit frame would then be scaled differently.
                            adjust_maximum_thr=0.0 if kind == 'cr2' else 0.75)
    rgb = rgb.astype(np.float32) / 65535.0
    # Ignore has a catch: a clipped channel reads low, its true value cut off,
    # while the others are still real and white balance scales them up (red
    # x2.1, blue x1.55). A green core blown far past the ceiling then rendered
    # PINK: green cut flat, red and blue (the green LED's leak into their
    # filters) still climbing and lifted above it (unexpected_error without
    # the ND, 2026-11-12: R,G,B 255,126,223). A clipped channel is at least as
    # bright as the brightest channel beside it, so it is raised to that: a
    # core whose leak stays low keeps its green, one whose leak climbs goes
    # white, as the light really did. Pixels with nothing clipped are untouched.
    if clipped is not None and clipped.any():
        if clipped.shape == rgb.shape:
            top = rgb.max(axis=2, keepdims=True)
            rgb = np.where(clipped, np.maximum(rgb, top), rgb)
        else:
            print(f"raw_paint: clipped-channel repair skipped, raw {clipped.shape[:2]} vs "
                  f"developed {rgb.shape[:2]}")
    rgb = np.rot90(rgb, -rotation(kind) // 90)     # rot90 turns anticlockwise
    return np.ascontiguousarray(rgb), at_ceiling


def to_srgb(x):
    """Linear 0..1 -> sRGB-encoded 0..1."""
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def to_srgb8(x):
    return (to_srgb(x) * 255 + 0.5).astype(np.uint8)


def write_tiff16(path, rgb, px_per_cm, note='', colour='srgb'):
    """Write an HxWx3 uint16 array as an uncompressed 16-bit RGB TIFF with an
    sRGB (or Display P3) profile, and `note` as its ImageDescription. Written
    by hand: PIL cannot save 16-bit RGB."""
    h, w = rgb.shape[:2]
    icc = icc_bytes(colour)
    icc += b'\0' * (len(icc) % 2)
    desc = picmeta.ascii_only(note).encode('ascii') + b'\0' if note else b''
    desc_len = len(desc)
    desc += b'\0' * (len(desc) % 2)
    n_tags = 15 if desc else 14
    extra = 8 + 2 + 12 * n_tags + 4           # where the out-of-line values start
    bits_at, xres_at, yres_at, icc_at = extra, extra + 6, extra + 14, extra + 22
    desc_at = icc_at + len(icc)
    data_at = desc_at + len(desc)
    ASCII, SHORT, LONG, RATIONAL, UNDEFINED = 2, 3, 4, 5, 7
    tags = [(256, LONG, 1, w), (257, LONG, 1, h),
            (258, SHORT, 3, bits_at),         # BitsPerSample 16,16,16
            (259, SHORT, 1, 1),               # no compression
            (262, SHORT, 1, 2),               # RGB
            (273, LONG, 1, data_at),          # one strip
            (277, SHORT, 1, 3),
            (278, LONG, 1, h),
            (279, LONG, 1, w * h * 6),
            (282, RATIONAL, 1, xres_at), (283, RATIONAL, 1, yres_at),
            (284, SHORT, 1, 1),               # chunky
            (296, SHORT, 1, 3),               # resolution per centimetre
            (34675, UNDEFINED, len(icc), icc_at)]
    if desc:
        tags.insert(5, (270, ASCII, desc_len, desc_at))   # tags go in number order
    assert len(tags) == n_tags
    res = struct.pack('<II', int(round(px_per_cm * 1000)), 1000)
    with open(path, 'wb') as f:
        f.write(struct.pack('<2sHI', b'II', 42, 8))
        f.write(struct.pack('<H', n_tags))
        for tag, kind, count, value in tags:
            f.write(struct.pack('<HHI', tag, kind, count))
            f.write(struct.pack('<HH', value, 0) if kind == SHORT and count == 1
                    else struct.pack('<I', value))
        f.write(struct.pack('<I', 0))
        f.write(struct.pack('<HHH', 16, 16, 16) + res + res + icc + desc)
        f.write(np.ascontiguousarray(rgb, dtype='<u2').tobytes())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('lit')
    ap.add_argument('dark')
    ap.add_argument('grid', nargs='?')
    ap.add_argument('--keep', type=float, default=99.9,
                    help='percentile of the subtracted image that renders as white')
    ap.add_argument('--white', default='auto',
                    help="white point: 'auto' (the --keep percentile of this frame), 'sensor' "
                         "(the sensor's ceiling, the same for every frame: for charts to be compared) "
                         "or a number in the subtracted image's units (an animation's frames: one "
                         "white for all, so they do not flicker)")
    ap.add_argument('--black', default='auto',
                    help="black point: 'auto' (background level + 2x its grain, from the darkest "
                         "third of the frame) or a number in the subtracted image's units; 0 = none")
    ap.add_argument('--dark-scale', default='auto',
                    help="share of the dark frame to subtract: 'auto' (lit time / dark time, from "
                         "the EXIF, so a longer dark frame from the library fits) or a number")
    ap.add_argument('--highlights', choices=sorted(HIGHLIGHTS),
                    help="how photosites at the sensor's ceiling are converted: clip (white), keep "
                         "(their colour), blend (gradual); default keep for CR2, clip for DNG")
    ap.add_argument('--colour', choices=sorted(COLOURS), default='srgb',
                    help='the colour space to develop into: srgb, or p3 (Display P3, the Mac '
                         'screen: holds more of the LED\'s colours; profile embedded)')
    ap.add_argument('--tiff', action='store_true',
                    help='also write LIT_rawsub_plot.tif, the result in 16 bits, for printing')
    a = ap.parse_args()

    hot = hot_pixels(a.dark)
    lit, lit_ceil = develop_linear(a.lit, hot, a.highlights, a.colour)
    dark, _ = develop_linear(a.dark, hot, a.highlights, a.colour)
    print(f"raw: {lit_ceil * 100:.2f}% of the lit frame's photosites at the sensor's ceiling; "
          f"{hot.sum()} hot photosites repaired")
    k_dark, dark_words = dark_scale(a.lit, a.dark, a.dark_scale)
    print(f"dark frame: {os.path.basename(a.dark)}, {dark_words}")
    diff = lit - k_dark * dark
    sub = np.clip(diff, 0, None)
    m = sub.max(2)
    bg = m[m <= np.percentile(m, 33)]
    grain = float(np.std(bg))
    # On the 700D the dark frame can come out a touch brighter than the lit
    # frame's background (fov_b04, 2026-11-05: a third of the frame clipped to
    # exactly 0), so the darkest third has no spread: grain read 3e-6 against
    # a true 2e-4, black fell to 0 and the despeckle took 85% of the frame for
    # lit. Then the same pixels' spread before clipping is the grain. (The
    # A570's background is not clipped like that, and keeps its old reading.)
    if np.mean(m == 0) > 0.05:
        grain = max(grain, float(np.std(diff.max(2)[m <= np.percentile(m, 33)])))
    # Outlier fill: in lit areas only, any channel value well off its 5x5
    # median becomes that median — the dots the hot-pixel repair missed.
    # The background is left alone (that would just smooth the grain).
    lum_med = median5(m)
    lit_area = lum_med > np.median(bg) + 5 * grain
    filled = 0
    for c in range(3):
        ch = sub[..., c]
        med = median5(ch)
        bad = lit_area & (np.abs(ch - med) > 0.3 * np.maximum(med, 0.2 * lum_med) + 4 * grain)
        sub[..., c] = np.where(bad, med, ch)
        filled += int(bad.sum())
    print(f"despeckle: {filled} values in lit areas replaced by their neighbourhood "
          f"({filled / m.size * 100:.2f}%)")
    m = sub.max(2)
    # Black point: the raw route keeps the faint tones the JPEG crushed, grain
    # included. Set black just above the background's grain so the surround is
    # inky again, while the wall halos, which sit above it, survive.
    if a.black == 'auto':
        black = float(np.median(bg) + 2 * grain)
    else:
        black = float(a.black)
    if a.white == 'sensor':
        white = DEVELOP_BRIGHT
        how = f"render: black point {black:.5f}, white at the sensor's ceiling {white:.5f}"
    elif a.white != 'auto':
        white = float(a.white)
        how = f"render: black point {black:.5f}, white given {white:.5f}"
    else:
        white = float(np.percentile(m, a.keep))
        how = f"render: black point {black:.5f}, white at the {a.keep:g}th percentile {white:.5f}"
    print(how)
    # The frame's note (LIT.txt, from plotter.pl) goes into the pictures, with
    # what was done here.
    note = picmeta.add(picmeta.sidecar(a.lit),
                       'dark frame: ' + os.path.basename(a.dark) + ', ' + dark_words,
                       f"raw: {lit_ceil * 100:.2f}% of the lit frame's photosites at the sensor's ceiling",
                       how)
    if a.colour != 'srgb':
        note = picmeta.add(note, 'colour: Display P3 (profile embedded)')
    out = a.lit + '_rawsub.png'
    rendered = np.clip(sub - black, 0, None) / max(white - black, 1e-9)
    Image.fromarray(to_srgb8(rendered)).save(out, pnginfo=picmeta.pnginfo(note),
                                             icc_profile=icc_bytes(a.colour))
    print(f"wrote {out}")

    if a.grid:
        # The grid frame carries the sensor's glow and dark-current haze, which
        # the camera's JPEG crushes to black but raw keeps; calmap's line
        # tracker follows it at the frame edges (held-out 4.8 px vs 2.3 px for
        # the JPEG). Subtracting the dark frame removes it (2.5 px).
        g, _ = develop_linear(a.grid, hot)
        k_grid, _ = dark_scale(a.grid, a.dark, 'auto')
        g = np.clip(g - k_grid * dark, 0, None)
        gpath = a.grid + '_rawgrid.png'
        Image.fromarray(to_srgb8(g / max(np.percentile(g.max(2), 99.5), 1e-9))).save(gpath)
        print(f"wrote {gpath}; fitting {os.path.basename(RAW_MAP)}")
        if subprocess.call([os.path.join(HERE, 'calmap.py'), 'fit', gpath, '--out', RAW_MAP, '--check']) != 0:
            sys.exit("raw_paint: calibration fit failed")
    if not os.path.exists(RAW_MAP):
        sys.exit("raw_paint: no cal_map_raw.json yet — run once with a GRID frame")
    if subprocess.call([os.path.join(HERE, 'calmap.py'), 'apply', out, '--map', RAW_MAP]) != 0:
        sys.exit("raw_paint: calmap apply failed")
    if a.tiff:
        with open(RAW_MAP) as fh:
            m = json.load(fh)
        deep, _, _ = calmap.warp_to_plot(to_srgb(rendered).astype(np.float32) * 65535, m,
                                         CC.PPM, CC.MARGIN, np.float32)
        tif = a.lit + '_rawsub_plot.tif'
        write_tiff16(tif, (deep + 0.5).astype(np.uint16), CC.PPM * 10,
                     picmeta.embedded(a.lit + '_rawsub_plot.png'), a.colour)
        print(f"wrote {tif} (16-bit {'Display P3' if a.colour == 'p3' else 'sRGB'})")
    print(f"RESULT {a.lit}_rawsub_plot.png")


if __name__ == '__main__':
    main()
