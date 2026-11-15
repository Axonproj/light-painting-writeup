#!/usr/bin/env python3
#
# plotdisplay.py — the HDMI monitor on plotcam: pictures while there is
# someone to see them, dark (HDMI off, monitor in standby) while the camera
# is taking a picture and while the room is empty.
#
#   dark   while a shot is being taken: plotter.pl writes SHOOT ("EXPIRY
#          TOKEN") before each exposure and waits for this program to answer
#          with the TOKEN in ACK, dark; it removes SHOOT when the exposure
#          is over. EXPIRY (epoch seconds) frees the screen if the Mac never
#          does (crash, lost connection).
#   on     while the room lights are on, or someone is in the room (the
#          radar's presence, which the lights controller follows in every
#          mode): so it can be watched in a room darkened by hand.
#   dark   otherwise (the lights went off by themselves: nobody there), and
#          if the lights controller cannot be reached for GRACE_S.
#
# The lights controller (basic_ota at 192.168.1.5):
#   GET /state -> {"mode":"auto","lights":"on","presence":"on"}
#
# What is shown:
#   - a new shot (NEW, pushed by the Mac's display_sync.py after each
#     plotsnap or paint) as soon as it arrives, for HOLD_S from when it is first on screen,
#     then the slideshow (test shots would otherwise sit there for hours);
#   - after the lights have gone off and on again with no new shot
#     meanwhile, a slideshow of SLIDES (the Photos album "Plotter display",
#     also pushed by display_sync.py).
#
# Pictures arrive ready for the framebuffer: raw RGB565 at the screen's size,
# gzipped (NAME.fb.gz). The slides live on the card's read-only layer, so
# they survive power cuts; the new shot is in RAM.
#
# Runs as a systemd service (plotdisplay.service, see pi/README.md).

import glob
import gzip
import json
import os
import random
import shutil
import time
import urllib.request

STATE_URL  = 'http://192.168.1.5/state'
POLL_S     = 0.5                     # the lights controller, this often
GRACE_S    = 10                      # its last answer stands this long (WiFi drops a poll now and then)
TICK_S     = 0.05                    # SHOOT, this often
SLIDE_S    = 15                      # each slideshow picture
HOLD_S     = 600                     # a new shot, this long on screen, then the slideshow
FB         = '/dev/fb0'
FB_BLANK   = '/sys/class/graphics/fb0/blank'
DPMS       = '/sys/class/drm/card0-HDMI-A-1/dpms'
NEW        = '/tmp/display/new.fb.gz'
SHOOT      = '/tmp/display/shoot'
ACK        = '/tmp/display/ack'
SLIDE_DIRS = ['/media/root-ro/home/pi/display/slides',   # under the read-only overlay
              '/home/pi/display/slides']                 # with the overlay off


def room():
    """(lights on, someone there), or None if the controller cannot be reached."""
    try:
        with urllib.request.urlopen(STATE_URL, timeout=1) as r:
            s = json.load(r)
        return s.get('lights') == 'on', s.get('presence') == 'on'
    except Exception:
        return None


def shoot_token():
    """The token of a shot in progress, or None."""
    try:
        expiry, token = open(SHOOT).read().split()
        return token if time.time() < float(expiry) else None
    except (OSError, ValueError):
        return None


def set_dark(dark):
    with open(FB_BLANK, 'w') as f:
        f.write('1' if dark else '0')


def show(path):
    try:
        with gzip.open(path, 'rb') as f:
            data = f.read()
        with open(FB, 'wb') as fb:
            fb.write(data)
        return True
    except Exception as e:
        print(f'plotdisplay: cannot show {path}: {e}', flush=True)
        return False


def slides():
    for d in SLIDE_DIRS:
        files = sorted(glob.glob(os.path.join(d, '*.fb.gz')))
        if files:
            return files
    return []


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


class Screen:
    def __init__(self):
        self.showing = None          # path on the screen, None = dark
        self.acked = None
        set_dark(True)

    def dark(self):
        if self.showing is not None:
            set_dark(True)
            self.showing = None

    def shooting(self):
        """Dark at once if a shot is starting, answering its token. True while one is on."""
        token = shoot_token()
        if token is None:
            return False
        self.dark()
        if token != self.acked:
            with open(ACK + '.tmp', 'w') as f:
                f.write(token)
            os.replace(ACK + '.tmp', ACK)
            self.acked = token
        return True

    def show(self, path, again=False):
        if path == self.showing and not again:
            # Lit as it should be? Someone else (the Mac's fallback) may
            # have blanked it.
            try:
                if open(DPMS).read().strip() == 'Off':
                    set_dark(False)
            except OSError:
                pass
            return
        # Drawn while still dark, and checked again before lighting up:
        # a shot may have started meanwhile.
        if show(path) and not self.shooting():
            if self.showing is None:
                set_dark(False)
            self.showing = path


def main():
    # Keep the text console off the framebuffer: no login prompt or cursor
    # drawn over the pictures.
    for vt in glob.glob('/sys/class/vtconsole/vtcon*'):
        try:
            if 'frame buffer' in open(os.path.join(vt, 'name')).read():
                open(os.path.join(vt, 'bind'), 'w').write('0')
        except OSError:
            pass
    # /tmp/display is where the Mac (as pi) leaves new shots and shot requests.
    os.makedirs(os.path.dirname(SHOOT), exist_ok=True)
    shutil.chown(os.path.dirname(SHOOT), 'pi', 'pi')

    screen = Screen()
    was_lit = None
    hold_new = False                 # showing the new shot, not the slideshow
    new_seen = mtime(NEW)            # a shot already there at start is not news
    new_while_off = False
    new_shown = None                 # when the new shot was first on screen
    deck, next_slide = [], 0.0
    last, last_t = None, 0.0         # the controller's last answer, and when

    while True:
        if screen.shooting():
            time.sleep(TICK_S)
            continue

        r = room()
        if r is not None:
            last, last_t = r, time.monotonic()
        elif time.monotonic() - last_t < GRACE_S:
            r = last                 # one missed poll: not worth a blink
        lit, someone = r if r else (False, False)
        if lit and was_lit is False:
            # Lights back on: keep a shot that came in while they were off,
            # else back to the slideshow.
            hold_new = new_while_off
            next_slide = 0.0
        if not lit and was_lit is not False:
            new_while_off = False
        was_lit = lit

        t = mtime(NEW)
        fresh = t is not None and t != new_seen
        if fresh:
            new_seen = t
            hold_new = True
            new_shown = None
            if not lit:
                new_while_off = True

        if r is None or not (lit or someone):
            screen.dark()
        elif hold_new and os.path.exists(NEW) and \
                (new_shown is None or time.monotonic() - new_shown < HOLD_S):
            screen.show(NEW, again=fresh)
            if new_shown is None and screen.showing == NEW:
                new_shown = time.monotonic()
        elif screen.showing in (None, NEW) or time.monotonic() >= next_slide:
            if not deck:
                deck = slides()
                random.shuffle(deck)
            if deck:
                screen.show(deck.pop(), again=True)
                next_slide = time.monotonic() + SLIDE_S
            else:
                screen.dark()        # nothing to show

        # Wait for the next poll, watching for a shot meanwhile.
        until = time.monotonic() + POLL_S
        while time.monotonic() < until and not screen.shooting():
            time.sleep(TICK_S)


if __name__ == '__main__':
    main()
