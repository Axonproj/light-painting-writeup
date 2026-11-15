// pen_carrier.ino — ESP-NOW receiver + NeoPixel driver (APA-106 RGB now; SK6812 RGBW before)
//
// Receives 24-bit RGB colour packets from the plotter controller (serial_bridge)
// via ESP-NOW and drives a NeoPixel on the pen carrier: an 8 mm APA-106 (RGB)
// since 2026-11-02, an SK6812 RGBW before it (the LED type, set at run time).
//
// GREY MEANS WHITE (SK6812 only, LED type 1; the APA-106 mixes grey from RGB): a colour with R == G == B lights the pixel's own WHITE
// element at that level, with R, G and B dark. The packet (and GRBL's M62 P,
// a float with 24 exact bits) has no room for a W byte, but grey already
// arrives as R=G=B, so the plot files, plotter.pl, GRBL and the bridge need
// no change. One phosphor die instead of three: a neutral white, and at the
// lowest step (level 1) about a third of the light of RGB (1,1,1) — which is
// what lets no_exit's grey border sit below the snakes at brightness 2-3.
// Every other colour is mixed from R, G and B, with W dark.
//
// WiFi-less: never associates. At boot it scans for the controller's AP (SSID from
// wifi_credentials.h) only to learn its channel, locks the radio to it, and runs
// ESP-NOW only. No association / IP / OTA.
//
// Idle shutdown: when idle, the carrier blips blue three times and enters deep
// sleep. The only wake source is the trembler switch (below); the power switch
// still works too. The earlier sleep/periodic-wake scheme was abandoned because
// the radio would not reliably come back from light sleep; a deep-sleep wake is
// a full restart, so the radio comes up exactly as it does at power-on.
//
// Wake on jolt: a trembler (SW-18010P, normally open) from D0 to GND, pulled up
// by the chip. D0 is GPIO0, a low-power pin, so a closure wakes the chip from
// deep sleep: the plotter nudges the carriage to call the carrier up. A jolt
// wake skips the channel scan (the channel is remembered across sleep) and is
// on the air about a quarter of a second after the jolt.
//
// Control packets (CTL_LEN bytes, CTL_MAGIC0 CTL_MAGIC1 first: no colour or
// update packet is that length) carry the bridge's `!csleep` (sleep now) and
// `!jolt on` (test mode), and the carrier's events back to the bridge: boot,
// jolt wake, sleeping, and in test mode every jolt. Test mode also shows dim
// blue where the LED would be dark and a white blip per jolt; it lapses when
// the bridge stops asking.
//
// What counts as idle (radio-on draw is ~80-100 mA; the cell is 150 mAh):
// IDLE_SHUTDOWN_MS with no LIT colour (a non-black packet, show or preload)
// and no keep-awake from the bridge (`!ckeep`, which plotter.pl sends when it
// checks the carrier before a plot). A connected session no longer keeps it
// up: waking is a nudge away, and plotter.pl nudges at `home` and before every
// light-pen plot. So it also sleeps through dark frames and between plots.
// The blips (blue x3 going to sleep, green on a jolt wake) are left out while
// a session is connected: the shutter may be open.
// A USB host attached (Serial connected) suppresses the shutdown, because deep
// sleep drops the USB-Serial-JTAG port and turns flashing into a replug dance.
//
// Colour packet: 3 or 4 bytes R,G,B[,MODE].  0,0,0 = off.
//   MODE 0 (SHOW, default for 3-byte packets) — light the LED at R,G,B.
//   MODE 1 (PRELOAD) — arm R,G,B but keep the LED dark, so the colour reaches the
//     carrier during an air move (whole move to propagate) and the following SHOW
//     just turns it on — kills dark stroke starts without drawing a radial line.
// Firmware update: over ESP-NOW (make carrier-ota), or USB (make carrier-upload).
// The image arrives on the same radio link the colours do — no WiFi association,
// which is the whole point: the AP reads about -96 dBm from here, but the bridge
// is a few feet away. Bytes land in the SPARE app slot; the bootloader is only
// pointed at it once the whole image has arrived and its CRC matches, so a
// transfer that dies half way leaves this firmware running and untouched.
//
// Board: XIAO ESP32-C6, USB CDC On Boot: Enabled.  Library: Adafruit NeoPixel.

