/*
 * The hard keys a board can have: PROTOCOL.md's hard-key table, plus the
 * keys that have no entry there. Plain C with no ESP-IDF dependency, shared
 * by board.h, the remote app's key mapping and the host tests.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    BOARD_KEY_NONE = 0,
    BOARD_KEY_UP,
    BOARD_KEY_DOWN,
    BOARD_KEY_LEFT,
    BOARD_KEY_RIGHT,
    BOARD_KEY_OK,
    BOARD_KEY_BACK,
    BOARD_KEY_VOLUME_UP,
    BOARD_KEY_VOLUME_DOWN,
    BOARD_KEY_MUTE,
    BOARD_KEY_CHANNEL_UP,
    BOARD_KEY_CHANNEL_DOWN,
    BOARD_KEY_PLAY,
    BOARD_KEY_STOP,
    BOARD_KEY_REWIND,
    BOARD_KEY_FORWARD,
    BOARD_KEY_POWER,
    BOARD_KEY_HOME,
    BOARD_KEY_SETTINGS,
    BOARD_KEY_INFO,
    BOARD_KEY_SOURCE,
    BOARD_KEY_RED,
    BOARD_KEY_GREEN,
    BOARD_KEY_YELLOW,
    BOARD_KEY_BLUE,
    BOARD_KEY_RECORD, /* OMOTE's Record: no PROTOCOL.md entry, does nothing */
    BOARD_KEY_COUNT,
} board_key_t;

/* One edge of one key. */
typedef struct {
    board_key_t key;
    bool down;
} board_key_event_t;

#ifdef __cplusplus
}
#endif
