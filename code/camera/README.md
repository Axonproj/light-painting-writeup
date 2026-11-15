# Camera

## [`remote_snap`](remote_snap)

The script `plotter.pl` calls to take every photograph. It works with
either camera, whichever is connected: a Canon EOS 700D through
[gphoto2](http://www.gphoto.org/), or a Canon A570 IS running
[CHDK](https://chdk.fandom.com/wiki/CHDK) through
[chdkptp](../chdkptp/). The camera can be on the computer's USB or on
a Raspberry Pi next to the plotter, reached over ssh. It sets the
exposure, aperture and focus, opens the shutter for as long as the
plot takes (bulb on the 700D), and saves the frame, and the raw file
if asked, with the plot's name and a number. It can also report when
the camera is idle, and read the camera's light meter, which is how
`plotsnap` checks that the room is dark before a shot.

## CHDK scripts

These ran on the A570 itself, copied to `CHDK/SCRIPTS/` on its SD
card. They come from before chdkptp, when the camera was triggered
through its USB port by an ESP32 sending pulses: CHDK can read the
USB power line as a remote button.

- [`usb_lightpaint.lua`](usb_lightpaint.lua): the main one. Counts
  pulses to shoot, or to choose the exposure time (1 s to 120 s) and
  the focus distance.
- [`usb_shutter.lua`](usb_shutter.lua) and
  [`usb_bulb.lua`](usb_bulb.lua): simpler versions, a plain shutter
  release and a bulb exposure.
- [`bulb_toggle.lua`](bulb_toggle.lua): an exposure beyond the camera's
  own 15-second limit, started by a half-press.
- [`test_usb.lua`](test_usb.lua), [`test_usb.bas`](test_usb.bas) and
  [`say_hello.lua`](say_hello.lua): first tests of the USB remote and
  of scripting on the camera.

chdkptp replaced all of this: it controls the camera directly, and
fetches each frame straight to the computer without writing the SD
card.