#include <WiFi.h>
#include <esp_wifi.h>      // esp_wifi_set_channel
#include <esp_now.h>
#include <esp_sleep.h>     // esp_deep_sleep_start
#include <driver/rtc_io.h> // rtc_gpio_*: the trembler pin's pull-up through deep sleep
#include <Adafruit_NeoPixel.h>
#include <esp_ota_ops.h>   // esp_ota_begin/write/end, set_boot_partition
#include <Preferences.h>   // the LED type, kept in flash

// Leave IN_PUTER_ROOM commented: ssid resolves to the home network's AP, the one the
// bridge associates to — that is the channel we must match for ESP-NOW.
// #define IN_PUTER_ROOM
#include <wifi_credentials.h>

// ---- Hardware configuration ------------------------------------------------
#define LED_PIN      23    // GPIO driving the pixel's data line — XIAO ESP32-C6 GPIO23
#define LED_COUNT    1
#define TREMBLER_PIN 0     // XIAO D0 = GPIO0: trembler switch to GND, wakes from deep sleep
// The bare trembler's closures are too brief to wake the chip against the
// pull-up: thousands of them in a 5 s rattle woke nothing (2026-10-05). It
// needs about 100 nF from this pin to GND to stretch them. Sleeping with the
// pull-up off instead was tried: the pin sinks low at once and wakes itself.
// LED TYPE, set at run time: the bridge's `!ledtype N` (plotter.pl's
// `led type NAME`, which a plot file may carry) sends CTL_OP_LED, and the
// carrier keeps the type in flash, so it survives sleep and power-off. A
// carrier that has never been told uses LED_DEFAULT.
//   0 APA-106 (8 mm, 11-02 to 11-04 and since 11-05): three bytes per pixel,
//     R,G,B: NEO_RGB, no white element.
//   1 SK6812 RGBW (09-09 to 11-02 and 11-04 to 11-05): four, G,R,B,W:
//     NEO_GRBW, and grey lights the white element.
// If red and green trade places after a part change, LED_TYPES is the table
// to correct.
struct rrLedType { neoPixelType order; bool has_w; const char *name; };
static const rrLedType LED_TYPES[] = {
  { NEO_RGB  + NEO_KHZ800, false, "APA-106" },
  { NEO_GRBW + NEO_KHZ800, true,  "SK6812 RGBW" },
};
static const uint8_t LED_TYPE_COUNT = sizeof(LED_TYPES) / sizeof(LED_TYPES[0]);
static const uint8_t LED_DEFAULT    = 0;
static const uint8_t LED_QUERY      = 0xFF;   // CTL_OP_LED with this: report, change nothing

// Used if the boot scan can't see the AP (it's weak at the carrier). Must match
// the home AP's 2.4 GHz channel — pin the AP to this channel in the router so a
// missed scan still lands on the bridge's channel.
static const uint8_t FALLBACK_CHANNEL = 1;
static const int      SCAN_ATTEMPTS   = 4;   // retry the scan — the AP is weak here

// Deep-sleep after this long with no packet from the bridge and no USB host. The pixel sits
// directly on the battery, so its ~1mA quiescent draw continues; what this saves
// is the ESP32's radio-on current (~80-100mA). Power-cycle to wake.
static const unsigned long IDLE_SHUTDOWN_MS    = 5UL * 60UL * 1000UL;
static const uint8_t       FLAG_SESSION        = 0x01;   // 5th packet byte, from the bridge
// A session counts as connected for this long after its last flagged packet.
static const unsigned long SESSION_HOLD_MS     = 5000;
// Trembler edges closer together than this are one jolt: the spring rings.
static const unsigned long JOLT_GAP_MS         = 100;
// Test mode holds this long after the bridge's last request for it.
static const unsigned long REPORT_HOLD_MS      = 3000;
// ----------------------------------------------------------------------------

Adafruit_NeoPixel strip(LED_COUNT, LED_PIN, LED_TYPES[LED_DEFAULT].order);
static uint8_t     led_type  = LED_DEFAULT;     // index into LED_TYPES
static bool        led_has_w = LED_TYPES[LED_DEFAULT].has_w;

// Colour-packet modes (4th byte). 3-byte packets are treated as MODE_SHOW.
#define MODE_SHOW    0   // light the LED at the packet colour
#define MODE_PRELOAD 1   // arm the colour but keep the LED dark

