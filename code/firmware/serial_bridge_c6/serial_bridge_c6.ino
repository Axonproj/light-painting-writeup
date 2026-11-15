// serial_bridge_c6.ino — Seeed XIAO ESP32-C6 serial bridge for GRBL
//
// C6 port of esp32/serial_bridge (the ESP32-C3 build), created to drive an
// EXTERNAL U.FL WiFi antenna in the hope of curing ESP-NOW colour dropouts to
// the pen carrier, and to use a larger SSD1306 128x64 (yellow/blue) I2C OLED.
// Behaviour is otherwise identical to the C3 bridge — same TCP/USB/ESP-NOW
// protocol, same keepalive logic.
//
// Forwards G-code from USB (Serial) and WiFi TCP to GRBL on Arduino Nano
// (Serial0). Relays GRBL responses back to the active source(s).
//
// Intercepts [C:n] lines from GRBL (M62 LED colour changes) and sends
// them via unicast ESP-NOW to the pen carrier. Does not forward [C:n] to USB/TCP.
//
// --- XIAO ESP32-C6 wiring ---
//   D0 (GPIO0)  → Nano RST (active low)
//   D4 (GPIO22) → I2C SDA (OLED)
//   D5 (GPIO23) → I2C SCL (OLED)
//   D10 (GPIO18) → UART0 TX → level shifter → Nano RX
//   D7 (GPIO17) → UART0 RX ← level shifter ← Nano TX
//   GPIO3/GPIO14: internal FM8625H RF switch (external antenna) — see setup().
//
// Board: XIAO_ESP32C6 (esp32:esp32:esp32c6), USB CDC On Boot: Enabled
// GRBL baud: 115200
// WiFi: station mode, TCP server on port 8080

#include <WiFi.h>
#include <ArduinoOTA.h>
#include <U8g2lib.h>
#include <Wire.h>
#include <esp_now.h>

// #define IN_PUTER_ROOM  // TEST: try the other AP
#include <wifi_credentials.h>

// --- XIAO ESP32-C6 RF switch (external antenna) ---
// The XIAO C6 routes the antenna through an FM8625H RF switch. GPIO3 powers the
// switch (drive LOW to enable), GPIO14 selects the antenna (LOW = onboard chip,
// HIGH = external U.FL). Without this the U.FL connector is dead. The board
// variant defines these aliases; fall back to raw GPIOs if not.
#ifndef WIFI_ENABLE
#define WIFI_ENABLE 3
#endif
#ifndef WIFI_ANT_CONFIG
#define WIFI_ANT_CONFIG 14
#endif

static const unsigned long GRBL_BAUD = 115200;
static const uint16_t TCP_PORT = 8080;
static const int NANO_RESET_PIN = 0;   // D0 / GPIO0 → Nano RST (active low)
static const int NANO_TX_PIN = 18;     // D10 / GPIO18 → level shifter → Nano RX
static const int NANO_RX_PIN = 17;     // D7 / GPIO17 ← level shifter ← Nano TX
static const int OLED_SCL_PIN = 23;    // D5 / GPIO23
static const int OLED_SDA_PIN = 22;    // D4 / GPIO22
static const int DUTY_PIN = 1;         // D1 / GPIO1 ← 10k/10k divider + 100nF ← Nano D11

// Static IP config.
static const IPAddress STATIC_IP(192, 168, 1, 25);
static const IPAddress GATEWAY(192, 168, 1, 1);
static const IPAddress SUBNET(255, 255, 255, 0);

// Pen carrier MAC address — unicast gives radio-layer ACK + auto-retransmit.
static const uint8_t CARRIER_MAC[6] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00};   // your carrier's WiFi MAC

// SSD1306 128x64 (yellow top 16px / blue lower 48px) over hardware I2C.
// HW-I2C constructor arg order: (rotation, reset, clock=SCL, data=SDA).
U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE, OLED_SCL_PIN, OLED_SDA_PIN);
WiFiServer tcp_server(TCP_PORT);
WiFiClient tcp_client;

// OLED display lines. 128px wide / 6px font = 21 chars usable; buffers are 22.
static char oled_ip[22] = "";
static char oled_gcode[22] = "";
static char oled_response[22] = "";
static char oled_status[22] = "";
static bool oled_dirty = true;

// ESP-NOW keepalive state.
// Packet is 4 bytes: R, G, B, MODE. MODE 0 = show (light it), 1 = preload (arm but
// stay dark). The keepalive resends the current packet *including its mode*, so a
// preload stays dark through a long air move and a show stays lit through a stroke.
#define ESPNOW_MODE_SHOW    0
#define ESPNOW_MODE_PRELOAD 1
// R, G, B, MODE, FLAGS. FLAGS bit 0 (ESPNOW_FLAG_SESSION): a TCP client (a
// plotter.pl session) is connected. The carrier stays awake while it sees it,
// and sleeps on its idle timer once it stops — staying awake merely because
// the bridge was powered flattened its 150 mAh cell in ~2 h (2026-10-04).
// Carriers that predate the byte ignore it (they read 3 or 4 bytes).
#define ESPNOW_FLAG_SESSION 0x01
// R, G, B, MODE, FLAGS, SCALE. SCALE multiplies the colour on the carrier:
// 255 = as sent, 0 = dark. It is 255 unless `!dyn on` (see duty_scale()).
// Carriers that predate the byte ignore it and show the colour unscaled.
#define ESPNOW_SCALE_FULL 255
static uint8_t espnow_pkt[6] = {0, 0, 0, ESPNOW_MODE_SHOW, 0, ESPNOW_SCALE_FULL};
static unsigned long espnow_keepalive_ms = 0;
static volatile bool espnow_tx_failed = false;  // Set by TX cb on no-ACK; triggers immediate resend.
static volatile bool espnow_acked = false;      // Set by TX cb on ACK; cleared on each new colour. True ⇒ carrier
                                                 // has confirmed the current packet, so resends can go quiet.
static volatile uint8_t espnow_retries = 0;     // Immediate resends since last good ACK (bounded).
static volatile unsigned long espnow_last_ack_ms = 0;  // millis() of last ACKed send; stale ⇒ carrier gone.
static unsigned long espnow_colour_ms = 0;             // millis() of last colour change; recent ⇒ fast cadence.
static const uint8_t ESPNOW_MAX_RETRIES = 2;    // Cap fast retries so a dead zone can't storm the radio.
static const unsigned long ESPNOW_KEEPALIVE_MS = 200;      // Resend rate while a colour is NOT yet ACKed (still
                                                           // trying to confirm the carrier received it).
static const unsigned long ESPNOW_IDLE_KEEPALIVE_MS = 1000; // Once the carrier has ACKed the current (static) colour
                                                           // it provably has it and holds it — so drop to a slow
                                                           // heartbeat. This is the mid-arc state, and 1000ms (vs
                                                           // 200ms) cuts the ESP-NOW airtime that shares the one
                                                           // radio with the WiFi/TCP G-code feed. Bursts of stolen
                                                           // airtime were tipping GRBL's shallow stream into a
                                                           // self-sustaining slow-crawl for a whole arc quadrant
                                                           // (over-exposed blob). The heartbeat still refreshes a
                                                           // rebooted carrier and feeds the absent-detector.
static const unsigned long ESPNOW_FAST_KEEPALIVE_MS = 20;  // Brief fast cadence right after a colour change
static const unsigned long ESPNOW_FAST_WINDOW_MS = 300;    // ...for this long, to punch the new colour through
                                                           // a marginal radio zone at the arc start (else the
                                                           // start draws dark until the slow keepalive). Bounded
                                                           // to colour changes, so it costs little airtime.
static const unsigned long ESPNOW_ABSENT_MS = 3000;        // No ACK this long ⇒ carrier absent (off). ≥3 missed
                                                           // idle heartbeats, so a single drop can't false-trip it.
static const unsigned long ESPNOW_SLOW_KEEPALIVE_MS = 2000; // Slow probe rate once carrier absent.

// --- Link-quality window (antenna tuning) ---
// on_data_sent() already measures the one thing that matters for the carrier hop:
// for unicast, SUCCESS means the carrier MAC-ACKed it. There is no RSSI to be had
// for this link at the bridge — RSSI arrives on the RECEIVE path and the carrier
// only ever sends MAC-layer ACKs, so the RSSI on the OLED is the home WiFi
// association, a different path entirely. Don't tune the carrier link by it.
// One bit per send, newest in bit 0. 32 bits rather than 64 so the WiFi-task
// callback and loop() can share it with no lock: an aligned 32-bit access is
// atomic on RV32, where a 64-bit one would tear.
static volatile uint32_t espnow_ack_hist = 0;
static volatile uint8_t espnow_hist_n = 0;      // samples so far, saturating at 32

