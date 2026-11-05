#include "base_proto.h"

#include <stdio.h>
#include <string.h>

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static const char PREFIX[] = "thermite ";
#define PREFIX_LEN (sizeof PREFIX - 1)

size_t bp_base64_encode(const uint8_t *data, size_t length, char *out, size_t capacity)
{
    size_t need = (length + 2) / 3 * 4;
    if (need + 1 > capacity) return 0;
    size_t o = 0;
    for (size_t i = 0; i < length; i += 3) {
        uint32_t v = (uint32_t)data[i] << 16;
        if (i + 1 < length) v |= (uint32_t)data[i + 1] << 8;
        if (i + 2 < length) v |= data[i + 2];
        out[o++] = B64[(v >> 18) & 63];
        out[o++] = B64[(v >> 12) & 63];
        out[o++] = i + 1 < length ? B64[(v >> 6) & 63] : '=';
        out[o++] = i + 2 < length ? B64[v & 63] : '=';
    }
    out[o] = '\0';
    return o;
}

static int b64_value(char c)
{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

int bp_base64_decode(const char *text, size_t length, uint8_t *out, size_t capacity)
{
    if (length % 4 != 0) return -1;
    size_t o = 0;
    for (size_t i = 0; i < length; i += 4) {
        bool last = i + 4 == length;
        int v[4];
        int pad = 0;
        for (int j = 0; j < 4; j++) {
            char c = text[i + j];
            if (c == '=' && last && j >= 2) {
                v[j] = 0;
                pad++;
                continue;
            }
            if (pad) return -1; /* data after padding */
            v[j] = b64_value(c);
            if (v[j] < 0) return -1;
        }
        uint32_t bits = (uint32_t)v[0] << 18 | (uint32_t)v[1] << 12 | (uint32_t)v[2] << 6 | (uint32_t)v[3];
        /* Canonical form only: the bits padding hides must be zero. */
        if ((pad == 1 && (bits & 0xff)) || (pad == 2 && (bits & 0xffff))) return -1;
        size_t n = 3 - (size_t)pad;
        if (o + n > capacity) return -1;
        out[o++] = (uint8_t)(bits >> 16);
        if (n > 1) out[o++] = (uint8_t)(bits >> 8);
        if (n > 2) out[o++] = (uint8_t)bits;
    }
    return (int)o;
}

static int hex_value(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

bool bp_mac_parse(const char *text, size_t length, uint8_t mac[BP_MAC_LEN])
{
    if (length != 17) return false;
    for (int i = 0; i < BP_MAC_LEN; i++) {
        int hi = hex_value(text[i * 3]), lo = hex_value(text[i * 3 + 1]);
        if (hi < 0 || lo < 0) return false;
        if (i < BP_MAC_LEN - 1 && text[i * 3 + 2] != ':') return false;
        mac[i] = (uint8_t)(hi << 4 | lo);
    }
    return true;
}

void bp_mac_format(const uint8_t mac[BP_MAC_LEN], char out[18])
{
    snprintf(out, 18, "%02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

/* Splits off the next word, after exactly one separating space when the cursor is on
 * one; returns false at the end or on an empty word (two spaces). The cursor stops on
 * the space after the word, so "cursor < end" afterwards means more text follows. */
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

/* A decimal number without sign or leading zeros, in [min, max]. */
static bool number(const char *text, size_t length, unsigned long min, unsigned long max, unsigned long *value)
{
    if (length == 0 || length > 5 || (text[0] == '0' && length > 1)) return false;
    unsigned long v = 0;
    for (size_t i = 0; i < length; i++) {
        if (text[i] < '0' || text[i] > '9') return false;
        v = v * 10 + (unsigned long)(text[i] - '0');
    }
    if (v < min || v > max) return false;
    *value = v;
    return true;
}

static bool is(const char *text, size_t length, const char *literal)
{
    return strlen(literal) == length && memcmp(text, literal, length) == 0;
}

static void fail(bp_command_t *command, const char *error)
{
    command->kind = BP_ERROR;
    command->error = error;
}

void bp_parse(const char *line, size_t length, bp_command_t *command)
{
    command->kind = BP_IGNORE;
    command->error = NULL;
    command->length = 0;
    if (length && line[length - 1] == '\r') length--;
    if (length < PREFIX_LEN || memcmp(line, PREFIX, PREFIX_LEN) != 0) return;
    const char *cursor = line + PREFIX_LEN, *end = line + length;
    const char *verb, *a, *b, *c, *extra;
    size_t verb_n, a_n, b_n, c_n, extra_n;
    if (!word(&cursor, end, &verb, &verb_n)) return fail(command, "empty command");

    if (is(verb, verb_n, "hello")) {
        if (cursor < end) return fail(command, "hello takes no arguments");
        command->kind = BP_HELLO;
        return;
    }
    if (is(verb, verb_n, "channel")) {
        unsigned long channel;
        if (!word(&cursor, end, &a, &a_n) || cursor < end || !number(a, a_n, BP_CHANNEL_MIN, BP_CHANNEL_MAX, &channel))
            return fail(command, "channel must be 1 to 13");
        command->kind = BP_CHANNEL;
        command->channel = (uint8_t)channel;
        return;
    }
    if (is(verb, verb_n, "send")) {
        unsigned long id;
        if (!word(&cursor, end, &a, &a_n) || !word(&cursor, end, &b, &b_n) || !word(&cursor, end, &c, &c_n) ||
            word(&cursor, end, &extra, &extra_n) || cursor < end)
            return fail(command, "send needs id mac base64");
        if (!number(a, a_n, 1, 65535, &id)) return fail(command, "send id must be 1 to 65535");
        if (!bp_mac_parse(b, b_n, command->mac)) return fail(command, "send mac is malformed");
        int n = bp_base64_decode(c, c_n, command->frame, sizeof command->frame);
        if (n < 0) return fail(command, "send frame is not base64 or over 1470 bytes");
        if (n == 0) return fail(command, "send frame is empty");
        command->kind = BP_SEND;
        command->id = (uint16_t)id;
        command->length = (size_t)n;
        return;
    }
    return fail(command, "unknown command");
}

static size_t finish(char *out, size_t capacity, int n)
{
    if (n < 0 || (size_t)n >= capacity) {
        if (capacity) out[0] = '\0';
        return 0;
    }
    return (size_t)n;
}

size_t bp_format_base(char *out, size_t capacity, const char *fw, const uint8_t mac[BP_MAC_LEN], uint8_t channel)
{
    char m[18];
    bp_mac_format(mac, m);
    return finish(out, capacity, snprintf(out, capacity, "thermite base 1 %s %s %u\n", fw, m, (unsigned)channel));
}

size_t bp_format_sent(char *out, size_t capacity, uint16_t id, bool ok)
{
    return finish(out, capacity, snprintf(out, capacity, "thermite sent %u %s\n", (unsigned)id, ok ? "ok" : "fail"));
}

size_t bp_format_rx(char *out, size_t capacity, const uint8_t mac[BP_MAC_LEN], int rssi, const uint8_t *frame,
                    size_t length)
{
    char m[18];
    bp_mac_format(mac, m);
    int n = snprintf(out, capacity, "thermite rx %s %d ", m, rssi);
    if (n < 0 || (size_t)n >= capacity) return finish(out, capacity, -1);
    size_t text = bp_base64_encode(frame, length, out + n, capacity - (size_t)n);
    if (text == 0 && length != 0) return finish(out, capacity, -1);
    size_t total = (size_t)n + text;
    if (total + 2 > capacity) return finish(out, capacity, -1);
    out[total++] = '\n';
    out[total] = '\0';
    return total;
}

size_t bp_format_error(char *out, size_t capacity, const char *words)
{
    return finish(out, capacity, snprintf(out, capacity, "thermite error %s\n", words));
}

bp_line_event_t bp_lines_push(bp_lines_t *lines, char byte)
{
    if (lines->ready) { /* the previous call returned a line */
        lines->ready = false;
        lines->length = 0;
    }
    if (byte == '\n') {
        if (lines->overflow) { /* already reported as too long */
            lines->overflow = false;
            lines->length = 0;
            return BP_LINE_NONE;
        }
        lines->data[lines->length] = '\0';
        lines->ready = true;
        return BP_LINE_READY;
    }
    if (lines->overflow) return BP_LINE_NONE;
    if (lines->length >= BP_LINE_MAX) {
        lines->overflow = true;
        lines->length = 0;
        return BP_LINE_TOO_LONG;
    }
    lines->data[lines->length++] = byte;
    return BP_LINE_NONE;
}

void bp_peers_init(bp_peers_t *peers, size_t capacity)
{
    memset(peers, 0, sizeof *peers);
    peers->capacity = capacity == 0 || capacity > BP_PEERS_MAX ? BP_PEERS_MAX : capacity;
}

bp_peer_action_t bp_peers_use(bp_peers_t *peers, const uint8_t mac[BP_MAC_LEN], uint8_t evicted[BP_MAC_LEN])
{
    uint32_t now = ++peers->clock;
    for (size_t i = 0; i < peers->count; i++) {
        if (memcmp(peers->mac[i], mac, BP_MAC_LEN) == 0) {
            peers->used[i] = now;
            return BP_PEER_KNOWN;
        }
    }
    if (peers->count < peers->capacity) {
        memcpy(peers->mac[peers->count], mac, BP_MAC_LEN);
        peers->used[peers->count++] = now;
        return BP_PEER_ADD;
    }
    size_t oldest = 0;
    for (size_t i = 1; i < peers->count; i++) {
        /* Unsigned difference keeps the order right across clock wrap. */
        if ((uint32_t)(now - peers->used[i]) > (uint32_t)(now - peers->used[oldest])) oldest = i;
    }
    memcpy(evicted, peers->mac[oldest], BP_MAC_LEN);
    memcpy(peers->mac[oldest], mac, BP_MAC_LEN);
    peers->used[oldest] = now;
    return BP_PEER_REPLACE;
}

void bp_peers_forget(bp_peers_t *peers, const uint8_t mac[BP_MAC_LEN])
{
    for (size_t i = 0; i < peers->count; i++) {
        if (memcmp(peers->mac[i], mac, BP_MAC_LEN) == 0) {
            peers->count--;
            memmove(peers->mac[i], peers->mac[peers->count], BP_MAC_LEN);
            peers->used[i] = peers->used[peers->count];
            return;
        }
    }
}