// ---- Firmware-update protocol (must match serial_bridge_c6.ino) ------------
// Colour packets are 3-6 bytes; every update packet is at least 4 and starts
// with OTA_MAGIC, so the two can never be confused for one another.
//   bridge -> carrier:  [MAGIC][op][seq lo][seq hi][payload...]
//   carrier -> bridge:  [MAGIC][ACK|NAK][seq lo][seq hi]
// Stop-and-wait: the bridge sends one chunk and waits for its ACK before the
// next. Slower than a window, but it needs no reassembly buffer and no gap
// tracking, and the transfer is a one-off that nobody is timing.
#define OTA_MAGIC    0xA5
#define OTA_OP_BEGIN 0x01   // payload: u32 image size, u32 CRC-32 of the image
#define OTA_OP_DATA  0x02   // payload: image bytes, in order
#define OTA_OP_END   0x03   // no payload — finish, verify, and switch over
#define OTA_OP_ACK   0x81   // carrier -> bridge: that seq is written
#define OTA_OP_NAK   0x82   // carrier -> bridge: refused (see the serial log)
#define OTA_HDR      4
#define OTA_CHUNK    240    // payload bytes per DATA packet (ESP-NOW caps at 250)

// ---- Control / event packets (must match serial_bridge_c6.ino) -------------
#define CTL_LEN        8
#define CTL_MAGIC0     0xA6
#define CTL_MAGIC1     0x6A
#define CTL_OP_SLEEP   'S'   // bridge -> carrier: sleep now
#define CTL_OP_REPORT  'R'   // bridge -> carrier: test mode on (resent while wanted)
#define CTL_OP_KEEP    'K'   // bridge -> carrier: restart the idle clock
#define CTL_OP_LED     'L'   // bridge -> carrier: LED type a (LED_QUERY: just report it)
#define CTL_EV_BOOT    'B'   // carrier -> bridge: power-on or reset; a = wake cause, b = reset reason
#define CTL_EV_WAKE    'W'   // carrier -> bridge: woken by the trembler; a = wake count
#define CTL_EV_SLEEP   'S'   // carrier -> bridge: going to sleep
#define CTL_EV_JOLT    'J'   // carrier -> bridge: a jolt; a = edges, b = its length in ms
#define CTL_EV_LED     'L'   // carrier -> bridge: the LED type in use; a = type, b = 1 if it changed

// One chunk in flight, handed from the receive callback to loop(): flash writes
// do not belong in the WiFi task's callback, and stop-and-wait means there is
// never more than one outstanding.
static volatile bool     ota_pending = false;
static uint8_t           ota_buf[OTA_CHUNK + OTA_HDR];
static volatile uint16_t ota_buf_len = 0;
static uint8_t           ota_peer[6];        // whoever is sending us the image
static bool              ota_peer_known = false;

static bool              ota_active = false; // a transfer is under way
static esp_ota_handle_t  ota_handle = 0;
static const esp_partition_t *ota_part = NULL;
static uint16_t          ota_expect_seq = 0;
static uint32_t          ota_size = 0, ota_want_crc = 0, ota_got = 0, ota_crc = 0;

// Shared state between ESP-NOW callback and main loop.
static volatile bool    pending_update = false;
static volatile uint8_t pending_r, pending_g, pending_b, pending_mode, pending_flags;
static volatile uint8_t pending_scale = 255;
static unsigned long    last_active_ms  = 0;  // millis() of the last lit colour, keep-awake or OTA traffic
static unsigned long    last_session_ms = 0;  // millis() of the last packet with the SESSION flag
static bool             session_seen    = false;   // any such packet since boot
static unsigned long    boot_ms         = 0;
static bool             wake_blip_due   = false;   // woken by a jolt: green blip not yet shown

// Trembler: written by the pin interrupt, read by loop().
static volatile uint32_t trembler_edges   = 0;
static volatile uint32_t trembler_first_ms = 0;   // first edge of the jolt in progress
static volatile uint32_t trembler_last_ms = 0;

