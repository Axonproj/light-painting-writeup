#!/usr/bin/env python3
"""display_sync.py — pictures for the room's HDMI monitor.

  ./display_sync.py new PICTURE      a new shot: on atelier's monitor (pi/aquarium.py) for 10 minutes
                                     in place of the aquarium, while someone is in the room
  ./display_sync.py selected         the picture selected in Photos, the same way (also ~/bin/show_pic)
  ./display_sync.py album [NAME]     the slideshow: the Photos album (default "Plotter display")

The Pi only copies pictures to its screen, so they go over ready to show:
fitted to the screen, black around them, raw pixels as the framebuffer
takes them (RGB565 or, on atelier since 2026-11-02, 32-bit XRGB8888),
gzipped.

A new shot goes to atelier's RAM (/tmp/display/new.fb.gz): plotter.pl sends
each plotsnap frame and each paint result there by itself. The monitor moved
from plotcam to atelier on 2026-10-23; the album slideshow below is still
plotcam's (pi/plotdisplay.py), which has had no monitor since.

The album is exported from Photos and kept on the Pi's card, on its
read-only layer (unlocked for the copy, locked again after), so the slideshow
survives power cuts. Only pictures added since the last sync are exported
and converted (cache: ~/Library/Caches/plotdisplay); pictures taken out of
the album leave the Pi too. A launchd agent runs this hourly while the Mac
is awake (pi/README.md).
"""

import gzip
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from PIL import Image, ImageOps

HOST       = 'plotcam'                              # the album slideshow
NEW_HOST   = 'atelier'                              # a new shot: the monitor's Pi
ALBUM      = 'Plotter display'
NEW_DIR    = '/tmp/display'
RO_ROOT    = '/media/root-ro'                        # the card under the overlay
SLIDES     = '/home/pi/display/slides'
CACHE      = os.path.expanduser('~/Library/Caches/plotdisplay/slides')


def ssh(cmd, host=HOST, **kw):
    return subprocess.run(['ssh', host, cmd], capture_output=True, text=True, timeout=120, **kw)


def screen_size(host=HOST):
    """The framebuffer's width, height and bits per pixel (16: RGB565, 32: XRGB8888)."""
    r = ssh('cat /sys/class/graphics/fb0/virtual_size /sys/class/graphics/fb0/bits_per_pixel '
            '/sys/class/graphics/fb0/stride', host)
    if r.returncode != 0:
        sys.exit(f'display_sync: cannot reach {host}: {r.stderr.strip()}')
    size, bpp, stride = r.stdout.split()
    w, h = (int(v) for v in size.split(','))
    bpp = int(bpp)
    if bpp not in (16, 32):
        sys.exit(f'display_sync: the Pi framebuffer is {bpp} bits per pixel, expected 16 or 32')
    if int(stride) != w * bpp // 8:
        sys.exit(f'display_sync: the Pi framebuffer has padded rows (stride {stride}), not handled')
    return w, h, bpp


