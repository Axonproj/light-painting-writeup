#!/usr/bin/env perl
#
# plotter.pl — Interactive control for Tektronix 4662 plotter via WiFi.
#
# Connects to ESP32-C3 TCP bridge and provides a simple command language
# that translates to G-code for GRBL.
#
# Usage: ./plotter.pl [host:port]
# Default: 192.168.1.25:8080

use strict;
use warnings;
use IO::Socket::INET;
use IO::Select;
use Socket qw(SOL_SOCKET SO_KEEPALIVE IPPROTO_TCP);
use Term::ReadLine;
use POSIX qw(ceil acos strftime);
use FindBin;
use File::Path qw(make_path);
use lib '.';
use PlotterSVG qw(load_svg svg_info);
use HersheyFont qw(render_text);

# remote_snap lives beside this script (chdkptp USB capture → dated .jpeg).
my $snap_cmd = "$FindBin::Bin/remote_snap";
my $last_image;   # path of the most recent snap/plotsnap capture, for `open`.
our $in_paint = 0; # set inside paint: its frames are not for the monitor, its result is.
my $last_plot_file;  # plot file of the most recent plotsnap, for lathist's header.
# The atelier counts as dark at or below this camera light-meter reading (Bv
# x96, one stop per 96). Measured 2026-10-04 on the A570: lights on +156,
# lights off -798 (its floor) — about 10 stops apart; -400 sits well clear.
use constant LIGHTS_DARK_BV96 => -400;

use constant PI => 4 * atan2(1, 1);
# M62 P encoding: low 24 bits = RGB, bit 24 = preload flag (arm the carrier colour
# but keep the LED dark, so it propagates over ESP-NOW during an air move).
use constant COLOUR_PRELOAD_BIT => 1 << 24;

my $DEFAULT_HOST = '192.168.1.25';
my $DEFAULT_PORT = 8080;

# Arguments: [host:port] and any number of -x "command" (batch mode: run the
# commands as if typed, then exit — e.g. the `paint` script's
# `plotter.pl -x "paint v2.plot 2"`).
my ($host, $port) = ($DEFAULT_HOST, $DEFAULT_PORT);
my @BATCH;
{
  my @a = @ARGV;
  while (@a) {
    my $arg = shift @a;
    if ($arg eq '-x' && @a)                         { push @BATCH, shift @a; }
    elsif ($arg =~ /^([^:]+):(\d+)$/)               { ($host, $port) = ($1, $2); }
    elsif ($arg =~ /^\d+\.\d+\.\d+\.\d+$/)          { $host = $arg; }
    else { die "Usage: $0 [host:port] [-x \"command\" ...]\n"; }
  }
}

# State.
my $feed_rate = 3000;   # 3000 for ink pen, 25000 for light pen
my $doing_light_pen = 0;  # 1 = light pen (no solenoid lift), 0 = ink pen
my $pen_is_down = 0;
my ($cur_x, $cur_y);     # tracked pen position in paper coords (undef = unknown -> query GRBL)
my $homed = 0;           # this session has homed: GRBL's coordinates can be trusted
my $cam_sock;           # TCP socket to ESP32-C6 USB remote (optional).
my $cam_shoot_delay = 3; # seconds to wait after shoot for AF light to clear.
my $arc_tolerance = 0.01;  # mm, polygonisation tolerance for arcs.
my $curve_accel = 0;       # mm/s² centripetal budget for curvature-aware feed (0 = off → constant feed_rate).
my $min_curve_feed = 1000; # mm/min floor so tight curvature doesn't crawl to a stop.
my $pen_up_delay = 0;     # seconds, dwell after pen up before moving.
my $pen_down_delay = 0;   # seconds, pull-in dwell before switching to hold.
my @line_style;            # bit pattern, e.g. (1,0,0,1,0,0,0,1).
my $line_style_len = 0;    # mm per bit in pattern (0 = solid).
my @colour_rgb = ();       # current LED colour (r,g,b 0-255), empty = off.
my $last_colour_p = -1;    # 24-bit RGB currently shown on the carrier; -1 forces re-emit.
my $colour_delay = 0.020;  # seconds, dwell after M62/M5 for ESP-NOW LED update (ink pen only).
my $led_blackout = 0;      # 1 = force every emitted colour to black (background-frame runs).
my $dynamic = 0;           # 1 = LED brightness follows the pen's real speed (see cmd_dynamic).
my $peak_seen;             # highest peak (%) print_peak has reported since paint cleared it.
my %peak_log;             # the speed reports since plotsnap cleared it: peak, ms, mean_ms, feed (for the frame's note).
my $led_brightness = 100;  # percent; scales every emitted colour, so plot files keep full-range values.

# Communication pipeline state.
my $recv_buf = '';      # shared receive buffer.
my @pipeline_lens = (); # byte-lengths of in-flight commands (oldest first).
# How many bytes the host keeps in flight in GRBL's 128-byte RX buffer. 100 is
# the streaming default: enough that GRBL's planner never starves on a dense SVG.
#
# History: for a while this had to be 50, because at 100 about a fifth of light-
# paint strokes started dark for 30-130ms. That was blamed on GRBL, but the
# lathist trace showed the bridge was reading [C:n] late: it forwarded each `ok`
# to the host one byte per TCP write, and a stalled write held the loop while
# the [C:n] sat in the UART buffer. Throttling to 50 only hid it (a stalled ok
# then just delayed the next command, with the pen at rest). The bridge now
# buffers host output and sends it once per pass; two v2 frames at 100 showed
# every stroke lighting within 3.5ms. Change at runtime with `rxbuf N`.
my $grbl_rx_buffer = 100;

# ---- Capture options ----------------------------------------------------------
# Everything snap/plotsnap will accept, checked BEFORE the camera or the lights
# are touched. Anything not listed is rejected: remote_snap passes unknown
# options through to the camera's remoteshoot, which ignores what it does not
# understand, so a typo used to fail silently (or after the lights were off).
# The help text is generated from this table so the two cannot drift.
#   value: regex the value must match (undef = flag takes no value)
#   plotsnap: 1 = only meaningful for plotsnap (rejected by snap)
my @SNAP_OPTS = (
  # BEGIN SNAP_OPTS
  { name => '-tv',     value => qr{^\d+(?:\.\d+)?$|^\d+/\d+$}, help => 'exposure, seconds or a fraction (8, 0.5, 1/250)' },
  { name => '-av',     value => qr{^\d+(?:\.\d+)?$},            help => 'aperture f-number (2.8, 8)' },
  { name => '-sv',     value => qr{^\d+$},                        help => 'ISO (80, 200)' },
  { name => '-focus',  value => qr{^\d+$},                        help => 'manual focus distance in mm (400, 800); omit for autofocus' },
  { name => '-nd',     value => qr{^(?:in|out)$}i,                 help => 'neutral-density filter in|out' },
  { name => '-name',   value => qr{^[A-Za-z0-9][A-Za-z0-9_-]*$},   help => 'file name: NAME_001.jpeg, NAME_002... (plotsnap defaults to the plot file\'s name)' },
  { name => '-dng',    value => undef,                             help => 'also keep the RAW frame (NAME.dng or, from the 700D, NAME.cr2 beside NAME.jpeg), for raw processing' },
  { name => '-open',   value => undef,                             help => 'view the frame afterwards in the macOS viewer' },
  { name => '-lights', value => undef, plotsnap => 1,              help => 'douse the room lights for the shot, restore after (plotsnap does this anyway: the default)' },
  { name => '-nolights', value => undef, plotsnap => 1,            help => 'leave the room lights alone (plotsnap only)' },
  # END SNAP_OPTS
);
# Things people reach for that mean one of the above.
my %SNAP_OPT_ALIASES = (show => '-open', view => '-open', display => '-open',
                        light => '-lights', lamp => '-lights', dark => '-lights',
                        f => '-focus', mf => '-focus', iso => '-sv', shutter => '-tv',
                        aperture => '-av', fstop => '-av', title => '-name', label => '-name');


# Log file — overwritten on each startup.
my $log_fh;
open($log_fh, '>', 'plotter.log') or warn "Cannot open plotter.log: $!\n";
if ($log_fh) {
  $log_fh->autoflush(1);
  my @t = localtime;
  printf $log_fh "# plotter.log started %04d-%02d-%02d %02d:%02d:%02d\n",
    $t[5]+1900, $t[4]+1, $t[3], $t[2], $t[1], $t[0];
}

sub log_msg {
  return unless $log_fh;
  my ($msg) = @_;
  my @t = localtime;
  printf $log_fh "%02d:%02d:%02d %s", $t[2], $t[1], $t[0], $msg;
}

# Machine travel limits (mm).
my $LIMIT_X = 393;
my $LIMIT_Y = 262;

# Paper calibration (machine coords of paper corners).
my ($paper_min_x, $paper_min_y);  # "set min" position
my ($paper_max_x, $paper_max_y);  # "set max" position
my $paper_set = 0;                # true when both corners defined

# Keep the Mac awake for as long as this session runs. On battery it idle-sleeps
# a minute after the last keypress, and a sleeping Mac stops streaming: a plot
# stalls part-way, or (as kept happening) the final `home` finishes on the
# machine while its `ok` waits for the Mac to wake, and the "30 s" timeout fires
# minutes later. caffeinate -i blocks idle SYSTEM sleep only (the display may
# still go off), and -w ends it by itself when this process exits.
my $caffeinate_pid;
{
  my $parent = $$;
  my $pid = fork();
  if (defined $pid && $pid == 0) {
    open(STDOUT, '>', '/dev/null');
    open(STDERR, '>', '/dev/null');
    exec('caffeinate', '-i', '-w', $parent) or exit 0;   # no caffeinate: just carry on
  }
  $caffeinate_pid = $pid;
}

# This session's own code, as loaded. A session left open for days keeps
# running the plotter.pl it started with, however the file changes after;
# the REPL warns when it has, and `restart` reloads it in place.
my $SELF       = "$FindBin::Bin/$FindBin::Script";
my $SELF_MTIME = (stat $SELF)[9] // 0;
my $STARTED    = localtime;
my @ORIG_ARGV  = @ARGV;

# Connect to plotter.
my ($sock, $sel);
print "Connecting to $host:$port...\n";
connect_bridge() or die "Cannot connect to $host:$port: $!\n";
print "Connected.\n";
my $term = Term::ReadLine->new('plotter');
my $prompt = 'plotter> ';

# The bridge resets the Nano on every new connection; anything sent before
# GRBL has booted (~1-2 s through the old bootloader) is lost. Wait for its
# banner before the first command.
wait_for_grbl(4);

# Batch mode: run the -x commands, then leave. A dead link here is fatal —
# nobody is at the keyboard to home after a reconnect.
if (@BATCH) {
  die "No reply from GRBL at $host:$port — is the plotter powered?\n" unless link_alive();
  for my $cmd (@BATCH) {
    print "plotter> $cmd\n";
    last if dispatch($cmd) eq 'quit';
  }
  cmd_dynamic(0) if $dynamic;
  print "Disconnected.\n";
  close($sock);
  exit 0;
}