// From the bridge's control packets.
static volatile bool     sleep_requested = false;
static volatile bool     keep_requested  = false;
static volatile uint16_t led_requested   = 0;     // CTL_OP_LED's a, plus 0x100 when one is pending
static volatile uint32_t report_asked_ms = 0;     // millis() of the last CTL_OP_REPORT
static volatile bool     report_asked    = false;
static uint32_t          lit_colour      = 0;     // what the bridge last asked the LED to show

// Kept across deep sleep, so a jolt wake needs no scan and can call the bridge
// at once.
RTC_DATA_ATTR static uint8_t  saved_channel = 0;  // 0 = not known
RTC_DATA_ATTR static uint8_t  bridge_mac[6];
RTC_DATA_ATTR static bool     bridge_known  = false;
RTC_DATA_ATTR static uint32_t wake_count    = 0;
static uint8_t  boot_event = 0;                   // CTL_EV_BOOT or CTL_EV_WAKE, for the repeats
static uint16_t boot_a = 0, boot_b = 0;           // and what it carries

//=============================================================================
static uint32_t crc32_update(
  uint32_t crc, const uint8_t *p, size_t n
)
// Plain CRC-32 (the zlib/802.3 one), bitwise: no table, so no 1KB of RAM, and
// at ~0.2s for a whole image it is not worth optimising. Written out rather
// than taken from ROM so the host's zlib.crc32 and this agree exactly.
{
  crc = ~crc;
  while(n--) {
    crc ^= *p++;
    for(int k = 0; k < 8; k++) crc = (crc >> 1) ^ (0xEDB88320u & (-(int32_t)(crc & 1)));
  }
  return ~crc;
}

//=============================================================================
static void ota_reply(
  uint8_t op, uint16_t seq
)
// One 4-byte answer to the bridge. Registering it as a peer on first contact is
// what lets a receive-only device talk back at all.
{
  if(!ota_peer_known) return;
  if(!esp_now_is_peer_exist(ota_peer)) {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, ota_peer, 6);
    peer.channel = 0;                 // whatever channel we are already locked to
    peer.encrypt = false;
    esp_now_add_peer(&peer);
  }
  uint8_t pkt[OTA_HDR] = { OTA_MAGIC, op, (uint8_t)(seq & 0xFF), (uint8_t)(seq >> 8) };
  esp_now_send(ota_peer, pkt, sizeof(pkt));
}

//=============================================================================
static void ota_abort(
  const char *why
)
{
  Serial.printf("OTA aborted: %s\n", why);
  if(ota_active && ota_handle) esp_ota_abort(ota_handle);
  ota_active = false;
  ota_handle = 0;
}

