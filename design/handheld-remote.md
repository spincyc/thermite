# Handheld remote hardware

Research, 2026-11-04. Choose a handheld remote whose firmware we write,
physically close to the Control4 Halo Touch, and the radio link Thermite
implements for it. Prices and availability are as found on that date; the
[handoff](handheld-remote-handoff.md) tracks what happens next.

## Recommendation

Prototype now on ESP32-S3 touch boards, and build toward the open-source
**OMOTE** remote family as the first Halo-shaped target. Carry button presses
over **ESP-NOW** to a base station connected to the Thermite server, and use
Wi-Fi only while docked, for layout sync and updates.

No assembled, Halo-shaped remote on sale accepts our own firmware. OMOTE is
the only flashable design in the right shape, and it is built from published
files. Its ESP32-S3 and LVGL stack matches inexpensive development boards, so
prototype code carries over to the remote unchanged.

## Requirements

| Requirement | Meaning |
| --- | --- |
| Firmware control | We write and flash the firmware ourselves; a vendor UI layer or app sandbox is not enough |
| Inexpensive early hardware | Prototypes cost tens of dollars and arrive within days |
| Fast iteration | USB flashing without opening the case, mask-ROM recovery, serial logs, a desktop UI simulator and published schematics |
| Halo-like body | Stick shape, color touchscreen above a D-pad, volume, channel and transport keys, and a charging dock |

## Reference: Halo Touch

| Property | C4-HALO-TS |
| --- | --- |
| Screen | 3.2-inch 480×800 touch LCD, 291 ppi |
| Body | Anodized aluminum, 193.6 × 51.5 × 19 mm, 165 g |
| Battery and dock | 1900 mAh, contact-charging USB-C dock; the remote has no USB data port |
| Radio | Wi-Fi a/b/g/n/ac only; the datasheet lists no BLE, Zigbee or IR |
| Processor | Cortex-A7 at 720 MHz with a Cortex-M4 coprocessor; NXP i.MX 7ULP is inferred, not confirmed |
| Controller link | Wi-Fi to a Control4 Director, OS 3.3.2 or later, QR-code setup |

