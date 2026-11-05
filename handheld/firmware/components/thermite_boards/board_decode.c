#include "board_decode.h"

tca_event_t tca8418_decode(uint8_t raw)
{
    tca_event_t event = {.kind = TCA_EVENT_NONE, .press = (raw & 0x80) != 0};
    uint8_t code = raw & 0x7f;
    if (code >= 1 && code <= 80) {
        event.kind = TCA_EVENT_KEY;
        event.row = (uint8_t)((code - 1) / 10);
        event.column = (uint8_t)((code - 1) % 10);
    } else if (code >= 97 && code <= 104) {
        event.kind = TCA_EVENT_GPI_ROW;
        event.pin = (uint8_t)(code - 97);
    } else if (code >= 105 && code <= 114) {
        event.kind = TCA_EVENT_GPI_COL;
        event.pin = (uint8_t)(code - 105);
    } else {
        event.press = false; /* 0 (empty FIFO) or a reserved code */
    }
    return event;
}

bool ft6x06_decode(const uint8_t regs[5], uint16_t *x, uint16_t *y)
{
    uint8_t points = regs[0] & 0x0f;
    if (points == 0 || points > 2) return false; /* 0x0f and other values: no valid touch */
    /* P1_XH bits 7-6: event flag (0 down, 1 up, 2 contact, 3 none). */
    uint8_t flag = regs[1] >> 6;
    if (flag == 1 || flag == 3) return false;
    *x = (uint16_t)((regs[1] & 0x0f) << 8 | regs[2]);
    *y = (uint16_t)((regs[3] & 0x0f) << 8 | regs[4]);
    return true;
}

bool cst328_decode(const uint8_t regs[7], uint16_t *x, uint16_t *y)
{
    uint8_t points = regs[5] & 0x0f;
    if (points == 0 || points > 5) return false; /* the vendor demo trusts the count alone */
    *x = (uint16_t)(regs[1] << 4 | regs[3] >> 4);
    *y = (uint16_t)(regs[2] << 4 | (regs[3] & 0x0f));
    return true;
}

bool cst3530_decode(const uint8_t r[CST3530_REPORT], uint16_t *x, uint16_t *y)
{
    if (r[2] != 0xFF) return false;
    unsigned fingers = r[3] & 0x0f, keys = r[3] >> 4;
    if (fingers == 0 || (keys + fingers) * 5 + 4 > CST3530_REPORT) return false;
    /* Bytes 0-1: 0x55 plus the sum of the point bytes, little-endian. */
    uint16_t sum = 0x55;
    for (unsigned i = 0; i < (keys + fingers) * 5; i++) sum = (uint16_t)(sum + r[4 + i]);
    if ((uint16_t)(r[0] | r[1] << 8) != sum) return false;
    unsigned at = keys * 5 + 4;                                          /* the first finger after any key records */
    if (at + 4 >= CST3530_REPORT || (r[at + 4] >> 4) == 0) return false; /* event 0: none */
    *x = (uint16_t)(r[at] | (r[at + 3] & 0x0f) << 8);
    *y = (uint16_t)(r[at + 1] | (r[at + 3] & 0xf0) << 4);
    return true;
}

uint16_t max17048_millivolts(uint16_t vcell)
{
    /* 78.125 uV = 5/64 mV. */
    return (uint16_t)(((uint32_t)vcell * 5u + 32u) / 64u);
}

int max17048_percent(uint16_t soc)
{
    int percent = soc >> 8;
    return percent > 100 ? 100 : percent;
}

int max17048_crate_tenths(uint16_t crate)
{
    /* 0.208 %/h per bit = 2.08 tenths. */
    int32_t value = (int16_t)crate;
    return (int)(value * 208 / 100);
}

int lipo_percent(uint32_t millivolts)
{
    /* A typical single-cell LiPo discharge curve at light load. */
    static const struct {
        uint16_t mv;
        uint8_t percent;
    } curve[] = {{3300, 0},  {3500, 5},  {3600, 10}, {3700, 25}, {3750, 40}, {3800, 50},
                 {3850, 60}, {3900, 70}, {4000, 80}, {4100, 90}, {4200, 100}};
    const size_t n = sizeof curve / sizeof curve[0];
    if (millivolts <= curve[0].mv) return 0;
    if (millivolts >= curve[n - 1].mv) return 100;
    for (size_t i = 1; i < n; i++) {
        if (millivolts <= curve[i].mv) {
            uint32_t span = curve[i].mv - curve[i - 1].mv;
            uint32_t into = millivolts - curve[i - 1].mv;
            return curve[i - 1].percent + (int)((curve[i].percent - curve[i - 1].percent) * into / span);
        }
    }
    return 100;
}

bool debounce_update(debounce_t *d, bool pressed, uint32_t now_ms)
{
    if (pressed != d->raw) {
        d->raw = pressed;
        d->since = now_ms;
        return false;
    }
    if (d->stable != d->raw && (uint32_t)(now_ms - d->since) >= DEBOUNCE_MS) {
        d->stable = d->raw;
        return true;
    }
    return false;
}

void touch_map(const touch_map_t *map, uint16_t raw_x, uint16_t raw_y, uint16_t *x, uint16_t *y)
{
    uint32_t tx = raw_x, ty = raw_y;
    if (map->swap_xy) {
        uint32_t t = tx;
        tx = ty;
        ty = t;
    }
    if (tx >= map->raw_width) tx = map->raw_width - 1u;
    if (ty >= map->raw_height) ty = map->raw_height - 1u;
    if (map->mirror_x) tx = map->raw_width - 1u - tx;
    if (map->mirror_y) ty = map->raw_height - 1u - ty;
    if (map->raw_width != map->width) tx = tx * map->width / map->raw_width;
    if (map->raw_height != map->height) ty = ty * map->height / map->raw_height;
    *x = (uint16_t)tx;
    *y = (uint16_t)ty;
}