//=============================================================================
static void ota_handle_packet(
  const uint8_t *d, int len
)
// Runs in loop(), never in the radio callback. Each packet is acknowledged only
// after its bytes are actually in flash, so an ACK the bridge sees is a promise.
{
  uint8_t  op  = d[1];
  uint16_t seq = (uint16_t)d[2] | ((uint16_t)d[3] << 8);

  if(op == OTA_OP_BEGIN) {
    if(len < OTA_HDR + 8) { ota_reply(OTA_OP_NAK, seq); return; }
    memcpy(&ota_size,     d + OTA_HDR,     4);
    memcpy(&ota_want_crc, d + OTA_HDR + 4, 4);
    if(ota_active) esp_ota_abort(ota_handle);      // a restarted transfer
    ota_part = esp_ota_get_next_update_partition(NULL);
    if(!ota_part || ota_size == 0 || ota_size > ota_part->size) {
      Serial.printf("OTA refused: %lu bytes, slot %lu\n",
                    (unsigned long)ota_size,
                    (unsigned long)(ota_part ? ota_part->size : 0));
      ota_reply(OTA_OP_NAK, seq);
      return;
    }
    if(esp_ota_begin(ota_part, ota_size, &ota_handle) != ESP_OK) {
      ota_reply(OTA_OP_NAK, seq);
      return;
    }
    ota_active = true;
    ota_expect_seq = 1;
    ota_got = 0;
    ota_crc = 0;
    Serial.printf("OTA begin: %lu bytes into '%s', crc %08lx\n",
                  (unsigned long)ota_size, ota_part->label, (unsigned long)ota_want_crc);
    ota_reply(OTA_OP_ACK, seq);
    return;
  }

  if(!ota_active) { ota_reply(OTA_OP_NAK, seq); return; }

  if(op == OTA_OP_DATA) {
    if(seq + 1 == ota_expect_seq) { ota_reply(OTA_OP_ACK, seq); return; }  // ACK was lost; re-ACK, do not rewrite
    if(seq != ota_expect_seq)     { ota_reply(OTA_OP_NAK, seq); return; }  // out of order: make the bridge back up
    const uint8_t *payload = d + OTA_HDR;
    size_t n = len - OTA_HDR;
    if(esp_ota_write(ota_handle, payload, n) != ESP_OK) {
      ota_abort("flash write failed");
      ota_reply(OTA_OP_NAK, seq);
      return;
    }
    ota_crc = crc32_update(ota_crc, payload, n);
    ota_got += n;
    ota_expect_seq++;
    // Progress on the pixel itself: blue, brightening as the image lands. Shown
    // sparingly — show() is a blocking bit-bang and this is a hot path.
    if((ota_expect_seq & 0x3F) == 0) {
      uint8_t v = 4 + (uint8_t)(40ULL * ota_got / (ota_size ? ota_size : 1));
      strip.setPixelColor(0, strip.Color(0, 0, v));
      strip.show();
    }
    ota_reply(OTA_OP_ACK, seq);
    return;
  }

  if(op == OTA_OP_END) {
    if(ota_got != ota_size || ota_crc != ota_want_crc) {
      Serial.printf("OTA bad image: %lu/%lu bytes, crc %08lx want %08lx\n",
                    (unsigned long)ota_got, (unsigned long)ota_size,
                    (unsigned long)ota_crc, (unsigned long)ota_want_crc);
      ota_abort("size or CRC mismatch");
      ota_reply(OTA_OP_NAK, seq);
      return;
    }
    if(esp_ota_end(ota_handle) != ESP_OK) {          // this is where a bad image is caught
      ota_abort("esp_ota_end rejected the image");
      ota_reply(OTA_OP_NAK, seq);
      return;
    }
    if(esp_ota_set_boot_partition(ota_part) != ESP_OK) {
      ota_abort("could not point the bootloader at the new slot");
      ota_reply(OTA_OP_NAK, seq);
      return;
    }
    ota_active = false;
    ota_handle = 0;
    Serial.printf("OTA complete — booting '%s'\n", ota_part->label);
    ota_reply(OTA_OP_ACK, seq);
    delay(50);                                        // let the ACK leave
    flash(0, 40, 0, 3, 120, 120);                     // three greens = new firmware
    ESP.restart();
  }
}

//=============================================================================
static void on_colour_receive(
  const esp_now_recv_info_t *info,
  const uint8_t *data,
  int len
)
// ESP-NOW receive callback. Decodes colour packet and flags an LED update.
{
  if(len >= OTA_HDR && data[0] == OTA_MAGIC) {
    // An update packet. Copy it out and let loop() do the flash write: this is
    // the WiFi task, and stop-and-wait guarantees only one is ever in flight.
    if(ota_pending || len > (int)sizeof(ota_buf)) return;   // sender will retry
    if(info) { memcpy(ota_peer, info->src_addr, 6); ota_peer_known = true; }
    memcpy(ota_buf, data, len);
    ota_buf_len = len;
    ota_pending = true;
    return;
  }
  if(info && (!bridge_known || memcmp(bridge_mac, info->src_addr, 6) != 0)) {
    memcpy(bridge_mac, info->src_addr, 6);
    bridge_known = true;
  }
  if(len == CTL_LEN && data[0] == CTL_MAGIC0 && data[1] == CTL_MAGIC1) {
    if(data[2] == CTL_OP_SLEEP) sleep_requested = true;
    if(data[2] == CTL_OP_KEEP)  keep_requested = true;
    if(data[2] == CTL_OP_LED)   led_requested = 0x100 | data[3];
    if(data[2] == CTL_OP_REPORT) { report_asked_ms = millis(); report_asked = true; }
    return;
  }
  if(len < 3) return;
  pending_r    = data[0];
  pending_g    = data[1];
  pending_b    = data[2];
  pending_mode = (len >= 4) ? data[3] : MODE_SHOW;   // 3-byte packets = show
  pending_flags = (len >= 5) ? data[4] : 0;          // older bridges send no flags
  pending_scale = (len >= 6) ? data[5] : 255;        // nor a scale: show it as sent
  pending_update = true;
  Serial.printf("[C] R=%d G=%d B=%d mode=%d scale=%d\n", pending_r, pending_g, pending_b,
                pending_mode, pending_scale);
}

