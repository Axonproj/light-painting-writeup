#!/usr/bin/env python3
#
# picmeta.py — how a light-paint picture was made, carried with the picture.
#
# plotter.pl writes a note beside every frame it shoots: FRAME.txt next to
# FRAME.jpeg, lines of `key: value` (plot, brightness, speed, peak, energy
# density, exposure ...). The scripts that make pictures from a frame put that
# note inside them, so it travels with the file:
#   PNG   a text chunk, keyword "Description"
#   TIFF  the ImageDescription tag
#   JPEG  the EXIF ImageDescription, in the camera's frame itself; the picture
#         data is not touched (nothing is re-compressed)
# and add what they did themselves (black and white points, the map used).
#
#   ./picmeta.py FILE ...        print the note in each picture or beside each frame
#   ./picmeta.py exif FRAME.jpeg the camera's aperture and ISO, as a note line
#   ./picmeta.py jpeg FRAME.jpeg put FRAME.txt into the frame's EXIF

import os
import struct
import sys

from PIL import Image
from PIL.PngImagePlugin import PngInfo

KEY = 'Description'
TIFF_DESCRIPTION = 270


def sidecar(stem):
    """The note written beside the frame `stem` (a path without extension), or ''."""
    try:
        with open(stem + '.txt') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def embedded(path):
    """The note inside a PNG, TIFF or JPEG, or ''."""
    try:
        with Image.open(path) as im:
            if im.format == 'TIFF':
                text = im.tag_v2.get(TIFF_DESCRIPTION, '')
            elif im.format == 'JPEG':
                text = im.getexif().get(TIFF_DESCRIPTION, '')
            else:
                text = im.info.get(KEY, '')
    except OSError:
        return ''
    return text.strip() if isinstance(text, str) else ''


def add(text, *lines):
    """The note with more lines on the end."""
    return '\n'.join([text] * bool(text) + [l for l in lines if l])


def ascii_only(text):
    return text.encode('ascii', 'replace').decode('ascii')


def pnginfo(text):
    """For Image.save(path, pnginfo=...); None when there is nothing to say."""
    if not text:
        return None
    info = PngInfo()
    info.add_text(KEY, ascii_only(text))
    return info


def exif_line(path):
    """'camera: f/8, ISO 80' from a frame's EXIF, or ''. The exposure time is
    left out: the camera records at most 15 s."""
    try:
        with Image.open(path) as im:
            ex = im.getexif().get_ifd(0x8769)
    except OSError:
        return ''
    parts = []
    if ex.get(33437):
        parts.append('f/%g' % float(ex[33437]))
    if ex.get(34855):
        iso = ex[34855]
        parts.append('ISO %d' % (iso[0] if isinstance(iso, tuple) else iso))
    return 'camera: ' + ', '.join(parts) if parts else ''


def set_jpeg_note(path, text):
    """Put `text` in a JPEG's EXIF ImageDescription, replacing any there. Only
    the EXIF block is rewritten: a new first directory with the description is
    added at its end, so nothing the camera wrote moves (its maker notes hold
    positions within the block). Doing it again replaces the directory added
    before. Returns '' if done, else why not."""
    with open(path, 'rb') as fh:
        d = fh.read()
    if d[:2] != b'\xff\xd8':
        return 'not a JPEG'
    i = 2
    while i + 4 <= len(d) and d[i] == 0xFF and d[i + 1] != 0xDA:
        size = struct.unpack('>H', d[i + 2:i + 4])[0]
        if d[i + 1] == 0xE1 and d[i + 4:i + 10] == b'Exif\0\0':
            break
        i += 2 + size
    else:
        return 'no EXIF block'
    t = d[i + 10:i + 2 + size]                 # the TIFF structure inside it
    e = {b'II': '<', b'MM': '>'}.get(t[:2])
    if not e:
        return 'EXIF block not understood'
    at = struct.unpack(e + 'I', t[4:8])[0]
    n = struct.unpack(e + 'H', t[at:at + 2])[0]
    entries = [t[at + 2 + 12 * k:at + 14 + 12 * k] for k in range(n)]
    after = t[at + 2 + 12 * n:at + 6 + 12 * n]     # the next directory (the thumbnail's)
    for en in entries:
        tag, kind, count, where = struct.unpack(e + 'HHII', en)
        if (tag == TIFF_DESCRIPTION and where == at + 6 + 12 * n
                and where + count + count % 2 == len(t)):
            t = t[:at]                         # the directory this added last time
    entries = [en for en in entries if struct.unpack(e + 'H', en[:2])[0] != TIFF_DESCRIPTION]
    desc = ascii_only(text).encode('ascii') + b'\0'
    t += b'\0' * (len(t) % 2)
    at = len(t)
    entries.append(struct.pack(e + 'HHII', TIFF_DESCRIPTION, 2, len(desc), at + 6 + 12 * (len(entries) + 1)))
    entries.sort(key=lambda en: struct.unpack(e + 'H', en[:2])[0])
    t = (t[:4] + struct.pack(e + 'I', at) + t[8:] + struct.pack(e + 'H', len(entries))
         + b''.join(entries) + after + desc + b'\0' * (len(desc) % 2))
    if len(t) + 8 > 0xFFFF:
        return 'the note does not fit in the EXIF block'
    out = d[:i + 2] + struct.pack('>H', len(t) + 8) + b'Exif\0\0' + t + d[i + 2 + size:]
    when = os.stat(path)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as fh:
        fh.write(out)
    os.replace(tmp, path)
    os.utime(path, (when.st_atime, when.st_mtime))
    return ''


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit('usage: picmeta.py FILE ... | picmeta.py exif|jpeg FRAME.jpeg')
    if args[0] == 'exif':
        print(exif_line(args[1]))
        return
    if args[0] == 'jpeg':
        text = sidecar(os.path.splitext(args[1])[0])
        why = set_jpeg_note(args[1], text) if text else 'no note beside it'
        if why:
            sys.exit(f"picmeta: {args[1]}: {why}")
        return
    for path in args:
        stem, ext = os.path.splitext(path)
        text = embedded(path) or (sidecar(stem) if ext.lower() in ('.jpg', '.jpeg') else '')
        if len(args) > 1:
            print(path + ':')
        print(text if text else '(no note)')
        if len(args) > 1:
            print()


if __name__ == '__main__':
    main()
