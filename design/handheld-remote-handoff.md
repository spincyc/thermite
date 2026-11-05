# Handheld remote handoff

Updated 2026-11-05. All parts are ordered. The protocol, bridge, command,
remote core, screen, simulator, and the firmware for the four boards and the
base are written ([handheld/](../handheld/README.md)), and the firmware
builds with ESP-IDF v5.5.6. Nothing has run on hardware.
Research and decisions are in [handheld-remote.md](handheld-remote.md).

## Owner directions

- Prefer a remote we flash with our own firmware, for the most control.
- Use inexpensive or older hardware for early prototypes.
- Choose hardware we can iterate on quickly once it arrives.
- Thermite implements whatever transport is right; existing APIs do not
  constrain the choice.

## Settled

| Question | Answer |
| --- | --- |
| Transport and protocol | ESP-NOW to a keyless USB base, sealed end to end: [PROTOCOL.md](../handheld/PROTOCOL.md) |
| Firmware licence | Our own, on ESP-IDF and LVGL; no OMOTE code is copied |
| Where firmware lives | `handheld/firmware/` in Thermite, excluded from releases |

## Verified on 2026-11-05, without hardware

- The C link reproduces every frame in
  [vectors.json](../handheld/vectors.json), which Node's OpenSSL sealed, with
  mbedTLS 3.6.7 as ESP-IDF builds it.
- The host build's 10 tests pass: link, remote core, base line protocol,
  board decoding, the remote app's key map, repeat and power policy (those
  three under ASan and UBSan), and every fixture rendered at five screen sizes.
- `tests/test_handheld.py` passes (32 tests). `make check` passes but for
  three tests that need `prometheus_client`, which fail the same way on `main`
  in an environment without it.
- End to end on one computer: `thermite handheld serve --mock --listen` and
  `thermite-remote-sim --script`: a bubble switched the room's source, keys
  reached the player, a held Volume + repeated (four presses in 800 ms), and
  a sleep and wake recovered the screen in the same session.
- The base and all four boards build with ESP-IDF v5.5.6 and LVGL 9.2.2
  without a warning; every line of their `sdkconfig` defaults takes effect;
  each remote app fills about 1.2 MB of its 3 MB partition.

## Next steps

[firmware/README.md](../handheld/firmware/README.md), *Unverified*, lists
what to check on each board.

| Step | Done when |
| --- | --- |
| 1. Flash the base on a DevKitC-1 and run `thermite handheld serve` | `thermite base` seen; one `sent` per `send`; only protocol lines on its USB port |
| 2. Waveshare: display colours and orientation, touch axes, enrol, drive the mock house | Its screen controls `--mock` as the simulator does |
| 3. StickS3: press-to-ack latency from deep sleep, awake and standby current | Measurements recorded in handheld-remote.md |
| 4. Against the real gateway | A handheld's commands reach openHAB and show in the command trail |
| 5. OMOTE / OpenRemote boards: i80 display orientation, TCA8418 wake key, LIS3DH lift threshold, MAX17048 | Every key works; the remote wakes by key and by lift |
| 6. Optional: tear down one used NEEO remote | SWD access and STM32 readout-protection level known |

## Known gaps

- The gateway's command trail names the bridge `tui/handheld.<host>`, one
  label for every remote. A `handheld` kind needs `web/remote/access.js` and
  `lib/oh_identity/trail.py` to accept it.
- The shared access client follows one room's lock policy, so the bridge also
  reloads the policy of each awake remote's room every 5 seconds.
- Firmware updates are over USB only.
- LVGL's built-in fonts have no accented letters; `tu_text` maps typographic
  punctuation to glyphs they have.
- ESP-NOW runs at its default 1 Mbit/s for range; a full screen is two to
  four frames.

## Open questions

- Can Yoke's per-room ESP32 IR endpoints double as ESP-NOW bases, or does the
  house need one USB base plus relay docks?
- What press-to-action latency and battery life are acceptable? Step 3
  measures both.
