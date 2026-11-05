# Handheld firmware

ESP-IDF firmware for the handheld remote and its base station
([PROTOCOL.md](../PROTOCOL.md) is normative). **Nothing here has run on
hardware yet.** Every pin below comes from a vendor schematic or vendor code
and is cited, but none is bench-verified; see [Unverified](#unverified).

| Path | What |
| --- | --- |
| [remote/](remote/) | The handheld app: board, radio, LVGL glue, console enrolment, keys, sleep |
| [base/](base/) | The base station: a keyless ESP-NOW relay on its native USB port |
| [components/thermite_link/](components/thermite_link/) | Frames and the remote's session end |
| [components/thermite_remote/](components/thermite_remote/) | The remote's behaviour without hardware |
| [components/thermite_ui/](components/thermite_ui/) | The screen in LVGL 9 |
| [components/thermite_boards/](components/thermite_boards/) | One board behind `include/board.h`: power, display, touch, keys, battery, sleep, and our own small drivers |
| [host/](host/) | Host tests and the simulator |

## Boards

| Board (`THERMITE_BOARD`) | Role | Console and flashing port | Keys |
| --- | --- | --- | --- |
| `waveshare-s3-lcd28` | Waveshare ESP32-S3-Touch-LCD-2.8, the touch prototype | Native USB-C (USB-Serial-JTAG), `/dev/ttyACM*` | BOOT and PWR wake it; they send nothing |
| `m5stick-s3` | M5Stack StickS3, the link test rig (no touch) | Native USB-C (USB-Serial-JTAG), `/dev/ttyACM*` | A is Play, B is Volume + (repeats) |
| `omote-rev5` | OMOTE Rev5, the target | CH340C on USB-C, UART0, `/dev/ttyUSB*` | 24 keys and SW_PWR, PROTOCOL.md's table |
| `openremote-rev6` | OpenRemote Rev6, as OMOTE with the BuyDisplay panel | As OMOTE | As OMOTE |
| base | Any ESP32-S3-DevKitC-1 (N8 or N16R8) | Protocol on the **USB** connector (native USB-Serial-JTAG); logs on the **UART** connector (UART0) | — |

OMOTE uses GPIO19/20 (the S3's USB pins) for I2C, so it has no native USB:
its build turns the USB-Serial-JTAG peripheral off.

## Toolchain

ESP-IDF **v5.5.6**, commit `880a203ae603d08a94d06aa01e0d6761958e5e9e`, as
[Yoke](https://github.com/spincyc/yoke/tree/main/firmware/esp32) pins it.
LVGL **9.2.2** is the only external component (`components/thermite_ui/idf_component.yml`,
fetched by the ESP-IDF component manager on first build). Run from the
repository root; tools and builds stay in `.scratch`, which Git ignores.

```sh
mkdir -p .scratch/esp/tmp .scratch/esp/cache
export IDF_TOOLS_PATH="$PWD/.scratch/esp/tools"
export TMPDIR="$PWD/.scratch/esp/tmp" XDG_CACHE_HOME="$PWD/.scratch/esp/cache"
git clone --branch v5.5.6 --depth 1 --recursive --shallow-submodules \
  https://github.com/espressif/esp-idf.git .scratch/esp/esp-idf
test "$(git -C .scratch/esp/esp-idf rev-parse HEAD)" = 880a203ae603d08a94d06aa01e0d6761958e5e9e
.scratch/esp/esp-idf/install.sh esp32s3
. .scratch/esp/esp-idf/export.sh
```

## Build and flash

Each board builds in its own directory with its own `sdkconfig`; the board's
defaults (`remote/sdkconfig.defaults`, `components/thermite_ui/sdkconfig.lvgl`
and `remote/sdkconfig.board.<name>`) apply only when that `sdkconfig` does
not exist yet. Bump `PROJECT_VER` in the app's `CMakeLists.txt` for every
build you hand out; it is the `<fw>` word on the wire.

```sh
# The base station
idf.py -C handheld/firmware/base -B "$PWD/.scratch/esp/base" \
  -D SDKCONFIG="$PWD/.scratch/esp/base/sdkconfig" build
idf.py -C handheld/firmware/base -B "$PWD/.scratch/esp/base" \
  -D SDKCONFIG="$PWD/.scratch/esp/base/sdkconfig" -p /dev/ttyACM0 flash

# A remote: BOARD is one of the four above
BOARD=omote-rev5
idf.py -C handheld/firmware/remote -B "$PWD/.scratch/esp/$BOARD" \
  -D SDKCONFIG="$PWD/.scratch/esp/$BOARD/sdkconfig" -D THERMITE_BOARD=$BOARD build
idf.py -C handheld/firmware/remote -B "$PWD/.scratch/esp/$BOARD" \
  -D SDKCONFIG="$PWD/.scratch/esp/$BOARD/sdkconfig" -D THERMITE_BOARD=$BOARD \
  -p /dev/ttyUSB0 flash monitor
```

Use the port from the [Boards](#boards) table; `/dev/serial/by-id/` names
are steadier than `ttyACM0`. A remote in deep sleep has no USB-Serial-JTAG
port: wake it with a key first, or hold BOOT and press RESET (Waveshare;
the StickS3's PMIC drives GPIO0, and M5Stack says holding its power key
enters download mode). OMOTE's CH340C resets it into the bootloader itself.
The base's `idf.py monitor` belongs on its UART connector; its USB connector
carries only protocol lines.

## Enrol

Plug the base in and start `thermite handheld serve` once
([handheld/README.md](../README.md)). Then connect the remote by USB and run
`thermite handheld enroll --name NAME --port PORT`. Underneath, the bridge
writes these lines to the remote's console:

```text
thermite hello                      -> thermite remote 1 <fw> <mac> <board> 0
thermite enroll <64 hex> <mac> <ch> -> thermite enrolled   (stored in NVS; the remote restarts)
thermite forget                     -> thermite forgotten  (erased; the remote restarts)
```

An unenrolled remote shows "Enrol this remote" with its MAC and never
sleeps. To try the lines by hand: `picocom -b 115200 --omap crlf PORT`.

## Behaviour

| What | Where | How |
| --- | --- | --- |
| Boot order | `remote/main/main.c` | `board_init` (power latch, I2C, keypad), clock, NVS, console, radio, `tr_create`, the key that woke it sent with `tr_key`, then `tr_wake`, then display and `tu_create` |
| One task | `main.c` `run()` | LVGL, `tr_poll`, frames, console lines, keys and touch all on app_main's task; the ESP-NOW callback, the console reader and the key interrupt only queue data and set notification bits |
| Radio | `main.c` `radio_start` | STA started, never associated, power save off, ESP-NOW with the base as its one unencrypted peer (frames are sealed end to end); frames from other MACs are dropped |
| Clock | `main.c` `clock_*` | esp_timer while awake; across deep sleep it resumes where it stopped plus the sleep's length from the RTC timer, so it never steps back |
| Keys | `remote/main/keymap.c` | PROTOCOL.md's table; repeat 420 ms then every 150 ms when the screen's `buttons[ids[0]].repeat` is true; a late loop gets one press, not a burst |
| Power | `remote/main/power_policy.c` | Dim at 10 s idle; at 20 s, once `tr_idle`, `tr_sleep`, then deep sleep on its ack or after 300 ms; deep sleep regardless after 60 s |
| Battery | `main.c` | `tr_status` and `tu_battery` at wake and every 30 s |
| Wake | board files | OMOTE: keypad INT (GPIO8) and LIS3DH motion (GPIO2). StickS3: A and B. Waveshare: BOOT, PWR, and touch INT (GPIO4) when it is idle-high at sleep |
| Base lines | `base/main/base_proto.c` | Exactly PROTOCOL.md; one `sent` per valid `send`, in order (sends and channel changes share one queue); peers added on demand, least recently used evicted beyond ESP-NOW's 20; channel kept in NVS (default 1); lines up to 2200 bytes; a malformed line gets `thermite error <words>` and no `sent` |

## Pins

V: read from the vendor's schematic or code for that revision.
I: inferred, third-party, or conflicting. Nothing is bench-verified.
Source keys are expanded at the top of each board file.

### Waveshare ESP32-S3-Touch-LCD-2.8 (`board_waveshare_s3_lcd28.c`)

| Function | GPIO | Source | |
| --- | --- | --- | --- |
| LCD ST7789(T3) SPI3: MOSI, SCLK, CS, DC, RST | 45, 40, 42, 41, 39 | Demo `LCD_Driver/ST7789.h:21-41`, schematic | V |
| LCD format | BGR, inverted, MADCTL MX; vendor tuning sent after init | Demo `ST7789.c:38,47`, `Vernon_ST7789T.c:160-202` | V |
| Backlight (NPN, active high) | 5 | Demo `ST7789.h:24`, schematic | V |
| Touch I2C1: SDA, SCL (CST328 at 0x1A) | 1, 3 | Demo `Touch_Driver/CST328.h:12,44-49` | V |
| Touch INT (active low), RST (active low) | 4, 2 | Demo `CST328.h:46-47` | V |
| Touch CST3530 at 0x58 on later boards | same pins | Third-party report; driver probes both | I |
| BOOT key, PWR key (active low, external pull-ups) | 0, 6 | Schematic, demo `PWR_Key/PWR_Key.h:4` | V |
| Battery latch (high keeps battery power; held through deep sleep) | 7 | Demo `PWR_Key.h:5`, `PWR_Key.c:39-58`, schematic | V |
| Battery ADC1 ch 7, divider ×3 | 8 | Demo `BAT_Driver/BAT_Driver.h:13-16`, schematic R5/R11 | V |

### M5Stack StickS3 (`board_m5stick_s3.c`)

| Function | GPIO | Source | |
| --- | --- | --- | --- |
| LCD ST7789P3 135×240 SPI3: MOSI, SCLK, DC, CS, RST | 39, 40, 45, 41, 21 | M5GFX `M5GFX.cpp:3030-3049` | V |
| LCD offset 52/40, inverted, BGR | — | M5GFX `M5GFX.cpp:3053-3057` | V |
| Backlight (active high) | 38 | M5GFX `M5GFX.cpp:3062`, schematic p3 | V |
| Keys A, B (active low, external pull-ups) | 11, 12 | M5Unified `M5Unified.inl:2503-2505`, schematic p3 | V |
| Internal I2C: SDA, SCL (M5PM1 0x6E) | 47, 48 | M5Unified `M5Unified.inl:103` | V |
| LCD rail: M5PM1 GPIO2 (regs 0x10/0x11/0x13/0x16 bit 2) | PMIC | M5GFX `M5GFX.cpp:3016-3027` | V |
| Battery mV: M5PM1 0x22-0x23; charging: PMIC GPIO0 low (0x12 bit 0) | PMIC | `M5PM1_Class.inl:33`, `Power_Class.inl:443-446` | V |
| Power key | PMIC only | Schematic p1 | V |

### OMOTE Rev5 and OpenRemote Rev6 (`board_omote.c`)

| Function | GPIO | Source | |
| --- | --- | --- | --- |
| I2C: SDA, SCL (pull-ups on the switched LCD rail) | 20, 19 | FW `displaydriver.h:19-20`, netlist | V |
| LCD rail enable (active low) | 38 | FW `displaydriver.h:23`, netlist Q4 | V |
| Backlight (active low) | 9 | FW `displaydriver.h:22`, netlist Q3 | V |
| LCD ILI9341 i80 8-bit: CS, DC, WR, RD | 39, 40, 41, 42 | FW `displaydriver.h:24-27`, netlist J2 | V |
| LCD D0-D7 | 48, 47, 21, 14, 13, 12, 11, 10 | FW `displaydriver.h:28-35`, netlist | V |
| LCD reset | none (RC); software reset | Netlist | V |
| LCD MADCTL | MY, MX, BGR (LovyanGFX path) vs MX, BGR (Arduino_GFX path) | FW `displaydriver0`/`displaydriver2` | I: conflict |
| Touch FT6206 at 0x38, INT | not connected: polled, no touch wake | Netlist J2-46 | V |
| TCA8418 at 0x34, INT (active low, pull-up to always-on 3V3) | 8 | FW `keypad_keys_hal_esp32.cpp:22,85`, netlist R36 | V |
| Matrix ROW0-4 × COL0-4; SW_PWR on TCA ROW5 (GPI) | — | FW `keypad_keys_hal_esp32.cpp:29-35`, netlist S17 | V |
| Key backlight (active high) | 46 | FW `keypad_keys_hal_esp32.cpp:23` | V |
| LIS3DH at 0x19, INT1 (push-pull, set active low) | 2 | FW `sleep_hal_esp32.cpp:22,39`, netlist U5 | V |
| MAX17048 at 0x36 | I2C | Netlist U9; address from the datasheet | V wiring, I address |
| Charging (TP4056 ~CHRG, low = charging) | 1 | FW `battery_hal_esp32.cpp:15,40` | V |
| Console UART0: TX, RX (CH340C) | 43, 44 | Netlist U3/U7 | V |
| Kept off in sleep: SD power (active low), IR receiver power | 16, 6 | FW `sd_card_hal_esp32.cpp`, `infrared_receiver_hal_esp32.cpp` | V |

Key map ([row][column], from OMOTE's firmware): row 0 Home (our choice for
its "?"; the schematic shows no switch there), Play, Settings, Rewind, Stop;
row 1 Forward, Power, Back, Up, Left; row 2 Blue, Channel −, Red, Yellow,
Green; row 3 Info, Right, Volume +, OK, Down; row 4 Source, Channel +,
Volume −, Mute, Record (no PROTOCOL.md entry: does nothing).

Rev6 shares Rev5's ESP32 nets (GPIO45 powers its microphone, unused here).
Its BuyDisplay ER-TFT028A2-4 needs colour inversion (the default for
`openremote-rev6`); BuyDisplay says ILI9341, and OpenRemote also offers an
ST7789V panel (`CONFIG_THERMITE_OMOTE_PANEL_ST7789`).

## Bring-up overrides

`idf.py ... menuconfig`, "Thermite board": display bus clock (default 40 MHz
SPI, 10 MHz i80), colour inversion, display mirror X/Y, touch swap and
mirror X/Y. Each board's defaults are its best-sourced values.

## Host tests and simulator

One host build runs every portable part: the link against
[../vectors.json](../vectors.json), the remote core, the device apps' and
drivers' portable C under the address and undefined-behaviour sanitizers, and
every fixture in [host/fixtures](host/fixtures) rendered at five screen sizes.
The same `thermite_remote` and `thermite_ui` run in the simulator
([host/sim.c](host/sim.c) documents every option; SDL2 for the window):

```sh
handheld/firmware/host/fetch-deps.sh .scratch/deps
cmake -S handheld/firmware/host -B .scratch/build/host -G Ninja -DTHERMITE_DEPS="$PWD/.scratch/deps"
cmake --build .scratch/build/host && ctest --test-dir .scratch/build/host --output-on-failure
.scratch/build/host/thermite-remote-sim --connect "$XDG_RUNTIME_DIR/thermite-base.sock" --key-file FILE
```

`FILE` is the key file `thermite handheld enroll --simulator` printed.

`base/test` covers the base's line parser, formatter, base64, line assembly
and peer eviction; `components/thermite_boards/test` the TCA8418, FT6x06,
CST328, CST3530 and MAX17048 decoding, battery curve, debouncing and touch
mapping; `remote/test` the key table, repeat timing, console lines and the
dim and sleep policy.

On 2026-11-05 the base and all four boards built with ESP-IDF v5.5.6 without
a warning, every line of their `sdkconfig` defaults took effect, and each
remote app is about 1.2 MB of its 3 MB partition (the base about 0.7 MB of
1.5 MB). None has yet run on a board.

## Unverified

Check these first when the hardware arrives, in this order.

1. **Base**: `thermite hello` on its USB port, a `send` answered by one
   `sent`, logs only on the UART connector.
2. **Displays**: colours and orientation on each board. OMOTE's MADCTL
   conflict (mirror Y) and its i80 clock (10 MHz chosen; OMOTE runs 40 MHz,
   beyond the ILI9341's write cycle); Waveshare's vendor tuning sent after
   esp_lcd's init.
3. **Touch axes**: CST328 raw range (logged at boot) and orientation; the
   FT6206's mirroring on OMOTE.
4. **Wake loops**: deep sleep must not wake at once. Watch for a stuck-low
   keypad INT, a latched LIS3DH INT1, or a CST328 INT held low (each is
   excluded when low at sleep, and logged).
5. **Wake latency and the wake key**: that OMOTE's TCA8418 FIFO still holds
   the press after the ESP32 boots, and that StickS3's A and B stay powered
   (the deep-sleep rail) to wake it.
6. **Power**: Waveshare stays on battery through deep sleep (GPIO7 held);
   StickS3's PMIC keeps the ESP32 rail with its watchdog off; OMOTE sleep
   current with the LCD rail off.
7. **Thresholds**: LIS3DH lift-to-wake (128 mg high-pass, chosen here, not
   OMOTE's); battery curves (Waveshare's ADC through a ×3 divider, StickS3's
   linear 3.3–4.1 V).
8. **StickS3 key choice**: A is Play and B is Volume +, our test-rig choice.