//=============================================================================
static uint32_t pixel_colour(uint8_t r, uint8_t g, uint8_t b, uint8_t scale)
// Grey -> the white element, if there is one; anything else -> RGB (see GREY MEANS WHITE).
// `scale` (255 = as sent) dims it: the bridge's dynamic brightness. Grey is
// judged before scaling, so a colour dimmed until its channels round equal
// does not jump to the white element.
{
  bool grey = (r == g && g == b);
  r = (r * scale + 127) / 255;
  g = (g * scale + 127) / 255;
  b = (b * scale + 127) / 255;
  if(led_has_w && grey) return strip.Color(0, 0, 0, r);
  return strip.Color(r, g, b);
}

//=============================================================================
static void flash(
  uint8_t r, uint8_t g, uint8_t b, int times, int on_ms, int off_ms
)
// Blink the LED a fixed colour `times`, leaving it off afterwards.
{
  for(int i = 0; i < times; i++) {
    strip.setPixelColor(0, strip.Color(r, g, b));
    strip.show();
    delay(on_ms);
    strip.clear();
    strip.show();
    if(i < times - 1) delay(off_ms);
  }
}

//=============================================================================
static void IRAM_ATTR on_trembler_edge()
{
  uint32_t now = millis();
  if(trembler_edges == 0) trembler_first_ms = now;
  trembler_edges++;
  trembler_last_ms = now;
}

