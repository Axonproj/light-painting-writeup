#!/usr/bin/env python3
# aquarium.py — the HDMI monitor on atelier (Pi 3 B+): the aquarium video,
# looped, while someone is in the room; HDMI off (monitor in standby) while
# nobody is.
#
#   on     while the lights controller's radar sees someone (presence on)
#   dark   once it has seen nobody for OFF_AFTER_S, and if the controller
#          cannot be reached for GRACE_S
#   shot   a new finished picture from plotter.pl (display_sync.py new, as
#          /tmp/display/new.fb.gz, raw pixels at the screen's size and depth) in place
#          of the aquarium, for HOLD_S from when it is first on screen; then
#          the aquarium again
#   dark   while plotter.pl is shooting, whoever is in the room: it writes
#          "EXPIRY TOKEN" to /tmp/display/shoot, this answers with the token
#          in /tmp/display/ack once the screen is dark (the same handshake as
#          plotcam's monitor, pi/plotdisplay.py), and it is over when the file
#          goes or the expiry passes
#
# The lights controller (basic_ota at 192.168.1.5):
#   GET /state -> {"mode":"auto","lights":"on","presence":"on"}
#
# Playing is GStreamer straight to the screen (`aquarium.py --play`): the
# hardware decoder's frames go to the display without a copy, so a 1080p
# video plays with nothing dropped and the CPU idle (mpv copied every frame
# and dropped ~1 in 5 at 1080p, even at 720p scaled). The loop is a segment
# seek, with no gap at the join. Dark is the player stopped and the
# framebuffer blanked, which turns the HDMI output off. Each time it comes on
# it starts somewhere new in the loop.
import glob
import gzip
import json
import os
import random
import subprocess
import sys
import time
import urllib.request

STATE_URL   = 'http://192.168.1.5/state'
POLL_S      = 1                      # the lights controller, this often
TICK_S      = 0.1                    # a shot request, this often
SHOOT       = '/tmp/display/shoot'
ACK         = '/tmp/display/ack'
GRACE_S     = 10                     # its last answer stands this long (WiFi drops a poll now and then)
OFF_AFTER_S = 30                     # nobody seen this long: dark (the radar drops out briefly)
VIDEOS      = ('/usr/local/share/aquarium/aquarium_long.mp4',    # ~25 min, card only (too big for the RAM overlay)
               '/usr/local/share/aquarium/aquarium1080.mp4')    # the first: 2.5 min
FB_BLANK    = '/sys/class/graphics/fb0/blank'
PIPELINE    = ('filesrc location="{}" ! qtdemux ! h264parse ! v4l2h264dec ! '
               'kmssink name=sink force-modesetting=true')   # the screen at the video's size
STALL_S     = 5                      # no new frame on the screen this long: start the player again
NEW         = '/tmp/display/new.fb.gz'   # the latest shot, ready for the framebuffer
FB          = '/dev/fb0'
HOLD_S      = 600                    # a new shot, this long on screen, then the aquarium


def video():
    """The first of VIDEOS that is there."""
    return next((v for v in VIDEOS if os.path.exists(v)), VIDEOS[-1])


def play(video, start):
    """Loop video on the screen for ever, from start (0-1 of the way in)."""
    import gi
    gi.require_version('Gst', '1.0')
    from gi.repository import GLib, Gst
    Gst.init(None)
    p = Gst.parse_launch(PIPELINE.format(video))
    bus = p.get_bus()
    p.set_state(Gst.State.PAUSED)
    m = bus.timed_pop_filtered(30 * Gst.SECOND,
                               Gst.MessageType.ASYNC_DONE | Gst.MessageType.ERROR)
    if m is None or m.type == Gst.MessageType.ERROR:
        sys.exit(f'aquarium: cannot play {video}: {m.parse_error()[0] if m else "timeout"}')
    ok, length = p.query_duration(Gst.Format.TIME)
    # A SEGMENT seek posts SEGMENT_DONE at the end instead of EOS; seeking
    # back to the start then, without a flush, joins the loop with no gap.
    p.seek_simple(Gst.Format.TIME,
                  Gst.SeekFlags.FLUSH | Gst.SeekFlags.SEGMENT | Gst.SeekFlags.KEY_UNIT,
                  int(length * start) if ok else 0)
    loop = GLib.MainLoop()

    def message(bus, m):
        if m.type == Gst.MessageType.SEGMENT_DONE:
            p.seek_simple(Gst.Format.TIME, Gst.SeekFlags.SEGMENT, 0)
        elif m.type in (Gst.MessageType.ERROR, Gst.MessageType.EOS):
            why = m.parse_error()[0] if m.type == Gst.MessageType.ERROR else 'end'
            print(f'aquarium: player stopped: {why}', flush=True)
            loop.quit()

    # Watchdog: the picture once froze (2026-10-25) with the player still
    # running. If the screen gets no new frame for STALL_S, give up; the
    # controller starts a new player.
    sink = p.get_by_name('sink')
    shown = [-1]

    def watch():
        n = sink.get_property('stats').get_value('rendered')
        if n == shown[0]:
            pos = p.query_position(Gst.Format.TIME)[1] / Gst.SECOND
            print(f'aquarium: picture stalled at {pos:.1f} s, {n} frames since the last seek',
                  flush=True)
            loop.quit()
            return False
        shown[0] = n
        return True

    bus.add_signal_watch()
    bus.connect('message', message)
    GLib.timeout_add_seconds(STALL_S, watch)
    p.set_state(Gst.State.PLAYING)
    loop.run()
    p.set_state(Gst.State.NULL)
    sys.exit(1)                      # the controller starts it again


