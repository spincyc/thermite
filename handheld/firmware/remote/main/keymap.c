#include "keymap.h"

#include <string.h>

#define IDS(...) {(const char *const[]){__VA_ARGS__}, sizeof((const char *const[]){__VA_ARGS__}) / sizeof(char *)}

typedef struct {
    const char *const *ids;
    size_t count;
} key_ids_t;

/* PROTOCOL.md, "Hard keys". */
static const key_ids_t KEYS[BOARD_KEY_COUNT] = {
    [BOARD_KEY_UP] = IDS("up"),
    [BOARD_KEY_DOWN] = IDS("down"),
    [BOARD_KEY_LEFT] = IDS("left"),
    [BOARD_KEY_RIGHT] = IDS("right"),
    [BOARD_KEY_OK] = IDS("ok"),
    [BOARD_KEY_BACK] = IDS("back"),
    [BOARD_KEY_VOLUME_UP] = IDS("volume_up"),
    [BOARD_KEY_VOLUME_DOWN] = IDS("volume_down"),
    [BOARD_KEY_MUTE] = IDS("mute"),
    [BOARD_KEY_CHANNEL_UP] = IDS("channel_up"),
    [BOARD_KEY_CHANNEL_DOWN] = IDS("channel_down"),
    [BOARD_KEY_PLAY] = IDS("play_pause", "play"),
    [BOARD_KEY_STOP] = IDS("stop"),
    [BOARD_KEY_REWIND] = IDS("rewind", "previous"),
    [BOARD_KEY_FORWARD] = IDS("fast_forward", "next"),
    [BOARD_KEY_POWER] = IDS("power"),
    [BOARD_KEY_HOME] = IDS("home", "menu"),
    [BOARD_KEY_SETTINGS] = IDS("settings", "menu"),
    [BOARD_KEY_INFO] = IDS("info", "guide"),
    [BOARD_KEY_SOURCE] = IDS("input"),
    [BOARD_KEY_RED] = IDS("red"),
    [BOARD_KEY_GREEN] = IDS("green"),
    [BOARD_KEY_YELLOW] = IDS("yellow"),
    [BOARD_KEY_BLUE] = IDS("blue"),
    /* BOARD_KEY_RECORD and BOARD_KEY_NONE: no entry. */
};

static const char *const NAMES[BOARD_KEY_COUNT] = {
    [BOARD_KEY_UP] = "up",
    [BOARD_KEY_DOWN] = "down",
    [BOARD_KEY_LEFT] = "left",
    [BOARD_KEY_RIGHT] = "right",
    [BOARD_KEY_OK] = "ok",
    [BOARD_KEY_BACK] = "back",
    [BOARD_KEY_VOLUME_UP] = "volume_up",
    [BOARD_KEY_VOLUME_DOWN] = "volume_down",
    [BOARD_KEY_MUTE] = "mute",
    [BOARD_KEY_CHANNEL_UP] = "channel_up",
    [BOARD_KEY_CHANNEL_DOWN] = "channel_down",
    [BOARD_KEY_PLAY] = "play",
    [BOARD_KEY_STOP] = "stop",
    [BOARD_KEY_REWIND] = "rewind",
    [BOARD_KEY_FORWARD] = "forward",
    [BOARD_KEY_POWER] = "power",
    [BOARD_KEY_HOME] = "home",
    [BOARD_KEY_SETTINGS] = "settings",
    [BOARD_KEY_INFO] = "info",
    [BOARD_KEY_SOURCE] = "source",
    [BOARD_KEY_RED] = "red",
    [BOARD_KEY_GREEN] = "green",
    [BOARD_KEY_YELLOW] = "yellow",
    [BOARD_KEY_BLUE] = "blue",
    [BOARD_KEY_RECORD] = "record",
};

board_key_t keymap_named(const char *name, size_t length)
{
    for (int key = BOARD_KEY_NONE + 1; key < BOARD_KEY_COUNT; key++) {
        if (NAMES[key] && strlen(NAMES[key]) == length && memcmp(NAMES[key], name, length) == 0) {
            return (board_key_t)key;
        }
    }
    return BOARD_KEY_NONE;
}

size_t keymap_ids(board_key_t key, const char *const **ids)
{
    if ((unsigned)key >= BOARD_KEY_COUNT || KEYS[key].count == 0) {
        *ids = NULL;
        return 0;
    }
    *ids = KEYS[key].ids;
    return KEYS[key].count;
}

bool keymap_repeats(const cJSON *screen, const char *id)
{
    const cJSON *buttons = cJSON_GetObjectItemCaseSensitive(screen, "buttons");
    const cJSON *spec = cJSON_GetObjectItemCaseSensitive(buttons, id);
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(spec, "repeat"));
}

void key_repeat_down(key_repeat_t *r, board_key_t key, bool repeats, uint32_t now_ms)
{
    r->key = repeats ? key : BOARD_KEY_NONE;
    r->next_ms = now_ms + KEY_REPEAT_FIRST_MS;
}

void key_repeat_up(key_repeat_t *r, board_key_t key)
{
    if (r->key == key) r->key = BOARD_KEY_NONE;
}

board_key_t key_repeat_due(key_repeat_t *r, uint32_t now_ms)
{
    if (r->key == BOARD_KEY_NONE || (int32_t)(now_ms - r->next_ms) < 0) return BOARD_KEY_NONE;
    r->next_ms += KEY_REPEAT_NEXT_MS;
    /* A late caller gets one press, not a burst of the ones it missed. */
    if ((int32_t)(now_ms - r->next_ms) >= 0) r->next_ms = now_ms + KEY_REPEAT_NEXT_MS;
    return r->key;
}

uint32_t key_repeat_wait(const key_repeat_t *r, uint32_t now_ms)
{
    if (r->key == BOARD_KEY_NONE) return UINT32_MAX;
    int32_t wait = (int32_t)(r->next_ms - now_ms);
    return wait > 0 ? (uint32_t)wait : 0;
}