//=============================================================================
static void send_event(
  uint8_t type, uint16_t a, uint16_t b
)
// One control packet to the bridge: unicast once its address is known (any
// packet from it teaches us), broadcast until then.
{
  static const uint8_t everyone[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
  const uint8_t *to = bridge_known ? bridge_mac : everyone;
  if(!esp_now_is_peer_exist(to)) {
    esp_now_peer_info_t peer = {};
    memcpy(peer.peer_addr, to, 6);
    peer.channel = 0;                 // whatever channel we are already locked to
    peer.encrypt = false;
    esp_now_add_peer(&peer);
  }
  uint8_t pkt[CTL_LEN] = { CTL_MAGIC0, CTL_MAGIC1, type,
                           (uint8_t)(a & 0xFF), (uint8_t)(a >> 8),
                           (uint8_t)(b & 0xFF), (uint8_t)(b >> 8), 0 };
  esp_now_send(to, pkt, sizeof(pkt));
}

//=============================================================================
static bool test_mode()
// True while the bridge keeps asking for jolt reports.
{
  return report_asked && millis() - report_asked_ms < REPORT_HOLD_MS;
}

//=============================================================================
static bool session_connected()
{
  return session_seen && millis() - last_session_ms < SESSION_HOLD_MS;
}

//=============================================================================
static void use_led_type(
  uint8_t t
)
// Drive the pixel as LED type t (an index into LED_TYPES), dark until the
// next colour: the pixel buffer changes size between three and four bytes.
{
  led_type  = t;
  led_has_w = LED_TYPES[t].has_w;
  strip.updateType(LED_TYPES[t].order);
  strip.clear();
  strip.show();
}

//=============================================================================
static void handle_led_request(
  uint8_t want
)
// CTL_OP_LED: change to type `want` (kept in flash) unless it is LED_QUERY or
// unknown, then say which type is in use. A colour already lit is shown again
// in the new type, re-encoded from the packet that set it.
{
  bool changed = false;
  if(want != LED_QUERY && want < LED_TYPE_COUNT && want != led_type) {
    use_led_type(want);
    Preferences prefs;
    prefs.begin("carrier", false);
    prefs.putUChar("led", want);
    prefs.end();
    changed = true;
    Serial.printf("LED type now %u (%s), saved\n", want, LED_TYPES[want].name);
    if(lit_colour) {
      lit_colour = pixel_colour(pending_r, pending_g, pending_b, pending_scale);
    }
  }
  send_event(CTL_EV_LED, led_type, changed ? 1 : 0);
}

//=============================================================================
static void show_led()
// The colour the bridge last asked for; in test mode, dim blue where that is
// dark, so "awake" can be seen from across the room.
{
  uint32_t c = lit_colour;
  if(c == 0 && test_mode()) c = strip.Color(0, 0, 40);
  strip.setPixelColor(0, c);
  strip.show();
}

//=============================================================================
static void idle_shutdown()
// Announce, drop the radio and deep-sleep until the trembler closes. Never
// returns: the wake is a restart, through setup().
{
  Serial.println("Sleeping. A jolt (or the power switch) wakes it.");
  Serial.flush();
  send_event(CTL_EV_SLEEP, 0, 0);
  if(session_connected()) delay(50);    // dark: a shutter may be open. Let the event go.
  else flash(0, 0, 40, 3, 100, 150);    // three blue blips = going to sleep
  detachInterrupt(digitalPinToInterrupt(TREMBLER_PIN));
  rtc_gpio_init((gpio_num_t)TREMBLER_PIN);
  rtc_gpio_set_direction((gpio_num_t)TREMBLER_PIN, RTC_GPIO_MODE_INPUT_ONLY);
  rtc_gpio_pulldown_dis((gpio_num_t)TREMBLER_PIN);
  rtc_gpio_pullup_en((gpio_num_t)TREMBLER_PIN);
  esp_sleep_enable_ext1_wakeup(1ULL << TREMBLER_PIN, ESP_EXT1_WAKEUP_ANY_LOW);
  esp_now_deinit();
  WiFi.mode(WIFI_OFF);
  esp_deep_sleep_start();
}

//=============================================================================
static uint8_t scan_for_channel(
  const char *target
)
// One-shot scan (retried — the AP is weak at the carrier); returns the channel of
// the strongest matching BSS, or FALLBACK_CHANNEL if not seen. No association —
// just a passive lookup so the carrier's ESP-NOW lands on the bridge's channel.
{
  for(int attempt = 1; attempt <= SCAN_ATTEMPTS; attempt++) {
    int n = WiFi.scanNetworks();
    int best_ch = -1, best_rssi = -999;
    for(int i = 0; i < n; i++) {
      if(WiFi.SSID(i) == target && WiFi.RSSI(i) > best_rssi) {
        best_rssi = WiFi.RSSI(i);
        best_ch   = WiFi.channel(i);
      }
    }
    WiFi.scanDelete();
    if(best_ch >= 1) {
      Serial.printf("AP '%s' on channel %d (RSSI %d), scan %d\n", target, best_ch, best_rssi, attempt);
      return (uint8_t)best_ch;
    }
    Serial.printf("scan %d/%d: '%s' not seen\n", attempt, SCAN_ATTEMPTS, target);
    delay(300);
  }
  Serial.printf("AP '%s' not found — using fallback channel %u\n", target, FALLBACK_CHANNEL);
  return FALLBACK_CHANNEL;
}

//=============================================================================
void setup()
{
  bool woken = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT1;
  Serial.begin(115200);
  // Up to 2s for a USB host at power-on; a jolt wake does not wait for one.
  for(int i = 0; !woken && i < 200 && !Serial; i++) delay(10);

  Preferences prefs;
  prefs.begin("carrier", true);
  uint8_t t = prefs.getUChar("led", LED_DEFAULT);
  prefs.end();
  strip.begin();
  use_led_type(t < LED_TYPE_COUNT ? t : LED_DEFAULT);
  Serial.printf("LED type %u (%s)\n", led_type, LED_TYPES[led_type].name);

  // Radio up in station mode, no association. Discover/lock the bridge's channel.
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                 // keep the radio in RX (no modem sleep)
  // A jolt wake reuses the channel found at power-on: the scan takes seconds.
  uint8_t channel = (woken && saved_channel) ? saved_channel : scan_for_channel(ssid);
  saved_channel = channel;
  esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);

  if(esp_now_init() != ESP_OK) {
    Serial.println("ERROR: esp_now_init() failed");
    flash(40, 0, 0, 3, 100, 100);       // 3 reds, then restart to retry
    delay(500);
    ESP.restart();
  }
  esp_now_register_recv_cb(on_colour_receive);

  Serial.printf("Pen carrier ready (ESP-NOW only, channel %u, MAC %s)\n",
                channel, WiFi.macAddress().c_str());
  rtc_gpio_deinit((gpio_num_t)TREMBLER_PIN);   // back from the low-power domain
  pinMode(TREMBLER_PIN, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(TREMBLER_PIN), on_trembler_edge, FALLING);

  if(woken) {
    wake_count++;
    boot_a = (uint16_t)wake_count;
    send_event(CTL_EV_WAKE, boot_a, boot_b);
    Serial.printf("Woken by the trembler (wake %lu).\n", (unsigned long)wake_count);
    wake_blip_due = true;               // shown in loop(), once we know no session is on
  } else {
    boot_a = (uint16_t)esp_sleep_get_wakeup_cause();
    boot_b = (uint16_t)esp_reset_reason();
    send_event(CTL_EV_BOOT, boot_a, boot_b);
    flash(40, 40, 40, 1, 300, 0);       // white once = powered on
  }
  boot_event = woken ? CTL_EV_WAKE : CTL_EV_BOOT;
  last_active_ms = boot_ms = millis();  // the idle clock starts now
}

