/*
 * One handheld board: power, display, touch, keys, battery and sleep, behind
 * one interface. Exactly one board is built in, chosen in menuconfig
 * ("Thermite board", the remote app's sdkconfig.board.<name>).
 *
 * Threading: board_init and board_display_init run once from the app task.
 * Everything else, except the notify callback, is also called from that one
 * task. notify runs in interrupt context.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "board_keys.h"
#include "esp_err.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    BOARD_CONSOLE_USB_SERIAL_JTAG = 0, /* the S3's native USB port */
    BOARD_CONSOLE_UART0,               /* a USB-UART bridge on GPIO43/44 (OMOTE's CH340C) */
} board_console_t;

typedef struct {
    const char *name;       /* the board word of "thermite remote ..." and tr_identity_t.board */
    uint16_t width, height; /* the display as LVGL draws it */
    bool touch;
    board_console_t console;
    /* Hard-key groups, for thermite_ui's tu_keys_t. */
    bool dpad, volume, channel, playback;
    /* The panel takes RGB565 with its bytes swapped (big-endian on the wire): LVGL's
     * flush must swap them (lv_draw_sw_rgb565_swap) before sending. */
    bool swap_bytes;
    /* The board can deep sleep and wake again (QEMU cannot): otherwise it only dims. */
    bool deep_sleep;
} board_info_t;

const board_info_t *board_info(void);

/* Holds power, starts the I2C bus and every peripheral but the display, and reads
 * what woke the board. Call first. */
esp_err_t board_init(void);

/* Called, in interrupt context, when a key or touch interrupt fires: the app task
 * should call board_keys_poll soon. Set before board_init's interrupts can fire;
 * may be NULL. */
void board_set_notify(void (*notify)(void *context), void *context);

/* Panel up and on, backlight still off. io and panel stay owned by the board. The
 * app registers its own color-done callback on io (esp_lcd_panel_io_register_event_callbacks);
 * io is NULL for a panel with no bus, which has drawn when draw_bitmap returns (QEMU's). */
esp_err_t board_display_init(esp_lcd_panel_io_handle_t *io, esp_lcd_panel_handle_t *panel);

/* Largest DMA transfer the display bus accepts, in bytes: size LVGL's buffers within it. */
size_t board_display_max_transfer(void);

/* Display backlight 0-100; 0 is off. */
void board_backlight(uint8_t percent);
/* Key backlight 0-100, on boards that have one. */
void board_key_backlight(uint8_t percent);

/* The touch point in display coordinates; false when not touched or no touch. */
bool board_touch_read(uint16_t *x, uint16_t *y);

/* Collects key edges into events (at most max); *count says how many. Returns the
 * milliseconds until it needs to run again even without a notify (debounce, held
 * buttons), or UINT32_MAX when only an interrupt can bring news. */
uint32_t board_keys_poll(board_key_event_t *events, size_t max, size_t *count);

/* After a wake from deep sleep: the key whose press woke the board, BOARD_KEY_NONE
 * otherwise. Its press is consumed: board_keys_poll reports only its release. */
board_key_t board_wake_key(void);

/* True when the board woke from deep sleep at all (any source), false at power-on. */
bool board_woke(void);

/* Battery percent 0-100, or -1 when unknown; *charging when on external power and
 * charging. */
int board_battery(bool *charging);

/* Display off, peripherals in their sleep modes, wake sources armed. Then the app
 * calls esp_deep_sleep_start(). Does not return an error: sleep must happen. */
void board_sleep_prepare(void);

#ifdef __cplusplus
}
#endif
