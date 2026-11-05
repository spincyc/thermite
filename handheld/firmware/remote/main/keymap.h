/*
 * Hard keys to PROTOCOL.md key arrays, and key repeat: a key whose button
 * repeats presses again 420 ms after it went down, then every 150 ms. Pure C
 * (cJSON for the screen's buttons); tested on the host by ../test.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "board_keys.h"
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

#define KEY_REPEAT_FIRST_MS 420
#define KEY_REPEAT_NEXT_MS 150

/* The key array for key, first choice first; 0 (and *ids NULL) for a key with no entry. */
size_t keymap_ids(board_key_t key, const char *const **ids);

/* The key a console line names (PROTOCOL.md, "Remote console lines"): up, down, left,
 * right, ok, back, volume_up, volume_down, mute, channel_up, channel_down, play, stop,
 * rewind, forward, power, home, settings, info, source, red, green, yellow, blue,
 * record. BOARD_KEY_NONE for any other word. */
board_key_t keymap_named(const char *name, size_t length);

/* Whether the current screen's buttons spec for id says "repeat": true. screen may be NULL. */
bool keymap_repeats(const cJSON *screen, const char *id);

/* The one key being held, for repeat. */
typedef struct {
    board_key_t key;  /* BOARD_KEY_NONE when nothing repeats */
    uint32_t next_ms; /* when it presses again */
} key_repeat_t;

/* A key went down at now_ms (after its first press was sent): starts repeating it when
 * repeats is true, and stops any other. */
void key_repeat_down(key_repeat_t *r, board_key_t key, bool repeats, uint32_t now_ms);
/* A key went up: stops repeating it. */
void key_repeat_up(key_repeat_t *r, board_key_t key);
/* Presses due by now_ms: returns the key to press again (BOARD_KEY_NONE for none) and
 * schedules the next. A late call gets one press, not the missed ones. */
board_key_t key_repeat_due(key_repeat_t *r, uint32_t now_ms);
/* Milliseconds until the next repeat, UINT32_MAX when none. */
uint32_t key_repeat_wait(const key_repeat_t *r, uint32_t now_ms);

#ifdef __cplusplus
}
#endif
