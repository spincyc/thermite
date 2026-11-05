/*
 * When the remote dims, and when it goes to deep sleep: dim after 10 s
 * without input, and after 20 s, once no operation awaits an ack, say
 * "sleep" to the bridge, wait briefly for its ack, then sleep. A board that
 * cannot deep sleep (QEMU) only dims. Pure C; tested on the host by ../test.
 */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define POWER_DIM_MS 10000
#define POWER_SLEEP_MS 20000
/* How long the "sleep" operation may wait for its ack. */
#define POWER_SLEEP_ACK_MS 300
/* Sleep regardless after this long without input: an unanswered operation must not
 * keep the remote awake once the link has given up. */
#define POWER_FORCE_MS 60000

typedef enum {
    POWER_NONE = 0,
    POWER_BRIGHTEN,   /* backlight to full */
    POWER_DIM,        /* backlight down */
    POWER_SEND_SLEEP, /* call tr_sleep() now */
    POWER_SLEEP_NOW,  /* board_sleep_prepare(), esp_deep_sleep_start() */
} power_action_t;

typedef struct {
    uint32_t last_input;
    uint32_t sleep_sent; /* when POWER_SEND_SLEEP was returned */
    bool dimmed, sleeping;
    bool sleeps; /* the board can deep sleep */
} power_policy_t;

void power_init(power_policy_t *p, uint32_t now_ms, bool sleeps);
/* Input (a key or touch) at now_ms: returns POWER_BRIGHTEN when dimmed. */
power_action_t power_input(power_policy_t *p, uint32_t now_ms);
/* What to do now; idle is tr_idle(). Call after every loop pass. */
power_action_t power_step(power_policy_t *p, uint32_t now_ms, bool idle);
/* Milliseconds until power_step has something new to say. */
uint32_t power_wait(const power_policy_t *p, uint32_t now_ms);

#ifdef __cplusplus
}
#endif