// Tune mode: a fixed fast cadence so the window refills in ~1.6s while the antenna
// is being moved around. The normal cadences are far too slow to tune against —
// 1000ms once ACKed means a 32-sample window takes half a minute to respond.
// OFF by default, never enabled at boot. A fast keepalive is exactly what wedged
// the bridge in June (15ms plus retry resends monopolised the shared radio and
// starved the WiFi/TCP G-code feed), so this is bounded three ways: 50ms keeps a
// wide margin under that, it self-expires, and any forwarded G-code line cancels
// it — see handle_gcode_byte(). It also never overrides the carrier-absent
// back-off, so it cannot storm a radio with nothing listening.
static bool espnow_tune = false;
static unsigned long espnow_tune_ms = 0;
static const unsigned long TUNE_KEEPALIVE_MS = 50;
static const unsigned long TUNE_TIMEOUT_MS = 120000;

// Max OLED refresh rate. A full 128x64 I2C flush (~25ms) must not run per
// forwarded line, or it throttles the G-code stream and judders arcs.
static const unsigned long OLED_REFRESH_MS = 150;

// Freeze the display outright while G-code is streaming. The 150ms cap above cut
// how OFTEN the ~25ms I2C flush blocks, but not that it blocks: loop() is what
// reads [C:n] off the UART and calls esp_now_send(), so a colour change arriving
// mid-flush is served up to 25ms late. At the 25000mm/min light-pen feed that is
// 10mm of dark line — which is the size of the late starts seen on IMG_0080
// (4.5mm and 2.9mm, both far SHORTER than the 20ms/8.3mm cost of one lost packet,
// so they were never dropped packets). Blocking 25ms in every 150ms also matches
// the ~11% of strokes affected. Frozen, oled_dirty just accumulates and the first
// refresh after the plot picks up the latest state.
static const unsigned long PLOT_FREEZE_MS = 1500;  // Quiet this long ⇒ plot over, resume.
static unsigned long last_gcode_ms = 0;

// --- Colour-change latency instrumentation ---
// Freezing the OLED cut the worst late start from ~11ms to ~6ms but left the event
// rate alone, so a second jitter source remains. This splits the bridge's share of
// it from GRBL's and the radio's. handle_grbl_byte() parses [C:n] and calls
// send_colour_espnow() back to back, so parse→send costs nothing; what CAN delay a
// colour change here is loop() not getting round to draining the UART, leaving the
// line sitting in the FIFO. So measure the loop gap, and specifically the gap on the
// pass that carried a [C:n] — that bounds how long it waited.
// Accumulated, never printed live: printing during a plot would perturb the very
// thing being measured. Read and zero with !lat once the plot has finished.
static unsigned long lat_loop_prev_us = 0;
static unsigned long lat_this_gap_us = 0;   // gap on the current pass
static unsigned long lat_loop_max_us = 0;   // worst pass-to-pass gap, any cause
static unsigned long lat_colour_max_us = 0; // worst gap on a pass carrying [C:n]
static unsigned long lat_colour_sum_us = 0;
static unsigned long lat_send_max_us = 0;   // worst send_colour_espnow() duration
static uint16_t lat_colour_n = 0;

// Per-colour-change trace. The aggregates above say how bad the worst event was
// but not WHICH event, and the late starts are bursty — one frame held 9 of 22.
// Correlating with the photograph needs the events in order, so keep a row each:
//
//   dt_us   gap since the previous [C:n] ARRIVED. The strokes are equal length at
//           a constant feed, so GRBL should emit these at a metronome cadence;
//           jitter here is upstream (GRBL's main loop, or the UART) and no amount
//           of radio tuning will touch it. This is the number that tests the
//           "[C:n] comes out of the main loop" theory without instrumenting GRBL.
//   gap_us  loop() pass gap on the pass that carried it — the bridge's own share.
//   send_us how long send_colour_espnow() took (the 5-packet burst queues async).
//   cb_us   send → first TX callback: the radio's share, incl. contention.
//   ok      that callback ACKed.
//   mode    preload (during the air move) or show (at the stroke start). Only the
//           show events can produce a late start; preloads are the control group.
//
// Saturates rather than wraps: 96 rows covers colour_hlines3 (27 strokes x
// preload+show = 54) with room to spare, and a wrapped buffer would silently
// lose the beginning of the plot, which is where the first stroke's lag lives.
#define LAT_TRACE_N 96
struct lat_ev {
  uint32_t dt_us;
  uint16_t gap_us;
  uint16_t send_us;
  uint16_t cb_us;
  uint8_t  ok;
  uint8_t  mode;
  uint16_t qwait_us;  // parse time minus the UART driver's burst stamp: bridge read latency
  uint16_t avail;     // bytes already queued behind this line's first byte
  uint16_t lmax_us;   // worst pass-to-pass gap since the previous row
  uint16_t smax_us;   // longest single loop() section since the previous row...
  uint8_t  sec;       // ...and which section (SEC_*)
};
static struct lat_ev lat_trace[LAT_TRACE_N];
static uint16_t lat_trace_n = 0;
static unsigned long lat_prev_colour_us = 0;
// Filled by the TX callback, which runs in the WiFi task, not loop(). It only
// ever touches cb_us/ok of the row lat_cb_idx names, and loop() only touches the
// other fields of that row, so the two never write the same bytes. -1 disarms:
// the burst is 5 packets and only the first callback is the one we want.
static volatile int16_t lat_cb_idx = -1;
static volatile unsigned long lat_send_mark_us = 0;

// --- Where does a late [C:n] spend its time? ---
// The dt_us column showed that [C:0] -> show is metronomic (272-276ms at
// rxbuf=100, i.e. the air move plus a fixed few ms) while the preload's [C:n]
// lands anywhere from 15 to 200ms after [C:0]. So the Nano's block-start timing
// is deterministic and the wander is in the emit/read path. gap_us cannot split
// that: it is the pass-to-pass gap at the TOP of the pass, and a stall INSIDE a
// pass (the per-byte TCP forwarding of the oks that precede every [C:n], or a
// higher-priority WiFi task on this single-core chip) reads the queued [C:n]
// later in the same pass with a small gap_us. Four views that can see it:
//   qwait_us  micros() at parse minus the UART driver's own stamp of when the
//             burst landed. onReceive runs in a priority-24 task, above loop(),
//             so the stamp cannot be delayed by anything loop() does. This is
//             the bridge's true read latency for the line: ~0 => the bytes
//             arrived late on the wire (Nano-side); large => bridge-side.
//             65535 = stamp older than the line's first byte (should not happen).
//   avail     Serial0.available() right after the line's first byte was read:
//             >= the rest of the line means it was already queued (a stall).
//   lmax_us   worst pass-to-pass gap since the previous [C:n] row.
//   smax_us   longest single loop() section since the previous row, and
//   sec       which one (SEC_*) — attributes a stall to its cause.
static volatile unsigned long rx_burst_us = 0;   // stamped by the UART event task
static unsigned long line_first_us = 0;          // loop-side: first byte of the current line
static uint16_t line_avail0 = 0;                 // bytes still queued behind that first byte
static unsigned long lat_row_lmax_us = 0;        // pass-gap max since the previous row
static unsigned long sec_max_us = 0;             // longest section since the previous row
static uint8_t sec_max_id = 0;
#define SEC_OTA     1
#define SEC_ACCEPT  2
#define SEC_USB_IN  3
#define SEC_TCP_IN  4
#define SEC_FWD     5   // flush_host_out(): the pass's GRBL responses, one write
#define SEC_LINK    6   // TCP-disconnect + WiFi-status checks
#define SEC_KEEPAL  7   // keepalive/retry esp_now_send
#define SEC_RSSI    8
#define SEC_OLED    9
#define SEC_ESPNOW 10   // send_colour_espnow() for a [C:n]

static inline uint16_t sat16(unsigned long v) { return v > 65535UL ? 65535 : (uint16_t)v; }
static inline void sec_mark(uint8_t id, unsigned long t_start) {
  unsigned long d = micros() - t_start;
  if(d > sec_max_us) { sec_max_us = d; sec_max_id = id; }
}
static void on_uart_rx() { rx_burst_us = micros(); }
static unsigned long prev_line_done_us = 0;      // loop-side: previous GRBL line completed

// Host output buffer. GRBL responses are NOT written to the host as they are
// parsed. The two v2 frames of 2026-09-08 showed every late [C:n] sitting behind
// a forward_to_host() of the preceding ok lines, done one byte per
// tcp_client.write() (an lwIP round trip and, with NoDelay, a TCP segment each),
// stalling 5-65+ ms inside a single loop() pass while the [C:n] waited in the
// UART ring. So the UART is drained completely first, [C:n] is dispatched the
// moment it completes, and everything for the host goes here to be sent once
// per pass as a single write. Flushed early only if it fills.
static char host_out[512];
static int host_out_len = 0;

