# plotcam — Pi Zero W camera host

The Canon A570 (CHDK) is driven over USB by `chdkptp`. With the camera plugged
into the Mac, `remote_snap` runs chdkptp locally. With the camera plugged into
this Pi instead, `remote_snap` runs the same chdkptp **on the Pi over ssh**: the
frame is written to the Pi's `/tmp` (RAM), copied back to the Mac, and deleted.
`snap`, `plotsnap` and the plot files don't change. A camera on the Mac's USB
always wins, so plugging it back into the Mac is the fallback.

## Hardware

- Raspberry Pi Zero W (v1.1, ARMv6, 512 MB).
- Camera USB lead into the Pi's **data** micro-USB socket (marked `USB`, the one
  nearer the middle — not `PWR`), through a micro-USB OTG adapter.
- Pi powered from its own 5 V supply on `PWR`. The camera stays on its AAs.

## Rebuilding from scratch

### 1. Card (Raspberry Pi Imager, on the Mac)

- Device: Raspberry Pi Zero. OS: Raspberry Pi OS (other) → **Raspberry Pi OS Lite (32-bit)**.
- Edit settings:
  - Hostname `plotcam`; user `pi` (any password); WiFi: your network, and country.
  - Services: enable SSH, **public-key only**, key = contents of `~/.ssh/plotcam_pi.pub`.

### 2. On the Mac (once)

```
ssh-keygen -t ed25519 -N '' -f ~/.ssh/plotcam_pi     # only if the key is missing
```

`~/.ssh/config`:

```
Host plotcam
    HostName plotcam.local
    User pi
    IdentityFile ~/.ssh/plotcam_pi
    IdentitiesOnly yes
    LogLevel ERROR
    ConnectTimeout 8
```

Passwordless sudo for `pi` (asks for the Pi password once):

```
ssh -t plotcam "echo 'pi ALL=(ALL) NOPASSWD:ALL' | sudo tee /etc/sudoers.d/010_pi-nopasswd && sudo chmod 440 /etc/sudoers.d/010_pi-nopasswd"
```

### 3. Packages

```
ssh plotcam 'sudo apt-get update && sudo apt-get install -y build-essential liblua5.2-dev libusb-dev rsync usbutils'
```

### 4. chdkptp

Copy the Mac's tree — **including its patched `lua/` scripts** — and build
(about 20 s on the Zero W):

```
cd ~/projects/chdkptp-build/simonswine
rsync -a --exclude '*.o' --exclude '*.a' --exclude '/chdkptp' --exclude '*.bak*' \
      --exclude config.mk ./ plotcam:chdkptp/
ssh plotcam 'cd chdkptp && printf "IUP_SUPPORT=0\nCD_SUPPORT=0\nPTPIP_SUPPORT=0\nLUA_INCLUDE_DIR=/usr/include/lua5.2\nLUA_LIB=lua5.2\nDEBUG=0\n" > config.mk && make'
```

The long-exposure fixes live in those scripts, not the binary (see memory /
`lua/cli.lua`): the camera's USB-capture timeout (`opts.cap_timeout = 900000`)
and chdkptp's two 15-minute waits. Check they arrived:

```
ssh plotcam 'grep -c "cap_timeout = 900000" chdkptp/lua/cli.lua; grep -c "timeout=900000" chdkptp/lua/cli.lua chdkptp/lua/chdku.lua'
```

Launcher `~/chdkptp/chdkptp.sh` on the Pi:

```
#!/bin/sh
CHDKPTP_DIR=/home/pi/chdkptp
export LUA_PATH="$CHDKPTP_DIR/lua/?.lua;;"
exec "$CHDKPTP_DIR/chdkptp" "$@"
```

USB access for `pi` without root:

```
echo 'SUBSYSTEM=="usb", ATTR{idVendor}=="04a9", MODE="0666"' | sudo tee /etc/udev/rules.d/60-canon-camera.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=usb
```

### 5. Test

```
ssh plotcam lsusb | grep -i canon              # camera seen by the Pi
./remote_snap -probe                           # "camera idle", via the Pi
./remote_snap -name=pitest -tv=1               # a frame, copied back
```

### 6. Read-only card (last)

Power can then be cut at any time without corrupting the card: everything is
written to a RAM overlay and discarded at power-off. chdkptp only writes to
`/tmp`, which is RAM anyway.

```
ssh plotcam 'sudo raspi-config nonint enable_overlayfs && sudo raspi-config nonint enable_bootro && sudo reboot'
```

To change anything later (updates, a new chdkptp), turn it off, reboot, make
the change, turn it back on:

```
ssh plotcam 'sudo raspi-config nonint disable_overlayfs && sudo raspi-config nonint disable_bootro && sudo reboot'
```

## The HDMI monitor: pictures while someone is in the room

A monitor on the Pi's HDMI shows pictures while someone is in the room, lit
or dark, and is dark (HDMI off, monitor in standby) while the camera is
taking a picture and while the room is empty.

