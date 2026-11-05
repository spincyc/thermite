#include "power_policy.h"

void power_init(power_policy_t *p, uint32_t now_ms)
{
    p->last_input = now_ms;
    p->sleep_sent = 0;
    p->dimmed = false;
    p->sleeping = false;
}

power_action_t power_input(power_policy_t *p, uint32_t now_ms)
{
    p->last_input = now_ms;
    p->sleeping = false;
    if (!p->dimmed) return POWER_NONE;
    p->dimmed = false;
    return POWER_BRIGHTEN;
}

power_action_t power_step(power_policy_t *p, uint32_t now_ms, bool idle)
{
    uint32_t quiet = now_ms - p->last_input;
    if (p->sleeping) {
        if (idle || (uint32_t)(now_ms - p->sleep_sent) >= POWER_SLEEP_ACK_MS) return POWER_SLEEP_NOW;
        return POWER_NONE;
    }
    if (quiet >= POWER_FORCE_MS) return POWER_SLEEP_NOW;
    if (quiet >= POWER_SLEEP_MS && idle) {
        p->sleeping = true;
        p->sleep_sent = now_ms;
        return POWER_SEND_SLEEP;
    }
    if (quiet >= POWER_DIM_MS && !p->dimmed) {
        p->dimmed = true;
        return POWER_DIM;
    }
    return POWER_NONE;
}

uint32_t power_wait(const power_policy_t *p, uint32_t now_ms)
{
    uint32_t quiet = now_ms - p->last_input;
    if (p->sleeping) {
        uint32_t waited = now_ms - p->sleep_sent;
        return waited >= POWER_SLEEP_ACK_MS ? 0 : POWER_SLEEP_ACK_MS - waited;
    }
    if (!p->dimmed) return quiet >= POWER_DIM_MS ? 0 : POWER_DIM_MS - quiet;
    /* Dimmed: the sleep deadline, then idle is polled by the caller's own wakeups. */
    if (quiet < POWER_SLEEP_MS) return POWER_SLEEP_MS - quiet;
    return quiet >= POWER_FORCE_MS ? 0 : 100;
}