// Line buffer for GRBL responses (Nano → host).
static char grbl_line[80];
static int grbl_line_len = 0;
static bool grbl_pending_cr = false; // saw \r waiting for \n

//=============================================================================
static void truncate_to_oled(
  const char *src,
  char *dst,
  int max_len
)
// Copies src into dst, truncated to max_len chars.
{
  int i = 0;
  while(i < max_len - 1 && src[i] != '\0') {
    dst[i] = src[i];
    i++;
  }
  dst[i] = '\0';
}

//=============================================================================
static void update_gcode_display(
  const char *line
)
// Shows last G-code sent on the OLED gcode line with > prefix.
{
  char buf[22];
  buf[0] = '>';
  int i = 0;
  while(line[i] != '\0' && line[i] != '\n' && line[i] != '\r' && i < 20) {
    buf[i + 1] = line[i];
    i++;
  }
  buf[i + 1] = '\0';
  truncate_to_oled(buf, oled_gcode, sizeof(oled_gcode));
  oled_dirty = true;
}

//=============================================================================
static void update_response_display(
  const char *line
)
// Shows last GRBL response on the OLED response line.
{
  truncate_to_oled(line, oled_response, sizeof(oled_response));
  oled_dirty = true;
}

//=============================================================================
static void update_status_display(
  const char *text
)
// Shows WiFi/ESP-NOW status on the OLED status line.
{
  truncate_to_oled(text, oled_status, sizeof(oled_status));
  oled_dirty = true;
}

//=============================================================================
static void refresh_oled()
// Redraws OLED if dirty flag is set. Layout for 128x64: the top ~16px is the
// yellow strip on this panel, so the IP header sits there (y=10); the G-code,
// response and status lines fall in the blue region below.
{
  if(!oled_dirty) return;
  oled_dirty = false;

  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(0, 10, oled_ip);        // yellow strip
  u8g2.drawHLine(0, 15, 128);          // divider under the yellow header
  u8g2.drawStr(0, 28, oled_gcode);     // blue region
  u8g2.drawStr(0, 42, oled_response);
  u8g2.drawStr(0, 56, oled_status);
  u8g2.sendBuffer();
}

//=============================================================================
static void on_data_sent(
  const wifi_tx_info_t *info,
  esp_now_send_status_t status
)
// ESP-NOW TX-status callback. For unicast, SUCCESS means the carrier MAC-ACKed
// the packet (radio link good); FAIL means no ACK (link dead / out of range).
// Prints '.' per acked packet, 'x' per failed one to USB serial (debug only —
// never to TCP, which carries the GRBL response stream). Guarded by `if(Serial)`
// so it never blocks when no USB host is attached. Watch with `make esp6-monitor`
// during a plot: a run of 'x' while the pen sits on the left = dead-zone dropout.
{
  (void)info;
  bool ok = (status == ESP_NOW_SEND_SUCCESS);
  if(ok) { espnow_retries = 0; espnow_last_ack_ms = millis(); espnow_acked = true; }  // Link good — reset retry budget + absence timer; carrier confirmed.
  else   { espnow_tx_failed = true; }  // No ACK — ask loop() to resend (bounded by ESPNOW_MAX_RETRIES).
  espnow_ack_hist = (espnow_ack_hist << 1) | (ok ? 1u : 0u);  // Rolling link quality for tune mode.
  if(espnow_hist_n < 32) espnow_hist_n++;
  {
    int16_t i = lat_cb_idx;  // Armed by the [C:n] that started this burst.
    if(i >= 0) {
      unsigned long d = micros() - lat_send_mark_us;
      lat_trace[i].cb_us = (d > 65535UL) ? 65535 : (uint16_t)d;
      lat_trace[i].ok = ok ? 1 : 0;
      lat_cb_idx = -1;  // First callback only — the other four are duplicates.
    }
  }
  if(Serial) Serial.write(ok ? '.' : 'x');
}

// ---- Firmware-update protocol (must match pen_carrier.ino) -----------------
// The carrier has no WiFi association and no IP, so ArduinoOTA cannot reach it.
// Its image comes over the same ESP-NOW link the colours use, relayed from the
// host by this bridge: the host streams the .bin down the TCP socket, and each
// 240-byte chunk goes out as one unicast packet, stop-and-wait on the carrier's
// ACK. Colour packets are 3-5 bytes and update packets start with OTA_MAGIC, so
// the carrier can always tell them apart.
#define OTA_MAGIC    0xA5
#define OTA_OP_BEGIN 0x01
#define OTA_OP_DATA  0x02
#define OTA_OP_END   0x03
#define OTA_OP_ACK   0x81
#define OTA_OP_NAK   0x82
#define OTA_HDR      4
#define OTA_CHUNK    240
static const unsigned long OTA_ACK_TIMEOUT_MS = 400;   // per attempt
static const int           OTA_MAX_RETRY      = 8;     // then give up on that chunk
static const unsigned long OTA_TCP_TIMEOUT_MS = 10000; // host stalled feeding us

// ---- Carrier control / event packets (must match pen_carrier.ino) ----------
// CTL_LEN bytes starting CTL_MAGIC0 CTL_MAGIC1: no colour or update packet is
// that length. Bridge -> carrier: sleep now (`!csleep`), and "test mode on",
// resent while `!jolt on` holds. Carrier -> bridge: its events, which go to
// the host as "[T:<type> <a> <b> <bridge ms>]" lines, only while `!jolt on`.
//   B power-on   W woken by its trembler (a = wake count)   S going to sleep
//   J a jolt of the trembler (a = contact edges, b = how long it rang, ms)
//   L the LED type in use (a = type, b = 1 if it just changed), the answer to
//     CTL_OP_LED: `!ledtype N` sets it (kept in the carrier's flash), `!ledtype`
//     asks. 0 APA-106 (RGB), 1 SK6812 RGBW; the table is in pen_carrier.ino.
#define CTL_LEN        8
#define CTL_MAGIC0     0xA6
#define CTL_MAGIC1     0x6A
#define CTL_OP_SLEEP   'S'
#define CTL_OP_REPORT  'R'
#define CTL_OP_KEEP    'K'   // restart the carrier's idle clock (`!ckeep`)
#define CTL_OP_LED     'L'   // set (or with 0xFF, ask) the carrier's LED type (`!ledtype`)
#define CTL_EV_LED     'L'
static const uint8_t       CTL_LED_QUERY = 0xFF;
static const unsigned long CTL_LED_WAIT_MS = 300;  // per attempt, for the carrier's answer
static volatile int        carrier_led_type = -1;  // last type the carrier reported, -1 none
static volatile unsigned long carrier_led_ms = 0;  // millis() of that report
static const unsigned long CTL_REPORT_MS = 500;    // resend rate for "test mode on"
struct rrCarrierEvent { uint8_t type; uint16_t a; uint16_t b; unsigned long ms; };
static rrCarrierEvent    ctl_events[16];           // receive callback -> loop()
static volatile uint8_t  ctl_head = 0;
static uint8_t           ctl_tail = 0;
static bool              jolt_report = false;      // `!jolt on`
static unsigned long     ctl_report_ms = 0;

