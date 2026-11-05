#include "tu_text.h"

#include <stdint.h>
#include <string.h>

#include "lvgl.h"

typedef struct {
    uint32_t code;
    const char *with;
} swap_t;

static const swap_t swaps[] = {
    { 0x00A0, " " },   { 0x00B7, "\xE2\x80\xA2" }, { 0x00D7, "x" },  { 0x2007, " " },  { 0x2009, " " },
    { 0x2013, "-" },   { 0x2014, "-" },            { 0x2018, "'" },  { 0x2019, "'" },  { 0x201A, "'" },
    { 0x201C, "\"" },  { 0x201D, "\"" },           { 0x201E, "\"" }, { 0x2026, "..." }, { 0x202F, " " },
    { 0x2032, "'" },   { 0x2033, "\"" },           { 0x2190, "<-" }, { 0x2192, "->" }, { 0x2212, "-" },
    { 0x26A0, LV_SYMBOL_WARNING },
};

/* The code point at text and its length in bytes; a malformed byte stands for itself. */
static uint32_t decode(const unsigned char *text, size_t *length)
{
    unsigned char lead = text[0];
    size_t extra = lead >= 0xF0 ? 3 : lead >= 0xE0 ? 2 : lead >= 0xC0 ? 1 : 0;
    uint32_t code = extra == 3 ? lead & 0x07u : extra == 2 ? lead & 0x0Fu : extra == 1 ? lead & 0x1Fu : lead;
    for (size_t i = 1; i <= extra; i++) {
        if ((text[i] & 0xC0) != 0x80) {
            *length = 1;
            return lead;
        }
        code = code << 6 | (text[i] & 0x3Fu);
    }
    *length = extra + 1;
    return code;
}

static const char *swap_for(uint32_t code)
{
    for (size_t i = 0; i < sizeof swaps / sizeof swaps[0]; i++) {
        if (swaps[i].code == code) return swaps[i].with;
    }
    return NULL;
}

void tu_normalize(const char *text, char *out, size_t capacity)
{
    size_t used = 0;
    const unsigned char *at = (const unsigned char *)(text ? text : "");
    if (capacity == 0) return;
    while (*at) {
        size_t length;
        uint32_t code = decode(at, &length);
        const char *with = swap_for(code);
        const char *piece = with ? with : (const char *)at;
        size_t size = with ? strlen(with) : length;
        if (used + size >= capacity) break; /* never split a character */
        memcpy(out + used, piece, size);
        used += size;
        at += length;
    }
    out[used] = '\0';
}

typedef struct {
    const char *name;
    const char *symbol;
} icon_t;

/* remotes.js icon names (index.html's #i-<icon>) with an LV_SYMBOL_* equivalent. */
static const icon_t icons[] = {
    { "up", LV_SYMBOL_UP },
    { "down", LV_SYMBOL_DOWN },
    { "left", LV_SYMBOL_LEFT },
    { "right", LV_SYMBOL_RIGHT },
    { "play", LV_SYMBOL_PLAY },
    { "pause", LV_SYMBOL_PAUSE },
    { "play-pause", LV_SYMBOL_PLAY LV_SYMBOL_PAUSE },
    { "stop", LV_SYMBOL_STOP },
    { "previous", LV_SYMBOL_PREV },
    { "next", LV_SYMBOL_NEXT },
    { "rewind", LV_SYMBOL_LEFT LV_SYMBOL_LEFT },
    { "forward", LV_SYMBOL_RIGHT LV_SYMBOL_RIGHT },
    { "home", LV_SYMBOL_HOME },
    { "power", LV_SYMBOL_POWER },
    { "settings", LV_SYMBOL_SETTINGS },
    { "menu", LV_SYMBOL_BARS },
    { "plus", LV_SYMBOL_PLUS },
    { "minus", LV_SYMBOL_MINUS },
    { "mute", LV_SYMBOL_MUTE },
    { "volume", LV_SYMBOL_VOLUME_MAX },
    { "eject", LV_SYMBOL_EJECT },
};

const char *tu_icon(const char *name)
{
    if (!name) return NULL;
    for (size_t i = 0; i < sizeof icons / sizeof icons[0]; i++) {
        if (strcmp(icons[i].name, name) == 0) return icons[i].symbol;
    }
    return NULL;
}