def to_fb(path, w, h, bpp):
    """PICTURE as gzipped raw pixels for the framebuffer, fitted to w x h on
    black: RGB565 at 16 bits, XRGB8888 (bytes B G R 0) at 32."""
    im = ImageOps.exif_transpose(Image.open(path)).convert('RGB')
    im.thumbnail((w, h), Image.LANCZOS)
    page = Image.new('RGB', (w, h))
    page.paste(im, ((w - im.width) // 2, (h - im.height) // 2))
    a = np.asarray(page)
    if bpp == 32:
        v = np.zeros((h, w, 4), np.uint8)
        v[..., 0], v[..., 1], v[..., 2] = a[..., 2], a[..., 1], a[..., 0]
        return gzip.compress(v.tobytes(), compresslevel=6)
    a = a.astype(np.uint16)
    v = ((a[..., 0] >> 3) << 11) | ((a[..., 1] >> 2) << 5) | (a[..., 2] >> 3)
    return gzip.compress(v.astype('<u2').tobytes(), compresslevel=6)


def cmd_new(picture):
    w, h, bpp = screen_size(NEW_HOST)
    data = to_fb(picture, w, h, bpp)
    # Written beside and renamed, so the Pi never shows half a picture.
    r = subprocess.run(['ssh', NEW_HOST, f'mkdir -p {NEW_DIR} && cat > {NEW_DIR}/.new.tmp && '
                        f'mv {NEW_DIR}/.new.tmp {NEW_DIR}/new.fb.gz'],
                       input=data, capture_output=True, timeout=120)
    if r.returncode != 0:
        sys.exit(f'display_sync: sending {picture} failed: {r.stderr.decode().strip()}')
    print(f'display_sync: {os.path.basename(picture)} sent to the monitor')


def cmd_selected():
    """The first picture selected in Photos, as a new shot."""
    with tempfile.TemporaryDirectory() as tmp:
        name = osa(f'''tell application "Photos"
  set s to selection
  if s is {{}} then return ""
  set m to item 1 of s
  try
    set n to filename of m
  on error msg
    -- Selected inside an album view, the reference can be "media item id X
    -- of album id Y" with an album that does not resolve (seen 2026-11-03);
    -- the item resolves by its own id.
    set AppleScript's text item delimiters to "media item id \\""
    set tailText to text item 2 of msg
    set AppleScript's text item delimiters to "\\""
    set mid to text item 1 of tailText
    set AppleScript's text item delimiters to ""
    set m to media item id mid
    set n to filename of m
  end try
  export {{m}} to POSIX file "{tmp}/"
  return n
end tell''')
        if not name:
            sys.exit('display_sync: nothing is selected in Photos')
        files = [f for f in os.listdir(tmp) if not f.startswith('.')]
        if not files:
            sys.exit(f'display_sync: Photos exported nothing for {name} (a video, or not downloaded?)')
        cmd_new(os.path.join(tmp, files[0]))


def osa(script):
    r = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        sys.exit(f'display_sync: Photos: {r.stderr.strip()}')
    return r.stdout.strip()


def album_ids(name):
    """The album's media item ids, making the album if it is not there."""
    out = osa(f'''tell application "Photos"
  if not (exists album "{name}") then make new album named "{name}"
  set out to ""
  repeat with m in media items of album "{name}"
    set out to out & (id of m) & linefeed
  end repeat
  return out
end tell''')
    return [l for l in out.splitlines() if l.strip()]


def slide_name(media_id):
    return re.sub(r'[^A-Za-z0-9]+', '_', media_id).strip('_') + '.fb.gz'


def cmd_album(name):
    ids = album_ids(name)
    w, h, bpp = screen_size()
    os.makedirs(CACHE, exist_ok=True)
    want = {slide_name(i): i for i in ids}

    # A different screen size makes the whole cache stale.
    size_tag = os.path.join(CACHE, 'SIZE')
    if not os.path.exists(size_tag) or open(size_tag).read() != f'{w}x{h}x{bpp}':
        for f in os.listdir(CACHE):
            os.remove(os.path.join(CACHE, f))
        open(size_tag, 'w').write(f'{w}x{h}x{bpp}')

    for fname, mid in want.items():
        dst = os.path.join(CACHE, fname)
        if os.path.exists(dst):
            continue
        with tempfile.TemporaryDirectory() as tmp:
            osa(f'''tell application "Photos"
  export {{media item id "{mid}"}} to POSIX file "{tmp}/"
end tell''')
            files = [f for f in os.listdir(tmp) if not f.startswith('.')]
            if not files:
                print(f'display_sync: Photos exported nothing for {mid} (a video, or not downloaded?)')
                continue
            try:
                data = to_fb(os.path.join(tmp, files[0]), w, h, bpp)
            except Exception as e:
                print(f'display_sync: skipping {files[0]}: {e}')
                continue
            open(dst + '.tmp', 'wb').write(data)
            os.replace(dst + '.tmp', dst)
            print(f'display_sync: + {files[0]}')
    for f in os.listdir(CACHE):
        if f.endswith('.fb.gz') and f not in want:
            os.remove(os.path.join(CACHE, f))
            print(f'display_sync: - {f} (left the album)')

    # Copy to the card only if something changed: each copy unlocks it.
    have = set(f for f in os.listdir(CACHE) if f.endswith('.fb.gz'))
    r = ssh(f'ls {RO_ROOT}{SLIDES} 2>/dev/null || ls {SLIDES} 2>/dev/null')
    on_pi = set(l for l in r.stdout.split() if l.endswith('.fb.gz'))
    if have == on_pi:
        print(f'display_sync: {len(have)} picture(s), the Pi is up to date')
        return
    overlay = ssh(f'mountpoint -q {RO_ROOT}').returncode == 0
    dest = RO_ROOT + SLIDES if overlay else SLIDES
    if overlay and ssh(f'sudo mount -o remount,rw {RO_ROOT}').returncode != 0:
        sys.exit('display_sync: cannot unlock the card')
    try:
        ssh(f'sudo mkdir -p {dest} && sudo chown pi:pi {dest}')
        r = subprocess.run(['rsync', '-rt', '--delete', '--include=*.fb.gz', '--exclude=*',
                            CACHE + '/', f'{HOST}:{dest}/'], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            print(f'display_sync: rsync failed: {r.stderr.strip()}')
    finally:
        if overlay:
            ssh(f'sync && sudo mount -o remount,ro {RO_ROOT}')
    print(f'display_sync: {len(have)} picture(s) on the Pi (+{len(have - on_pi)} -{len(on_pi - have)})')


def main():
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == 'new':
        cmd_new(args[1])
    elif args == ['selected']:
        cmd_selected()
    elif args and args[0] == 'album' and len(args) <= 2:
        cmd_album(args[1] if len(args) == 2 else ALBUM)
    else:
        sys.exit(__doc__.split('\n\n')[1])


if __name__ == '__main__':
    main()