// Speed fraction from GRBL. In laser mode ($32=1) under M4, GRBL sets the PWM duty
// on Nano D11 to actual speed / programmed feed, updated every 10 ms from its
// stepper code. A divider and capacitor turn that into a voltage on DUTY_PIN.
// `!duty` reads it once; `!duty on` streams "[D:<bridge ms> <mV>]" lines.
static const unsigned long DUTY_SAMPLE_US = 1000;  // one ADC reading per millisecond
static const unsigned long DUTY_REPORT_MS = 20;    // stream rate under `!duty on`
// The two ends as this ADC and these resistors read them (measured 2026-10-12:
// 58 mV stopped, 2367 mV at the programmed feed; linear between to 0.6%). The
// margins make a stopped pen exactly 0 and the programmed feed exactly full.
static const uint32_t      DUTY_ZERO_MV = 58;
static const uint32_t      DUTY_FULL_MV = 2367;
static const uint32_t      DUTY_ZERO_MARGIN_MV = 15;
static const uint32_t      DUTY_FULL_MARGIN_MV = 12;
static uint32_t          duty_filt = 0;            // smoothed reading, 1/16 mV
static unsigned long     duty_sample_us = 0;
static bool              duty_report = false;      // `!duty on`
static unsigned long     duty_report_ms = 0;
// Dynamic brightness (`!dyn on`): the carrier's colour follows the speed
// fraction, so a line gets the same light per millimetre however fast the pen
// is going, and none while it is stopped. GRBL changes its PWM every 10 ms, so
// that is how often a new scale is worth sending — and only when it has moved
// by DYN_STEP, or reached either end: noise on a steady speed sends nothing.
static const unsigned long DYN_SEND_MS = 10;
static const int           DYN_STEP = 3;           // of 255, about 1.2%
static bool              dyn_on = false;           // `!dyn on`
static unsigned long     dyn_send_ms = 0;
// Speed report (`!peak`): how fast the pen really went since the last `!peak`,
// as a fraction of the programmed feed. A plot that never reaches its feed is
// drawn below its colour's brightness throughout; the peak says by how much,
// and the histogram how the drawing time divides among the tenths of the feed.
// Only time with the pen moving on a drawing stroke counts (scale above 0).
#define PEAK_BINS 10
static uint8_t           peak_max = 0;             // highest scale seen, of 255
static uint64_t          peak_moving_us = 0;
static uint64_t          peak_weighted = 0;        // sum of scale x microseconds
static uint32_t          peak_hist_ms[PEAK_BINS];  // time in each tenth
static uint32_t          peak_hist_frac_us[PEAK_BINS];  // remainders under 1 ms

static volatile bool     ota_ack_seen = false;   // set by the receive callback
static volatile bool     ota_ack_good = false;   // ACK (true) or NAK (false)
static volatile uint16_t ota_ack_seq  = 0;

//=============================================================================
static void on_espnow_recv(
  const esp_now_recv_info_t *info,
  const uint8_t *data,
  int len
)
// The carrier talks back during a firmware update, and with its events (boot,
// wake, sleep, jolts). Anything that is not well formed is ignored rather than
// guessed at.
{
  (void)info;
  if(len == CTL_LEN && data[0] == CTL_MAGIC0 && data[1] == CTL_MAGIC1) {
    if(data[2] == CTL_EV_LED) { carrier_led_type = data[3]; carrier_led_ms = millis(); }
    uint8_t next = (ctl_head + 1) % (sizeof(ctl_events) / sizeof(ctl_events[0]));
    if(next == ctl_tail) return;                  // full: drop it
    ctl_events[ctl_head].type = data[2];
    ctl_events[ctl_head].a    = (uint16_t)data[3] | ((uint16_t)data[4] << 8);
    ctl_events[ctl_head].b    = (uint16_t)data[5] | ((uint16_t)data[6] << 8);
    ctl_events[ctl_head].ms   = millis();
    ctl_head = next;
    return;
  }
  if(len < OTA_HDR || data[0] != OTA_MAGIC) return;
  if(data[1] != OTA_OP_ACK && data[1] != OTA_OP_NAK) return;
  ota_ack_seq  = (uint16_t)data[2] | ((uint16_t)data[3] << 8);
  ota_ack_good = (data[1] == OTA_OP_ACK);
  ota_ack_seen = true;
}

//=============================================================================
static bool ota_send_and_wait(
  uint8_t op, uint16_t seq, const uint8_t *payload, size_t n
)
// One packet, then wait for the carrier to acknowledge that exact sequence
// number. Retries the send on silence — a dropped packet and a dropped ACK look
// identical from here, and the carrier re-ACKs a chunk it has already written
// rather than writing it twice.
{
  uint8_t pkt[OTA_HDR + OTA_CHUNK];
  pkt[0] = OTA_MAGIC;
  pkt[1] = op;
  pkt[2] = (uint8_t)(seq & 0xFF);
  pkt[3] = (uint8_t)(seq >> 8);
  if(n) memcpy(pkt + OTA_HDR, payload, n);

  for(int attempt = 0; attempt < OTA_MAX_RETRY; attempt++) {
    ota_ack_seen = false;
    esp_now_send(CARRIER_MAC, pkt, OTA_HDR + n);
    unsigned long t0 = millis();
    while(millis() - t0 < OTA_ACK_TIMEOUT_MS) {
      if(ota_ack_seen && ota_ack_seq == seq) return ota_ack_good;
      delay(1);                       // let the WiFi task run; this is not a hot path
    }
  }
  return false;
}

//=============================================================================
static void run_ota_transfer(
  uint32_t size, uint32_t crc
)
// Pump the image from the TCP socket to the carrier. This blocks the bridge for
// the length of the transfer — deliberately: nothing should be plotting while
// the pen carrier is being reflashed, and a state machine spread across loop()
// would be far more code for no benefit at a once-a-week operation.
{
  uint8_t begin[8];
  memcpy(begin,     &size, 4);
  memcpy(begin + 4, &crc,  4);
  if(!ota_send_and_wait(OTA_OP_BEGIN, 0, begin, sizeof(begin))) {
    tcp_client.print("ota: carrier refused the transfer (asleep? out of range?)\r\n");
    return;
  }
  update_status_display("OTA 0%");

  uint8_t  buf[OTA_CHUNK];
  uint32_t sent = 0;
  uint16_t seq = 1;
  int      last_pct = -1;
  while(sent < size) {
    size_t want = (size - sent > OTA_CHUNK) ? OTA_CHUNK : (size_t)(size - sent);
    size_t got = 0;
    unsigned long t0 = millis();
    while(got < want) {
      if(!tcp_client.connected()) { tcp_client.stop(); return; }   // host vanished
      int n = tcp_client.read(buf + got, want - got);
      if(n > 0) { got += n; t0 = millis(); }
      else if(millis() - t0 > OTA_TCP_TIMEOUT_MS) {
        tcp_client.print("ota: timed out waiting for image data\r\n");
        return;
      }
      else delay(1);
    }
    if(!ota_send_and_wait(OTA_OP_DATA, seq, buf, got)) {
      tcp_client.printf("ota: carrier stopped acknowledging at chunk %u\r\n", seq);
      return;
    }
    sent += got;
    seq++;
    int pct = (int)(100ULL * sent / size);
    if(pct != last_pct && pct % 5 == 0) {                 // the host prints these
      tcp_client.printf("ota: %d%%\r\n", pct);
      char line[22];
      snprintf(line, sizeof(line), "OTA %d%%", pct);
      update_status_display(line);
      last_pct = pct;
    }
  }
  if(!ota_send_and_wait(OTA_OP_END, seq, NULL, 0)) {
    tcp_client.print("ota: carrier rejected the finished image (CRC?)\r\n");
    update_status_display("OTA failed");
    return;
  }
  tcp_client.print("ota: done — carrier rebooting into the new firmware\r\n");
  update_status_display("OTA done");
}

//=============================================================================
static uint8_t duty_scale()
// GRBL's speed fraction as a packet SCALE byte: 0 stopped, 255 at the
// programmed feed.
{
  uint32_t mv = duty_filt >> 4;
  if(mv <= DUTY_ZERO_MV + DUTY_ZERO_MARGIN_MV) return 0;
  if(mv >= DUTY_FULL_MV - DUTY_FULL_MARGIN_MV) return ESPNOW_SCALE_FULL;
  uint32_t span = DUTY_FULL_MV - DUTY_FULL_MARGIN_MV - DUTY_ZERO_MV;
  return (uint8_t)(((mv - DUTY_ZERO_MV) * ESPNOW_SCALE_FULL + span / 2) / span);
}

//=============================================================================
static void send_colour_espnow(
  uint32_t rgb, uint8_t mode
)
// Sends a 24-bit RGB colour (and mode: show or preload) to the pen carrier via
// unicast ESP-NOW. Packet: 4 bytes R,G,B,MODE. Sent 5 times for reliability.
// Updates keepalive state so loop() keeps resending this packet.
{
  espnow_pkt[0] = (rgb >> 16) & 0xFF;
  espnow_pkt[1] = (rgb >>  8) & 0xFF;
  espnow_pkt[2] =  rgb        & 0xFF;
  espnow_pkt[3] =  mode;
  espnow_pkt[4] = (tcp_client && tcp_client.connected()) ? ESPNOW_FLAG_SESSION : 0;
  espnow_pkt[5] = dyn_on ? duty_scale() : ESPNOW_SCALE_FULL;
  espnow_retries = 0;  // Fresh fast-retry budget for this colour change.
  espnow_acked = false;  // Not confirmed yet — keepalive stays at the trying-to-confirm rate until the carrier ACKs.
  espnow_keepalive_ms = millis();
  espnow_colour_ms = millis();  // Open the fast-cadence window to punch this colour through.
  // Burst back-to-back with NO delay: esp_now_send() queues to the WiFi TX async, so a
  // delay() here would just block loop() — and that block stalls the GRBL serial feed,
  // the very contention we're avoiding. The 20ms fast window backstops any queue-dropped packet.
  for(int i = 0; i < 5; i++) {
    esp_now_send(CARRIER_MAC, espnow_pkt, sizeof(espnow_pkt));
  }
}