# Main REPL.
while (defined(my $input = $term->readline($prompt))) {
  $input =~ s/^\s+|\s+$//g;
  next if $input eq '';
  $term->addhistory($input);
  if ($input eq 'restart') { restart_self(); }
  if (((stat $SELF)[9] // 0) != $SELF_MTIME) {
    print "!! plotter.pl has changed since this session started ($STARTED) — "
        . "type `restart` to load it\n";
  }
  # Before anything that moves the machine or opens the shutter, make sure
  # GRBL is actually answering: a sleep, a bridge reboot or a WiFi drop can
  # leave this end holding a dead connection, where commands vanish silently.
  if ($input =~ /^(?:home|run|svg|plotsnap)\b/i && !link_alive()) {
    print "!! No reply from GRBL — reconnecting to $host:$port...\n";
    if (!reconnect_bridge()) {
      print "!! Cannot reach the bridge ($!). Is the plotter powered? Nothing sent.\n";
      next;
    }
    if ($input ne 'home') {
      print "!! Reconnected. GRBL was reset by the reconnect and must be homed: "
          . "type `home`, then repeat the command.\n";
      next;
    }
    print "Reconnected; homing.\n";
  }
  last if dispatch($input) eq 'quit';
}

# Open the TCP connection to the bridge. Keepalive (idle probe after 60 s)
# so a link that dies while the session sits idle is noticed, rather than
# found by the next command vanishing into it.
sub connect_bridge {
  $sock = IO::Socket::INET->new(
    PeerAddr => $host,
    PeerPort => $port,
    Proto    => 'tcp',
    Timeout  => 5,
  ) or return 0;
  $sock->autoflush(1);
  setsockopt($sock, SOL_SOCKET, SO_KEEPALIVE, 1);
  eval { setsockopt($sock, IPPROTO_TCP, 0x10, 60) };   # TCP_KEEPALIVE (macOS): idle secs
  $sel = IO::Select->new($sock);
  return 1;
}

sub reconnect_bridge {
  close($sock) if $sock;
  @pipeline_lens = ();
  $recv_buf = '';
  ($cur_x, $cur_y) = (undef, undef);
  $homed = 0;
  return 0 unless connect_bridge();
  wait_for_grbl(4);              # the bridge resets the Nano: let GRBL boot
  return 1;
}

# Wait up to $secs for GRBL's "Grbl 1.1h ['$' for help]" banner, printing
# what arrives. Returns 1 when seen; 0 means no reset happened (bridge
# without the reset wire) or GRBL is silent — the link check sorts that out.
sub wait_for_grbl {
  my ($secs) = @_;
  my $deadline = time() + $secs;
  while (time() <= $deadline) {
    while ($recv_buf =~ s/^(.*?)\r?\n//) {
      my $line = $1;
      print "  << $line\n" if length $line;
      if ($line =~ /^Grbl /) { drain_responses(0.3); return 1; }
    }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      return 0 if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  return 0;
}

# Is GRBL answering? A status query ('?', real-time, needs no 'ok') must come
# back within 2 s. Other lines that arrive meanwhile are left for their owners.
sub link_alive {
  return 0 unless $sock && $sock->connected;
  print $sock "?";
  my $deadline = time() + 2;
  while (time() <= $deadline) {
    if ($recv_buf =~ s/^(<[^\n]*>)\r?\n//m) { return 1; }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      return 0 if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  return 0;
}

# Reload this script in place, same arguments: a fresh session on the
# current code. Drops the session's settings and resets GRBL (new connection).
sub restart_self {
  print "Restarting plotter.pl...\n";
  close($sock) if $sock;
  close($cam_sock) if $cam_sock;
  kill 'TERM', $caffeinate_pid if $caffeinate_pid;   # the new session starts its own
  exec($^X, $SELF, @ORIG_ARGV) or print "!! restart failed: $!\n";
}

sub dispatch {
  my ($input) = @_;

  if ($input eq 'home') {
    cmd_home();
  } elsif ($input =~ /^moveto\s+([\d.+-]+)\s+([\d.+-]+)$/i) {
    cmd_moveto($1, $2);
  } elsif ($input =~ /^moveby\s+([\d.+-]+)\s+([\d.+-]+)$/i) {
    cmd_moveby($1, $2);
  } elsif ($input =~ /^drawto\s+([\d.+-]+)\s+([\d.+-]+)$/i) {
    cmd_drawto($1, $2);
  } elsif ($input =~ /^drawby\s+([\d.+-]+)\s+([\d.+-]+)$/i) {
    cmd_drawby($1, $2);
  } elsif ($input =~ /^arc\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)$/i) {
    cmd_arc($1, $2, $3);
  } elsif ($input eq 'penup') {
    cmd_penup();
  } elsif ($input eq 'pendown') {
    cmd_pendown();
  } elsif ($input =~ /^delay\s+([\d.]+)$/i) {
    cmd_delay($1 + 0);
  } elsif ($input eq 'platen on') {
    send_line('M8');
  } elsif ($input eq 'platen off') {
    send_line('M9');
  } elsif ($input =~ /^speed\s+(\d+)$/i) {
    $feed_rate = $1;
    print "Feed rate set to $feed_rate mm/min\n";
  } elsif ($input =~ /^dynamic\s+(on|off)$/i) {
    cmd_dynamic(lc($1) eq 'on');
  } elsif ($input =~ /^peak$/i) {
    print_peak();
  } elsif ($input =~ /^light\s*pen\s+(on|off)$/i) {
    my $want = lc($1);
    cmd_dynamic(0) if $dynamic && $want eq 'off';   # laser mode breaks the ink pen
    $doing_light_pen = ($want eq 'on') ? 1 : 0;
    $feed_rate       = $doing_light_pen ? 25000 : 3000;
    print "Light pen: " . ($doing_light_pen ? "on" : "off") . ", speed $feed_rate mm/min\n";
  } elsif ($input =~ /^tolerance\s+([\d.]+)$/i) {
    $arc_tolerance = $1;
    print "Arc tolerance set to $arc_tolerance mm\n";
  } elsif ($input =~ /^curve.?accel\s+(off|0)$/i) {
    $curve_accel = 0;
    print "Curvature-aware feed OFF (constant $feed_rate mm/min)\n";
  } elsif ($input =~ /^curve.?accel\s+([\d.]+)$/i) {
    $curve_accel = $1 + 0;
    printf "Curvature-aware feed ON: a=%.0f mm/s² → v=sqrt(a*R), floor %d, cap %d mm/min\n",
           $curve_accel, $min_curve_feed, $feed_rate;
  } elsif ($input =~ /^curve.?feed.?floor\s+([\d.]+)$/i) {
    $min_curve_feed = $1 + 0;
    print "Curvature feed floor set to $min_curve_feed mm/min\n";
  } elsif ($input =~ /^pen.?up.?delay\s+([\d.]+)$/i) {
    $pen_up_delay = $1;
    print "Pen up delay set to $pen_up_delay s\n";
  } elsif ($input =~ /^pen.?down.?delay\s+([\d.]+)$/i) {
    $pen_down_delay = $1;
    print "Pen down delay set to $pen_down_delay s\n";
  } elsif ($input =~ /^colour.?delay\s+([\d.]+)$/i) {
    $colour_delay = $1;
    print "Colour delay set to $colour_delay s\n";
  } elsif ($input =~ /^linestyle\s+([01]+)\s+([\d.]+)$/i) {
    @line_style = split //, $1;
    $line_style_len = $2;
    print "Line style: $1, $line_style_len mm/bit\n";
  } elsif ($input =~ /^linestyle\s+(off|solid)$/i) {
    @line_style = ();
    $line_style_len = 0;
    print "Line style: solid\n";
  } elsif ($input =~ /^colour\s+(\d+)\s+(\d+)\s+(\d+)$/i) {
    cmd_colour($1+0, $2+0, $3+0);
  } elsif ($input =~ /^colour\s+off$/i) {
    cmd_colour_off();
  } elsif ($input =~ /^led\s+(on|off)$/i) {
    cmd_led_blackout(lc($1) eq 'off');
  } elsif ($input =~ /^led\s+type(?:\s+(\S+))?$/i) {
    cmd_led_type($1);
  } elsif ($input =~ /^brightness\s+(\d+(?:\.\d+)?)\s*%?$/i) {
    cmd_brightness($1 + 0);
  } elsif ($input =~ /^brightness$/i) {
    print "Brightness: $led_brightness%\n";
  } elsif ($input =~ /^testled\s+(\d+)\s+(\d+)\s+(\d+)$/i) {
    my $p = encode_colour($1+0, $2+0, $3+0);
    send_line("!espnow:$p");
  } elsif ($input =~ /^testled\s+off$/i) {
    send_line("!espnow:0");
  } elsif ($input eq 'status') {
    flush_pipeline();
    send_raw("?");
    drain_responses(0.5);
  } elsif ($input eq 'unlock') {
    send_line('$X');
  } elsif ($input eq 'reset') {
    print $sock "\x18";
    drain_responses(1.0);
    $pen_is_down = 0;
    ($cur_x, $cur_y) = (undef, undef);   # position unknown until re-homed
    $homed = 0;
  } elsif ($input =~ /^rxbuf(?:\s+(\d+))?$/i) {
    # In-flight byte budget for GRBL's RX buffer. Low (20) = one command at a
    # time, for the late-[C:n] contention test; 100 = the old streaming default.
    if (defined $1) { $grbl_rx_buffer = $1 + 0; }
    print "GRBL RX budget: $grbl_rx_buffer bytes"
        . ($grbl_rx_buffer < 30 ? " (one command in flight)" : "") . "\n";
  } elsif ($input eq 'lat') {
    # Colour-change latency the bridge accumulated during the last plot. Reading
    # it also zeroes it, so ask once per plot, after the plot has finished.
    flush_pipeline();      # send_line used to do this; don't read mid-plot
    send_raw("!lat\n");   # see the note under `lathist`: no 'ok' comes back
    drain_responses(1.0);
  } elsif ($input =~ /^lathist\s*(\S*)$/i) {
    # Per-colour-change trace from the last plot: one row per [C:n], in order,
    # so a late start seen in the photograph can be matched to the stroke that
    # produced it. dt_us is the arrival cadence (upstream/GRBL jitter), gap_us
    # the bridge's loop delay, cb_us the radio's. Reading it also zeroes it.
    # Longer drain than 'lat': this is up to 96 lines, not four.
    #
    # The trace is also SAVED, stem-matched to the frame plotsnap just captured
    # (remote_pics/<stamp>.jpeg -> remote_pics/<stamp>.lat), so the electrical
    # timing can be paired with the photograph afterwards. An explicit path
    # overrides. Reading zeroes the buffer, so this is one shot per plot.
    # send_raw, NOT send_line: bridge '!' commands are not G-code and never
    # answer 'ok', so send_line would burn its full 30s drain_one_ok timeout AND
    # consume the trace while waiting, leaving nothing for drain_responses.
    my $dest = $1;
    my @trace;
    flush_pipeline();   # let the plot actually finish before the read zeroes it
    send_raw("!lathist\n");
    drain_responses(3.0, \@trace);
    save_lathist(\@trace, $dest);
  } elsif ($input =~ /^tune\s+(on|off)$/i) {
    # Antenna tuning: bridge probes the carrier at a fixed fast cadence and shows
    # the ACK rate on its OLED. Idle machine only — the bridge drops tune mode on
    # the next G-code line, and it self-expires after two minutes.
    send_line('!tune ' . lc($1));
    drain_responses(0.5);
    print "Watch the bridge OLED: TUNE <acked>/<sent> <pct>%\n" if lc($1) eq 'on';
  } elsif ($input eq 'esp-reset') {
    send_line('!reset');
    print "ESP32 resetting, reconnect in a few seconds.\n";
    return 'quit';
  } elsif ($input eq 'nano-upload') {
    print $sock "!nano-upload\n";
    drain_responses(1.0);
    print "Flash Nano over USB now.\n";
    print "Run 'esp-reset' when done.\n";
    return 'quit';
  } elsif ($input =~ /^load$/i) {
    cmd_load();
  } elsif ($input =~ /^carrier$/i) {
    my ($ok, $report) = carrier_alive();
    print defined $report ? "$report\n" : "No answer from the bridge to !carrier.\n";
  } elsif ($input =~ /^carrier\s+sleep$/i) {
    # Send the carrier to sleep now; the next light-pen plot nudges it awake.
    flush_pipeline();
    send_raw("!csleep\n");
    drain_responses(0.5);
  } elsif ($input =~ /^carrier\s+wake$/i) {
    my ($ok) = carrier_alive();
    if ($ok) { print "Pen carrier is already awake.\n"; }
    else {
      my $woke = wake_carrier('carrier');
      print "A nudge did not wake the pen carrier.\n" if defined $woke && !$woke;
    }
  } elsif ($input =~ /^!\s*(.+)$/) {
    my $raw = $1;
    if ($raw =~ /^\?/) {
      send_raw("?");
      drain_responses(0.5);
    } else {
      send_line($raw);
      ($cur_x, $cur_y) = (undef, undef);   # raw g-code may move the pen -> re-query
    }
  } elsif ($input eq 'set min') {
    cmd_set_min();
  } elsif ($input eq 'set max') {
    cmd_set_max();
  } elsif ($input eq 'paper clear') {
    $paper_set = 0;
    print "Paper calibration cleared.\n";
  } elsif ($input eq 'paper') {
    cmd_paper_info();
  } elsif ($input =~ /^text\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+([\w-]+)\s+"([^"]*)"$/i) {
    cmd_text($1, $2, $3, $5, $4);
  } elsif ($input =~ /^text\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)\s+"([^"]*)"$/i) {
    cmd_text($1, $2, $3, $4);
  } elsif ($input =~ /^text\s+([\d.+-]+)\s+([\d.+-]+)\s+"([^"]*)"$/i) {
    cmd_text($1, $2, 10, $3);
  } elsif ($input =~ /^svg\s+info\s+(.+)$/i) {
    cmd_svg_info($1);
  } elsif ($input =~ /^svg\s+(\S+)\s+fit$/i) {
    cmd_svg($1, 'fit');
  } elsif ($input =~ /^svg\s+(\S+)\s+scale\s+([\d.]+)$/i) {
    cmd_svg($1, 'scale', $2);
  } elsif ($input =~ /^svg\s+(\S+)$/i) {
    cmd_svg($1);
  } elsif ($input =~ /^run\s+(.+)$/i) {
    cmd_run($1);
  } elsif ($input =~ /^cam\s+connect\s+([^:\s]+):(\d+)$/i) {
    cmd_cam_connect($1, $2);
  } elsif ($input =~ /^cam\s+connect\s+([^:\s]+)$/i) {
    cmd_cam_connect($1, 8080);
  } elsif ($input =~ /^cam\s+connect$/i) {
    cmd_cam_connect('192.168.1.23', 8080);
  } elsif ($input =~ /^cam\s+tv\s+(\d+)$/i) {
    cmd_cam_send("tv $1");
  } elsif ($input =~ /^cam\s+dist\s+(\d+)$/i) {
    cmd_cam_send("dist $1");
  } elsif ($input =~ /^cam\s+shoot$/i) {
    cmd_cam_shoot();
  } elsif ($input =~ /^cam\s+shoot\s+delay\s+([\d.]+)$/i) {
    $cam_shoot_delay = $1 + 0;
    printf "Shoot delay set to %.1fs\n", $cam_shoot_delay;
  } elsif ($input =~ /^cam\s+tv\?$/i) {
    cmd_cam_send('tv?');
  } elsif ($input =~ /^cam\s+dist\?$/i) {
    cmd_cam_send('dist?');
  } elsif ($input =~ /^cam\s+status$/i) {
    cmd_cam_send('status');
  } elsif ($input =~ /^cam\s+disconnect$/i) {
    cmd_cam_disconnect();
  } elsif ($input =~ /^plotsnap\s+(\S+)(?:\s+(\d+(?:\.\d+)?)%?)?\s*(.*)$/i) {
    # plotsnap FILE [N] [options]: N is the LED brightness for this shot, as
    # `paint FILE N`; the brightness in force before is put back afterwards.
    my ($file, $pct, $opts) = ($1, $2, $3);
    if (defined $pct && $pct > 100) { print "plotsnap: brightness is 0-100%\n"; }
    else {
      my $old_brightness = $led_brightness;
      cmd_brightness($pct) if defined $pct;
      cmd_plotsnap($file, $opts);
      cmd_brightness($old_brightness) if defined $pct;
    }
  } elsif ($input =~ /^paint\s+(\S+)(?:\s+(\d+(?:\.\d+)?)%?)?\s*(.*)$/i) {
    cmd_paint($1, $2, $3);
  } elsif ($input =~ /^darks\b\s*(.*)$/i) {
    cmd_darks($1);
  } elsif ($input =~ /^snap\b\s*(.*)$/i) {
    cmd_snap($1);
  } elsif ($input =~ /^(?:open|view)\s*(.*)$/i) {
    cmd_open_image($1);
  } elsif ($input =~ /^lights\s+(on|off|auto)$/i) {
    cmd_lights(lc $1);
  } elsif ($input eq 'quit' || $input eq 'exit') {
    cmd_dynamic(0) if $dynamic;
    return 'quit';
  } elsif ($input eq 'help') {
    print_help();
  } else {
    print "Unknown command: $input (type 'help' for commands)\n";
  }
  return 'ok';
}

print "Disconnected.\n";
close($sock);
close($cam_sock) if $cam_sock;
exit 0;

# ---- Commands ---------------------------------------------------------------

sub cmd_cam_connect {
  my ($host, $port) = @_;
  if ($cam_sock) {
    close($cam_sock);
    $cam_sock = undef;
  }
  $cam_sock = IO::Socket::INET->new(
    PeerAddr => $host,
    PeerPort => $port,
    Proto    => 'tcp',
    Timeout  => 5,
  ) or do { print "Cannot connect to camera $host:$port: $!\n"; return; };
  $cam_sock->autoflush(1);
  print "Camera connected ($host:$port).\n";
  cam_drain(0.5);
}

sub cmd_cam_disconnect {
  return unless $cam_sock;
  close($cam_sock);
  $cam_sock = undef;
  print "Camera disconnected.\n";
}

# Send a command and read lines until "ok" (ESP32-C6 protocol).
sub cmd_cam_send {
  my ($cmd) = @_;
  unless ($cam_sock) {
    print "No camera connected (use 'cam connect').\n";
    return;
  }
  print $cam_sock "$cmd\n";
  print "  cam>> $cmd\n";
  my $cam_sel = IO::Select->new($cam_sock);
  my $buf     = '';
  my $deadline = time() + 30;
  while (time() < $deadline) {
    if ($cam_sel->can_read(0.2)) {
      my $n = sysread($cam_sock, my $chunk, 1024);
      if (!defined $n || $n == 0) { print "  cam: connection closed\n"; return; }
      $buf .= $chunk;
      while ($buf =~ s/^(.*?)\r?\n//) {
        my $line = $1;
        return if $line eq 'ok';
        print "  cam<< $line\n";
      }
    }
  }
  print "  cam: timeout waiting for ok\n";
}

# Trigger shutter then sleep to let the AF light finish before continuing.
sub cmd_cam_shoot {
  cmd_cam_send('shoot');
  printf "  cam: waiting %.1fs for AF...\n", $cam_shoot_delay;
  select(undef, undef, undef, $cam_shoot_delay);
}

sub cam_drain {
  my ($wait) = @_;
  return unless $cam_sock;
  my $cam_sel = IO::Select->new($cam_sock);
  my $deadline = time() + $wait;
  my $buf = '';
  while (time() < $deadline) {
    if ($cam_sel->can_read(0.1)) {
      my $n = sysread($cam_sock, my $chunk, 1024);
      last if !defined $n || $n == 0;
      $buf .= $chunk;
      while ($buf =~ s/^(.*?)\r?\n//) {
        my $line = $1;
        print "  cam<< $line\n" unless $line eq 'ok';
      }
      $deadline = time() + 0.3;
    }
  }
}