- **Shots.** `plotter.pl` holds the monitor dark through every `snap`,
  `plotsnap` and the whole of `paint` (`MonitorDark`): it writes
  `/tmp/display/shoot` ("EXPIRY TOKEN") and waits, before the lights go off
  and the shutter opens, for `plotdisplay.py` to answer with the token in
  `/tmp/display/ack` and for the HDMI output to be off (about 0.3 s). If the
  display program does not answer in 5 s, it blanks the screen itself and
  says so. The hold ends when the shot does; on the Pi it expires after an
  hour in case the Mac never says so.
- **The room.** `pi/plotdisplay.py` (service `plotdisplay`) polls the lights
  controller twice a second: `curl 192.168.1.5/state` →
  `{"mode":"auto","lights":"on","presence":"on"}`. `lights` is what the
  controller last told the lamps and `presence` the radar, followed in every
  mode (both added to `basic_ota.ino` 2026-10-18). On while the lights are on
  or someone is there, so it can be watched in a room darkened by hand
  (`lights off`); dark when the lights went off by themselves (nobody there)
  or the controller cannot be reached for 10 s (a single missed poll, which
  WiFi drops about one in sixty of, keeps the last answer). If the service stops or dies, systemd
  blanks the screen.
- **New shots.** `plotter.pl` sends each plotsnap frame and each paint result
  (`display_sync.py new PICTURE`, in the background) to `/tmp/display/new.fb.gz`
  (RAM). It is shown at once (after the hold), for 10 minutes from when it
  is first on screen, then the slideshow resumes (a test shot would
  otherwise sit there until the lights were cycled). Paint's own lit, dark
  and grid frames are not sent.
- **Slideshow.** The Photos album **Plotter display**, a picture every 15 s,
  shuffled. After the lights go off and on again with no new shot meanwhile,
  the screen goes back to it. `py/display_sync.py album` exports what is new in
  the album, converts it and copies it to the card's read-only layer
  (`/media/root-ro/home/pi/display/slides`, unlocked for the copy only), so it
  survives power cuts; pictures taken out of the album are removed. The launchd
  agent `com.example.plotdisplay` (copy in `com.example.plotdisplay.plist`, installed
  in `~/Library/LaunchAgents`) runs it hourly while the Mac is awake; log in
  `~/Library/Logs/plotdisplay.log`.
- **Format.** The Mac does all the image work: each picture is fitted to the
  framebuffer (1920 × 1080, 16-bit RGB565, read from the Pi), letterboxed on
  black, gzipped. The Pi just writes it to `/dev/fb0`; no packages needed.
  The text console is unbound from the framebuffer so no cursor or login prompt
  is drawn over the pictures.

Installing (or updating) without turning the overlay off — the files go into
both the read-only layer and the running system:

```
scp pi/plotdisplay.py pi/plotdisplay.service plotcam:/tmp/
ssh plotcam 'sudo mount -o remount,rw /media/root-ro
for R in /media/root-ro ""; do
  sudo install -m 755 /tmp/plotdisplay.py $R/usr/local/bin/plotdisplay.py
  sudo install -m 644 /tmp/plotdisplay.service $R/etc/systemd/system/plotdisplay.service
  sudo mkdir -p $R/etc/systemd/system/multi-user.target.wants
  sudo ln -sf /etc/systemd/system/plotdisplay.service $R/etc/systemd/system/multi-user.target.wants/
done
sudo mkdir -p /media/root-ro/home/pi/display/slides && sudo chown -R pi:pi /media/root-ro/home/pi/display
sync; sudo mount -o remount,ro /media/root-ro
sudo systemctl daemon-reload && sudo systemctl restart plotdisplay'
```

A monitor's standby LED can still show in a long exposure: tape over it.

# atelier — Pi 3 B+ aquarium monitor

A second Pi, `atelier` (Raspberry Pi 3 Model B+, Raspberry Pi OS Lite 64-bit,
trixie), plays an aquarium video on its HDMI monitor while someone is in the
room. Not part of the camera chain, but in the same room: its monitor would
light a long exposure, so plotter.pl holds it dark through every shot.

- **Network.** Fixed IP 192.168.1.28 (`nmcli con mod netplan-wlan0-YOURNETWORK
  ipv4.method manual ipv4.addresses 192.168.1.28/24 ipv4.gateway 192.168.1.1
  ipv4.dns "YOUR DNS SERVERS"`). `ssh atelier` (user `pi`, key
  `~/.ssh/plotcam_pi`); password-free sudo in `/etc/sudoers.d/010_pi-nopasswd`.
- **Power.** It needs the official supply: on a weak one it reported
  under-voltage (`vcgencmd get_throttled` = `0x50005`; `0x0` is good).