//=============================================================================
static void send_carrier_ctl(
  uint8_t op, uint8_t a = 0
)
// One control packet to the carrier.
{
  uint8_t pkt[CTL_LEN] = { CTL_MAGIC0, CTL_MAGIC1, op, a, 0, 0, 0, 0 };
  esp_now_send(CARRIER_MAC, pkt, sizeof(pkt));
}

//=============================================================================
static void init_espnow()
// Initialises ESP-NOW and registers pen carrier as unicast peer.
// Must be called after WiFi.mode(WIFI_STA).
{
  if(esp_now_init() != ESP_OK) {
    Serial.println("ESP-NOW init failed");
    update_status_display("ESPNOW fail");
    return;
  }

  esp_now_register_send_cb(on_data_sent);  // TX-status trace for dead-zone diagnosis.
  esp_now_register_recv_cb(on_espnow_recv);  // the carrier answers only during an update.

  esp_now_peer_info_t peer = {};
  memcpy(peer.peer_addr, CARRIER_MAC, 6);
  peer.channel = 0;  // 0 = use current WiFi channel
  peer.encrypt = false;
  esp_now_add_peer(&peer);
}

//=============================================================================
static void flush_host_out()
// One write of everything queued for the host this pass (USB and TCP).
{
  if(host_out_len == 0) return;
  if(Serial) Serial.write((const uint8_t *)host_out, host_out_len);
  if(tcp_client && tcp_client.connected()) {
    tcp_client.write((const uint8_t *)host_out, host_out_len);
  }
  host_out_len = 0;
}

//=============================================================================
static void forward_to_host(
  const char *buf,
  int len
)
// Queues bytes for the host; flush_host_out() sends them after the UART drain.
{
  for(int i = 0; i < len; i++) {
    if(host_out_len >= (int)sizeof(host_out)) flush_host_out();
    host_out[host_out_len++] = buf[i];
  }
}

//=============================================================================
static void handle_grbl_byte(
  char c
)
// Accumulates Nano response bytes line by line.
// On newline: intercepts [C:n] for ESP-NOW, forwards everything else to USB/TCP.
{
  if(grbl_line_len == 0 && c != '\r' && c != '\n') {
    // First byte of a line: note when loop() got to it and how much of the
    // line (or more) was already waiting in the driver's ring buffer.
    line_first_us = micros();
    line_avail0 = (uint16_t)Serial0.available();
  }

  if(c == '\r') {
    grbl_pending_cr = true;
    return;
  }

  if(c == '\n') {
    grbl_line[grbl_line_len] = '\0';

    if(grbl_line_len > 0) {
      // [C:n] — LED colour change from M62. Broadcast via ESP-NOW, suppress from host.
      // n is 25-bit: low 24 bits = RGB, bit 24 = preload flag (arm but stay dark).
      if(grbl_line[0] == '[' && grbl_line[1] == 'C' && grbl_line[2] == ':') {
        uint32_t n = strtoul(grbl_line + 3, NULL, 10);
        uint8_t mode = (n & 0x1000000) ? ESPNOW_MODE_PRELOAD : ESPNOW_MODE_SHOW;
        unsigned long t0 = micros();
        // Claim and arm the row BEFORE sending: esp_now_send() queues async and
        // the callback can land before the burst loop has even finished.
        int16_t row = (lat_trace_n < LAT_TRACE_N) ? (int16_t)lat_trace_n : -1;
        if(row >= 0) {
          lat_trace[row].dt_us = lat_prev_colour_us ? (uint32_t)(t0 - lat_prev_colour_us) : 0;
          lat_trace[row].gap_us = (lat_this_gap_us > 65535UL) ? 65535 : (uint16_t)lat_this_gap_us;
          lat_trace[row].send_us = 0;
          lat_trace[row].cb_us = 0;
          lat_trace[row].ok = 0;
          lat_trace[row].mode = mode;
          // Stale stamp (older than this line's first byte) => sentinel, not a
          // bogus wrap-around value.
          // The driver's task stamps the burst BEFORE loop() can read any of
          // it, so a fresh stamp lies between the previous line's completion
          // and this line's first byte. Older than that = stale = sentinel.
          {
            unsigned long stamp = rx_burst_us;
            bool fresh = ((long)(stamp - prev_line_done_us) >= 0)
                      && ((long)(line_first_us - stamp) >= 0);
            lat_trace[row].qwait_us = fresh ? sat16(line_first_us - stamp) : 65535;
          }
          lat_trace[row].avail   = line_avail0;
          lat_trace[row].lmax_us = sat16(lat_row_lmax_us);
          lat_trace[row].smax_us = sat16(sec_max_us);
          lat_trace[row].sec     = sec_max_id;
          lat_send_mark_us = t0;
          lat_cb_idx = row;
        }
        lat_row_lmax_us = 0;   // per-row maxima restart here, whether or not a row was free
        sec_max_us = 0;
        sec_max_id = 0;
        lat_prev_colour_us = t0;
        unsigned long ts_espnow = micros();
        send_colour_espnow(n & 0xFFFFFF, mode);
        unsigned long dt = micros() - t0;
        sec_mark(SEC_ESPNOW, ts_espnow);
        if(row >= 0) {
          lat_trace[row].send_us = (dt > 65535UL) ? 65535 : (uint16_t)dt;
          lat_trace_n++;  // Publish the row only once it is fully written.
        }
        if(dt > lat_send_max_us) lat_send_max_us = dt;
        if(lat_this_gap_us > lat_colour_max_us) lat_colour_max_us = lat_this_gap_us;
        lat_colour_sum_us += lat_this_gap_us;
        lat_colour_n++;
        grbl_line_len = 0;
        grbl_pending_cr = false;
        prev_line_done_us = micros();
        return; // do not forward to USB/TCP
      }

      // Normal response — update OLED and forward with CRLF.
      update_response_display(grbl_line);
      forward_to_host(grbl_line, grbl_line_len);
    }

    // Terminate line for host.
    if(grbl_pending_cr) forward_to_host("\r", 1);
    forward_to_host("\n", 1);

    grbl_line_len = 0;
    grbl_pending_cr = false;
    prev_line_done_us = micros();
    return;
  }

  if(grbl_line_len < (int)sizeof(grbl_line) - 1) {
    grbl_line[grbl_line_len++] = c;
  }
}

// Line buffer for incoming G-code (to capture for OLED display).
static char gcode_line[80];
static int gcode_line_len = 0;

// Line buffer for TCP command interception (ESP commands like !reset).
static char cmd_line[64];   // must fit the longest '!' command: "!ota <size> <crc>"

static int cmd_line_len = 0;

//=============================================================================
static void handle_gcode_byte(
  char c
)
// Accumulates G-code bytes to capture the line for OLED display.
{
  if(c == '\n' || c == '\r') {
    if(gcode_line_len > 0) {
      gcode_line[gcode_line_len] = '\0';
      update_gcode_display(gcode_line);
      gcode_line_len = 0;
      last_gcode_ms = millis();   // Streaming — hold the OLED off the I2C bus.
      // A plot is starting — drop tune mode's fast keepalive before it can steal
      // airtime from the feed. Structural, so the fast cadence cannot be left on
      // by accident: you have to re-enable it deliberately after the machine idles.
      espnow_tune = false;
    }
    return;
  }
  if(gcode_line_len < (int)sizeof(gcode_line) - 1) {
    gcode_line[gcode_line_len++] = c;
  }
}

//=============================================================================
static void enable_external_antenna()
// XIAO ESP32-C6: power the FM8625H RF switch (GPIO3 low) and route RF to the
// external U.FL connector (GPIO14 high). Must run before WiFi starts.
{
  pinMode(WIFI_ENABLE, OUTPUT);
  digitalWrite(WIFI_ENABLE, LOW);      // enable RF switch
  delay(100);
  pinMode(WIFI_ANT_CONFIG, OUTPUT);
  digitalWrite(WIFI_ANT_CONFIG, HIGH); // select external antenna
}

