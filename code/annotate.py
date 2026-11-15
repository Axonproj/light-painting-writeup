#!/usr/bin/env python3
#
# annotate.py — lettering and line graphics on a finished light-paint picture,
# from lines in the plot file that the plotter skips.
#
# A picture corrected into plot space (..._plot.png) has the bed's millimetres
# for its coordinates, so a label given in millimetres lands where a drawn one
# would have, without the plotter spending time drawing it in light.
#
# In the plot file, behind `#@` (a comment to plotter.pl), the plot's own
# commands, in the same millimetres:
#
#   #@ colour 200 200 200        colour of what follows (default 200 200 200)
#   #@ width 0.3                 line width in mm (default 0.25)
#   #@ text 12 18.5 7 "0.031"    text at X Y, cap height 7 mm (10 if left out)
#   #@ moveto 50 22
#   #@ drawto 54 22              a line; more drawto's carry it on
#
# Text may hold fields from the frame's note (see picmeta.py), in braces:
# "ED {ed}  brightness {brightness}  {shot}". A field's value is the note's,
# less anything in brackets after it; one the note lacks is left as written.
#
#   ./annotate.py PICTURE_plot.png FILE.plot      -> PICTURE_plot_notes.png
#
# The picture itself is not changed. paint does this when the plot has `#@`
# lines.

import argparse
import os
import re
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cal_colour as CC                      # plot space: BED, PPM, MARGIN
import picmeta

FONTS = ['/System/Library/Fonts/Helvetica.ttc',
         '/System/Library/Fonts/Supplemental/Arial.ttf',
         '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
DEFAULT_COLOUR = (200, 200, 200)
DEFAULT_WIDTH_MM = 0.25
DEFAULT_TEXT_MM = 10.0     # plotter.pl's `text X Y "str"`


def commands(plot_file):
    """The `#@` lines of a plot file, as (line number, command)."""
    out = []
    with open(plot_file) as fh:
        for n, line in enumerate(fh, 1):
            m = re.match(r'\s*#@\s*(\S.*?)\s*$', line)
            if m:
                out.append((n, m.group(1)))
    return out


def note_fields(note):
    """The note's `key: value` lines as a dict, values less a trailing (...)."""
    fields = {}
    for line in note.splitlines():
        key, sep, value = line.partition(':')
        if sep:
            fields[key.strip().lower()] = re.sub(r'\s*\(.*\)\s*$', '', value.strip())
    return fields


def font_for(cap_px):
    """A font whose capitals are cap_px high."""
    for path in FONTS:
        if os.path.exists(path):
            probe = ImageFont.truetype(path, 100)
            box = probe.getbbox('H')
            return ImageFont.truetype(path, max(1, round(100 * cap_px / (box[3] - box[1]))))
    sys.exit("annotate: no font found (looked for %s)" % ', '.join(FONTS))


def annotate(img, cmds, fields, ppm, margin):
    """Draw the commands on the picture. Returns how many texts and lines."""
    draw = ImageDraw.Draw(img)
    px = lambda x, y: ((x + margin) * ppm, (CC.BED[1] + margin - y) * ppm)
    colour, width, at = DEFAULT_COLOUR, DEFAULT_WIDTH_MM, None
    texts = lines = 0
    num = r'([-+]?\d*\.?\d+)'
    for n, c in cmds:
        m = re.match(r'colou?r\s+(\d+)\s+(\d+)\s+(\d+)$', c, re.I)
        if m:
            colour = tuple(min(255, int(v)) for v in m.groups())
            continue
        m = re.match(r'width\s+%s$' % num, c, re.I)
        if m:
            width = float(m.group(1))
            continue
        m = re.match(r'moveto\s+%s\s+%s$' % (num, num), c, re.I)
        if m:
            at = (float(m.group(1)), float(m.group(2)))
            continue
        m = re.match(r'drawto\s+%s\s+%s$' % (num, num), c, re.I)
        if m:
            to = (float(m.group(1)), float(m.group(2)))
            if at is None:
                sys.exit("annotate: line %d: drawto before any moveto" % n)
            draw.line([px(*at), px(*to)], fill=colour, width=max(1, round(width * ppm)))
            at = to
            lines += 1
            continue
        m = re.match(r'text\s+%s\s+%s(?:\s+%s)?\s+"(.*)"$' % (num, num, num), c, re.I)
        if m:
            x, y = float(m.group(1)), float(m.group(2))
            h = float(m.group(3)) if m.group(3) else DEFAULT_TEXT_MM
            said = re.sub(r'\{([^{}]+)\}',
                          lambda f: fields.get(f.group(1).strip().lower(), f.group(0)), m.group(4))
            # X Y is the left end of the baseline, as for the plotter's text.
            draw.text(px(x, y), said, fill=colour, font=font_for(h * ppm), anchor='ls')
            texts += 1
            continue
        sys.exit("annotate: line %d: not understood: #@ %s" % (n, c))
    return texts, lines


def main():
    ap = argparse.ArgumentParser(description="Draw a plot file's #@ lines on its corrected picture.")
    ap.add_argument('picture', help='a picture in plot space (..._plot.png)')
    ap.add_argument('plot', help='the plot file with the #@ lines')
    ap.add_argument('--suffix', default='_notes')
    ap.add_argument('--ppm', type=float, default=CC.PPM)
    ap.add_argument('--margin', type=float, default=CC.MARGIN)
    a = ap.parse_args()

    cmds = commands(a.plot)
    if not cmds:
        sys.exit("annotate: no #@ lines in %s" % a.plot)
    img = Image.open(a.picture).convert('RGB')
    want = (int(round((CC.BED[0] + 2 * a.margin) * a.ppm)), int(round((CC.BED[1] + 2 * a.margin) * a.ppm)))
    if img.size != want:
        sys.exit("annotate: %s is %dx%d, not a plot-space picture (%dx%d): give the ..._plot.png"
                 % ((a.picture,) + img.size + want))
    note = picmeta.embedded(a.picture)
    texts, lines = annotate(img, cmds, note_fields(note), a.ppm, a.margin)
    root, ext = os.path.splitext(a.picture)
    out = root + a.suffix + ext
    note = picmeta.add(note, "annotated: from the #@ lines of %s" % os.path.basename(a.plot))
    img.save(out, pnginfo=picmeta.pnginfo(note))
    print("%s -> %s  (%d text(s), %d line(s))" % (a.picture, out, texts, lines))
    print("RESULT %s" % out)


if __name__ == '__main__':
    main()
