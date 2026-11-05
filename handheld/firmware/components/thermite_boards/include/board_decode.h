/*
 * The board drivers' arithmetic, without I/O: register decoding for the key
 * scanner, touch controllers and fuel gauge, battery voltage to charge,
 * button debouncing and touch-to-display mapping. Plain C, tested on the
 * host by ../test.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* TCA8418 (TI SCPS215): one KEY_EVENT_A byte from the FIFO. Bit 7 is 1 for a press,
 * 0 for a release; bits 6-0 are 1-80 for matrix key row * 10 + column + 1, 97-104 for
 * GPI ROW0-7 and 105-114 for GPI COL0-9; 0 means the FIFO is empty. */
typedef enum { TCA_EVENT_NONE = 0, TCA_EVENT_KEY, TCA_EVENT_GPI_ROW, TCA_EVENT_GPI_COL } tca_event_kind_t;
typedef struct {
    tca_event_kind_t kind;
    bool press;
    uint8_t row, column; /* TCA_EVENT_KEY */
    uint8_t pin;         /* TCA_EVENT_GPI_ROW: ROWn; TCA_EVENT_GPI_COL: COLn */
} tca_event_t;
tca_event_t tca8418_decode(uint8_t raw);

/* FT6x06 / FT6206 / FT6236 (FocalTech): registers 0x02-0x06 read in one burst
 * (TD_STATUS, P1_XH, P1_XL, P1_YH, P1_YL). True with the first point when touched. */
bool ft6x06_decode(const uint8_t regs[5], uint16_t *x, uint16_t *y);

/* CST328 (Hynitron): the first point record, registers 0xD000-0xD006, plus the point
 * count from 0xD005's low nibble. True when one finger or more is down. */
bool cst328_decode(const uint8_t regs[7], uint16_t *x, uint16_t *y);

/* CST3530 (Hynitron), reported on later Waveshare 2.8-inch boards (UNVERIFIED): the 32-byte
 * report after command 0xD0070000. True with the first finger's point when the report is
 * well-formed (marker 0xFF, checksum) and a finger is down. */
#define CST3530_REPORT 32
bool cst3530_decode(const uint8_t report[CST3530_REPORT], uint16_t *x, uint16_t *y);

/* MAX17048 (Analog Devices): VCELL (0x02) in millivolts, 78.125 uV per bit; SOC (0x04)
 * in whole percent, clamped to 0-100 (the gauge reports up to 255.996 %); CRATE (0x16)
 * in tenths of a percent per hour, 0.208 %/h per bit, signed. */
uint16_t max17048_millivolts(uint16_t vcell);
int max17048_percent(uint16_t soc);
int max17048_crate_tenths(uint16_t crate);

/* A single LiPo cell at rest: millivolts to a rough 0-100 % charge. */
int lipo_percent(uint32_t millivolts);

/* A push button sampled by polling: an edge is reported once the raw level has held for
 * the debounce time. */
typedef struct {
    bool stable;    /* the debounced state, true = pressed */
    bool raw;       /* the last sample */
    uint32_t since; /* when raw last changed, ms */
} debounce_t;
#define DEBOUNCE_MS 20
/* Feeds one sample; returns true when the debounced state changed (read d->stable). */
bool debounce_update(debounce_t *d, bool pressed, uint32_t now_ms);

/* Raw touch coordinates to display coordinates: optional axis swap, then mirroring, then
 * scaling from the controller's range to the display's, clamped. */
typedef struct {
    uint16_t raw_width, raw_height; /* the controller's range after any swap */
    uint16_t width, height;         /* the display's */
    bool swap_xy, mirror_x, mirror_y;
} touch_map_t;
void touch_map(const touch_map_t *map, uint16_t raw_x, uint16_t raw_y, uint16_t *x, uint16_t *y);

#ifdef __cplusplus
}
#endif