//=============================================================================
void setup()
{
  enable_external_antenna();  // Before any radio use.

  // Hardware-reset Nano in case ESP32 reflash sent garbage on TX.
  pinMode(NANO_RESET_PIN, OUTPUT);
  digitalWrite(NANO_RESET_PIN, LOW);
  delay(10);
  digitalWrite(NANO_RESET_PIN, HIGH);
  pinMode(NANO_RESET_PIN, INPUT);  // High-Z, Nano pullup holds RST high.
  delay(500);  // Wait for GRBL to boot.

  Serial.begin(GRBL_BAUD);   // USB CDC — host side
  Serial0.begin(GRBL_BAUD, SERIAL_8N1, NANO_RX_PIN, NANO_TX_PIN);  // UART0 → Nano
  // Stamp every RX burst from the driver's own event task (priority 24, above
  // loop()) — the reference for qwait_us. Fires on the 1-symbol RX timeout, i.e.
  // ~90us after the last byte of a burst lands; it does not consume the data.
  Serial0.onReceive(on_uart_rx);

  u8g2.begin();
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(0, 10, "Plotter");
  u8g2.drawStr(0, 28, "WiFi...");
  u8g2.sendBuffer();

  // Connect to WiFi.
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // Disable modem sleep — prevents multi-second packet delays.
  WiFi.config(STATIC_IP, GATEWAY, SUBNET);
  WiFi.begin(ssid, password);

  unsigned long start = millis();
  while(WiFi.status() != WL_CONNECTED && millis() - start < 15000) {
    delay(250);
  }

  if(WiFi.status() == WL_CONNECTED) {
    String ip = WiFi.localIP().toString();
    truncate_to_oled(ip.c_str(), oled_ip, sizeof(oled_ip));
    update_status_display("WiFi: OK");
    Serial.printf("WiFi connected, channel %d, MAC %s\n",
      WiFi.channel(), WiFi.macAddress().c_str());
  } else {
    oled_ip[0] = '\0';  // Empty — triggers reconnect in loop().
    update_status_display("WiFi...");
  }

  // ESP-NOW: init after WiFi mode is set.
  init_espnow();

  // OTA updates.
  ArduinoOTA.setHostname("plotter");
  ArduinoOTA.onStart([]() {
    update_status_display("OTA start");
    refresh_oled();
  });
  ArduinoOTA.onProgress([](unsigned int progress, unsigned int total) {
    char buf[15];
    snprintf(buf, sizeof(buf), "OTA %u%%", progress * 100 / total);
    update_status_display(buf);
    refresh_oled();
  });
  ArduinoOTA.onEnd([]() {
    update_status_display("OTA done!");
    refresh_oled();
  });
  ArduinoOTA.onError([](ota_error_t error) {
    update_status_display("OTA FAIL");
    refresh_oled();
  });
  ArduinoOTA.begin();

  tcp_server.begin();
  oled_dirty = true;
  refresh_oled();
}