def someone():
    """True if the radar sees someone, None if the controller cannot be reached."""
    try:
        with urllib.request.urlopen(STATE_URL, timeout=1) as r:
            return json.load(r).get('presence') == 'on'
    except Exception:
        return None


def shoot_token():
    """The token of a shot in progress, or None."""
    try:
        expiry, token = open(SHOOT).read().split()
        return token if time.time() < float(expiry) else None
    except (OSError, ValueError):
        return None


def ack(token):
    with open(ACK + '.tmp', 'w') as f:
        f.write(token)
    os.replace(ACK + '.tmp', ACK)


def set_blank(blank):
    with open(FB_BLANK, 'w') as f:
        f.write('1' if blank else '0')


def new_shot():
    """The latest shot's file time, or None."""
    try:
        return os.stat(NEW).st_mtime
    except OSError:
        return None


class Screen:
    def __init__(self):
        self.player = None
        self.picture = False         # the shot is on the screen

    def stop_player(self):
        if self.player and self.player.poll() is None:
            self.player.terminate()
            try:
                self.player.wait(5)
            except subprocess.TimeoutExpired:
                self.player.kill()
                self.player.wait()
        self.player = None

    def play(self):
        self.picture = False
        if self.player and self.player.poll() is None:
            return
        self.player = subprocess.Popen([sys.executable, '-u', os.path.abspath(__file__),
                                        '--play', video(), str(random.random())])

    def show(self):
        """The latest shot, still; False if it cannot be shown."""
        if self.picture:
            return True
        self.stop_player()           # the player leaves the screen to the framebuffer
        try:
            with gzip.open(NEW, 'rb') as f:
                data = f.read()
            with open(FB, 'wb') as fb:
                fb.write(data)
        except Exception as e:
            print(f'aquarium: cannot show the new shot: {e}', flush=True)
            return False
        set_blank(False)
        self.picture = True
        return True

    def dark(self):
        self.stop_player()
        self.picture = False
        set_blank(True)


def main():
    # Keep the text console off the framebuffer: no login prompt or cursor
    # between plays.
    for vt in glob.glob('/sys/class/vtconsole/vtcon*'):
        try:
            if 'frame buffer' in open(os.path.join(vt, 'name')).read():
                open(os.path.join(vt, 'bind'), 'w').write('0')
        except OSError:
            pass

    os.makedirs(os.path.dirname(SHOOT), exist_ok=True)
    os.chmod(os.path.dirname(SHOOT), 0o1777)   # plotter.pl writes there as pi
    screen = Screen()
    screen.dark()
    last, last_t = None, 0.0         # the controller's last answer, and when
    seen_t = None                    # when someone was last seen
    acked = None                     # the shot last answered
    shot = new_shot()                # a shot already there at start is old: not shown
    shot_t = None                    # when the latest shot was first on screen
    holding = False                  # a new shot waits for, or is in, its HOLD_S

    while True:
        token = shoot_token()
        if token is not None:
            screen.dark()
            if token != acked:
                ack(token)
                acked = token
            time.sleep(TICK_S)
            continue
        r = someone()
        now = time.monotonic()
        if r is not None:
            last, last_t = r, now
        elif now - last_t < GRACE_S:
            r = last                 # one missed poll: not worth a blink
        if r:
            seen_t = now
        t = new_shot()
        if t is not None and t != shot:
            shot, shot_t, holding = t, None, True
            screen.picture = False   # draw the new one
        if holding and shot_t is not None and now - shot_t >= HOLD_S:
            holding = False
        if seen_t is not None and now - seen_t < OFF_AFTER_S:
            if holding and screen.show():
                if shot_t is None:
                    shot_t = now
            else:
                holding = False
                screen.play()        # also restarts the player if it died
        else:
            screen.dark()
        # Wait out the poll, but answer a shot request at once.
        until = time.monotonic() + POLL_S
        while time.monotonic() < until and shoot_token() is None:
            time.sleep(TICK_S)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--play']:
        play(sys.argv[2], float(sys.argv[3]))
    else:
        main()
