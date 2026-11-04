# Handheld remote handoff

Updated 2026-11-04. Research is complete; no hardware is ordered and no code
exists. Findings, sources and rejected candidates are in
[handheld-remote.md](handheld-remote.md).

## Owner directions

- Prefer a remote we flash with our own firmware, for the most control.
- Use inexpensive or older hardware for early prototypes.
- Choose hardware we can iterate on quickly once it arrives.
- Thermite implements whatever transport is right; existing APIs do not
  constrain the choice.

## Next steps

| Step | Done when |
| --- | --- |
| 1. Order a Waveshare ESP32-S3-Touch-LCD-2.8 with a LiPo cell, an M5Stack StickS3 and a bare ESP32-S3 board as the base | Hardware in hand |
| 2. Bring up LVGL in the desktop simulator and on the touch board | A Thermite room screen runs on both from one source tree |
| 3. Measure the link on the StickS3: deep sleep plus ESP-NOW against Wi-Fi light sleep | Press-to-base latency from sleep and standby current recorded for both |
| 4. Write the ADR for the remote protocol: ESP-NOW messages, base serial framing, pairing, operation IDs, state changes and the layout blob | Reviewed ADR |
| 5. Decide the firmware licence: GPL-3.0 OMOTE fork, or our own on ESP-IDF and LVGL reusing Yoke's MIT ESP32 components | Decision recorded with its reason |
| 6. Order OMOTE Rev5 or OpenRemote Rev6 boards from JLCPCB, a BuyDisplay ER-TFT028A2-4 panel and a printed case | Step 2 firmware runs on the remote |
| 7. Optional: tear down one used NEEO remote | SWD access and STM32 readout-protection level known |

Steps 2 and 3 can run in parallel. Order step 6 hardware once step 3
confirms ESP-NOW; JLCPCB assembly takes about two to four weeks.

## Open questions

- Which repository owns the remote and base firmware? Yoke already builds
  ESP32-S3 firmware with ESP-IDF v5.5.6 and authenticated updates, and
  Thermite consumes Yoke's published contracts rather than owning firmware.
- Can Yoke's per-room ESP32 IR endpoints double as ESP-NOW bases, or does the
  house need one USB base plus relay docks?
- What are the acceptable press-to-action latency and battery life targets?