//=============================================================================
void loop()
{
  // Pass-to-pass gap. A [C:n] arriving just after the previous drain waits this
  // long before anything can see it. Unsigned subtraction survives the micros() wrap.
  {
    unsigned long now_us = micros();
    lat_this_gap_us = lat_loop_prev_us ? (now_us - lat_loop_prev_us) : 0;
    lat_loop_prev_us = now_us;
    if(lat_this_gap_us > lat_loop_max_us) lat_loop_max_us = lat_this_gap_us;
    if(lat_this_gap_us > lat_row_lmax_us) lat_row_lmax_us = lat_this_gap_us;
  }

  { unsigned long ts = micros(); ArduinoOTA.handle(); sec_mark(SEC_OTA, ts); }

  // Accept new TCP client (one at a time).
  unsigned long ts_accept = micros();
  if(!tcp_client || !tcp_client.connected()) {
    WiFiClient new_client = tcp_server.available();
    if(new_client) {
      tcp_client = new_client;
      tcp_client.setNoDelay(true);

      // Hard-reset Nano to guarantee known GRBL state.
      pinMode(NANO_RESET_PIN, OUTPUT);
      digitalWrite(NANO_RESET_PIN, LOW);
      delay(10);
      digitalWrite(NANO_RESET_PIN, HIGH);
      pinMode(NANO_RESET_PIN, INPUT);
      delay(500);  // Wait for GRBL to boot.

      // Warm up ESP-NOW radio link — first packets after init are often dropped.
      for(int i = 0; i < 5; i++) {
        uint8_t off[3] = {0, 0, 0};
        esp_now_send(CARRIER_MAC, off, sizeof(off));
        delay(20);
      }

      // Drain any GRBL startup messages.
      while(Serial0.available()) Serial0.read();

      // Clear ESP soft state.
      grbl_line_len = 0;
      gcode_line_len = 0;
      cmd_line_len = 0;
      host_out_len = 0;
      grbl_pending_cr = false;
      oled_gcode[0] = '\0';
      oled_response[0] = '\0';
      memset(espnow_pkt, 0, sizeof(espnow_pkt));  // 0,0,0,show = off
      espnow_pkt[5] = ESPNOW_SCALE_FULL;
      espnow_keepalive_ms = millis();
      jolt_report = false;                        // test mode never outlives its session
      duty_report = false;
      dyn_on = false;                             // nor does dynamic brightness
      update_status_display("TCP: conn");
    }
  }
  sec_mark(SEC_ACCEPT, ts_accept);

  // USB Host → GRBL
  unsigned long ts_usb = micros();
  while(Serial.available()) {
    char c = Serial.read();
    handle_gcode_byte(c);
    Serial0.write(c);
  }
  sec_mark(SEC_USB_IN, ts_usb);

  // TCP Client → GRBL (intercept ESP commands, forward the rest).
  unsigned long ts_tcp = micros();
  if(tcp_client && tcp_client.connected()) {
    while(tcp_client.available()) {
      char c = tcp_client.read();
      if(cmd_line_len > 0) {
        // Accumulating a '!' command — wait for newline.
        if(c == '\n' || c == '\r') {
          cmd_line[cmd_line_len] = '\0';
          if(strcmp(cmd_line, "!reset") == 0) {
            tcp_client.println("Resetting...");
            tcp_client.flush();
            delay(100);
            ESP.restart();
          } else if(strcmp(cmd_line, "!nano-upload") == 0) {
            // Hold Nano in reset while releasing UART to avoid glitches.
            pinMode(NANO_RESET_PIN, OUTPUT);
            digitalWrite(NANO_RESET_PIN, LOW);
            Serial0.end();
            pinMode(NANO_TX_PIN, INPUT);
            delay(10);
            digitalWrite(NANO_RESET_PIN, HIGH);
            pinMode(NANO_RESET_PIN, INPUT);
            update_status_display("Nano upload");
            tcp_client.println("UART TX released — flash Nano now.");
            tcp_client.println("Send !reset when done.");
          } else if(strncmp(cmd_line, "!espnow:", 8) == 0) {
            uint32_t n = strtoul(cmd_line + 8, NULL, 10);
            uint8_t mode = (n & 0x1000000) ? ESPNOW_MODE_PRELOAD : ESPNOW_MODE_SHOW;
            send_colour_espnow(n & 0xFFFFFF, mode);
            tcp_client.print("ok\r\n");
          } else if(strncmp(cmd_line, "!ota ", 5) == 0) {
            // "!ota <size> <crc32>" — everything after the newline is the image
            // itself, raw, straight off the socket and out over the radio.
            unsigned long osize = 0, ocrc = 0;
            if(sscanf(cmd_line + 5, "%lu %lx", &osize, &ocrc) == 2 && osize > 0) {
              tcp_client.printf("ota: ready for %lu bytes\r\n", osize);
              run_ota_transfer((uint32_t)osize, (uint32_t)ocrc);
            } else {
              tcp_client.print("ota: usage !ota <size> <crc32hex>\r\n");
            }
          } else if(strncmp(cmd_line, "!jolt", 5) == 0) {
            // Carrier test mode: its trembler jolts (and boot/wake/sleep) come
            // back as [T:...] lines. Off by default: plotter.pl never sees them.
            jolt_report = (strcmp(cmd_line, "!jolt on") == 0);
            tcp_client.printf("jolt %s\r\n", jolt_report ? "on" : "off");
          } else if(strncmp(cmd_line, "!duty", 5) == 0) {
            // GRBL's speed fraction as read on DUTY_PIN: once, or as a stream of
            // [D:...] lines for watching it follow a move.
            if(strcmp(cmd_line, "!duty") == 0) {
              tcp_client.printf("duty %lu mV  scale %u/255  dyn %s\r\n",
                                (unsigned long)(duty_filt >> 4), (unsigned)duty_scale(),
                                dyn_on ? "on" : "off");
            } else {
              duty_report = (strcmp(cmd_line, "!duty on") == 0);
              tcp_client.printf("duty %s\r\n", duty_report ? "on" : "off");
            }
          } else if(strcmp(cmd_line, "!peak") == 0) {
            // Speed report since the last !peak, then start again. One line:
            // peak and mean as percent of the programmed feed, the drawing
            // time, and the milliseconds spent in each tenth of the feed.
            unsigned long moving_ms = (unsigned long)(peak_moving_us / 1000);
            unsigned long mean = peak_moving_us
              ? (unsigned long)(peak_weighted * 100 / ESPNOW_SCALE_FULL / peak_moving_us) : 0;
            tcp_client.printf("peak %u%%  mean %lu%%  moving %lu ms  hist",
                              (unsigned)(peak_max * 100 / ESPNOW_SCALE_FULL), mean, moving_ms);
            for(int i = 0; i < PEAK_BINS; i++) {
              tcp_client.printf(" %lu", (unsigned long)peak_hist_ms[i]);
            }
            tcp_client.print("\r\n");
            peak_max = 0;
            peak_moving_us = peak_weighted = 0;
            memset(peak_hist_ms, 0, sizeof(peak_hist_ms));
            memset(peak_hist_frac_us, 0, sizeof(peak_hist_frac_us));
          } else if(strncmp(cmd_line, "!dyn", 4) == 0) {
            // Dynamic brightness on or off. Off puts the colour back to full at
            // once, so a stopped pen can be lit by hand again.
            dyn_on = (strcmp(cmd_line, "!dyn on") == 0);
            espnow_pkt[5] = dyn_on ? duty_scale() : ESPNOW_SCALE_FULL;
            esp_now_send(CARRIER_MAC, espnow_pkt, sizeof(espnow_pkt));
            tcp_client.print("ok\r\n");
          } else if(strcmp(cmd_line, "!ckeep") == 0) {
            // Restart the carrier's idle clock: plotter.pl sends this when it has
            // checked the carrier before a plot, so it cannot nod off before
            // the first colour arrives.
            for(int i = 0; i < 3; i++) { send_carrier_ctl(CTL_OP_KEEP); delay(5); }
            tcp_client.print("ckeep sent\r\n");
          } else if(strcmp(cmd_line, "!up") == 0) {
            // How long the bridge has been running, and why it last restarted
            // (esp_reset_reason: 1 power-on, 3 software, 4 panic, 5-7 watchdogs,
            // 9 brownout): tells a dropped connection from a bridge crash.
            tcp_client.printf("up %lu ms  reset reason %d\r\n", millis(), (int)esp_reset_reason());
          } else if(strncmp(cmd_line, "!ledtype", 8) == 0) {
            // The carrier's LED type: `!ledtype N` sets it, `!ledtype` asks.
            // Waits for the carrier's answer (up to three tries) and reports
            // "ledtype N" with the type it says it is using, or "ledtype none"
            // if it did not answer (asleep, or firmware without LED types).
            const char *arg = cmd_line + 8;
            while(*arg == ' ') arg++;
            uint8_t want = (*arg >= '0' && *arg <= '9') ? (uint8_t)atoi(arg) : CTL_LED_QUERY;
            int got = -1;
            for(int attempt = 0; attempt < 3 && got < 0; attempt++) {
              unsigned long t0 = millis();
              send_carrier_ctl(CTL_OP_LED, want);
              while(millis() - t0 < CTL_LED_WAIT_MS) {
                if(carrier_led_ms - t0 < 0x80000000UL && carrier_led_type >= 0) { got = carrier_led_type; break; }
                delay(2);
              }
            }
            if(got < 0) tcp_client.print("ledtype none\r\n");
            else        tcp_client.printf("ledtype %d\r\n", got);
          } else if(strcmp(cmd_line, "!csleep") == 0) {
            // Send the carrier to sleep now, without waiting out its idle timer.
            for(int i = 0; i < 3; i++) { send_carrier_ctl(CTL_OP_SLEEP); delay(5); }
            tcp_client.print("csleep sent\r\n");
          } else if(strncmp(cmd_line, "!tune", 5) == 0) {
            // Antenna tuning: fast keepalive + ACK-rate on the OLED. Idle machine
            // only — the next G-code line cancels it, and it self-expires.
            espnow_tune = (strcmp(cmd_line, "!tune on") == 0);
            espnow_tune_ms = millis();
            espnow_ack_hist = 0;
            espnow_hist_n = 0;
            tcp_client.printf("tune %s\r\n", espnow_tune ? "on" : "off");
          } else if(strcmp(cmd_line, "!lat") == 0) {
            // Colour-change latency since the last !lat. Run a plot, then ask.
            tcp_client.printf("colour changes  %u\r\n", lat_colour_n);
            tcp_client.printf("loop gap max    %lu us\r\n", lat_loop_max_us);
            tcp_client.printf("at [C:n]  max   %lu us   mean %lu us\r\n",
                              lat_colour_max_us,
                              lat_colour_n ? lat_colour_sum_us / lat_colour_n : 0UL);
            tcp_client.printf("espnow send max %lu us\r\n", lat_send_max_us);
            lat_loop_max_us = lat_colour_max_us = lat_send_max_us = 0;
            lat_colour_sum_us = 0;
            lat_colour_n = 0;
          } else if(strcmp(cmd_line, "!carrier") == 0) {
            // Is the pen carrier alive? It has no WiFi, so the only sign of life
            // is the MAC-layer ACK on the bridge's ESP-NOW keepalive (1-2s cadence).
            unsigned long age = millis() - espnow_last_ack_ms;
            tcp_client.printf("carrier %s  last ack %lu ms ago  acks %u/%u  pkt %u,%u,%u mode %u flags %u scale %u\r\n",
                              (age > ESPNOW_ABSENT_MS) ? "ABSENT" : "present",
                              age,
                              (unsigned)__builtin_popcount(espnow_ack_hist), (unsigned)espnow_hist_n,
                              espnow_pkt[0], espnow_pkt[1], espnow_pkt[2], espnow_pkt[3], espnow_pkt[4],
                              espnow_pkt[5]);
          } else if(strcmp(cmd_line, "!lathist") == 0) {
            // Per-event trace, oldest first. Run a plot, then ask — printing
            // during one would perturb the thing being measured.
            tcp_client.printf("n=%u  (mode 0=show 1=preload; sec 1=ota 2=accept 3=usbin 4=tcpin 5=fwd 6=link 7=keepal 8=rssi 9=oled 10=espnow)\r\n", lat_trace_n);
            tcp_client.printf("  i     dt_us  gap_us  send_us   cb_us  ok  mode  qwait_us  avail  lmax_us  smax_us  sec\r\n");
            for(uint16_t i = 0; i < lat_trace_n; i++) {
              tcp_client.printf("%3u  %8lu  %6u  %7u  %6u  %2u  %4u  %8u  %5u  %7u  %7u  %3u\r\n",
                                i,
                                (unsigned long)lat_trace[i].dt_us,
                                lat_trace[i].gap_us,
                                lat_trace[i].send_us,
                                lat_trace[i].cb_us,
                                lat_trace[i].ok,
                                lat_trace[i].mode,
                                lat_trace[i].qwait_us,
                                lat_trace[i].avail,
                                lat_trace[i].lmax_us,
                                lat_trace[i].smax_us,
                                lat_trace[i].sec);
            }
            lat_trace_n = 0;
            lat_prev_colour_us = 0;
            lat_cb_idx = -1;
          }
          // Unknown '!' command — discard it.
          cmd_line_len = 0;
        } else if(cmd_line_len < (int)sizeof(cmd_line) - 1) {
          cmd_line[cmd_line_len++] = c;
        }
      } else if(c == '!') {
        // Start accumulating a '!' command.
        cmd_line[cmd_line_len++] = c;
      } else {
        // Normal data — forward immediately.
        handle_gcode_byte(c);
        Serial0.write(c);
      }
    }
  }
  sec_mark(SEC_TCP_IN, ts_tcp);

  // Carrier events → host, and the "test mode on" request to the carrier,
  // both only under `!jolt on`.
  while(ctl_tail != ctl_head) {
    rrCarrierEvent ev = ctl_events[ctl_tail];
    ctl_tail = (ctl_tail + 1) % (sizeof(ctl_events) / sizeof(ctl_events[0]));
    if(jolt_report) {
      char line[48];
      int n = snprintf(line, sizeof(line), "[T:%c %u %u %lu]\r\n", (char)ev.type,
                       (unsigned)ev.a, (unsigned)ev.b, ev.ms);
      forward_to_host(line, n);
    }
  }
  // Test mode ends with the session that asked for it.
  if(jolt_report && !(tcp_client && tcp_client.connected())) jolt_report = false;
  if(jolt_report && millis() - ctl_report_ms >= CTL_REPORT_MS) {
    ctl_report_ms = millis();
    send_carrier_ctl(CTL_OP_REPORT);
  }

  // GRBL's speed fraction: one reading a millisecond, smoothed over about four.
  if(micros() - duty_sample_us >= DUTY_SAMPLE_US) {
    unsigned long dt_us = micros() - duty_sample_us;
    duty_sample_us = micros();
    uint32_t mv16 = (uint32_t)analogReadMilliVolts(DUTY_PIN) << 4;
    duty_filt = (duty_filt * 3 + mv16) / 4;
    uint8_t scale = duty_scale();
    if(scale && dt_us < 100000UL) {     // a long gap is a stall here, not drawing
      if(scale > peak_max) peak_max = scale;
      peak_moving_us += dt_us;
      peak_weighted += (uint64_t)scale * dt_us;
      int bin = scale * PEAK_BINS / (ESPNOW_SCALE_FULL + 1);
      peak_hist_frac_us[bin] += dt_us;
      peak_hist_ms[bin] += peak_hist_frac_us[bin] / 1000;
      peak_hist_frac_us[bin] %= 1000;
    }
  }
  if(dyn_on && millis() - dyn_send_ms >= DYN_SEND_MS) {
    uint8_t scale = duty_scale();
    int moved = abs((int)scale - (int)espnow_pkt[5]);
    bool at_end = (scale == 0 || scale == ESPNOW_SCALE_FULL);
    if(moved >= DYN_STEP || (moved && at_end)) {
      dyn_send_ms = millis();
      espnow_pkt[5] = scale;
      esp_now_send(CARRIER_MAC, espnow_pkt, sizeof(espnow_pkt));
    }
  }
  if(duty_report && !(tcp_client && tcp_client.connected())) duty_report = false;
  if(duty_report && millis() - duty_report_ms >= DUTY_REPORT_MS) {
    duty_report_ms = millis();
    char line[32];
    int n = snprintf(line, sizeof(line), "[D:%lu %lu]\r\n", duty_report_ms,
                     (unsigned long)(duty_filt >> 4));
    forward_to_host(line, n);
  }

  // GRBL → USB Host + TCP Client (line-buffered; [C:n] intercepted for ESP-NOW).
  while(Serial0.available()) {
    handle_grbl_byte((char)Serial0.read());
  }
  // Everything the drain queued for the host goes out in one write, AFTER any
  // [C:n] in the same burst has already been dispatched.
  { unsigned long ts = micros(); flush_host_out(); sec_mark(SEC_FWD, ts); }

  // Detect TCP disconnect.
  unsigned long ts_link = micros();
  if(tcp_client && !tcp_client.connected()) {
    tcp_client.stop();
    update_status_display("WiFi: OK");
  }

  // Reconnect WiFi if disconnected.
  static unsigned long wifi_retry_time = 0;
  if(WiFi.status() != WL_CONNECTED && millis() > wifi_retry_time) {
    update_status_display("WiFi...");
    WiFi.disconnect();
    WiFi.begin(ssid, password);
    wifi_retry_time = millis() + 5000;  // Retry every 5s.
  } else if(WiFi.status() == WL_CONNECTED && oled_ip[0] == '\0') {
    // Just connected — update display.
    String ip = WiFi.localIP().toString();
    truncate_to_oled(ip.c_str(), oled_ip, sizeof(oled_ip));
    update_status_display("WiFi: OK");
  }
  sec_mark(SEC_LINK, ts_link);

  // ESP-NOW resend cadence. While the carrier is ACKing, keepalive every
  // ESPNOW_KEEPALIVE_MS plus up to ESPNOW_MAX_RETRIES immediate resends after a no-ACK —
  // the bounded fast retries punch the current colour through a brief mid-plot dropout
  // (e.g. the far-left marginal zone) without waiting a full keepalive, killing dark line
  // starts. But if NO ACK has arrived for ESPNOW_ABSENT_MS the carrier is gone (battery
  // off — its normal idle state), so back off to a slow ESPNOW_SLOW_KEEPALIVE_MS probe and
  // drop the retry storm: continuously failing ESP-NOW TX otherwise monopolises the shared
  // radio and starves the WiFi/TCP/OTA stack, wedging the bridge (associated + ARP-able but
  // no ICMP/TCP). A single ACK from a returning carrier resets the timer → fast cadence.
  // "Off" (black) colours skip the fast cadence: an off at a stroke end would
  // otherwise resend black every 20ms for 300ms, and a stale black packet landing
  // just after the next stroke's colour shows as a brief dark start. Off still gets
  // the 5x burst for prompt turn-off, and falls back to the slow steady keepalive.
  bool carrier_absent = (millis() - espnow_last_ack_ms > ESPNOW_ABSENT_MS);
  bool colour_off = (espnow_pkt[0] == 0 && espnow_pkt[1] == 0 && espnow_pkt[2] == 0);
  // Every keepalive carries the current session state (see ESPNOW_FLAG_SESSION).
  espnow_pkt[4] = (tcp_client && tcp_client.connected()) ? ESPNOW_FLAG_SESSION : 0;
  unsigned long ka_ms;
  if(espnow_tune && millis() - espnow_tune_ms > TUNE_TIMEOUT_MS) espnow_tune = false;  // Self-expire.
  if(carrier_absent)                                                          ka_ms = ESPNOW_SLOW_KEEPALIVE_MS;  // 2000: quiet probe
  else if(espnow_tune)                                                        ka_ms = TUNE_KEEPALIVE_MS;         // 50: antenna tuning, idle machine only
  else if(!colour_off && millis() - espnow_colour_ms < ESPNOW_FAST_WINDOW_MS)  ka_ms = ESPNOW_FAST_KEEPALIVE_MS;  // 20: just after a real colour change
  else if(espnow_acked)                                                       ka_ms = ESPNOW_IDLE_KEEPALIVE_MS;  // 1000: carrier confirmed this colour — go quiet, cede radio to the feed
  else                                                                        ka_ms = ESPNOW_KEEPALIVE_MS;       // 200: not yet confirmed, keep trying
  bool retry = false;
  if(espnow_tx_failed) {
    espnow_tx_failed = false;
    if(!carrier_absent && espnow_retries < ESPNOW_MAX_RETRIES) { espnow_retries++; retry = true; }
  }
  if(retry || (millis() - espnow_keepalive_ms >= ka_ms)) {
    unsigned long ts = micros();
    esp_now_send(CARRIER_MAC, espnow_pkt, sizeof(espnow_pkt));
    espnow_keepalive_ms = millis();
    sec_mark(SEC_KEEPAL, ts);
  }

  // Show WiFi signal strength on the OLED status line, refreshed every 2s.
  // Higher (closer to 0) dBm = stronger; ~-58 is a healthy link.
  // In tune mode the line shows the carrier-hop ACK rate instead, refreshed often
  // enough to follow an antenna being moved by hand. Only sets oled_dirty — the
  // rate limiter below owns the I2C flush, which must not run per event.
  static unsigned long rssi_ms = 0;
  unsigned long ts_rssi = micros();
  if(millis() - rssi_ms >= (espnow_tune ? 250UL : 2000UL)) {
    rssi_ms = millis();
    char buf[22];
    if(espnow_tune) {
      unsigned ok = __builtin_popcount(espnow_ack_hist);
      unsigned n = espnow_hist_n;
      snprintf(buf, sizeof(buf), "TUNE %u/%u %u%%", ok, n, n ? (100u * ok / n) : 0u);
    } else {
      snprintf(buf, sizeof(buf), "RSSI %ld ch%d", WiFi.RSSI(), WiFi.channel());
    }
    update_status_display(buf);
  }
  sec_mark(SEC_RSSI, ts_rssi);

  // Rate-limit the OLED. A full 128x64 I2C flush is ~25ms; refreshing on every
  // G-code line and every GRBL response (as the per-line dirty flags request)
  // would block the byte-forwarding path and starve GRBL's planner, juddering
  // segment-dense arcs. The C3's 72x40 panel was fast enough to hide this. Cap
  // refreshes here so the display stays current without throttling the feed;
  // oled_dirty accumulates and the next tick picks up the latest state.
  // ...and freeze it entirely while a plot is streaming, so the flush can never sit
  // between a [C:n] arriving and its ESP-NOW send. See PLOT_FREEZE_MS.
  static unsigned long oled_refresh_ms = 0;
  // last_gcode_ms == 0 means nothing has streamed yet — don't freeze the boot screen.
  bool plotting = last_gcode_ms && (millis() - last_gcode_ms < PLOT_FREEZE_MS);
  if(!plotting && millis() - oled_refresh_ms >= OLED_REFRESH_MS) {
    unsigned long ts = micros();
    oled_refresh_ms = millis();
    refresh_oled();
    sec_mark(SEC_OLED, ts);
  }
}