[Datasheet](https://docs.control4.com/docs/product/halo-remote-touch/data-sheet/english/latest)
(rev. 2023-08-24);
[firmware 2.4.3 notes](https://help.snapone.com/halo-ig/Content/FW%20RN/Version%202.4.3.htm)
(2026-01-15).

Existing Halo remotes are not a practical platform. No root, ADB, bootloader
unlock or protocol description is public. The remote has no data port, it pairs
to the Director using certificates, and Control4's licence prohibits reverse
engineering. Opening one unit to look for UART or boot-mode pads is a possible
research spike, not a prototype path.

## Flashable hardware

| Hardware | Availability | Screen, keys, battery | Processor and radios | Iteration loop | Halo likeness |
| --- | --- | --- | --- | --- | --- |
| [Waveshare ESP32-S3-Touch-LCD-2.8](https://waveshare.com/ESP32-S3-Touch-LCD-2.8.htm) | New, $25.99 | 2.8-inch 240×320 capacitive touch, no keys, battery connector (battery not included), QMI8658 IMU | ESP32-S3; Wi-Fi, BLE, ESP-NOW | USB-C to mask ROM, OTA, LVGL simulator; first custom screen on day one | 1, a bare board |
| [M5Stack StickS3](https://www.robotshop.com/products/m5sticks3-esp32s3-mini-iot-dev) | New, about $21.50–28.90 (released 2026-01-23) | 1.14-inch screen without touch, two keys, 250 mAh, BMI270 IMU, IR transmit and receive | ESP32-S3-PICO-1-N8R8; Wi-Fi, BLE, ESP-NOW | USB-C to mask ROM | 2, a small stick |
| [OMOTE Rev5](https://github.com/OMOTE-Community/OMOTE-Hardware) | Build only: JLCPCB-assembled PCB, 3D-printed case; roughly $80–150 per unit in a small batch (estimate) | 2.8-inch 240×320 capacitive touch, 25 keys, 2000 mAh, LIS3DH lift-to-wake, MAX17048 fuel gauge | ESP32-S3-WROOM-1-N16R8; Wi-Fi, BLE, ESP-NOW, IR | CH340C USB-C with auto-reset, mask ROM, OTA, LVGL simulator, KiCad sources | 4, stick with touchscreen above keys, printed plastic |
| [OpenRemote Rev6](https://github.com/LORDSn1per/OpenRemote-Hardware) | Build only, OMOTE fork | Adds the in-production BuyDisplay ER-TFT028A2-4 panel, an I2S microphone and a pogo-pin dock input | As OMOTE; the dock design uses an ESP32-C3 and CC1101 | As OMOTE; the dock PCB is untested | 4 |
| Used NEEO remote | Used only, price not verified | 3-inch 480×800 touch, full key set, aluminum | STM32F429 (community FAQ); Wi-Fi and 802.15.4 | SWD reflash is possible only if the pads are reachable and readout protection is not at level 2; unknown until a teardown | 5, the Halo Touch's ancestor |

The OMOTE parts list confirms the ESP32-S3 module, TCA8418 key scanner,
LIS3DH accelerometer, MAX17048 gauge, TP4056 charger and CH340C USB bridge.
OMOTE's original Adafruit 2770 display is discontinued and suffers ghost
touches; OpenRemote Rev6 replaces it.

The NEEO processor is identified only in a single aside in the
[FindingNEEO FAQ](https://github.com/jac459/neeo2021onward/blob/main/FAQ.md).
Without reflashing, the remote renders server-supplied `gui_xml` from an
emulated Brain such as
[neeo-custom-brain](https://github.com/sbaechler/neeo-custom-brain), which still
needs a physical Brain for 6LoWPAN discovery and pairing.

## Closed or application-level hardware

| Hardware | Status, 2026-11-04 | What we could control | Reason not chosen |
| --- | --- | --- | --- |
| Unfolded Circle Remote 3 | Pre-order, €329, or €419 with Dock 3 | The GPL [remote-ui](https://github.com/unfoldedcircle/remote-ui) Qt application and on-device [integration drivers](https://unfoldedcircle.github.io/core-api/) | Closed core OS with no root; small vendor; owners report about 2 s wake and failed first presses after wake. The best closed fallback |
| Unfolded Circle Remote Two | Discontinued, used about $250 | As Remote 3 | As Remote 3 |
| Sanytron Astrion HA100 | First batch $145 (2025); current price unverified | Our Android app, installed over ADB through an internal USB-C port | MT6580 with 1 GB RAM; application-level only; Wi-Fi only |
| PARAM KEY | Beta waitlist (announced 2026-11-02) | ESPHome firmware; [repository](https://github.com/paramworks-git/param-key) licence shows as unasserted on GitHub | 2-inch screen, not yet available; worth watching for its ESP-NOW design |
| OpenMote | Crowd Supply, $99, delivery about February 2027 | ESP32-S3 firmware | No screen |
| Sofabaton X2 | New, $349.99 | MQTT button presses and a reverse-engineered hub protocol | The screen cannot be customized or show our state |
| Logitech Harmony Elite and 950 | Discontinued, used only | Hub API | No custom firmware; reconfiguration depends on Logitech servers |
| SwitchBot Universal Remote | New, $59.99 | Cloud API or Matter bridge | Screen is not touch; individual buttons are not exposed |
| Crestron TSR-310 | New about €1,350; one used unit sold for $200 | Emulated processor over CIP, port 41794 | The UI must be built in Crestron's dealer-only VT Pro-e |
| Savant Pro Remote X2, RTI, URC, ELAN, Josh | Various | None documented | No documented path to run our own code |

## Radio link

Wi-Fi alone makes a poor sleeping remote. An ESPHome touch remote on a 2.8-inch
ESP32-S3 board takes
[about 5 s](https://github.com/RWTL/esphome-touch-remote) to reach Wi-Fi after
deep sleep. Remote 3 owners report failed first presses after wake. Three
independent remote projects use an ESP-NOW bridge instead:
[OMOTE Hub](https://github.com/OMOTE-Community/OMOTE-Firmware/pull/100) (open
since 2025-03-11, claims about 450 ms faster than MQTT over Wi-Fi),
[ESPHomeRemote](https://github.com/Aggro600/ESPHomeRemote) (the dock relays
ESP-NOW) and PARAM KEY.

Proposed shape, to be specified in an ADR before implementation:

- The remote sends key events over ESP-NOW to a base station. Each event
  carries a pairing identity, room, button and operation ID. Bench work starts
  with an ESP32 on the Thermite server's USB port.
- Yoke's ESP32-S3 IR endpoint
  ([ADR 0025](https://github.com/spincyc/yoke/blob/main/doc/decisions/0025-esp32-rmt-ir-endpoint-itach-subset.md))
  uses W5500 Ethernet and leaves its radio free, so per-room IR endpoints
  could also receive ESP-NOW and serve as bases. That needs validating
  alongside RMT IR output; Yoke's hardware checks are not yet run.
- The base reports events to Thermite over an authenticated local protocol
  with bounded messages and operation IDs, so a retried event is never
  applied twice.
- A Thermite remote integration maps events to existing openHAB Items and
  pushes compact state changes back: source, volume, lock state, now playing.
- The remote caches a layout derived from the house's `REMOTE_LAYOUTS`
  ([web/remote/remotes.js](../web/remote/remotes.js)) and refreshes it over
  Wi-Fi while docked.
- Pair each remote with an individually enrolled key. Generate no
  fleet-wide credentials. ESP-IDF limits how many ESP-NOW peers can be
  encrypted; check the configured limit against the number of remotes and
  docks.

Thread or other 802.15.4 links need different silicon (ESP32-C6, ESP32-H2 or
nRF). No handheld with a touchscreen on that silicon was found for sale.

## Risks

- **Body quality.** OMOTE is printed plastic with a 240×320 panel; the Halo Touch
  is aluminum at 480×800. Matching it needs a custom enclosure later, or a
  successful NEEO takeover.
- **Licensing.** OMOTE hardware and firmware are GPL-3.0. A distributed fork of
  its firmware must stay GPL-3.0. Thermite is unaffected, because it only talks
  to the remote over a protocol. OpenRemote's firmware repository has no
  licence, so its code cannot be reused. Our own firmware on ESP-IDF and LVGL
  avoids both constraints, and can reuse Yoke's MIT-licensed
  [ESP32 firmware](https://github.com/spincyc/yoke/tree/main/firmware/esp32):
  pinned ESP-IDF v5.5.6, per-device credentials and authenticated local
  updates after the first USB flash
  ([ADR 0019](https://github.com/spincyc/yoke/blob/main/doc/decisions/0019-esp32-local-ethernet-firmware-updates.md)).
- **Supply.** OMOTE has no assembled units or kits on Tindie, Lectronz or
  Elecrow. The Freenove FNK0104 board is sold out at its store.

## Not verified

Used prices for the NEEO remote and Harmony remotes; the Astrion's current
price; the Remote 3's screen resolution; OMOTE's claimed six-month battery
life; the NEEO remote's Wi-Fi module and readout protection; whether a
Control4-branded NEEO remote can pair with a NEEO Brain again; whether the Halo
has IR.
