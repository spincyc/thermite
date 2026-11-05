/* The board's panel under LVGL 9: partial rendering into two DMA-capable buffers,
 * flushed through esp_lcd, ticked from esp_timer, with the touch as a pointer. */
#pragma once

#include <stdbool.h>

#include "lvgl.h"

/* Brings up the panel and LVGL; backlight stays off until display_backlight. */
lv_display_t *display_start(void);

/* True once since the last call when the touch was down: input for the power policy. */
bool display_touched(void);

/* The screen shown while no enrolment is stored. */
void display_enrol_screen(const char *mac, const char *board, const char *fw);