- **The video.** `/usr/local/share/aquarium/aquarium1080.mp4`, 1920 × 1080,
  30 fps, H.264, 2 min 37 s, 182 MB, built by `pi/make_aquarium1080.sh` from
  Mixkit clips that are free at 1080p (one aquarium, the rest reefs in the
  sea: the Roku's aquarium clips are free at 720p only). The Roku's 720p
  `aquarium.mp4` is beside it, unused.
  **Since 2026-11-08 it plays `aquarium_long.mp4`** when that is there (the
  player takes the first of the two it finds): 23 min 20 s, 1.19 GB,
  6.8 Mbit/s, built by `pi/make_aquarium_long.sh` from the 32 clips in
  `pi/aquarium_clips.tsv` (the eight Mixkit 1080p clips and 24 from Wikimedia
  Commons: jellyfish, aquarium tanks, reefs; Commons' 1080p transcodes, as the
  originals run to 1 GB a minute). Credits (CC BY / BY-SA) in
  `aquarium_credits.txt` beside it. The Pi decodes it at ~43 fps, nothing
  dropped.
- **Playing.** `pi/aquarium.py` (service `aquarium`) starts itself again as
  `aquarium.py --play`, a GStreamer pipeline (`v4l2h264dec ! kmssink`, the
  screen set to the video's size): the hardware decoder's frames go to the
  display without a copy, so 1080p plays with nothing dropped and the CPU
  nearly idle (47 °C). mpv could not: it copies every frame, and dropped about
  1 in 5 at 1080p (none only at 720p). The loop is a segment seek, joined
  with no gap (one frame dropped). Each time it comes on it starts somewhere
  new in the loop. Packages: `gstreamer1.0-tools gstreamer1.0-plugins-good
  gstreamer1.0-plugins-bad python3-gi gir1.2-gstreamer-1.0`
  (`--no-install-recommends`).
- **The room.** It polls the lights controller once a second
  (`192.168.1.5/state`) and plays while `presence` is `on`. Once the radar
  has seen nobody for 30 s, or the controller cannot be reached, the player
  stops and the framebuffer is blanked: HDMI off, monitor in standby. If the
  service stops or dies, systemd blanks the screen.
- **Shots.** plotter.pl's `snap`, `plotsnap` and `paint` ask atelier and
  plotcam together to go dark, by the same handshake as plotcam's display
  (section above): `EXPIRY TOKEN` written to `/tmp/display/shoot`, answered
  with the token in `/tmp/display/ack` once the screen is dark (about 0.15 s).
  The aquarium stays dark until the file goes or an hour passes. A Pi with no
  monitor plugged in (HDMI `disconnected`) is taken as dark. If the player
  freezes (no new frame for 5 s) it logs `picture stalled` and restarts.
- **New shots.** Since 2026-11-02 the latest picture is shown here (the
  monitor moved from plotcam on 2026-10-23, and the pictures were lost with
  it): plotter.pl's `display_sync.py new` sends each plotsnap frame and
  paint result as `/tmp/display/new.fb.gz` (raw pixels, 1920 × 1080; 32-bit
  XRGB8888 since 2026-11-02, from `video=HDMI-A-1:1920x1080-32@60` at the end
  of `/boot/firmware/cmdline.txt`, the 16-bit original kept as
  `cmdline.txt.bak16`: at 16 bits the halos' darks went in steps). The
  aquarium stops and the picture is drawn straight to the framebuffer, for
  10 minutes from when it is first on screen (while someone is in the room),
  then the aquarium again. Dark through shots as before. A picture already
  there when the service starts is not shown. `show_pic` (in `~/bin`;
  `display_sync.py selected`) sends the picture selected in Photos the
  same way.
- **Read-only card.** `raspi-config nonint enable_overlayfs` and
  `enable_bootro`, as on plotcam (section 6): it can be unplugged at any time.

Installing (or updating) without turning the overlay off:

```
scp pi/aquarium.py pi/aquarium.service atelier:/tmp/
ssh atelier 'sudo mount -o remount,rw /media/root-ro
for R in /media/root-ro ""; do
  sudo install -m 755 /tmp/aquarium.py $R/usr/local/bin/aquarium.py
  sudo install -m 644 /tmp/aquarium.service $R/etc/systemd/system/aquarium.service
done
sync; sudo mount -o remount,ro /media/root-ro
sudo systemctl daemon-reload && sudo systemctl restart aquarium'
```

A new video goes to `$R/usr/local/share/aquarium/aquarium1080.mp4` the same
way. A video bigger than the RAM overlay (~450 MB free), such as
`aquarium_long.mp4`, goes into the read-only layer only, streamed (not via
`/tmp`, which is RAM too); a new file there shows through the overlay:

```
ssh atelier 'sudo mount -o remount,rw /media/root-ro'
cat aquarium_long.mp4 | ssh atelier 'sudo tee /media/root-ro/usr/local/share/aquarium/aquarium_long.mp4 >/dev/null'
ssh atelier 'sync; sudo mount -o remount,ro /media/root-ro; sudo systemctl restart aquarium'
``` New packages need the overlay off: `sudo raspi-config nonint
disable_overlayfs`, reboot, install, `enable_overlayfs` and `enable_bootro`,
reboot (`disable_bootro` refuses while the overlay is in use; `/boot` can stay
read-only).