# GRBL's state word (Idle, Alarm, Run, ...) from a status report, or undef if
# none comes within $wait seconds. GRBL answers `?` at any time except during
# the homing cycle itself, so this also waits a cycle out.
sub grbl_state {
  my ($wait) = @_;
  flush_pipeline();
  my $deadline = time() + $wait;
  my $last_poll = 0;
  while (time() < $deadline) {
    if (time() - $last_poll >= 0.3) { print $sock "?"; $last_poll = time(); }
    while ($recv_buf =~ s/^(.*?)\r?\n//) {
      my $line = $1;
      return $1 if $line =~ /^<(\w+)/;
      print "  << $line\n" if length $line;
    }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      return undef if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  return undef;
}

# True if the machine homed. A failed cycle leaves GRBL in Alarm, and the
# replies to $H cannot be trusted to say so: after a run of rejected commands
# they arrive out of step. So ask GRBL for its state.
sub cmd_home {
  # LED off first, straight through the bridge: GRBL may be locked and would
  # refuse an M5, and a homing run should not paint a streak across the bed.
  send_line('!espnow:0');
  $last_colour_p = -1;                # the next stroke must send its colour again
  # GRBL refuses $H (error:8) while a move is still running, as after a rapid
  # just before `home`. A dwell is not parsed past until the planner is empty,
  # so the $H behind it arrives with GRBL idle. In Alarm it is refused, harmlessly.
  send_line('G4 P0');
  send_line('$H');
  send_line('G10 L2 P1 X-393 Y-262');
  $pen_is_down = 0;
  my $state = grbl_state(25);
  unless (defined $state && $state eq 'Idle') {
    printf "home: homing FAILED (GRBL %s) — the machine is not homed.\n",
           defined $state ? "is in $state" : "did not answer";
    print "  `unlock`, move clear of the limit switches, then `home` again.\n";
    print "  (ALARM:8 = the 1 mm pull-off did not clear a switch: \$27.)\n";
    ($cur_x, $cur_y) = (undef, undef);
    $homed = 0;
    return 0;
  }
  $homed = 1;
  ($cur_x, $cur_y) = ($LIMIT_X, 0);   # home = bottom-right corner in paper coords
  # The carrier sleeps after five minutes idle, so a session starts by calling it
  # up. One nudge only: in an ink session it is switched off, and that is fine.
  my ($ok, $report) = carrier_alive();
  return 1 unless defined $report;
  if ($ok || nudge_carrier('home', 1)) { carrier_keep(); }
  else { print "home: pen carrier not answering (switched off, or flat).\n"; }
  return 1;
}

# Bring the carrier to where the aperture can be fitted, with its LED on:
# wake it, go to LOAD_X,LOAD_Y, and draw a short dim-green stroke so the LED is
# left lit (a lit colour also keeps the carrier awake). `penup` puts it out.
use constant LOAD_X => 20;
use constant LOAD_Y => 20;
use constant LOAD_GREEN => 16;      # of 255
sub cmd_load {
  unless ($homed) { return unless cmd_home(); }
  return unless check_carrier('load');
  unless ($doing_light_pen) {         # never the ink pen's solenoid for this
    $doing_light_pen = 1;
    $feed_rate = 25000;
    print "Light pen: on, speed $feed_rate mm/min\n";
  }
  cmd_moveto(LOAD_X, LOAD_Y);
  # A dim green, the same whatever brightness the session is set to: enough to
  # see where the light falls, not enough to dazzle at arm's length.
  my $session_brightness = $led_brightness;
  $led_brightness = 100;
  cmd_colour(0, LOAD_GREEN, 0);
  cmd_drawby(10, 10);
  flush_pipeline();
  $led_brightness = $session_brightness;
  $last_colour_p = -1;                # the next colour is emitted at the session's brightness
  print "load: LED lit at @{[LOAD_X + 10]},@{[LOAD_Y + 10]}. `penup` switches it off.\n";
}

sub check_limits {
  my ($x, $y) = @_;
  if ($x < 0 || $x > $LIMIT_X || $y < 0 || $y > $LIMIT_Y) {
    printf "Out of limits: (%.1f, %.1f) — must be within (0,0)-(%.0f,%.0f)\n",
           $x, $y, $LIMIT_X, $LIMIT_Y;
    return 0;
  }
  return 1;
}

sub cmd_moveto {
  my ($px, $py) = @_;
  # Zero-length move: nothing to travel to, so skip the whole pen-up/preload/G0
  # cycle. SVGs that split a polyline into one <path> per segment emit a moveto to
  # the current position at every vertex; without this the preload M62 blanks the
  # LED and the stroke flickers dark at each vertex. Skipping also keeps the pen
  # down so the polyline stays one continuous stroke. No preload is lost: preload
  # exists to use the air-move time, and there is no air move here.
  # Only when the position is known: before homing (or after a reset or raw
  # G-code) it is undef, and an unknown position cannot be shown to be the same
  # as the target — so make the move rather than skipping it.
  return if defined $cur_x
         && abs($px - $cur_x) < 1e-6 && abs($py - $cur_y) < 1e-6;
  my ($x, $y) = paper_to_machine($px, $py);
  return unless check_limits($x, $y);
  if ($pen_is_down) {
    cmd_penup();
  }
  emit_preload();   # arm the carrier colour (LED dark) during the upcoming air move
  queue_motion("G0 X$x Y$y");
  ($cur_x, $cur_y) = ($px, $py);
}

sub cmd_drawto {
  my ($px, $py) = @_;
  my ($x, $y) = paper_to_machine($px, $py);
  return unless check_limits($x, $y);
  if ($line_style_len > 0 && @line_style) {
    draw_styled_line($x, $y);
  } else {
    if (!$pen_is_down) {
      cmd_pendown();
    }
    emit_colour();
    queue_motion("G1 X$x Y$y F$feed_rate");
  }
  ($cur_x, $cur_y) = ($px, $py);
}

sub draw_styled_line {
  my ($tx, $ty) = @_;
  my ($px, $py) = get_position();
  if (!defined $px) {
    print "Could not read position.\n";
    return;
  }
  draw_styled_polyline([[$tx, $ty]], $px, $py);
}

sub draw_styled_polyline {
  my ($points, $px, $py) = @_;
  if (!defined $px) {
    ($px, $py) = get_position();
    if (!defined $px) {
      print "Could not read position.\n";
      return;
    }
  }

  my $bit_idx = 0;
  my $bit_remain = $line_style_len;

  for my $pt (@$points) {
    my ($tx, $ty) = @$pt;
    my $dx = $tx - $px;
    my $dy = $ty - $py;
    my $seg_len = sqrt($dx * $dx + $dy * $dy);
    next if $seg_len < 0.001;
    my $ux = $dx / $seg_len;
    my $uy = $dy / $seg_len;
    my $pos = 0;

    while ($pos < $seg_len) {
      my $step = $bit_remain;
      $step = $seg_len - $pos if $step > $seg_len - $pos;
      $pos += $step;
      $bit_remain -= $step;
      my $ex = $px + $ux * $pos;
      my $ey = $py + $uy * $pos;
      if ($line_style[$bit_idx]) {
        if (!$pen_is_down) { cmd_pendown(); }
        queue_motion(sprintf "G1 X%.3f Y%.3f F%d", $ex, $ey, $feed_rate);
      } else {
        if ($pen_is_down) { cmd_penup(); }
        queue_motion(sprintf "G0 X%.3f Y%.3f", $ex, $ey);
      }
      if ($bit_remain < 0.001) {
        $bit_idx = ($bit_idx + 1) % scalar @line_style;
        $bit_remain = $line_style_len;
      }
    }
    $px = $tx;
    $py = $ty;
  }
}

sub cmd_moveby {
  my ($dx, $dy) = @_;
  my ($x, $y) = current_position();
  if (!defined $x) {
    print "Could not read position.\n";
    return;
  }
  cmd_moveto($x + $dx, $y + $dy);
}

sub cmd_drawby {
  my ($dx, $dy) = @_;
  my ($x, $y) = current_position();
  if (!defined $x) {
    print "Could not read position.\n";
    return;
  }
  cmd_drawto($x + $dx, $y + $dy);
}

# Local radius of curvature at vertex B from three consecutive points (Menger
# curvature: R = |AB|·|BC|·|CA| / (4·area)). Source-agnostic — works for any
# polyline (arc, Bézier, SVG, hand-drawn). Collinear points → ~infinite radius.
sub menger_radius {
  my ($a, $b, $c) = @_;
  my $area2 = abs(($b->[0]-$a->[0])*($c->[1]-$a->[1]) - ($c->[0]-$a->[0])*($b->[1]-$a->[1]));
  return 1e12 if $area2 < 1e-9;   # collinear → straight → effectively infinite R
  my $ab = sqrt(($b->[0]-$a->[0])**2 + ($b->[1]-$a->[1])**2);
  my $bc = sqrt(($c->[0]-$b->[0])**2 + ($c->[1]-$b->[1])**2);
  my $ca = sqrt(($a->[0]-$c->[0])**2 + ($a->[1]-$c->[1])**2);
  return ($ab * $bc * $ca) / (2 * $area2);   # area = area2/2 ⇒ /(4·area) = /(2·area2)
}

# Curvature-aware per-segment feed for a polyline. Given the start point and the
# subsequent points (all machine coords), returns one feed (mm/min) per segment:
# v = sqrt(curve_accel · R_local), clamped to [min_curve_feed, feed_rate], using the
# tighter (slower) endpoint of each segment. Kept under GRBL's own junction/buffer
# limits so GRBL obeys the commanded feed rather than throttling on its own — which
# makes the speed (and any speed-derived brightness) exactly what the host chose.
sub curvature_feeds {
  my ($sx, $sy, $points) = @_;
  my @v = ([$sx, $sy], @$points);          # full vertex list including the start point
  my $n = scalar @v;
  my @R;
  for my $i (0 .. $n-1) {
    push @R, ($i == 0 || $i == $n-1) ? undef : menger_radius($v[$i-1], $v[$i], $v[$i+1]);
  }
  $R[0]    = $R[1]    if $n >= 2;          # ends inherit nearest interior curvature
  $R[$n-1] = $R[$n-2] if $n >= 2;
  my @feeds;
  for my $j (0 .. $n-2) {                  # segment v[j] → v[j+1]; use the tighter end
    my $r = $R[$j];
    $r = $R[$j+1] if defined $R[$j+1] && $R[$j+1] < $r;
    my $f = sqrt($curve_accel * $r) * 60;  # mm/s → mm/min
    $f = $min_curve_feed if $f < $min_curve_feed;
    $f = $feed_rate      if $f > $feed_rate;
    push @feeds, int($f + 0.5);
  }
  return \@feeds;
}

sub cmd_arc {
  my ($cx, $cy, $angle_deg) = @_;
  my ($px, $py) = current_position();  # tracked; avoids a pipeline-flushing query (LED dark-start)
  if (!defined $px) {
    print "Could not read position.\n";
    return;
  }
  my $dx = $px - $cx;
  my $dy = $py - $cy;
  my $r = sqrt($dx * $dx + $dy * $dy);
  if ($r < 0.001) {
    print "Current position is at the arc centre.\n";
    return;
  }

  my $start_angle = atan2($dy, $dx);
  my $sweep = $angle_deg * PI / 180;

  # Number of segments: ensure sagitta <= tolerance.
  # sagitta = r * (1 - cos(step/2)), so step = 2 * acos(1 - tol/r).
  my $n;
  if ($arc_tolerance >= $r) {
    $n = 1;
  } else {
    my $max_step = 2 * acos(1 - $arc_tolerance / $r);
    $n = ceil(abs($sweep) / $max_step);
    $n = 1 if $n < 1;
  }

  printf "Arc: r=%.1f, sweep=%.1f°, %d segments\n", $r, $angle_deg, $n;

  # Generate polygon points from arc.
  my @points;
  for my $i (1 .. $n) {
    my $a = $start_angle + $sweep * $i / $n;
    my $x = $cx + $r * cos($a);
    my $y = $cy + $r * sin($a);
    ($x, $y) = paper_to_machine($x, $y);
    return unless check_limits($x, $y);
    push @points, [$x, $y];
  }

  if ($line_style_len > 0 && @line_style) {
    draw_styled_polyline(\@points);
  } else {
    if (!$pen_is_down) {
      cmd_pendown();
    }
    emit_colour();
    if ($curve_accel > 0) {
      my ($sx, $sy) = paper_to_machine($px, $py);   # arc start in machine coords
      my $feeds = curvature_feeds($sx, $sy, \@points);
      my ($fmin, $fmax) = ($feeds->[0], $feeds->[0]);
      for my $f (@$feeds) { $fmin = $f if $f < $fmin; $fmax = $f if $f > $fmax; }
      printf "  curvature feed: %d..%d mm/min\n", $fmin, $fmax;
      for my $i (0 .. $#points) {
        queue_motion(sprintf "G1 X%.3f Y%.3f F%d", $points[$i][0], $points[$i][1], $feeds->[$i]);
      }
    } else {
      for my $pt (@points) {
        queue_motion(sprintf "G1 X%.3f Y%.3f F%d", $pt->[0], $pt->[1], $feed_rate);
      }
    }
  }
  my $a_end = $start_angle + $sweep;          # track the arc end as the new pen position
  ($cur_x, $cur_y) = ($cx + $r * cos($a_end), $cy + $r * sin($a_end));
}

sub cmd_set_min {
  my ($x, $y) = get_position();
  if (!defined $x) {
    print "Could not read position.\n";
    return;
  }
  ($paper_min_x, $paper_min_y) = ($x, $y);
  print "Paper min set to machine ($x, $y)\n";
  check_paper_ready();
}

sub cmd_set_max {
  my ($x, $y) = get_position();
  if (!defined $x) {
    print "Could not read position.\n";
    return;
  }
  ($paper_max_x, $paper_max_y) = ($x, $y);
  print "Paper max set to machine ($x, $y)\n";
  check_paper_ready();
}

sub check_paper_ready {
  if (defined $paper_min_x && defined $paper_max_x) {
    my $w = abs($paper_max_x - $paper_min_x);
    my $h = abs($paper_max_y - $paper_min_y);
    $paper_set = 1;
    printf "Paper ready: %.1f x %.1f mm\n", $w, $h;
    printf "  Paper (0,0) -> (%.1f, %.1f)\n", $w, $h;
  }
}

sub cmd_paper_info {
  if (!$paper_set) {
    print "No paper calibration. Use 'set min' and 'set max'.\n";
    return;
  }
  my $w = abs($paper_max_x - $paper_min_x);
  my $h = abs($paper_max_y - $paper_min_y);
  printf "Paper: %.1f x %.1f mm\n", $w, $h;
  printf "  min: machine (%.1f, %.1f)\n", $paper_min_x, $paper_min_y;
  printf "  max: machine (%.1f, %.1f)\n", $paper_max_x, $paper_max_y;
}

sub paper_to_machine {
  my ($x, $y) = @_;
  return ($x, $y) unless $paper_set;
  my $mx = $paper_min_x + $x;
  my $my = $paper_min_y + $y;
  return ($mx, $my);
}

sub current_position {
  # Prefer the software-tracked position (set by home/moveto/drawto/arc). Avoids a
  # GRBL status query, which flushes the motion pipeline and waits at a standstill —
  # that stall serialises a following arc's M62 colour change to a stationary start,
  # producing an LED dark-start. Falls back to a hardware query if position is unknown.
  return ($cur_x, $cur_y) if defined $cur_x;
  return get_position();
}

sub get_position {
  flush_pipeline();
  my $deadline = time() + 10;   # allow time for the last move to finish
  my $last_poll = 0;
  while (time() < $deadline) {
    if (time() - $last_poll >= 0.1) {   # (re)request a status report
      print $sock "?";
      $last_poll = time();
    }
    while ($recv_buf =~ s/^(.*?)\r?\n//) {
      my $line = $1;
      next unless $line =~ /^<Idle/;    # only trust position once motion has stopped
      return ($1, $2) if $line =~ /WPos:([-\d.]+),([-\d.]+)/;
      return ($1, $2) if $line =~ /MPos:([-\d.]+),([-\d.]+)/;
    }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      return (undef, undef) if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  return (undef, undef);
}

# Is this file an SVG rather than a list of REPL commands? Trust the extension,
# but sniff the head too — plot files never open with an <svg> element.
sub is_svg_file {
  my ($file) = @_;
  return 0 unless defined $file && -f $file;
  return 1 if $file =~ /\.svg$/i;
  open(my $fh, '<', $file) or return 0;
  read($fh, my $head, 512);
  close $fh;
  return (defined $head && $head =~ /<svg\b/i) ? 1 : 0;
}

# A plot or SVG named without a folder: the current folder first, then the
# project's plots/ and svg/ (where they have lived since the 2026-11-09 tidy).
sub find_plot_file {
  my ($f) = @_;
  return $f if !defined $f || $f eq '' || -e $f || $f =~ m{^/};
  for my $d ("$FindBin::Bin/plots", "$FindBin::Bin/svg") {
    return "$d/$f" if -e "$d/$f";
  }
  return $f;
}

sub cmd_run {
  my ($file) = @_;
  $file = find_plot_file($file);
  # `run drawing.svg` (and plotsnap, which runs its file through here) used to
  # feed XML to the command dispatcher a line at a time. Plot it instead.
  return cmd_svg($file) if is_svg_file($file);
  open(my $fh, '<', $file) or do {
    print "Cannot open $file: $!\n";
    return;
  };
  if (plot_needs_carrier($file) && !check_carrier('run')) { close($fh); return; }
  my $t0 = time();
  my $line_num = 0;
  while (my $line = <$fh>) {
    $line_num++;
    $line =~ s/^\s+|\s+$//g;
    $line =~ s/\s*#.*$//;        # strip inline comments (GRBL has no use for #)
    next if $line eq '' || $line =~ /^#/;
    print "[$line_num] $line\n";
    log_msg("[$line_num] $line\n");
    my $rc = dispatch($line);
    last if $rc eq 'quit';
  }
  close($fh);
  cmd_penup() if $pen_is_down;
  flush_pipeline();
  my $elapsed = time() - $t0;
  printf "Script $file finished (%d:%02d elapsed)\n", int($elapsed / 60), $elapsed % 60;
}

# Open an image in the macOS default viewer. With no argument, opens the last
# snap/plotsnap capture of this session, or the newest .jpeg in remote_pics/.
sub cmd_open_image {
  my ($arg) = @_;
  $arg =~ s/^\s+|\s+$//g if defined $arg;
  my $f = (defined $arg && length $arg) ? $arg : $last_image;
  unless ($f && -f $f) {
    my $dir = $ENV{REMOTE_PICS_DIR} || "$ENV{HOME}/projects/plotter/remote_pics";
    ($f) = sort { -M $a <=> -M $b } glob("$dir/*.jpeg");   # -M: newest first
  }
  unless ($f && -f $f) { print "No image to open.\n"; return; }
  system("open", $f) == 0 ? print "Opening $f\n" : print "Could not open $f\n";
}

# ---- Room lights (basic_ota controller over HTTP) ---------------------------

# The atelier's brightness from the A570's own light meter (remote_snap
# -meter): Bv x96, or undef if the camera can't be read. It used to be the
# Mac's camera, which measured whatever room the Mac happened to be in.
sub room_bv96 {
  my $out = `$snap_cmd -meter 2>&1`;
  return $out =~ /^bv96 (-?\d+)$/m ? $1 + 0 : undef;
}

# Switch the lights off and CONFIRM the atelier actually went dark — the
# 433MHz RF is unreliable and gives no feedback. Up to 3 tries, bouncing the
# mode each retry so the controller re-fires the RF (a repeated same-mode
# command is a no-op). Dark = the camera meters at or below LIGHTS_DARK_BV96,
# an absolute level, so daylight in the room fails it too — rightly.
sub ensure_dark {
  for my $try (1 .. 3) {
    if ($try > 1) { cmd_lights('on'); select(undef,undef,undef,0.5); }  # bounce to force an RF resend
    return 0 unless cmd_lights('off');
    select(undef, undef, undef, 2);            # mains receiver + lamps fade
    my $bv = room_bv96();
    unless (defined $bv) {
      print "lights: can't read the camera's light meter — is the A570 on (playback mode)?\n";
      return 0;
    }
    printf "lights: attempt %d — atelier meters Bv %+.1f stops (dark at <= %+.1f)\n",
           $try, $bv / 96, LIGHTS_DARK_BV96 / 96;
    return 1 if $bv <= LIGHTS_DARK_BV96;
  }
  print "lights: atelier still lit after 3 tries — lights not switching, or daylight getting in?\n";
  return 0;
}


# Switch the room lights via the basic_ota ESP32 at 192.168.1.5:
#   off  = MANUAL_OFF (forced off, overrides the presence radar — use this for a
#          dark-room light-paint run so movement can't re-trigger the lights)
#   on   = MANUAL_ON
#   auto = back to mmWave presence control
sub cmd_lights {
  my ($mode) = @_;
  my $s = IO::Socket::INET->new(PeerAddr => '192.168.1.5', PeerPort => 80,
                                Proto => 'tcp', Timeout => 3)
    or do { print "lights: can't reach controller at 192.168.1.5 ($!)\n"; return 0; };
  $s->autoflush(1);
  print $s "GET /setmode?mode=$mode HTTP/1.0\r\nHost: 192.168.1.5\r\n\r\n";
  local $/;
  my $resp = <$s>;
  close $s;
  if (defined $resp && $resp =~ m{^HTTP/\S+\s+200}) { print "Lights: $mode\n"; return 1; }
  print "lights: unexpected response setting '$mode'\n";
  return 0;
}

# ---- Camera capture via chdkptp (remote_snap) -------------------------------

# True if a Canon (USB vendor 0x04a9 = 1193) is on this Mac's USB, or on the
# Pi camera host (`plotcam`, see pi/README.md). remote_snap makes the same
# choice, but plotsnap needs to know BEFORE running a plot so it doesn't draw
# into a dark room with no shutter open.
sub camera_present {
  my $out = `ioreg -p IOUSB -l -w 0 2>/dev/null`;
  return 1 if $out =~ /"idVendor" = 1193/;
  my $host = $ENV{PLOTCAM_HOST} || 'plotcam';
  return system("ssh -o BatchMode=yes -o ConnectTimeout=5 $host 'lsusb -d 04a9: >/dev/null' 2>/dev/null") == 0;
}

# Ask the bridge whether the pen carrier is answering its ESP-NOW keepalives.
# The carrier has no WiFi, so the bridge's `!carrier` report is the only sign of
# life. Returns (1, report) if present, (0, report) if absent, (0, undef) if the
# bridge didn't answer. The reply carries no 'ok', so flush first: nothing else
# may be in flight while we read it.
sub carrier_alive {
  flush_pipeline();
  send_raw("!carrier\n");
  my $deadline = time() + 3;
  while (time() < $deadline) {
    while ($recv_buf =~ s/^(.*?)\r?\n//) {
      my $line = $1;
      return (($1 eq 'present') ? 1 : 0, $line) if $line =~ /^carrier\s+(present|ABSENT)\b/;
    }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      return (0, undef) if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  return (0, undef);
}

# Will plotting this file drive the carrier? Light pen already on, an SVG that
# declares LED mode, or a plot file that turns the light pen on. Blackout runs
# never light the LED, so they don't need it.
sub plot_needs_carrier {
  my ($file) = @_;
  return 0 if $led_blackout;
  return 1 if $doing_light_pen;
  if (is_svg_file($file)) {
    my $mode = svg_declared_mode($file);
    return (defined $mode && $mode eq 'led') ? 1 : 0;
  }
  open(my $fh, '<', $file) or return 0;
  while (my $line = <$fh>) {
    if ($line =~ /^\s*light\s*pen\s+on\b/i) { close $fh; return 1; }
  }
  close $fh;
  return 0;
}

# Restart the carrier's idle clock (it sleeps five minutes after its last lit
# colour), so one just found awake cannot nod off before the plot's first
# colour reaches it. The bridge's reply carries no 'ok'.
sub carrier_keep {
  flush_pipeline();
  send_raw("!ckeep\n");
  my $deadline = time() + 1;
  while (time() < $deadline) {
    return if $recv_buf =~ s/^ckeep sent\r?\n//m;
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      return if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
}

# Jolt a sleeping carrier awake: its trembler switch brings it out of deep
# sleep. Each try is a short diagonal buzz: WAKE_REVS out-and-backs of WAKE_MM.
# Small and quick shakes the switch most (a 5 mm throw registers nothing, and
# 2 mm wakes it later than 1 mm), and WHERE it is done decides whether it
# works at all. With the aperture tube on the carrier (2026-10-08) the switch
# registers nothing at 300,100 or 200,150, but near the home corner, at
# WAKE_X,WAKE_Y, 1 mm buzzes woke it 12 of 12, about 0.3 s in. So the carriage
# goes there for the nudge and comes back to where it began. The machine must
# be homed. True once the carrier answers.
use constant WAKE_TRIES => 3;
use constant WAKE_X     => 380;
use constant WAKE_Y     => 20;
use constant WAKE_MM    => 1;     # diagonal throw, each axis
use constant WAKE_REVS  => 4;     # out-and-backs per try
sub nudge_carrier {
  my ($who, $tries) = @_;
  # Not on an unhomed machine: after `unlock` GRBL is idle but its coordinates
  # are fiction, and a move to WAKE_X,WAKE_Y could run into a limit.
  return 0 unless $homed;
  my ($x, $y) = get_position();
  return 0 unless defined $x;
  my $woke = 0;
  queue_motion(sprintf "G0 X%.3f Y%.3f", WAKE_X, WAKE_Y);
  TRY: for my $try (1 .. $tries) {
    print "$who: pen carrier asleep — nudging it awake"
        . ($tries > 1 ? " (try $try of $tries)" : "") . "...\n";
    for (1 .. WAKE_REVS) {
      queue_motion(sprintf "G0 X%.3f Y%.3f", WAKE_X + WAKE_MM, WAKE_Y + WAKE_MM);
      queue_motion(sprintf "G0 X%.3f Y%.3f", WAKE_X, WAKE_Y);
    }
    flush_pipeline();
    # It is on the air within a second; the bridge's probe of an absent
    # carrier comes round every 2 s.
    for (1 .. 4) {
      select(undef, undef, undef, 1.0);
      my ($ok) = carrier_alive();
      if ($ok) { print "$who: pen carrier awake.\n"; $woke = 1; last TRY; }
    }
  }
  queue_motion(sprintf "G0 X%.3f Y%.3f", $x, $y);
  flush_pipeline();
  return $woke;
}

# Wake a sleeping carrier, homing first if the machine is not homed (`home`
# makes one attempt of its own). True once the carrier answers, 0 if it does
# not, undef if the machine could not be homed.
sub wake_carrier {
  my ($who) = @_;
  my ($x) = $homed ? get_position() : ();
  unless (defined $x) {
    print "$who: homing, so the carriage can be nudged to wake the carrier...\n";
    return undef unless cmd_home();     # not homed: nothing can be nudged
    my ($ok) = carrier_alive();
    return 1 if $ok;
  }
  return nudge_carrier($who, WAKE_TRIES);
}

# Pre-flight for a light-pen plot: a dead carrier (flat battery, or its idle
# sleep) otherwise runs the whole plot dark. A sleeping one is woken with a
# nudge of the carriage; one that answers has its idle clock restarted. True
# if it's safe to go on.
sub check_carrier {
  my ($who) = @_;
  my ($ok, $report) = carrier_alive();
  if ($ok) { carrier_keep(); return 1; }
  if (defined $report) {
    my $woke = wake_carrier($who);
    if ($woke) { carrier_keep(); return 1; }
    unless (defined $woke) { print "$who: aborting — homing failed (nothing plotted).\n"; return 0; }
    ($ok, $report) = carrier_alive();
    print "$who: aborting — pen carrier not responding (nothing plotted).\n";
    print "  $report\n" if defined $report;
    print "  A nudge did not wake it: check its power switch and battery.\n";
  } else {
    print "$who: aborting — no answer from the bridge to !carrier (nothing plotted).\n";
  }
  return 0;
}

# Pull the saved path out of remote_snap's output (it prints the .jpeg path).
sub snap_path { my ($o) = @_; return $o =~ m{(/\S+\.jpe?g)} ? $1 : undef; }

# One-shot capture. Options pass straight through (-tv= -av= -focus=).
# Validate a snap/plotsnap option string. Returns 1 if every option is known
# and well-formed, else prints why and returns 0. $for is 'snap' or 'plotsnap'.
sub validate_snap_opts {
  my ($opts, $for) = @_;
  my %known = map { $_->{name} => $_ } @SNAP_OPTS;
  my $ok = 1;
  for my $tok (grep { length } split /\s+/, ($opts // '')) {
    my ($name, $eq, $val) = $tok =~ /^(-[A-Za-z]+)(=?)(.*)$/;
    if (!defined $name) {
      print "$for: '$tok' is not an option (options look like -tv=8 or -open)\n";
      $ok = 0; next;
    }
    my $o = $known{lc $name};
    if (!$o) {
      my $bare = lc substr($name, 1);
      my $hint = $SNAP_OPT_ALIASES{$bare};
      if (!$hint) {   # nearest known name, if it is close
        my ($best, $bd) = (undef, 3);
        for my $k (keys %known) {
          my $d = edit_distance($bare, substr($k, 1));
          ($best, $bd) = ($k, $d) if $d < $bd;
        }
        $hint = $best;
      }
      print "$for: unknown option $name" . ($hint ? " (did you mean $hint?)" : '') . "\n";
      $ok = 0; next;
    }
    if ($o->{plotsnap} && $for ne 'plotsnap') {
      print "$for: $o->{name} only applies to plotsnap\n"; $ok = 0; next;
    }
    if (defined $o->{value}) {
      if (!$eq || !length $val)      { print "$for: $o->{name} needs a value: $o->{name}=<$o->{help}>\n"; $ok = 0; }
      elsif ($val !~ $o->{value})    { print "$for: bad value '$val' for $o->{name} ($o->{help})\n"; $ok = 0; }
    } elsif ($eq) {
      print "$for: $o->{name} takes no value\n"; $ok = 0;
    }
  }
  print "$for: aborted — nothing sent to the camera. 'help' lists the options.\n" unless $ok;
  return $ok;
}

sub edit_distance {
  my ($s, $t) = @_;          # not $a/$b: those are sort's globals
  my @d = (0 .. length $t);
  for my $i (1 .. length $s) {
    my @n = ($i);
    for my $j (1 .. length $t) {
      my $c = substr($s, $i-1, 1) eq substr($t, $j-1, 1) ? 0 : 1;
      my $m = $d[$j] + 1;
      $m = $n[$j-1] + 1  if $n[$j-1] + 1  < $m;
      $m = $d[$j-1] + $c if $d[$j-1] + $c < $m;
      push @n, $m;
    }
    @d = @n;
  }
  return $d[-1];
}

# One help line per option, for the help text.
sub snap_opts_help {
  return join '', map { sprintf "                  %-12s %s\n", $_->{name} . (defined $_->{value} ? '=V' : ''), $_->{help} } @SNAP_OPTS;
}

sub cmd_snap {
  my ($opts) = @_;
  return unless validate_snap_opts($opts, 'snap');
  unless (camera_present()) {
    print "No camera on USB or plotcam — connect it in PLAYBACK mode, power on, Enable Remote OFF.\n";
    return;
  }
  # -open is handled here (remote_snap has its own, for use from the shell): strip it, view after.
  my $do_open = ($opts =~ s/(?:^|\s)-open\b//i) ? 1 : 0;
  my $monitor = MonitorDark->new;
  my $out = `$snap_cmd $opts 2>&1`;
  undef $monitor;
  my $p = snap_path($out);
  if ($p) { $last_image = $p; print "Snap saved: $p\n"; cmd_open_image($p) if $do_open; }
  else    { chomp $out; print "Snap failed: $out\n"; }
}

# Estimate a plot's drawing time in seconds from its move/draw geometry.
# Approximate by design — air moves use the feed rate too (a safe over-estimate)
# and accel/dwells aren't modelled; the caller pads it. `svg` lines are measured
# by parsing the drawing; arcs from their radius and sweep; text lines can't be
# and are counted so plotsnap can warn (an arc too, if the pen's position is unknown).
# Time (s) for one move of length L mm at commanded feed (mm/min), accounting
# for acceleration — a distance/speed estimate badly undershoots on many short
# strokes (each ramps up and back down). Trapezoidal if it reaches cruise speed,
# triangular if not. A = GRBL accel ($120/$121), V capped at the max rate ($110).
use constant ACCEL_MM_S2 => 800;   # $120/$121
use constant MAX_RATE    => 25000; # $110/$111 mm/min
use constant HOME_SEEK   => 3000;  # $25 mm/min: `home` runs X to its switch, then Y
use constant HOME_SETTLE => 1;     # seconds a `home` takes when already there (measured)
sub move_time {
  my ($L, $feed) = @_;
  return 0 if $L <= 0;
  my $v = (($feed < MAX_RATE ? $feed : MAX_RATE) || 1) / 60.0;   # mm/s
  my $a = ACCEL_MM_S2;
  my $d_acc = $v * $v / (2 * $a);           # dist to reach cruise
  return 2 * $v / $a + ($L - 2 * $d_acc) / $v if 2 * $d_acc <= $L;  # trapezoid
  return 2 * sqrt($L / $a);                                          # triangle
}

# An SVG's drawing time, in the shape estimate_plot_seconds returns, plus the
# end state (position and feed) so a `svg` line inside a plot file can be folded
# into that file's estimate. Geometry comes from the same parse the plot uses,
# so air moves are counted exactly as they will be flown.
#   %args: mode/scale (as `svg FILE fit|scale N`), x/y = pen position before the
#   drawing starts (the first move is free if unknown), feed = mm/min in force.
sub estimate_svg_seconds {
  my ($file, %args) = @_;
  my $cmds = eval { load_svg($file, svg_load_opts($args{mode}, $args{scale})) };
  return undef if $@ || !$cmds;
  my $declared = svg_declared_mode($file);
  my $feed = $declared ? ($declared eq 'led' ? 25000 : 3000)
                       : ($args{feed} // $feed_rate);
  my ($x, $y) = ($args{x}, $args{y});
  my ($mm, $secs) = (0.0, 0.0);
  for my $c (@$cmds) {
    next unless exists $c->{x};              # colour commands carry no geometry
    if (defined $x) {
      my $d = sqrt(($c->{x} - $x)**2 + ($c->{y} - $y)**2);
      $mm += $d; $secs += move_time($d, $feed);
    }
    ($x, $y) = ($c->{x}, $c->{y});
  }
  return { secs => $secs, mm => $mm, unknown => 0, x => $x, y => $y, feed => $feed };
}

sub estimate_plot_seconds {
  my ($file) = @_;
  open(my $fh, '<', $file) or return undef;
  my $feed = $feed_rate;          # mm/min
  my $lp   = $doing_light_pen;    # light pen: a colour change does not stop the motion
  my ($x, $y);                    # tracked mm position
  my ($mm, $secs, $overhead, $unknown) = (0.0, 0.0, 0.0, 0);
  # Consecutive drawto's in one direction are a single run: GRBL carries full
  # feed through a straight junction, so only the run's ends ramp. Timing each
  # piece as its own start-stop move nearly doubled the estimate for a line
  # cut into short colour steps.
  my ($run, $ux, $uy) = (0.0);
  my $flush = sub { $secs += move_time($run, $feed); $run = 0.0; };
  while (my $l = <$fh>) {
    $l =~ s/^\s+|\s+$//g; $l =~ s/\s*#.*$//;
    next if $l eq '';
    if ($l =~ /^drawto\s+([\d.+-]+)\s+([\d.+-]+)/i) {
      if (defined $x) {
        my ($dx, $dy) = ($1 - $x, $2 - $y);
        my $d = sqrt($dx**2 + $dy**2);
        if ($d > 0) {
          $flush->() unless $run > 0 && ($dx * $ux + $dy * $uy) / $d > 0.9999;
          ($ux, $uy) = ($dx / $d, $dy / $d);
          $run += $d; $mm += $d;
        }
      }
      ($x, $y) = ($1 + 0, $2 + 0);
      next;
    }
    if ($l =~ /^colour\b/i) {
      unless ($lp) { $flush->(); $overhead += $colour_delay; }
      next;
    }
    $flush->();
    if    ($l =~ /^light\s*pen\s+on/i)   { $feed = 25000; $lp = 1; }
    elsif ($l =~ /^light\s*pen\s+off/i)  { $feed = 3000;  $lp = 0; }
    elsif ($l =~ /^speed\s+(\d+)/i)      { $feed = $1 + 0; }
    elsif ($l =~ /^home\b/i) {
      # Homing seeks one axis at a time, slowly; from the far corner it is the
      # longest move in a plot. A plot that opens with `home` starts from there,
      # so its first air move is known too.
      $secs += HOME_SETTLE;
      $secs += (abs($LIMIT_X - $x) + abs($y)) / (HOME_SEEK / 60.0) if defined $x;
      ($x, $y) = ($LIMIT_X, 0);
    }
    elsif ($l =~ /^moveto\s+([\d.+-]+)\s+([\d.+-]+)/i) {
      # An air move is a G0: it flies at the max rate, whatever the feed.
      if (defined $x) { my $d = sqrt(($1-$x)**2 + ($2-$y)**2); $mm += $d; $secs += move_time($d, MAX_RATE); }
      ($x, $y) = ($1 + 0, $2 + 0);
    }
    elsif ($l =~ /^(?:moveby|drawby)\s+([\d.+-]+)\s+([\d.+-]+)/i) {
      my $d = sqrt($1**2 + $2**2); $mm += $d; $secs += move_time($d, $feed);
      if (defined $x) { $x += $1; $y += $2; }
    }
    elsif ($l =~ /^svg\s+(?!info\b)(\S+)\s*(?:(fit)|scale\s+([\d.]+))?/i) {
      my $e = estimate_svg_seconds($1, mode => ($2 ? 'fit' : $3 ? 'scale' : undef),
                                   scale => $3, x => $x, y => $y, feed => $feed);
      if ($e) { $mm += $e->{mm}; $secs += $e->{secs};
                ($x, $y, $feed) = ($e->{x}, $e->{y}, $e->{feed}); }
      else    { $unknown++; }
    }
    elsif ($l =~ /^arc\s+([\d.+-]+)\s+([\d.+-]+)\s+([\d.+-]+)/i && defined $x) {
      # Round the centre from where the pen is, at the speed the curve allows:
      # sqrt(accel x radius), the most a circle of that radius can be flown at.
      my ($cx, $cy, $sweep) = ($1, $2, $3 * PI / 180);
      my $r = sqrt(($x - $cx)**2 + ($y - $cy)**2);
      my $d = $r * abs($sweep);
      my $v = sqrt(ACCEL_MM_S2 * $r) * 60;
      $mm += $d; $secs += move_time($d, $v < $feed ? $v : $feed);
      my $a = atan2($y - $cy, $x - $cx) + $sweep;
      ($x, $y) = ($cx + $r * cos($a), $cy + $r * sin($a));
    }
    elsif ($l =~ /^(?:text|arc)\b/i)     { $unknown++; }
  }
  $flush->();
  close($fh);
  return { secs => $secs + $overhead, mm => $mm, unknown => $unknown };
}

# Run a plot with the shutter held open for its whole duration (light painting).
# The A570 has no true open/close bulb over PTP, but in a dark room an over-long
# fixed exposure adds no light, so we size a fixed -tv to cover the plot with
# margin. Exposure runs in a forked remote_snap; the plot runs while it's open.
# Write the lathist trace to a file stem-matched to the frame plotsnap just took,
# so the bridge's electrical timing can be paired with the photograph. Falls back
# to a timestamped name when no capture has happened this session (e.g. a bare
# `plot` rather than `plotsnap`), and says which it chose either way.
sub save_lathist {
  my ($lines, $dest) = @_;
  unless ($lines && @$lines) { print "lathist: nothing returned — not saved.\n"; return; }

  my $path = $dest;
  if (!length($path)) {
    if (defined $last_image && $last_image =~ /^(.*)\.[^.\/]+$/) {
      $path = "$1.lat";                       # remote_pics/<stamp>.jpeg -> .lat
    } else {
      my @t = localtime;
      $path = sprintf "remote_pics/lathist_%04d%02d%02d%02d%02d%02d.lat",
                      $t[5]+1900, $t[4]+1, $t[3], $t[2], $t[1], $t[0];
      print "lathist: no capture this session — saving unpaired.\n";
    }
  }

  my $fh;
  unless (open $fh, '>', $path) { print "lathist: cannot write $path: $!\n"; return; }
  my @t = localtime;
  printf $fh "# lathist — per-[C:n] colour-change trace from the bridge\n";
  printf $fh "# image:    %s\n", (defined $last_image     ? $last_image     : '(none)');
  printf $fh "# plot:     %s\n", (defined $last_plot_file ? $last_plot_file : '(unknown)');
  printf $fh "# captured: %04d-%02d-%02d %02d:%02d:%02d\n",
             $t[5]+1900, $t[4]+1, $t[3], $t[2], $t[1], $t[0];
  printf $fh "# feed:     %d mm/min   light pen: %s\n",
             $feed_rate, ($doing_light_pen ? 'on' : 'off');
  printf $fh "# brightness: %g%%\n", $led_brightness;
  printf $fh "# rxbuf:    %d\n", $grbl_rx_buffer;   # streaming vs throttled changes what dt_us means
  printf $fh "#\n";
  printf $fh "# Rows are in emission order, one per [C:n]. Each stroke contributes a\n";
  printf $fh "# 'preload' (armed during the air move, LED dark) then a 'show' (at the\n";
  printf $fh "# stroke start). Only 'show' rows can produce a late start; the preloads\n";
  printf $fh "# are the control group. dt_us = gap since the previous [C:n] ARRIVED\n";
  printf $fh "# (GRBL-side cadence), gap_us = the bridge's loop delay before sending,\n";
  printf $fh "# cb_us = the radio's send-callback duration.\n";
  printf $fh "# Later bridge builds add: qwait_us = parse time minus the UART driver's\n";
  printf $fh "# own burst stamp (true bridge-side read latency; ~0 = the bytes arrived\n";
  printf $fh "# late on the wire), avail = bytes already queued behind the line's first\n";
  printf $fh "# byte, lmax_us = worst loop pass gap since the previous row, smax_us/sec =\n";
  printf $fh "# longest single loop() section since the previous row and which one.\n";
  printf $fh "# Analyse with: python3 analysis/lathist.py remote_pics/<stamp>.lat\n";
  printf $fh "#\n";
  print  $fh "$_\n" for @$lines;
  close $fh;
  printf "lathist: %d lines saved to %s\n", scalar @$lines, $path;
}

# The feed a dynamic-brightness plot draws at, for `paint -ed=`: the `speed`
# in force at its strokes. Says why and returns nothing if the file is not a
# plot with `dynamic on` and one feed throughout: energy density is brightness
# / speed only while the LED follows the speed, and only against one speed.
sub dynamic_plot_feed {
  my ($file) = @_;
  if (is_svg_file($file)) { print "paint: -ed= needs a plot file with `dynamic on`, not an SVG\n"; return; }
  open(my $fh, '<', $file) or do { print "paint: cannot open $file: $!\n"; return; };
  my ($feed, $dyn, %feeds) = ($feed_rate, 0);
  while (my $l = <$fh>) {
    $l =~ s/^\s+|\s+$//g; $l =~ s/\s*#.*$//;
    if    ($l =~ /^light\s*pen\s+on/i)  { $feed = 25000; }
    elsif ($l =~ /^light\s*pen\s+off/i) { $feed = 3000; }
    elsif ($l =~ /^speed\s+(\d+)/i)     { $feed = $1 + 0; }
    elsif ($l =~ /^dynamic\s+on/i)      { $dyn = 1; }
    elsif ($l =~ /^(?:draw(?:to|by)|arc)\b/i) { $feeds{$feed} = 1; }
  }
  close($fh);
  unless ($dyn) { print "paint: -ed= needs `dynamic on` in $file: without it the LED does not follow the speed\n"; return; }
  my @f = sort { $a <=> $b } keys %feeds;
  unless (@f) { print "paint: -ed=: $file draws nothing\n"; return; }
  if (@f > 1) { print "paint: -ed= needs one speed throughout; $file draws at @f\n"; return; }
  return $f[0];
}

# paint FILE [N] [-tv=S] [-av=F] [-focus=MM] [-nocal]: a finished light-paint picture
# in one go. In ONE homing: the lit frame at brightness N, an identical dark
# frame (LED off), and a calibration grid; then subtract the dark frame,
# fit the grid's photo->plot map, warp the result into plot space, open it.
# The grid costs ~1.5 min; -nocal reuses the last cal_map.json (lens
# correction still right, absolute mm off by up to ~1-2 mm).
# -ed=X in place of N, for a plot with `dynamic on`: the brightness that gives
# the line an energy density of X (brightness per 1000 mm/min). The plot is
# first run once with the LED off for its peak speed, so the answer is right
# whether or not the file's `speed` is one the pen reaches, and that run's
# time sets the exposure. -nodry skips it and takes the file's `speed` as the
# peak.
# A plot with `#@` lines also gets ..._plot_notes.png, the result with those
# drawn on it (see annotated).
# -grid=N: the calibration grid's brightness, when the default (set for the
# LED behind its ND filter and diffuser) is wrong for what is on the LED now.
# -dark=FRAME: no dark frame is shot; FRAME (a dark frame from an earlier paint,
# NAME_dark.jpeg or its stem, in remote_pics/ or by path) is subtracted
# instead. It must have the same exposure, aperture and ISO, and the camera
# must not have moved: the repeats of one plot (2026-11-05, colour trials of
# no_exit) then cost one frame each, not two.
# ---- Dark frame library (`paint -darklib`, `darks`) ------------------------
# On the 700D the dark frame's real work is finding the hot photosites; its
# glow is about 1.5 DN. A dark frame from the library, scaled by lit time /
# dark time (raw_paint.py does it from the EXIF), does that job as well as a
# fresh one (2026-11-11: the blue wedge with a 325 s dark scaled x0.363: the
# background within 0.1 level of its own dark frame, no specks), and saves a
# frame as long as the lit one. Keep a fresh dark frame for finished pictures:
# hot photosites drift with the sensor's temperature.
# Frames: remote_pics/darks/dark_iso<ISO>_<secs>s_<date-time>.{jpeg,cr2}.
use constant DARKLIB_DIR      => 'remote_pics/darks';
use constant DARKLIB_MAX_X    => 4;     # a library frame up to this many times the exposure
use constant DARKLIB_MAX_DAYS => 60;

# (seconds, ISO) of a frame, from its EXIF (raw beside it preferred: the A570's
# JPEG caps the time at 15 s).
sub frame_exposure {
  my ($jpeg) = @_;
  (my $stem = $jpeg) =~ s/\.jpe?g$//i;
  my ($src) = grep { -f } map { "$stem.$_" } qw(cr2 CR2 dng DNG);
  $src //= $jpeg;
  my @v = split /\s+/, `exiftool -s3 -n -ExposureTime -ISO '$src' 2>/dev/null`;
  return (($v[0] // '') =~ /^\d*\.?\d+$/ ? $v[0] + 0 : undef,
          ($v[1] // '') =~ /^\d+$/ ? $v[1] + 0 : undef);
}

# The library frame for an exposure of $tv s at $iso: the same ISO, at least
# as long (scaling down keeps the dark frame's noise small), at most
# DARKLIB_MAX_X times, not older than DARKLIB_MAX_DAYS; the shortest, then the
# newest. undef if none.
sub darklib_pick {
  my ($tv, $iso) = @_;
  my @c;
  for my $j (glob(DARKLIB_DIR . '/dark_iso*_*s_*.jpeg')) {
    my ($i, $t) = $j =~ m{/dark_iso(\d+)_(\d+(?:\.\d+)?)s_[^/]+\.jpeg$} or next;
    next if defined $iso && $i != $iso;
    next if $t < 0.97 * $tv || $t > DARKLIB_MAX_X * $tv;
    next if -M $j > DARKLIB_MAX_DAYS;
    (my $stem = $j) =~ s/\.jpeg$//;
    next unless grep { -f "$stem.$_" } qw(cr2 CR2 dng DNG);
    push @c, [$j, $t, -M $j];
  }
  my ($best) = sort { $a->[1] <=> $b->[1] || $a->[2] <=> $b->[2] } @c;
  return $best ? $best->[0] : undef;
}

# Copy a dark frame (JPEG, raw and note) into the library; returns its new name.
sub darklib_add {
  my ($jpeg) = @_;
  my ($t, $iso) = frame_exposure($jpeg);
  unless (defined $t && defined $iso) { print "darks: no exposure in the EXIF of $jpeg — not added\n"; return undef; }
  make_path(DARKLIB_DIR) unless -d DARKLIB_DIR;
  my @lt = localtime((stat $jpeg)[9]);
  my $name = sprintf("%s/dark_iso%d_%ds_%04d%02d%02d-%02d%02d%02d", DARKLIB_DIR, $iso, int($t + 0.5),
                     $lt[5] + 1900, $lt[4] + 1, @lt[3, 2, 1, 0]);
  (my $stem = $jpeg) =~ s/\.jpe?g$//i;
  for my $ext (qw(jpeg cr2 CR2 dng DNG txt)) {
    next unless -f "$stem.$ext";
    my $to = $name . '.' . ($ext eq 'jpeg' ? 'jpeg' : lc $ext);
    system('cp', '-p', "$stem.$ext", $to) == 0 or print "darks: could not copy $stem.$ext\n";
  }
  return "$name.jpeg";
}

# `darks S [S ...]`: shoot dark frames of S seconds for the library, with the
# room dark, the LED off and the plotter parked. -av= is accepted and makes no
# difference (no light reaches the sensor).
sub cmd_darks {
  my ($args) = @_;
  my $av = $args =~ s/(?:^|\s)-av=(\d+(?:\.\d+)?)(?=\s|$)// ? " -av=$1" : '';
  my @secs = grep { /^\d+$/ } split /\s+/, $args;
  unless (@secs) { print "darks: give exposures in seconds, e.g. `darks 60 120 240 480`\n"; return; }
  for my $s (@secs) {
    print "darks: shooting a $s s dark frame...\n";
    my $before = $last_image // '';
    cmd_led_blackout(1);
    cmd_plotsnap('cal_colour.plot', "-lights -tv=$s -dng -name=darklib$av", 1);
    cmd_led_blackout(0);
    if (!$last_image || $last_image eq $before) { print "darks: the $s s frame failed — stopping\n"; return; }
    my $kept = darklib_add($last_image);
    print "darks: $kept\n" if $kept;
  }
}

sub cmd_paint {
  my ($file, $pct, $opts) = @_;
  $file = find_plot_file($file);
  local $in_paint = 1;
  $opts //= '';
  unless (-f $file) { print "paint: no such plot file: $file\n"; return; }
  my $tv    = $opts =~ /(?:^|\s)-tv=(\S+)/    ? $1 : undef;
  my $focus = $opts =~ /(?:^|\s)-focus=(\d+)/ ? $1 : 400;
  my $nocal = $opts =~ /(?:^|\s)-nocal\b/      ? 1  : 0;
  my $raw   = $opts =~ /(?:^|\s)-raw\b/        ? 1  : 0;   # process the raw data, DNG or CR2 (raw_paint.py)
  my $movingdark = $opts =~ /(?:^|\s)-movingdark\b/ ? 1 : 0;   # dark frame re-runs the plot, LED off
  my $ed    = $opts =~ /(?:^|\s)-ed=(\d*\.?\d+)(?=\s|$)/ ? $1 + 0 : undef;
  my $nodry = $opts =~ /(?:^|\s)-nodry\b/     ? 1  : 0;
  my $grid  = $opts =~ /(?:^|\s)-grid=(\d*\.?\d+)(?=\s|$)/ ? $1 + 0 : undef;
  my $given_dark = $opts =~ /(?:^|\s)-dark=(\S+)/ ? $1 : undef;
  my $darklib = $opts =~ /(?:^|\s)-darklib\b/ ? 1 : 0;   # dark frame from the library (raw only)
  my $av    = $opts =~ /(?:^|\s)-av=(\d+(?:\.\d+)?)(?=\s|$)/ ? $1 : undef;   # f-number, every frame
  my $rest  = $opts;
  $rest =~ s/(?:^|\s)-(?:tv=\S+|av=\d+(?:\.\d+)?|focus=\d+|ed=\d*\.?\d+|grid=\d*\.?\d+|dark=\S+|darklib|nocal|nodry|raw|movingdark)(?=\s|$)//g;
  if ($rest =~ /\S/) { print "paint: unknown option(s):$rest (use -tv= -av= -focus= -ed= -nodry -grid= -dark= -darklib -nocal -raw -movingdark)\n"; return; }
  if ($darklib) {
    if (!$raw)                 { print "paint: -darklib needs -raw (only the raw route scales a dark frame)\n"; return; }
    if (defined $given_dark)   { print "paint: -darklib and -dark= together: pick one\n"; return; }
    if ($movingdark)           { print "paint: -darklib and -movingdark together: pick one\n"; return; }
  }
  if (defined $given_dark) {
    my $d = $given_dark;
    $d .= '.jpeg' unless $d =~ /\.jpe?g$/i;
    $d = "remote_pics/$d" if !-f $d && -f "remote_pics/$d";
    unless (-f $d) { print "paint: -dark=: no such frame: $given_dark\n"; return; }
    (my $stem = $d) =~ s/\.jpe?g$//i;
    if ($raw && !grep { -f "$stem.$_" } qw(cr2 CR2 dng DNG)) {
      print "paint: -dark=: $d has no raw frame beside it (.cr2/.dng), and -raw needs one\n"; return;
    }
    if ($movingdark) { print "paint: -dark= and -movingdark together: there is no dark frame to shoot\n"; return; }
    $given_dark = $d;
  }
  if (defined $grid) {
    if ($nocal) { print "paint: -grid= and -nocal together: there is no grid to light\n"; return; }
    if ($grid <= 0 || $grid > 100) { print "paint: -grid= is a brightness, above 0 and up to 100\n"; return; }
  }
  my $ed_feed;
  if (defined $ed) {
    if (defined $pct) { print "paint: give a brightness or -ed=, not both\n"; return; }
    if ($ed <= 0)     { print "paint: -ed= must be above 0\n"; return; }
    $ed_feed = dynamic_plot_feed($file) or return;
  } elsif ($nodry) {
    print "paint: -nodry only goes with -ed=\n"; return;
  }
  my $old_brightness = $led_brightness;

  # The monitor dark from here to the end, not flashing on between frames.
  my $monitor = MonitorDark->new;

  # 1. Home, and refuse to go on unless GRBL is idle at the home corner.
  print "paint: homing...\n";
  cmd_home();
  my ($x, $y) = get_position();
  unless (defined $x && abs($x - 393) < 3 && abs($y) < 3) {
    printf "paint: aborting — not at home after homing (WPos %s)\n",
           defined $x ? sprintf("%.3f,%.3f", $x, $y) : 'unknown';
    return;
  }

  # The lit frame needs the carrier: check it (waking it) before anything
  # long. An absent carrier also slows the dry run to about half speed
  # (harmonograph_blue, 2026-10-18: 684 s for 313 s of drawing).
  return unless check_carrier('paint');

  # 1a. -ed=: the brightness from the energy density. The LED is at the
  #     brightness only at the plot's `speed`, and the pen may never get there,
  #     so a dry run (LED off) measures the peak it does reach.
  my ($dry_secs, $ed_peak);
  if (defined $ed) {
    my $peak = 100;
    unless ($nodry) {
      print "paint: dry run with the LED off, for the peak speed...\n";
      undef $peak_seen;
      cmd_led_blackout(1);
      my $t0 = time();
      cmd_run($file);
      $dry_secs = time() - $t0;
      cmd_led_blackout(0);
      unless ($peak_seen) {
        print "paint: aborting — the dry run gave no speed report (it comes at the plot's `dynamic off`)\n";
        return;
      }
      $peak = $peak_seen;
    }
    $ed_peak = $peak;
    $pct = sprintf('%.1f', $ed * $ed_feed * $peak / 100 / 1000) + 0;
    my $value = int(255 * $pct / 100 + 0.5);
    my $from  = sprintf("ED %g at %d mm/min%s", $ed, $ed_feed, ($nodry ? '' : ", peak $peak%"));
    if ($pct > 100) {
      printf "paint: aborting — %s wants brightness %g%%; the most this plot can have is ED %.2f\n",
             $from, $pct, 100 * 1000 / ($ed_feed * $peak / 100);
      return;
    }
    printf "paint: %s -> brightness %g%% (LED value %d of 255)\n", $from, $pct, $value;
    if ($value < 1) { print "paint: aborting — that leaves the LED dark\n"; return; }
    print "paint: note — only $value steps for dynamic brightness to dim through; the slowest parts will be coarse\n"
      if $value < 16;
  }

  # 2. Exposure: the plot's estimated drawing time, plus the 6 s lead and a
  #    margin. The estimate is exact for moveto/drawto plots; text and arcs
  #    aren't in it, so fall back to plotsnap's generous rule for those.
  #    After a dry run, what that took instead: the estimate is far over for
  #    a curve in short segments (it stops the pen at every vertex).
  if (!defined $tv && defined $dry_secs) {
    $tv = int($dry_secs * 1.05 + 10);
    printf "paint: the dry run took %d s -> exposure -tv=%d\n", $dry_secs, $tv;
  }
  unless (defined $tv) {
    my $est = is_svg_file($file) ? estimate_svg_seconds($file) : estimate_plot_seconds($file);
    my $secs = $est ? $est->{secs} : 0;
    $tv = ($est && !$est->{unknown}) ? int($secs * 1.05 + 9) : int($secs * 1.3 + 6);
    printf "paint: estimated drawing %.0f s -> exposure -tv=%d\n", $secs, $tv;
  }
  # -av= goes to every frame: the dark frame must match the lit one, and the
  # grid is then lit for the same aperture (-grid= if it is not).
  my $av_opt = defined $av ? " -av=$av" : '';
  my $snap_opts = "-lights -focus=$focus -tv=$tv$av_opt" . ($raw ? ' -dng' : '');
  if (defined $given_dark) {
    # The dark frame must match the lit one's exposure; the camera says what
    # it was (bulb frames carry their real length). Only a warning: an A570
    # JPEG caps the value at 15 s.
    my $et = `exiftool -s3 -ExposureTime '$given_dark' 2>/dev/null`;
    chomp $et;
    if ($et =~ m{^(\d+)/(\d+)$}) { $et = $1 / $2; }
    if ($et =~ /^\d*\.?\d+$/ && $et > 15 && abs($et - $tv) > 0.05 * $tv) {
      printf "paint: WARNING — the dark frame was %.0f s, this exposure is %s s: subtract with care\n", $et, $tv;
    }
    if (defined $av) {
      my $fn = `exiftool -s3 -FNumber '$given_dark' 2>/dev/null`;
      chomp $fn;
      if ($fn =~ /^\d*\.?\d+$/ && abs($fn - $av) > 0.05 * $av) {
        print "paint: WARNING — the dark frame was shot at f/$fn, this one at f/$av: subtract with care\n";
      }
    }
  }

  # 3. Lit, then dark — the same exposure with the plotter parked. A dark
  #    frame shot while re-running the plot with the LED off (-movingdark, the
  #    old way) could not be told from a parked one: against two parked frames
  #    it differed by less than they differed from each other (v2.plot, 45 s,
  #    2026-10-05). Parked halves the wear and the noise.
  cmd_brightness($pct) if defined $pct;
  my $before = $last_image // '';
  cmd_plotsnap($file, $snap_opts);
  my $lit = $last_image;
  if (!$lit || $lit eq $before) { print "paint: lit frame failed — stopping.\n"; cmd_brightness($old_brightness); return; }
  my $dark;
  if ($darklib) {
    my ($t_lit, $iso) = frame_exposure($lit);
    $dark = darklib_pick($t_lit // $tv, $iso);
    if ($dark) {
      my ($t) = frame_exposure($dark);
      printf "paint: no dark frame shot — the library's %s (%g s), scaled to this one\n", $dark, $t;
    }
  }
  if ($dark) {
    # from the library, above
  } elsif (defined $given_dark) {
    $dark = $given_dark;
    print "paint: no dark frame shot — using $dark\n";
  } else {
    cmd_led_blackout(1);
    cmd_plotsnap($file, $snap_opts, !$movingdark);
    cmd_led_blackout(0);
    $dark = $last_image;
    if (!$dark || $dark eq $lit) { print "paint: dark frame failed — stopping (lit frame kept: $lit).\n"; cmd_brightness($old_brightness); return; }
    if ($darklib) {
      my $kept = darklib_add($dark);
      print "paint: no library frame fitted; this dark frame joins it as $kept\n" if $kept;
    }
  }

  # 4. Calibration grid in the same homing (the map is only as good as that).
  my $cal;
  #    The grid's brightness is set for the LED as it is now, behind its ND
  #    filter and diffuser: the JPEG fit wants the lines near clipping, the raw
  #    fit works well below that (4 served for both before the filter, and
  #    does again with the LED bare: -grid=4).
  unless ($nocal) {
    cmd_brightness($grid // ($raw ? 30 : 100));
    cmd_plotsnap('cal_colour.plot', "-lights -focus=$focus$av_opt" . ($raw ? ' -dng' : ''));
    $cal = $last_image if $last_image && $last_image ne $dark;
    print "paint: calibration grid failed — using the previous cal_map.json.\n" unless $cal;
  }
  cmd_brightness($old_brightness);
  cmd_home();

  # 4a. What paint itself knows goes on the lit frame's note, before the
  #     processing puts the note into the pictures.
  my @said;
  push @said, sprintf("paint: brightness from -ed=%g at %d mm/min, %s", $ed, $ed_feed,
                      $nodry ? "no dry run" : "dry run peak $ed_peak%") if defined $ed;
  push @said, "paint: " . ($raw ? "raw route" : "JPEG route");
  if ($cal) { (my $g = $cal) =~ s{.*/}{}; push @said, sprintf("grid: %s at brightness %g%%", $g, $grid // ($raw ? 30 : 100)); }
  else      { push @said, "grid: none, the map already there was used"; }
  add_to_frame_note($lit, @said);

  # 5. Process: subtract, fit, warp, show.
  my $bin = $FindBin::Bin;
  if ($raw) {
    (my $ls = $lit) =~ s/\.jpe?g$//; (my $ds = $dark) =~ s/\.jpe?g$//;
    my @args = ("$bin/py/raw_paint.py", $ls, $ds);
    if ($cal) { (my $cs = $cal) =~ s/\.jpe?g$//; push @args, $cs; }
    print "paint: processing the raw frames...\n";
    my $out_txt = `@{[ join ' ', map { "'$_'" } @args ]} 2>&1`;
    print $out_txt;
    my ($res) = $out_txt =~ /^RESULT (\S+)$/m;
    unless ($res && -f $res) { print "paint: raw processing failed.\n"; return; }
    while ($out_txt =~ /(\d+) (horizontals|verticals) \(pattern has (\d+)\)/g) {
      print "paint: WARNING — only $1 of $3 $2 seen: part of the bed is out of frame\n" if $1 < $3;
    }
    print "paint: done (raw).\n  lit   $lit (+ raw)\n  dark  $dark (+ raw)\n";
    print "  grid  $cal (+ raw)\n" if $cal;
    print "  RESULT $res\n";
    my $shown = annotated($res, $file);
    show_on_monitor($shown);
    cmd_open_image($shown);
    return;
  }
  (my $sub = $lit) =~ s/\.jpe?g$/_sub.png/;
  (my $out = $sub) =~ s/\.png$/_plot.png/;
  print "paint: subtracting the dark frame...\n";
  if (system("$bin/py/subtract_bg.py", $lit, '--bg', $dark, '--despeckle') != 0) {
    print "paint: subtract_bg.py failed.\n"; return;
  }
  if ($cal) {
    print "paint: fitting the calibration map...\n";
    my $fit = `cd "$bin" && py/calmap.py fit "$cal" --check 2>&1`;
    print $fit;
    if ($? != 0) {
      print "paint: calibration fit failed — keeping the previous cal_map.json.\n";
    } else {
      my ($held) = $fit =~ /held-out[^:]*:\s*RMS\s*([\d.]+)\s*px/;
      print "paint: WARNING — fit is loose (held-out $held px; ~0.25 mm is $held px < 2.5)\n"
        if defined $held && $held > 4;
      while ($fit =~ /(\d+) (horizontals|verticals) \(pattern has (\d+)\)/g) {
        print "paint: WARNING — only $1 of $3 $2 seen: part of the bed is out of frame\n" if $1 < $3;
      }
    }
  }
  print "paint: correcting into plot space...\n";
  if (system("$bin/py/calmap.py", 'apply', $sub, '--map', "$bin/cal_map.json") != 0) {
    print "paint: calmap.py apply failed.\n"; return;
  }
  print "paint: done.\n  lit   $lit\n  dark  $dark\n";
  print "  grid  $cal\n" if $cal;
  print "  sub   $sub\n  RESULT $out\n";
  my $shown = annotated($out, $file);
  show_on_monitor($shown);
  cmd_open_image($shown);
}

# A plot file's `#@` lines are lettering and lines for the finished picture,
# not for the plotter (annotate.py). If $file has any, draw them on a copy of
# the plot-space picture $pic and return that; otherwise, or if it fails, $pic.
sub annotated {
  my ($pic, $file) = @_;
  open(my $fh, '<', $file) or return $pic;
  my $any = grep { /^\s*#@/ } <$fh>;
  close($fh);
  return $pic unless $any;
  my $said = `'$FindBin::Bin/py/annotate.py' '$pic' '$file' 2>&1`;
  my ($res) = $said =~ /^RESULT (\S+)$/m;
  unless ($res && -f $res) { print "paint: annotation failed: $said"; return $pic; }
  print "  NOTES  $res\n";
  return $res;
}

# The HDMI monitors in the room are on whenever someone is there, lights or
# not, so they must be dark while the shutter is open: plotcam's pictures
# (pi/plotdisplay.py) and atelier's aquarium (pi/aquarium.py). A MonitorDark
# object holds them dark while it exists (nested ones share the hold): each
# Pi's program is asked by a file, and the shot waits for its answer and for
# the HDMI output to be off. The request expires on the Pi after an hour, in
# case this side never says it is over. The Pis are asked at the same time.
my $monitor_holds = 0;
# A sub, not a `my` list: the commands run before the file's later lines are reached.
sub monitor_hosts { ('plotcam', 'atelier') }

sub monitor_dark_begin {
  my $token = join '-', $$, time, int(rand(1e6));
  my $remote = <<"SH";
# No monitor plugged in: nothing to light the frame.
[ "\$(cat /sys/class/drm/card0-HDMI-A-1/status 2>/dev/null)" = disconnected ] && exit 0
mkdir -p /tmp/display
echo "\$((\$(date +%s) + 3600)) $token" > /tmp/display/.shoot && mv /tmp/display/.shoot /tmp/display/shoot
for i in \$(seq 100); do
  [ "\$(cat /tmp/display/ack 2>/dev/null)" = "$token" ] && [ "\$(cat /sys/class/drm/card0-HDMI-A-1/dpms)" = Off ] && exit 0
  sleep 0.05
done
# No answer in 5 s: the display program is not running. Blank it here.
sudo sh -c 'echo 1 > /sys/class/graphics/fb0/blank'
[ "\$(cat /sys/class/drm/card0-HDMI-A-1/dpms)" = Off ] && exit 2
exit 1
SH
  my %host;                                   # pid -> host
  for my $h (monitor_hosts()) {
    my $pid = fork();
    if (!defined $pid) { print "monitor: WARNING — cannot ask $h\n"; next; }
    if ($pid == 0) { exec('ssh', $h, $remote) or POSIX::_exit(1); }
    $host{$pid} = $h;
  }
  for my $pid (keys %host) {
    waitpid($pid, 0);
    my $h = $host{$pid};
    my $rc = $? >> 8;
    print "monitor: the display program on $h did not answer; blanked the screen directly\n" if $rc == 2;
    print "monitor: WARNING — could not make sure the $h monitor is dark (it may light the frame)\n"
      if $rc != 0 && $rc != 2;
  }
}

sub monitor_dark_end {
  system('ssh', $_, 'rm -f /tmp/display/shoot') for monitor_hosts();
}

{
  package MonitorDark;
  sub new {
    main::monitor_dark_begin();   # again when nested: renews the hour
    $monitor_holds++;
    return bless {}, shift;
  }
  sub DESTROY { main::monitor_dark_end() if --$monitor_holds == 0; }
}

# A finished picture to the HDMI monitor on atelier (pi/aquarium.py, display_sync.py),
# shown as soon as the room lights are on. In the background: the Pi may be
# slow or away, and the picture is not needed here.
sub show_on_monitor {
  my ($pic) = @_;
  my $pid = fork();
  return unless defined $pid;
  if ($pid == 0) {
    # Fork again and leave at once, so nothing is left to reap.
    exit 0 if fork();
    open(STDOUT, '>', '/dev/null'); open(STDERR, '>', '/dev/null');
    exec("$FindBin::Bin/py/display_sync.py", 'new', $pic) or POSIX::_exit(127);
  }
  waitpid($pid, 0);
}

sub cmd_plotsnap {
  # $static: expose for the same time but leave the plotter parked (paint's dark
  # frame). $file then only names the picture.
  my ($file, $opts, $static) = @_;
  $file = find_plot_file($file);
  $opts //= '';
  return unless validate_snap_opts($opts, 'plotsnap');
  unless (-f $file)        { print "No such plot file: $file\n"; return; }
  $last_plot_file = $file;   # for the lathist header
  # Before the shutter opens or the lights go off: a dark carrier wastes the frame.
  return if !$static && plot_needs_carrier($file) && !check_carrier('plotsnap');
  unless (camera_present()){ print "No camera on USB or plotcam — aborting (playback mode, Enable Remote OFF).\n"; return; }
  # Pre-flight: the camera must be able to run a script NOW. An exposure left
  # running by an interrupted plotsnap makes chdkptp refuse the shot, and that
  # used to surface only after the plot had run in the dark. remote_snap waits
  # a while for a finishing exposure before giving up.
  print "plotsnap: checking the camera is idle...\n";
  if (system("$snap_cmd -probe") != 0) {
    print "plotsnap: aborting — camera busy (nothing plotted).\n";
    return;
  }

  # Exposure duration: an explicit -tv wins; otherwise estimate and pad.
  unless ($opts =~ /(?:^|\s)-tv=/) {
    my $est  = is_svg_file($file) ? estimate_svg_seconds($file)
                                 : estimate_plot_seconds($file);
    my $secs = $est ? $est->{secs} : 0;
    my $tv   = int($secs * 1.3 + 6);   # 30% for accel/air + margins both ends
    printf "plotsnap: estimated plot %.0fs (%.0f mm) -> exposure -tv=%d\n",
           $secs, ($est ? $est->{mm} : 0), $tv;
    print "  WARNING: $est->{unknown} text/arc/svg line(s) not in the estimate; "
        . "pass -tv=<sec> if the frame is truncated.\n"
      if $est && $est->{unknown};
    $opts = "-tv=$tv $opts";
  } else {
    print "plotsnap: using your -tv (ensure it exceeds the plot time + ~6s lead).\n";
  }

  # The room lights are doused (MANUAL_OFF, radar-proof) before the shutter
  # opens, and restored to AUTO after the capture: the default since
  # 2026-11-06 (user); -nolights leaves them alone, -lights is still accepted.
  # Not remoteshoot options, so strip them from $opts. Abort if the lights
  # won't switch off — better than wasting a plot in a lit room.
  my $do_open   = ($opts =~ s/(?:^|\s)-open\b//i)   ? 1 : 0;
  $opts =~ s/(?:^|\s)-lights\b//i;
  my $do_lights = ($opts =~ s/(?:^|\s)-nolights\b//i) ? 0 : 1;
  # Name the frame after the plot unless told otherwise: v2.plot -> v2_001.jpeg.
  # Below full brightness the level goes in too, as the sweep files spell it:
  # `brightness 3` -> no_exit_b03_001.jpeg (a fraction: 2.5 -> b2p5).
  # A blackout run is a background frame, so label it: v2_002_dark.jpeg.
  unless ($opts =~ /(?:^|\s)-name=/) {
    (my $stem = $file) =~ s{.*/}{};
    $stem =~ s/\.[^.]+$//;
    $stem =~ s/[^A-Za-z0-9_-]+/_/g;
    $stem = "plot$stem" unless $stem =~ /^[A-Za-z0-9]/;
    if ($led_brightness != 100) {
      (my $b = sprintf($led_brightness == int($led_brightness) ? '%02d' : '%g',
                       $led_brightness)) =~ s/\./p/;
      $stem .= "_b$b";
    }
    $opts .= " -name=$stem";
  }
  $opts .= ' -dark' if $led_blackout;
  my $monitor = MonitorDark->new;   # dark until this returns
  if ($do_lights) {
    print "plotsnap: switching lights off and confirming darkness (camera light meter)...\n";
    unless (ensure_dark()) {
      print "plotsnap: aborting — room did not go dark.\n";
      cmd_lights('auto');   # leave the controller in a sane state
      return;
    }
  }

  my $LEAD = 6;                         # seconds for the shutter to open first (700D: ~3.6 s, 2026-11-04)
  my $run_secs;
  my $tmp  = "/tmp/plotsnap.$$";
  my $pid  = fork();
  unless (defined $pid) {
    print "plotsnap: fork failed: $!\n";
    cmd_lights('auto') if $do_lights;
    return;
  }
  if ($pid == 0) {                      # child: open the shutter (blocks for -tv)
    exec("$snap_cmd $opts > $tmp 2>&1");
    exit 127;
  }
  if ($static) {
    print "plotsnap: shutter opening, plotter parked — waiting for the exposure/download to finish...\n";
  } else {
    printf "plotsnap: shutter opening, drawing starts in %ds...\n", $LEAD;
    select(undef, undef, undef, $LEAD);
    %peak_log = ();
    my $t0 = time();
    cmd_run($file);
    $run_secs = time() - $t0;
    print "plotsnap: plot done — waiting for the exposure/download to finish...\n";
  }
  waitpid($pid, 0);
  if ($do_lights) { print "plotsnap: restoring lights...\n"; cmd_lights('auto'); }
  my $out = '';
  if (open(my $f, '<', $tmp)) { local $/; $out = <$f>; close $f; }
  unlink $tmp;
  my $p = snap_path($out // '');
  if ($p) {
    $last_image = $p; print "plotsnap saved: $p\n";
    write_frame_note($p, $file, $opts, $run_secs) unless $static || $led_blackout;
    cmd_open_image($p) if $do_open;
    show_on_monitor($p) unless $static || $led_blackout || $in_paint;
  }
  else    { chomp(my $o = $out // ''); print "plotsnap: capture failed: $o\n"; }
}

# The note beside a frame: FRAME.txt next to FRAME.jpeg, how the picture was
# made, as `key: value` lines, and the same inside the JPEG (its EXIF
# description). subtract_bg.py and raw_paint.py put it inside the pictures
# they make from the frame (see picmeta.py). Energy density is only given for
# a plot drawn with dynamic brightness: without it the light per millimetre
# follows the pen's speed, and there is no one figure.
sub write_frame_note {
  my ($frame, $file, $opts, $run_secs) = @_;
  (my $note = $frame) =~ s/\.jpe?g$/.txt/i or return;
  (my $name = $frame) =~ s{.*/}{};
  my $stamp = sub { strftime('%Y-%m-%d %H:%M:%S', localtime($_[0])) };
  my @l = (sprintf("plot: %s (modified %s)", $file, $stamp->((stat $file)[9])),
           "frame: $name",
           "shot: " . $stamp->(time()),
           sprintf("brightness: %g%% (LED value %d of 255 for a colour value of 255)",
                   $led_brightness, int(255 * $led_brightness / 100 + 0.5)));
  my (%seen, @colours);
  if (!is_svg_file($file) && open(my $fh, '<', $file)) {
    while (my $c = <$fh>) {
      next unless $c =~ /^\s*colou?r\s+(\d+)\s+(\d+)\s+(\d+)/i;
      push @colours, "$1 $2 $3" unless $seen{"$1 $2 $3"}++;
    }
    close($fh);
  }
  push @l, "colours: " . (@colours > 8 ? scalar(@colours) . " different" : join(', ', @colours)) if @colours;
  my $feed = $peak_log{feed} // $feed_rate;
  push @l, "speed: $feed mm/min";
  if ($peak_log{ms}) {
    push @l, sprintf("dynamic: on, peak %d%% of the speed, mean %.0f%%, drawing %.1f s",
                     $peak_log{peak}, $peak_log{mean_ms} / $peak_log{ms}, $peak_log{ms} / 1000);
    push @l, sprintf("ed: %.3g (brightness per 1000 mm/min, at the peak speed)",
                     $led_brightness * 1000 / ($feed * $peak_log{peak} / 100)) if $peak_log{peak};
  } else {
    push @l, "dynamic: off";
  }
  push @l, "plot time: $run_secs s" if defined $run_secs;
  my @exp;
  push @exp, "$1 s"        if $opts =~ /(?:^|\s)-tv=(\S+)/;
  push @exp, "focus $1 mm" if $opts =~ /(?:^|\s)-focus=(\d+)/;
  push @l, "exposure: " . join(', ', @exp) if @exp;
  chomp(my $cam = `'$FindBin::Bin/py/picmeta.py' exif '$frame' 2>/dev/null` // '');
  push @l, $cam if $cam =~ /\S/;
  chomp(my $rev = `git -C '$FindBin::Bin' describe --always --dirty 2>/dev/null` // '');
  push @l, "software: plotter.pl at $rev" if $rev =~ /\S/;
  if (open(my $out, '>', $note)) { print $out map { "$_\n" } @l; close($out); }
  else { print "plotsnap: could not write $note: $!\n"; return; }
  note_into_jpeg($frame);
}

# The note also goes inside the frame's JPEG, as its EXIF description.
sub note_into_jpeg {
  my ($frame) = @_;
  system("$FindBin::Bin/py/picmeta.py", 'jpeg', $frame) == 0
    or print "plotsnap: the note is beside $frame but did not go into it\n";
}

# More lines on the end of a frame's note.
sub add_to_frame_note {
  my ($frame, @lines) = @_;
  (my $note = $frame) =~ s/\.jpe?g$/.txt/i or return;
  open(my $out, '>>', $note) or return;
  print $out map { "$_\n" } @lines;
  close($out);
  note_into_jpeg($frame);
}

# 'pen' or 'led' if the SVG declares a PlotterDraw mode in its header comment,
# else undef. Read by both the plotter and the time estimate, so they agree on
# the feed rate the plot will run at.
sub svg_declared_mode {
  my ($file) = @_;
  open(my $fh, '<', $file) or return undef;
  my $header = '';
  while (my $line = <$fh>) {
    $header .= $line;
    last if length($header) > 512 || $header =~ /<(?:path|g|rect|circle)/;
  }
  close $fh;
  return $header =~ /plotter-mode:\s*(pen|led)/i ? lc $1 : undef;
}

# load_svg options for a given `svg FILE [fit|scale N]` — one place, so the
# estimate parses the drawing exactly as the plot will.
sub svg_load_opts {
  my ($mode, $scale_val) = @_;
  my %opts = (tolerance => $arc_tolerance);
  if (defined $mode && $mode eq 'fit') {
    $opts{mode} = 'fit';
    if ($paper_set) {
      $opts{area_w} = abs($paper_max_x - $paper_min_x);
      $opts{area_h} = abs($paper_max_y - $paper_min_y);
    }
  } elsif (defined $mode && $mode eq 'scale') {
    $opts{mode}  = 'scale';
    $opts{scale} = $scale_val + 0;
  }
  return %opts;
}

sub cmd_svg {
  my ($file, $mode, $scale_val) = @_;
  $file = find_plot_file($file);
  if (! -f $file) {
    print "File not found: $file\n";
    return;
  }

  # Auto-detect pen/LED mode from PlotterDraw comment at top of file.
  if (my $detected = svg_declared_mode($file)) {
    my $prev = $doing_light_pen ? 'led' : 'pen';
    if ($detected ne $prev) {
      $doing_light_pen = ($detected eq 'led') ? 1 : 0;
      $feed_rate       = $doing_light_pen ? 25000 : 3000;
      print "SVG mode: $detected (was $prev), speed $feed_rate mm/min\n";
    }
  }

  return if $doing_light_pen && !$led_blackout && !check_carrier('svg');

  my %opts = svg_load_opts($mode, $scale_val);

  my $cmds;
  eval { $cmds = load_svg($file, %opts); };
  if ($@) {
    print "SVG error: $@\n";
    return;
  }

  my $moves = grep { $_->{cmd} eq 'moveto' } @$cmds;
  my $draws = grep { $_->{cmd} eq 'drawto' } @$cmds;
  my $colours = grep { $_->{cmd} eq 'colour' } @$cmds;
  printf "SVG: %d commands (%d moveto, %d drawto, %d colour)\n",
         scalar @$cmds, $moves, $draws, $colours;

  my $t0 = time();
  for my $c (@$cmds) {
    if ($c->{cmd} eq 'moveto') {
      cmd_moveto($c->{x}, $c->{y});
    } elsif ($c->{cmd} eq 'drawto') {
      cmd_drawto($c->{x}, $c->{y});
    } elsif ($c->{cmd} eq 'colour' && $doing_light_pen) {
      if ($c->{colour} =~ /^#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})$/) {
        cmd_colour(hex($1), hex($2), hex($3));
      }
    }
  }
  cmd_penup() if $pen_is_down;
  flush_pipeline();
  my $elapsed = time() - $t0;
  printf "SVG plot complete (%d:%02d elapsed)\n", int($elapsed / 60), $elapsed % 60;
}

sub cmd_svg_info {
  my ($file) = @_;
  $file = find_plot_file($file);
  if (! -f $file) {
    print "File not found: $file\n";
    return;
  }
  my $info;
  eval { $info = svg_info($file); };
  if ($@) {
    print "SVG error: $@\n";
    return;
  }
  printf "File:     %s\n", $info->{file};
  printf "SVG size: %.1f x %.1f mm\n", $info->{svg_w}, $info->{svg_h};
  printf "Commands: %d (%d moveto, %d drawto)\n",
         $info->{commands}, $info->{moveto}, $info->{drawto};
  if (defined $info->{width}) {
    printf "Bounds:   (%.2f, %.2f) to (%.2f, %.2f)\n",
           $info->{min_x}, $info->{min_y}, $info->{max_x}, $info->{max_y};
    printf "Extent:   %.2f x %.2f mm\n", $info->{width}, $info->{height};
  }
}

sub cmd_text {
  my ($x, $y, $height, $text, $font) = @_;
  $font //= 'simplex';
  my $cmds = render_text($text, x => $x + 0, y => $y + 0, height => $height + 0,
                         font => $font);
  my $moves = grep { $_->{cmd} eq 'moveto' } @$cmds;
  my $draws = grep { $_->{cmd} eq 'drawto' } @$cmds;
  printf "Text: %d commands (%d moveto, %d drawto)\n",
         scalar @$cmds, $moves, $draws;

  my $t0 = time();
  for my $c (@$cmds) {
    if ($c->{cmd} eq 'moveto') {
      cmd_moveto($c->{x}, $c->{y});
    } elsif ($c->{cmd} eq 'drawto') {
      cmd_drawto($c->{x}, $c->{y});
    }
  }
  cmd_penup() if $pen_is_down;
  my $elapsed = time() - $t0;
  printf "Text plot complete (%d:%02d elapsed)\n",
         int($elapsed / 60), $elapsed % 60;
}

sub cmd_penup {
  if ($doing_light_pen) {
    # No G4: M62 attaches directly to the G1 block, so ESP-NOW propagates during
    # the preceding G0 air move. [C:0] from M5 propagates before pipeline flush.
    queue_motion('M5');
  } else {
    # Ink pen: de-energise solenoid via M5, then dwell for LED and pen lift settling.
    send_line('M5');
    send_line("G4 P$colour_delay") if @colour_rgb && $colour_delay > 0;
    send_line("G4 P$pen_up_delay") if $pen_up_delay > 0;
  }
  $pen_is_down = 0;
  $last_colour_p = -1;
}

# ---- Colour -----------------------------------------------------------------

# Encode 8-bit R,G,B into a 24-bit P value for M62.
# P = R<<16 | G<<8 | B.  Range 0-16777215.
sub encode_colour {
  my ($r, $g, $b) = @_;
  return ($r << 16) | ($g << 8) | $b;
}

# The current colour as it goes to the carrier: scaled by `brightness`, encoded.
sub shown_colour_p {
  return encode_colour(map { int($_ * $led_brightness / 100 + 0.5) } @colour_rgb);
}

# Brightness scales at emit time, so plot files keep full-range colours and the
# same file can be shot at several levels. Forces a re-emit: a level change must
# reach the carrier even when the colour itself is unchanged.
sub cmd_brightness {
  my ($pct) = @_;
  if ($pct > 100) { print "Brightness is 0-100%\n"; return; }
  $led_brightness = $pct;
  $last_colour_p = -1;
  print "Brightness: $led_brightness%\n";
}

sub cmd_colour {
  my ($r, $g, $b) = @_;
  for ($r, $g, $b) { $_ = 0 if $_ < 0; $_ = 255 if $_ > 255; }
  # Only invalidate when the colour actually changes. Re-setting the same colour
  # (per-segment SVG paths all carrying the same stroke=) must not force a
  # redundant M62 — that defeats the dedup in emit_colour and re-cycles the LED
  # mid-stroke.
  my $changed = !@colour_rgb
              || $colour_rgb[0] != $r || $colour_rgb[1] != $g || $colour_rgb[2] != $b;
  @colour_rgb = ($r, $g, $b);
  $last_colour_p = -1 if $changed;  # force re-emit on next draw.
  printf "Colour RGB(%d,%d,%d)%s → P%d\n", $r, $g, $b,
         ($led_brightness == 100 ? '' : " at $led_brightness%"), shown_colour_p();
}

sub cmd_colour_off {
  @colour_rgb = ();
  $last_colour_p = -1;
  if ($doing_light_pen && $pen_is_down) {
    cmd_penup();
    flush_pipeline();
  }
  print "Colour off\n";
}

# The carrier's LED type, kept in its flash: `led type NAME` sets it, `led type`
# asks. A plot file may carry the line, so it can never be shot with the
# carrier set for another LED. The names, and the type numbers the carrier's
# LED_TYPES table uses:
#   apa106   0  APA-106, R,G,B (the 8 mm one)
#   sk6812   1  SK6812 RGBW, G,R,B,W; grey lights its white element
sub cmd_led_type {
  my ($arg) = @_;
  # Here, not at file level: the main loop runs before file-level code below it.
  my @LED_TYPE_NAMES = ('APA-106', 'SK6812 RGBW');
  my %LED_TYPE_BY_NAME = (apa106 => 0, 'apa-106' => 0, sk6812 => 1, rgbw => 1, 'sk6812rgbw' => 1);
  my $want;
  if (defined $arg) {
    my $key = lc $arg;
    $want = ($key =~ /^\d+$/ && $key < @LED_TYPE_NAMES) ? $key + 0 : $LED_TYPE_BY_NAME{$key};
    unless (defined $want) {
      print "led type: '$arg'? Known: apa106, sk6812\n";
      return 0;
    }
  }
  my ($alive) = carrier_alive();
  unless ($alive) {
    my $woke = wake_carrier('led type');
    unless ($woke) {
      print "led type: the pen carrier is not answering — the LED type is NOT set\n";
      return 0;
    }
  }
  flush_pipeline();
  send_raw(defined $want ? "!ledtype $want\n" : "!ledtype\n");
  my $deadline = time() + 3;
  my $got;
  while (time() < $deadline && !defined $got) {
    if ($recv_buf =~ s/^ledtype\s+(\S+)\r?\n//m) { $got = $1; last; }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      last if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  if (!defined $got || $got eq 'none') {
    print "led type: no answer from the carrier (asleep, or firmware without LED types)\n";
    return 0;
  }
  my $name = $LED_TYPE_NAMES[$got] // "type $got";
  if (defined $want && $got != $want) {
    print "led type: asked for $LED_TYPE_NAMES[$want], the carrier says $name\n";
    return 0;
  }
  print "LED type: $name\n";
  return 1;
}

# Blackout: run a plot with the LED never lit, so the frame captures only ambient
# light and can be subtracted from a coloured exposure of the same plot. Every M62
# is still emitted (as black) to keep motion timing identical between the two runs.
# Affects plotting only — `testled` stays live as a manual diagnostic.
sub cmd_led_blackout {
  my ($off) = @_;
  $led_blackout = $off ? 1 : 0;
  $last_colour_p = -1;   # force the next emit so the change takes effect at once.
  if ($led_blackout) {
    send_line('!espnow:0');   # extinguish now if a colour is currently showing.
    print "LED off (blackout) — plots run dark, for a background frame\n";
  } else {
    print "LED on — plots emit their colours normally\n";
  }
}

# Show the current colour on the carrier: emit M62 P{rgb} if it has changed since
# the last shown colour. The M62 attaches to the next motion block, so [C:n] fires
# as drawing begins. In light pen mode the colour was already preloaded during the
# preceding air move (see emit_preload), so this just enables an already-armed
# colour — no dark start, no dwell. Ink pen keeps the dwell to let its (decorative)
# LED settle.
sub emit_colour {
  return unless @colour_rgb;
  my $p = shown_colour_p();
  return if $p == $last_colour_p;
  # Blackout: emit the M62 anyway, but black. Suppressing it instead would change
  # the command count and so the plot's timing, which would make the resulting
  # background frame no longer subtractable from a lit one. Dedup still keys off
  # the real colour, so the number of M62s is identical either way. The zero is
  # padded to the real value's width because queue_motion's RX-buffer budget
  # blocks on byte length — a shorter line would run the pipeline deeper here
  # than in the lit run and shift the feed timing.
  queue_motion($led_blackout ? sprintf("M62 P%0*d", length($p), 0) : "M62 P$p");
  if (!$doing_light_pen && $colour_delay > 0) {
    queue_motion("G4 P$colour_delay");
  }
  $last_colour_p = $p;
}

# Light pen: arm the current colour on the carrier with the LED kept dark (preload
# bit set), queued before an air move so it propagates over ESP-NOW while travelling.
# The stroke-start emit_colour then just enables it. No dark start, and no radial
# line (the LED stays off through the air move). No-op for ink pen / no colour set.
sub emit_preload {
  return unless $doing_light_pen && @colour_rgb;
  # Blackout arms black instead of the colour. No padding needed: the preload bit
  # puts the value over 16777215 either way, so the line length is unchanged.
  my $p = ($led_blackout ? 0 : shown_colour_p()) | COLOUR_PRELOAD_BIT;
  queue_motion("M62 P$p");
}

# Host-side pause: let the machine finish, then wait. The flush matters — the
# pipeline can hold several queued moves, so without it the sleep would overlap
# the motion still in flight and the wait would start early. For a dwell that
# belongs to the motion itself (pen settling), use G4 via `! G4 P<n>` instead,
# which blocks in GRBL's planner rather than here.
sub cmd_delay {
  my ($secs) = @_;
  flush_pipeline();
  printf "Waiting %gs...\n", $secs;
  select(undef, undef, undef, $secs);
}

# Dynamic brightness: the LED follows the pen's real speed, so a line gets the
# same light per millimetre through its ramps and bends, and none while the pen
# is stopped. GRBL does the work: in laser mode ($32=1) under M4 it scales the
# PWM on D11 by actual speed / programmed feed; the bridge reads that (`!dyn on`)
# and the carrier scales its colour by it. So a colour's brightness is what it
# has at the programmed feed, and a plot that never reaches its `speed` is
# drawn dimmer throughout.
# D11 is also the ink pen's solenoid, which must be UNPLUGGED: it would be held
# at up to 100%. Laser mode also breaks the ink pen's pull-in, so $32 is 1 only
# while this is on. `dynamic off` always sends $32=0, to clear one left behind.
sub cmd_dynamic {
  my ($on) = @_;
  if ($on && !$doing_light_pen) {
    print "dynamic: light pen only (`light pen on` first)\n";
    return;
  }
  cmd_penup() if $pen_is_down;
  send_line('G4 P0.1');               # GRBL refuses $32 unless it is idle
  unless (send_line($on ? '$32=1' : '$32=0')) {
    print "dynamic: GRBL refused \$32 — nothing changed\n";
    return;
  }
  my $was_on = $dynamic;
  send_line($on ? '!dyn on' : '!dyn off');
  $dynamic = $on ? 1 : 0;
  print $on ? "Dynamic brightness ON — the solenoid must be unplugged\n"
            : "Dynamic brightness off\n";
  # The speed report covers one dynamic section: cleared as it starts, printed
  # as it ends.
  if ($on) { peak_report(); } elsif ($was_on) { print_peak(); }
}

# How fast the pen really went, from the bridge (`!peak`): it reads the same
# speed fraction dynamic brightness uses, so this only measures strokes drawn
# with dynamic on. Reading it clears it. Returns (peak %, mean %, drawing ms,
# [ms in each tenth of the programmed feed]), or () if the bridge said nothing.
sub peak_report {
  flush_pipeline();
  send_raw("!peak\n");
  my $deadline = time() + 3;
  while (time() < $deadline) {
    while ($recv_buf =~ s/^(.*?)\r?\n//) {
      my $line = $1;
      if ($line =~ /^peak (\d+)%\s+mean (\d+)%\s+moving (\d+) ms\s+hist((?: \d+)+)/) {
        return ($1, $2, $3, [split ' ', $4]);
      }
    }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      return () if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  return ();
}

sub print_peak {
  my ($peak, $mean, $ms, $hist) = peak_report();
  unless (defined $peak) { print "peak: no answer from the bridge\n"; return; }
  unless ($ms) { print "peak: nothing drawn with dynamic on since the last report\n"; return; }
  $peak_seen = $peak if !defined $peak_seen || $peak > $peak_seen;
  $peak_log{peak} = $peak if $peak > ($peak_log{peak} // 0);
  $peak_log{ms} += $ms; $peak_log{mean_ms} += $mean * $ms; $peak_log{feed} = $feed_rate;
  printf "Speed while drawing (%.1f s): peak %d%% of %d mm/min, mean %d%%\n",
         $ms / 1000, $peak, $feed_rate, $mean;
  print "  share of drawing time at each tenth of the programmed feed:\n   ";
  for my $i (0 .. $#$hist) {
    my $share = 100 * $hist->[$i] / $ms;
    printf " %d-%d%%: %.0f%%", $i * 10, $i * 10 + 10, $share if $share >= 0.5;
  }
  print "\n";
  if ($peak < 90) {
    my $speed = 100 * ceil($feed_rate * $peak / 100 / 100);
    print "  `speed $speed` would draw this as fast, at the colour's full brightness.\n";
  }
}

sub cmd_pendown {
  if ($doing_light_pen) {
    # Light pen: pipeline M3 S0 — no solenoid, so no need to block for the ok.
    # G4 is not needed: GRBL's planner ensures M3 S0 executes before the next motion.
    # Dynamic brightness: M4 S255, GRBL's speed-scaled output (see cmd_dynamic).
    queue_motion($dynamic ? 'M4 S255' : 'M3 S0');
  } else {
    # Ink pen: pull-in solenoid at full power, sync, then drop to hold current.
    send_line('M3 S255');
    send_line("G4 P$pen_down_delay");  # sync required even at 0ms
    send_line('M3 S100');
  }
  $pen_is_down = 1;
}

# ---- Communication ----------------------------------------------------------

# Drain one ok from the pipeline. Skips push messages (status, feedback).
# Returns 1 on ok, 0 on error/alarm/timeout.
sub drain_one_ok {
  my $deadline = time() + 30;
  while (time() < $deadline) {
    while ($recv_buf =~ s/^(.*?)\r?\n//) {
      my $line = $1;
      print "  << $line\n";
      log_msg("  << $line\n");
      if ($line eq 'ok') { shift @pipeline_lens; return 1; }
      if ($line =~ /^error/ || $line =~ /^ALARM/) { shift @pipeline_lens; return 0; }
    }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      die "Connection lost\n" if !defined $n || $n == 0;
      $recv_buf .= $chunk;
    }
  }
  print "  !! Timeout waiting for ok\n";
  log_msg("  !! Timeout waiting for ok (pipeline depth: " . scalar(@pipeline_lens) . ")\n");
  shift @pipeline_lens;
  return 0;
}

sub flush_pipeline {
  while (@pipeline_lens) {
    last unless drain_one_ok();
  }
}

# Send a motion command (G0/G1/M62) with pipelining. Blocks only when the
# next command would overflow GRBL's RX buffer.
sub queue_motion {
  my ($line) = @_;
  my $len = length($line) + 1;  # +1 for \n
  my $inflight_bytes = 0;
  $inflight_bytes += $_ for @pipeline_lens;
  while (@pipeline_lens && $inflight_bytes + $len > $grbl_rx_buffer) {
    drain_one_ok();
    $inflight_bytes = 0;
    $inflight_bytes += $_ for @pipeline_lens;
  }
  print $sock "$line\n";
  print "  >> $line\n";
  log_msg("  >> $line\n");
  push @pipeline_lens, $len;
}

# Send a command synchronously: flush any pipelined motions first, then
# send and wait for ok. Used for modal commands (M3, M5, G4, $H, etc.).
sub send_line {
  my ($line) = @_;
  flush_pipeline();
  print $sock "$line\n";
  print "  >> $line\n";
  log_msg("  >> $line\n");
  push @pipeline_lens, length($line) + 1;
  drain_one_ok();
}

sub send_raw {
  my ($data) = @_;
  print $sock $data;
}

sub drain_responses {
  # $collect (optional) is an arrayref: every line printed is also pushed onto it,
  # so a caller like `lathist` can write the trace to a file as well as show it.
  my ($wait_secs, $collect) = @_;
  my $deadline = time() + $wait_secs;
  # Hard ceiling. The deadline below is extended on every arrival, so a peer that
  # keeps talking would otherwise hold this loop for ever — which is exactly how
  # `lathist` hung after printing its trace. Never wait more than this.
  my $hard = time() + $wait_secs + 15;
  while (time() < $deadline && time() < $hard) {
    while ($recv_buf =~ s/^(.*?)\r?\n//) {
      print "  << $1\n";
      push @$collect, $1 if $collect;
    }
    my @ready = $sel->can_read(0.1);
    if (@ready) {
      my $n = sysread($sock, my $chunk, 1024);
      last if !defined $n || $n == 0;
      $recv_buf .= $chunk;
      $deadline = time() + 0.2;  # extend if data still coming
      $deadline = $hard if $deadline > $hard;
    }
  }
  print "  (drain: hit the ${\ ($wait_secs + 15) }s ceiling — peer still sending)\n"
    if time() >= $hard;
  if ($recv_buf =~ /\S/) {
    print "  << $recv_buf\n";
    push @$collect, $recv_buf if $collect;
    $recv_buf = '';
  }
}

sub print_help {
  print <<'END';
Commands:
  home            LED off, home axes and set work origin; wakes the carrier
  load            Wake the carrier, go to 20,20 and leave the LED lit dim green, for
                  fitting the aperture (`penup` switches it off)
  restart         Reload plotter.pl in place (after it changes; resets GRBL)
  paint FILE [N] [-tv=S] [-av=F] [-focus=MM] [-nocal] [-raw] [-grid=N] [-dark=FRAME]
                  Finished light-paint picture: home, lit + dark frames at brightness N,
                  calibration grid, subtract, undistort, open (-av=F: aperture for
                  every frame, else the camera's own; -nocal: reuse last map;
                  -grid=N: the grid's brightness, default 100 or 30 with -raw, for the
                  LED behind its ND filter and diffuser; bare it wants 4; -dark=FRAME:
                  shoot no dark frame, subtract an earlier one of the same exposure;
                  -darklib (with -raw): use the library's dark frame, scaled, or shoot
                  one and add it — for trials; finished pictures want a fresh one)
  paint FILE -ed=X [-nodry] ...
                  The same for a `dynamic on` plot, at the brightness that gives energy
                  density X (brightness per 1000 mm/min): a dry run with the LED off
                  finds the peak speed first (-nodry: take the file's `speed` as the peak)
  moveto X Y      Pen up, rapid move to X,Y
  moveby dX dY    Pen up, rapid move relative to current position
  drawto X Y      Pen down, draw line to X,Y
  drawby dX dY    Pen down, draw line relative to current position
  arc CX CY ANGLE Arc from current pos around centre, ANGLE degrees (+CCW)
  penup           Lift pen
  pendown         Lower pen
  delay N         Wait N seconds after the machine has caught up (host-side;
                  for a dwell inside the motion use `! G4 P<n>`)
  platen on/off   Enable/disable electrostatic platen (M8/M9 → Nano A3)
  light pen on/off  Switch between light pen (no solenoid) and ink pen mode
  dynamic on/off  Light pen: LED brightness follows the pen's real speed (even
                  exposure through ramps and bends). UNPLUG THE SOLENOID first.
                  `dynamic off` prints how fast the pen really went.
  peak            That speed report now: peak and mean speed while drawing with
                  dynamic on, as percent of `speed`, since the last report
  speed N         Set draw speed (mm/min, default 3000)
  tolerance N     Set arc polygonisation tolerance (mm, default 0.5)
  curveaccel N    Curvature-aware feed: v=sqrt(N*R) mm/s, slows tight curves (off=constant)
  curvefeedfloor N  Min feed for curvature-aware mode (mm/min, default 1000)
  penupdelay N    Set pen up dwell before moving (seconds, default 0)
  pendowndelay N  Set pen down pull-in dwell before hold (seconds, default 0)
  colourdelay N   Ink-pen dwell after M62/M5 for ESP-NOW LED update (s, default 0.02;
                  light pen preloads colour during the air move instead)
  linestyle P N   Set line pattern P (e.g. 11100) with N mm/bit; off=solid
  colour R G B    Set LED colour (0-255 each); emits M62 P{n} before each draw
  colour off      Turn LED off
  led type [NAME] The carrier's LED: apa106 (8 mm, RGB) or sk6812 (RGBW); kept in
                  its flash. Bare `led type` asks. Plot files may carry it
  led on|off      Blackout for background frames: off = plots run with the LED dark
                  (same motion and timing, so the frame subtracts from a lit one)
  brightness N    Scale every plotted colour to N% (0-100, default 100; no arg shows it).
                  Plot files keep full-range colours; testled is not scaled
  set min         Set paper origin at current pen position
  set max         Set paper far corner at current pen position
  paper           Show paper calibration
  paper clear     Remove paper calibration
  status          Query GRBL status
  unlock          Clear alarm lock ($X)
  reset           Soft reset GRBL (ctrl-x)
  tune on|off     Antenna tuning: ACK rate to the pen carrier on the bridge OLED
  rxbuf [N]       Bytes kept in flight to GRBL (20 = one cmd at a time, 100 = stream)
  lat             Colour-change latency from the last plot (bridge loop timing)
  lathist [FILE]  Per-[C:n] trace from the last plot; saves alongside the frame
                  plotsnap captured (<stamp>.jpeg -> <stamp>.lat). One shot per plot.
  nano-upload     Release ESP32 TX for Nano USB upload
  esp-reset       Restart ESP32-C3 (reconnect after)
  text X Y "str"           Plot text at X,Y with default 10mm cap height (simplex font)
  text X Y H "str"         Plot text at X,Y with H mm cap height (simplex font)
  text X Y H FONT "str"    Plot text with specified font (simplex, scriptc, scripts)
  svg FILE        Plot SVG file (1:1 mm mapping)
  svg FILE fit    Scale SVG to fit paper (or machine area)
  svg FILE scale N  Scale SVG by factor N
  svg info FILE   Show SVG dimensions and segment count
  run FILE        Run a script file (one command per line, # comments);
                  a .svg file is plotted 1:1 instead
  ! <gcode>       Send raw G-code (e.g. ! M3 S200)
  cam connect [HOST[:PORT]]  Connect to ESP32-C6 USB remote (default 192.168.1.23:8080)
  cam tv N        Set shutter speed index (tv? to list options)
  cam dist N      Set focus distance index (dist? to list options)
  cam shoot       Trigger shutter, then wait for AF light to clear
  cam shoot delay N  Set post-shoot AF delay in seconds (default 3)
  cam status      Show current tv/dist settings
  cam disconnect  Disconnect from camera
  lights on|off|auto  Switch room lights (basic_ota @ .27); off=forced (radar-proof)
  darks S [S ...] Dark frames of S seconds for paint -darklib's library (remote_pics/darks),
                  room dark, LED off, plotter parked
  snap [opts]     Capture via chdkptp to remote_pics/ (e.g. snap -tv=8 -av=8 -focus=800)
  open [file]     Open the last capture (or newest in remote_pics/) in the macOS viewer
  plotsnap FILE [opts]  Run a plot (.plot script or .svg) with the shutter open
                  for it (light painting), the room lights off (-nolights: leave them);
                  exposure auto-sized unless -tv is given. Options (checked
                  before anything runs; unknown ones abort):
END
  print snap_opts_help();
  print <<'END';
  help            Show this help
  quit            Exit

Paper calibration:
  Jog pen to bottom-left corner of paper, type 'set min'.
  Jog pen to top-right corner, type 'set max'.
  moveto/drawto then use paper coordinates (0,0 = min corner).
END
}
