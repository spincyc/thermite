#include "console_proto.h"

#include <stdio.h>
#include <string.h>

static const char PREFIX[] = "thermite ";
#define PREFIX_LEN (sizeof PREFIX - 1)

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static bool hex_bytes(const char *text, size_t length, uint8_t *out, size_t count)
{
    if (length != count * 2) return false;
    for (size_t i = 0; i < count; i++) {
        int hi = hex_value(text[2 * i]), lo = hex_value(text[2 * i + 1]);
        if (hi < 0 || lo < 0) return false;
        out[i] = (uint8_t)(hi << 4 | lo);
    }
    return true;
}

static bool mac_parse(const char *text, size_t length, uint8_t mac[CP_MAC_LEN])
{
    if (length != 17) return false;
    for (int i = 0; i < CP_MAC_LEN; i++) {
        if (i < CP_MAC_LEN - 1 && text[i * 3 + 2] != ':') return false;
        if (!hex_bytes(text + i * 3, 2, &mac[i], 1)) return false;
    }
    return true;
}

/* As base_proto.c: one space between words, the cursor stops after the word. */
static bool word(const char **cursor, const char *end, const char **start, size_t *length)
{
    const char *p = *cursor;
    if (p < end && *p == ' ') p++;
    if (p >= end) return false;
    const char *q = p;
    while (q < end && *q != ' ') q++;
    *start = p;
    *length = (size_t)(q - p);
    *cursor = q;
    return *length > 0;
}

static bool is(const char *text, size_t length, const char *literal)
{
    return strlen(literal) == length && memcmp(text, literal, length) == 0;
}

static void fail(cp_command_t *command, const char *error)
{
    command->kind = CP_ERROR;
    command->error = error;
}

void cp_parse(const char *line, size_t length, cp_command_t *command)
{
    command->kind = CP_IGNORE;
    command->error = NULL;
    if (length && line[length - 1] == '\r') length--;
    if (length < PREFIX_LEN || memcmp(line, PREFIX, PREFIX_LEN) != 0) return;
    const char *cursor = line + PREFIX_LEN, *end = line + length;
    const char *verb, *key, *mac, *channel;
    size_t verb_n, key_n, mac_n, channel_n;
    if (!word(&cursor, end, &verb, &verb_n)) return fail(command, "empty command");
    if (is(verb, verb_n, "hello") || is(verb, verb_n, "forget")) {
        if (cursor < end) return fail(command, "no arguments expected");
        command->kind = verb_n == 5 ? CP_HELLO : CP_FORGET;
        return;
    }
    if (is(verb, verb_n, "enroll")) {
        if (!word(&cursor, end, &key, &key_n) || !word(&cursor, end, &mac, &mac_n) ||
            !word(&cursor, end, &channel, &channel_n) || cursor < end)
            return fail(command, "enroll needs key mac channel");
        if (!hex_bytes(key, key_n, command->key, CP_KEY_LEN)) return fail(command, "key must be 64 hex digits");
        if (!mac_parse(mac, mac_n, command->base)) return fail(command, "base mac is malformed");
        unsigned value = 0;
        bool ok = channel_n >= 1 && channel_n <= 2 && channel[0] != '0';
        for (size_t i = 0; ok && i < channel_n; i++) {
            ok = channel[i] >= '0' && channel[i] <= '9';
            value = value * 10 + (unsigned)(channel[i] - '0');
        }
        if (!ok || value < 1 || value > 13) return fail(command, "channel must be 1 to 13");
        command->kind = CP_ENROLL;
        command->channel = (uint8_t)value;
        return;
    }
    fail(command, "unknown command");
}

size_t cp_format_remote(char *out, size_t capacity, const char *fw, const uint8_t mac[CP_MAC_LEN], const char *board,
                        bool enrolled)
{
    int n = snprintf(out, capacity, "thermite remote 1 %s %02x:%02x:%02x:%02x:%02x:%02x %s %d\n", fw, mac[0], mac[1],
                     mac[2], mac[3], mac[4], mac[5], board, enrolled ? 1 : 0);
    if (n < 0 || (size_t)n >= capacity) {
        if (capacity) out[0] = '\0';
        return 0;
    }
    return (size_t)n;
}

cp_line_event_t cp_lines_push(cp_lines_t *lines, char byte)
{
    if (lines->ready) {
        lines->ready = false;
        lines->length = 0;
    }
    if (byte == '\n') {
        if (lines->overflow) {
            lines->overflow = false;
            lines->length = 0;
            return CP_LINE_NONE;
        }
        lines->data[lines->length] = '\0';
        lines->ready = true;
        return CP_LINE_READY;
    }
    if (lines->overflow) return CP_LINE_NONE;
    if (lines->length >= CP_LINE_MAX) {
        lines->overflow = true;
        lines->length = 0;
        return CP_LINE_TOO_LONG;
    }
    lines->data[lines->length++] = byte;
    return CP_LINE_NONE;
}