//=============================================================================
void loop()
{
  if(ota_pending) {
    ota_handle_packet(ota_buf, ota_buf_len);
    ota_pending = false;
    last_active_ms = millis();      // an update in progress is not idle
  }

  if(pending_update) {
    pending_update = false;
    // SHOW lights the packet colour; PRELOAD arms it but stays dark. Every packet
    // carries the RGB, so the colour is implicitly held — a PRELOAD during the air
    // move followed by a SHOW at the stroke start lights instantly, no dark start.
    bool on = (pending_mode == MODE_SHOW);
    lit_colour = on ? pixel_colour(pending_r, pending_g, pending_b, pending_scale) : 0;
    show_led();
    // A lit colour is activity; so is a connected session (see the header).
    if(pending_r || pending_g || pending_b) last_active_ms = millis();
    if(pending_flags & FLAG_SESSION) { last_session_ms = millis(); session_seen = true; }
    // The first packet after a jolt wake says whether a session is on: green
    // once = woken by a jolt, but not into a frame being exposed.
    if(wake_blip_due) {
      wake_blip_due = false;
      if(!session_connected()) { flash(0, 40, 0, 1, 150, 0); show_led(); }
    }
  }
  if(wake_blip_due && millis() - boot_ms >= 3000) {      // no bridge about: show it
    wake_blip_due = false;
    flash(0, 40, 0, 1, 150, 0);
    show_led();
  }
  if(keep_requested) { keep_requested = false; last_active_ms = millis(); }
  uint16_t led_req = led_requested;
  if(led_req & 0x100) {
    led_requested = 0;
    handle_led_request((uint8_t)led_req);
    show_led();
  }

  // Say again that we are up: the first packets after radio start are often lost.
  static uint8_t boot_repeats = 0;
  if(boot_repeats < 3 && millis() - boot_ms >= 400UL * (boot_repeats + 1)) {
    boot_repeats++;
    send_event(boot_event, boot_a, boot_b);
  }

  // Test mode coming or going changes what a dark LED shows.
  static bool was_test = false;
  bool test = test_mode();
  if(test != was_test) { was_test = test; show_led(); }

  // A jolt is over once the trembler has been still for JOLT_GAP_MS.
  noInterrupts();
  uint32_t edges = trembler_edges;
  uint32_t first = trembler_first_ms, last = trembler_last_ms;
  if(edges && millis() - last >= JOLT_GAP_MS) trembler_edges = 0; else edges = 0;
  interrupts();
  if(edges && test) {
    send_event(CTL_EV_JOLT, (uint16_t)(edges > 0xFFFF ? 0xFFFF : edges),
               (uint16_t)(last - first > 0xFFFF ? 0xFFFF : last - first));
    strip.setPixelColor(0, led_has_w ? strip.Color(0, 0, 0, 40) : strip.Color(40, 40, 40));   // white blip
    strip.show();
    delay(40);
    show_led();
  }

  if(sleep_requested && !ota_active) idle_shutdown();    // the bridge's !csleep

  // Idle shutdown — but never with a USB host attached (see the header note),
  // and never part way through a firmware update: deep sleep there would leave
  // a half-written slot and a carrier that has to be fetched and cabled up.
  unsigned long now = millis();
  if(!Serial && !ota_active && now - last_active_ms >= IDLE_SHUTDOWN_MS) idle_shutdown();

  delay(5);
}
