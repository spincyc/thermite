/*
 * The handheld remote's radio frames and its end of a session
 * (handheld/PROTOCOL.md). The bridge's end is handheld/link.js. No I/O and
 * no allocation: tl_remote_t is plain data, so it can live in RTC memory
 * across deep sleep, and the key is passed to each call.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define TL_VERSION 1
#define TL_HEADER 16
#define TL_TAG 16
#define TL_FRAME_MAX 1470
#define TL_PART_MAX (TL_FRAME_MAX - TL_HEADER - TL_TAG)
#define TL_MESSAGE_MAX 8192
#define TL_KEY_LEN 32

typedef enum { TL_HELLO = 1, TL_SYNC = 2, TL_DATA = 3 } tl_type_t;
typedef enum { TL_FROM_REMOTE = 1, TL_FROM_BRIDGE = 2 } tl_direction_t;

typedef struct {
    uint8_t type;
    uint8_t message;
    uint8_t index;
    uint8_t count;
    uint64_t session;
    uint32_t counter;
} tl_header_t;

/* Seals one part into out; returns the frame's length, or 0 when it does not fit. */
size_t tl_seal(const uint8_t key[TL_KEY_LEN], tl_direction_t direction, const tl_header_t *header,
               const uint8_t *plaintext, size_t length, uint8_t *out, size_t capacity);

/* Opens one frame into out; returns the plaintext's length, or -1 for a frame to drop. */
int tl_open(const uint8_t key[TL_KEY_LEN], tl_direction_t direction, const uint8_t *frame, size_t length,
            tl_header_t *header, uint8_t *out, size_t capacity);

/* The remote's end of a session. Zeroed, it has none. */
typedef struct {
    uint64_t session; /* the adopted session, 0 for none */
    uint64_t waiting; /* the hello nonce a SYNC must echo, 0 for none */
    uint32_t sent;    /* the last counter sent in the session */
    uint32_t heard;   /* the last counter accepted in the session */
    uint8_t message;  /* the next outgoing message number */
} tl_remote_t;

/* Gathers one incoming message. Keep it in ordinary memory. */
typedef struct {
    uint8_t data[TL_MESSAGE_MAX + 1]; /* NUL-terminated once complete */
    size_t last_length;
    uint16_t received;
    uint8_t message;
    uint8_t count;
} tl_assembler_t;

typedef enum { TL_NOTHING = 0, TL_SYNCED, TL_MESSAGE } tl_event_t;

typedef void (*tl_emit_t)(void *context, const uint8_t *frame, size_t length);

/* Forgets the session; the next send needs a HELLO and its SYNC. */
void tl_remote_reset(tl_remote_t *remote);

/* A HELLO carrying status (JSON) under a fresh, nonzero nonce. Returns its length, or 0. */
size_t tl_remote_hello(tl_remote_t *remote, const uint8_t key[TL_KEY_LEN], uint64_t nonce,
                       const char *status, size_t length, uint8_t *out, size_t capacity);

/* Sends one JSON message as DATA frames through emit. Returns the frame count, or -1
 * with no session, an empty or oversized message, or exhausted counters. */
int tl_remote_send(tl_remote_t *remote, const uint8_t key[TL_KEY_LEN], const char *message, size_t length,
                   tl_emit_t emit, void *context);

/* Takes one frame from the bridge. TL_MESSAGE sets *message (NUL-terminated, inside
 * assembler) and *length until the next call. */
tl_event_t tl_remote_receive(tl_remote_t *remote, tl_assembler_t *assembler, const uint8_t key[TL_KEY_LEN],
                             const uint8_t *frame, size_t length, const char **message, size_t *size);

/* The nonce as the SYNC prints it: 16 lower-case hex digits and a NUL. */
void tl_hex(uint64_t value, char out[17]);

#ifdef __cplusplus
}
#endif
