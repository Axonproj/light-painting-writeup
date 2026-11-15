# Firmware

The plotter runs on three microcontrollers.

## The bridge: [`serial_bridge_c6`](serial_bridge_c6/serial_bridge_c6.ino)

An ESP32-C6 (Seeed XIAO) wired to the Arduino Nano's serial port. It
joins the home WiFi and passes G-code from `plotter.pl` (a TCP
connection on port 8080) to GRBL, and GRBL's replies back. It passes
GRBL's `[C:n]` colour messages on to the pen carrier over ESP-NOW at
the moment each stroke starts. It also reads GRBL's laser-mode PWM, so
the LED's brightness can follow the pen's real speed, and shows
what's happening on a small OLED. It can be updated over WiFi, and
can pass an update on to the carrier.

## The pen carrier: [`pen_carrier`](pen_carrier/pen_carrier.ino)

An ESP32-C6 (Seeed XIAO) on a small battery, riding where the pen
was, driving one addressable LED (an 8 mm APA-106, or an SK6812 RGBW;
the type is set at run time and kept in flash). It never joins the
WiFi network: it only listens for the bridge's ESP-NOW packets, on the
same channel. Five minutes after its last lit colour it goes into deep
sleep, and a trembler switch wakes it when the plotter shakes the
carriage.

## GRBL: [`grbl_1.1h_tektronix.patch`](grbl/grbl_1.1h_tektronix.patch)

The Nano runs [GRBL 1.1h](https://github.com/gnea/grbl), with these
changes:

- Homing for two axes, X first and then Y.
- The spindle PWM at 62.5 kHz with no prescaler, which drove the pen
  solenoid without buzzing.
- A new M-code, `M62 Pn`, for the LED's colour. The colour travels
  with the next motion block through the planner and stepper, and when
  that block starts to move GRBL prints `[C:n]` for the bridge. So the
  colour changes when the stroke starts, not when the line is read.
- `M5` (pen up) drops any colour still waiting, and sends `[C:0]` to
  put the LED out.
- Soft limits skip the Z axis, which is never homed.
- The Nano's LED flashes three times at reset.

The patch applies to the v1.1h.20190825 release: from inside its
`grbl` folder, `patch -p1 < grbl_1.1h_tektronix.patch`. GRBL is under
the GPL v3, and so is this patch.

## Building

The ESP32 sketches build with the Arduino ESP32 core (3.x) and need
Adafruit NeoPixel (carrier) and U8g2 (bridge). They include a
`wifi_credentials.h` that isn't here: it defines `ssid` and `password`
for your network. The bridge's static address and the carrier's MAC
address (`CARRIER_MAC`) need setting for your own boards.

Apart from GRBL, the firmware is under the MIT licence, as the rest of
the code: see [LICENSE](../LICENSE).
