/*
 * The remote's screen in LVGL 9: the room, its bubbles, the buttons of the
 * layout on show, the volume line, toasts and the input list, drawn from what
 * thermite_remote holds. Taps call thermite_remote's operations. One layout
 * serves 240x320 portrait and larger; a compact one serves small screens such
 * as 135x240. Call from the LVGL task only.
 */
#pragma once

#include <stdbool.h>

#include "lvgl.h"
#include "thermite_remote.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Which buttons the board has as hard keys; the screen leaves those out where it is short of room. */
typedef struct {
    bool dpad;     /* Up, Down, Left, Right, OK, Back */
    bool volume;   /* Volume +, Volume -, Mute */
    bool channel;  /* Channel +, Channel - */
    bool playback; /* Play, Stop, Rewind, Forward */
} tu_keys_t;

/* Builds the screen on display's active screen. */
void tu_create(lv_display_t *display, tr_t *remote, const tu_keys_t *keys);

/* Redraws what changed (TR_CHANGED_* bits, from tr_platform_t.changed). */
void tu_changed(unsigned what);

/* The battery mark: percent 0-100 or -1 when unknown. */
void tu_battery(int percent, bool charging);

#ifdef __cplusplus
}
#endif
