/*
 * The remote's behavior without hardware (handheld/PROTOCOL.md): it keeps the
 * session with the bridge, numbers, sends and retries operations, scans
 * channels, and holds what the bridge last said to show. The device app
 * (handheld/firmware/remote) and the host simulator give it a platform and
 * forward radio frames and input; thermite_ui draws what it holds.
 *
 * Not thread-safe: call everything, including the platform's changed hook's
 * consumers, from one task.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "cJSON.h"
#include "thermite_link.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Operations wait this long for an ack before they are sent again. */
#define TR_RETRY_MS 250
/* A HELLO waits this long for its SYNC before the next channel is tried. */
#define TR_HELLO_MS 300
/* Operations outstanding at once; more are refused until acks arrive. */
#define TR_PENDING_MAX 8

/* What changed, for tr_platform_t.changed. */
enum {
    TR_CHANGED_SCREEN = 1 << 0,
    TR_CHANGED_AUDIO = 1 << 1,
    TR_CHANGED_TOAST = 1 << 2,
    TR_CHANGED_INPUTS = 1 << 3,
    TR_CHANGED_LINK = 1 << 4,
};

typedef enum {
    TR_LINK_OFFLINE = 0, /* no session, nothing in flight */
    TR_LINK_CONNECTING,  /* a HELLO is waiting, or operations are being retried */
    TR_LINK_CONNECTED,   /* a session, and the bridge answered the last operation */
} tr_link_t;

typedef struct {
    void *context;
    /* One frame to the base, by ESP-NOW unicast. */
    void (*send)(void *context, const uint8_t *frame, size_t length);
    /* Listen and send on this 2.4 GHz channel, 1-13, from now on. */
    void (*set_channel)(void *context, uint8_t channel);
    /* A monotonic millisecond clock that keeps counting across deep sleep. */
    uint32_t (*now_ms)(void *context);
    /* A uniformly random 64-bit number. */
    uint64_t (*random64)(void *context);
    /* Something the UI shows changed: TR_CHANGED_* bits. */
    void (*changed)(void *context, unsigned what);
} tr_platform_t;

/* What survives deep sleep. Place it in RTC memory; tr_create validates it. */
typedef struct {
    uint32_t magic;
    tl_remote_t link;
    uint32_t op;     /* the last operation number used */
    uint8_t channel; /* the channel the bridge last answered on */
} tr_persist_t;

typedef struct {
    const char *board; /* e.g. "omote-rev5" */
    const char *fw;    /* firmware version */
    uint16_t width, height;
    uint8_t channel;   /* the channel enrolment named */
} tr_identity_t;

typedef struct tr tr_t;

/* A remote on persist (restored if valid, else reset), with the enrolled key. */
tr_t *tr_create(tr_persist_t *persist, const uint8_t key[TL_KEY_LEN], const tr_identity_t *identity,
                const tr_platform_t *platform);
void tr_destroy(tr_t *remote);

/* Battery 0-100, or -1 when unknown; sent with the next operation. */
void tr_status(tr_t *remote, int battery, bool charging);

/* Operations (PROTOCOL.md, Remote to bridge). Each returns false when refused:
 * TR_PENDING_MAX are outstanding. */
bool tr_wake(tr_t *remote);
bool tr_key(tr_t *remote, const char *const *ids, size_t count);
bool tr_bubble(tr_t *remote, const char *id);
bool tr_room(tr_t *remote, const char *id);
bool tr_all_off(tr_t *remote);
bool tr_input(tr_t *remote, const char *value);
bool tr_sleep(tr_t *remote);

/* One frame from the base. */
void tr_receive(tr_t *remote, const uint8_t *frame, size_t length);
/* A bridge message as received, after decryption (tests and the simulator's screenshots use it). */
void tr_apply(tr_t *remote, const char *message);

/* Runs timers: retries, HELLO, channel scan. Returns the milliseconds until it needs to run
 * again, or UINT32_MAX when nothing is scheduled; callers cap the wait. */
uint32_t tr_poll(tr_t *remote);

/* What to show. Pointers stay valid until the next call into the remote. NULL when absent. */
const cJSON *tr_screen(const tr_t *remote);
const cJSON *tr_audio(const tr_t *remote);
const cJSON *tr_toast(const tr_t *remote);  /* the latest; tr_clear_toast() once shown */
const cJSON *tr_inputs(const tr_t *remote); /* the open input list; tr_clear_inputs() once closed */
void tr_clear_toast(tr_t *remote);
void tr_clear_inputs(tr_t *remote);
tr_link_t tr_link(const tr_t *remote);

/* True when no operation awaits an ack: the device may sleep without losing one. */
bool tr_idle(const tr_t *remote);

#ifdef __cplusplus
}
#endif
