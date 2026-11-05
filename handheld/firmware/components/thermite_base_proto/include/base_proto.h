/*
 * The base station's USB lines (handheld/PROTOCOL.md, "Base station lines"):
 * parsing host lines, formatting base lines, base64, MACs, line assembly and
 * the ESP-NOW peer table. Pure C with no ESP-IDF dependency, so the host
 * tests in ../test exercise exactly this file. The base speaks these lines on
 * its USB port; the qemu board's radio speaks them on UART1.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Longest line either way, without its newline. */
#define BP_LINE_MAX 2200
/* Longest radio frame (ESP-NOW v2, TL_FRAME_MAX). */
#define BP_FRAME_MAX 1470
#define BP_MAC_LEN 6
#define BP_CHANNEL_MIN 1
#define BP_CHANNEL_MAX 13

typedef enum {
    BP_IGNORE = 0, /* not a "thermite " line: a log, or empty */
    BP_ERROR,      /* malformed or refused: answer "thermite error <error>" */
    BP_HELLO,
    BP_CHANNEL,
    BP_SEND,
} bp_kind_t;

typedef struct {
    bp_kind_t kind;
    const char *error; /* BP_ERROR: the words after "thermite error " */
    uint8_t channel;   /* BP_CHANNEL */
    uint16_t id;       /* BP_SEND */
    uint8_t mac[BP_MAC_LEN];
    size_t length;
    uint8_t frame[BP_FRAME_MAX];
} bp_command_t;

/* Parses one line, without its newline (a trailing CR is ignored). */
void bp_parse(const char *line, size_t length, bp_command_t *command);

/* Formatters: each writes one line with its "\n" and a NUL, and returns its length
 * without the NUL, or 0 when it does not fit. */
size_t bp_format_base(char *out, size_t capacity, const char *fw, const uint8_t mac[BP_MAC_LEN], uint8_t channel);
size_t bp_format_sent(char *out, size_t capacity, uint16_t id, bool ok);
size_t bp_format_rx(char *out, size_t capacity, const uint8_t mac[BP_MAC_LEN], int rssi, const uint8_t *frame,
                    size_t length);
size_t bp_format_error(char *out, size_t capacity, const char *words);

/* Base64, standard alphabet with padding. encode returns the text length (NUL written),
 * or 0 when it does not fit; decode returns the byte count, or -1 for invalid text or no room. */
size_t bp_base64_encode(const uint8_t *data, size_t length, char *out, size_t capacity);
int bp_base64_decode(const char *text, size_t length, uint8_t *out, size_t capacity);

/* "aa:bb:cc:dd:ee:ff"; parse accepts either case and exactly 17 characters. */
bool bp_mac_parse(const char *text, size_t length, uint8_t mac[BP_MAC_LEN]);
void bp_mac_format(const uint8_t mac[BP_MAC_LEN], char out[18]);

/* Gathers bytes into lines. A line longer than BP_LINE_MAX is reported once as too long
 * and its remainder up to the next newline is dropped. */
typedef struct {
    char data[BP_LINE_MAX + 1];
    size_t length;
    bool overflow;
    bool ready;
} bp_lines_t;

typedef enum { BP_LINE_NONE = 0, BP_LINE_READY, BP_LINE_TOO_LONG } bp_line_event_t;

/* Feeds one byte. BP_LINE_READY: data holds the line (NUL-terminated, no newline) and
 * length its size, until the next call. */
bp_line_event_t bp_lines_push(bp_lines_t *lines, char byte);

/* ESP-NOW peers the base has added, least recently used evicted beyond capacity. */
#define BP_PEERS_MAX 20
typedef struct {
    uint8_t mac[BP_PEERS_MAX][BP_MAC_LEN];
    uint32_t used[BP_PEERS_MAX];
    uint32_t clock;
    size_t count;
    size_t capacity; /* at most BP_PEERS_MAX */
} bp_peers_t;

typedef enum {
    BP_PEER_KNOWN = 0, /* already a peer */
    BP_PEER_ADD,       /* add it */
    BP_PEER_REPLACE,   /* delete *evicted, then add it */
} bp_peer_action_t;

void bp_peers_init(bp_peers_t *peers, size_t capacity);
/* Marks mac used now, making room for it; on BP_PEER_REPLACE evicted is the MAC to delete. */
bp_peer_action_t bp_peers_use(bp_peers_t *peers, const uint8_t mac[BP_MAC_LEN], uint8_t evicted[BP_MAC_LEN]);
/* Forgets mac, after a failed add. */
void bp_peers_forget(bp_peers_t *peers, const uint8_t mac[BP_MAC_LEN]);

#ifdef __cplusplus
}
#endif
