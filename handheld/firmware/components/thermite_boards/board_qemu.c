/*
 * The qemu board: the remote app in Espressif's QEMU (esp32s3 machine), which
 * emulates the CPU, flash, PSRAM, UARTs and the RNG but no radio, I2C, SPI,
 * LEDC or GPIO matrix. The display is QEMU's virtual RGB panel, a device real
 * hardware lacks; its registers follow Espressif's esp_lcd_qemu_rgb
 * (idf-extra-components, Apache-2.0). Input is the console's test lines
 * (PROTOCOL.md) and the radio is remote/main/radio_lines.c. It never deep
 * sleeps. handheld/firmware/qemu/run.sh runs it.
 */
#include "sdkconfig.h"
#if CONFIG_THERMITE_BOARD_QEMU

#include <string.h>

#include "board_priv.h"
#include "esp_lcd_panel_interface.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "soc/syscon_reg.h"

static const char *TAG = "qemu";

#define WIDTH 240
#define HEIGHT 320
/* QEMU copies an area at its display's next refresh, then clears the start bit; with
 * no display it never does. Waiting at most this long keeps such a run going. */
#define DRAW_WAIT_US 100000

/* QEMU writes "QEMU" into the word before SYSCON's date register. */
#define QEMU_ORIGIN 0x51454d55u

/* The virtual RGB panel's registers: coordinates pack x (or width) in the high half. */
typedef volatile struct {
    uint32_t version;
    uint32_t size;        /* width << 16 | height */
    uint32_t update_from; /* x << 16 | y */
    uint32_t update_to;   /* x << 16 | y, exclusive */
    const void *update_content;
    uint32_t update_status; /* write 1 to draw; reads 0 once drawn */
    uint32_t bpp;
} qemu_rgb_t;
#define RGB ((qemu_rgb_t *)0x21000000)

static const board_info_t INFO = {
    .name = "qemu",
    .width = WIDTH,
    .height = HEIGHT,
    .touch = true,
    .console = BOARD_CONSOLE_UART0,
    .swap_bytes = false, /* the panel reads RGB565 as LVGL writes it */
    .deep_sleep = false,
};

static esp_err_t draw(esp_lcd_panel_t *panel, int x1, int y1, int x2, int y2, const void *pixels)
{
    (void)panel;
    RGB->update_from = (uint32_t)x1 << 16 | (uint32_t)y1;
    RGB->update_to = (uint32_t)x2 << 16 | (uint32_t)y2;
    RGB->update_content = pixels;
    RGB->update_status = 1;
    int64_t start = esp_timer_get_time();
    while ((RGB->update_status & 1) && esp_timer_get_time() - start < DRAW_WAIT_US) {
    }
    return ESP_OK;
}

static esp_err_t nothing(esp_lcd_panel_t *panel)
{
    (void)panel;
    return ESP_OK;
}

static esp_lcd_panel_t s_panel = {.reset = nothing, .init = nothing, .del = nothing, .draw_bitmap = draw};

const board_info_t *board_info(void)
{
    return &INFO;
}

/* QEMU's default eFuses hold no MAC: give the remote a locally administered one. */
esp_err_t board_init(void)
{
    if (REG_READ(SYSCON_DATE_REG - 4) != QEMU_ORIGIN) {
        ESP_LOGE(TAG, "this firmware runs only in QEMU");
        return ESP_ERR_NOT_SUPPORTED;
    }
    uint8_t mac[6] = {0};
    static const uint8_t zero[6] = {0};
    if (esp_efuse_mac_get_default(mac) != ESP_OK || memcmp(mac, zero, sizeof mac) == 0) {
        static const uint8_t ours[6] = {0x02, 0x51, 0x45, 0x4d, 0x55, 0x01}; /* 02:"QEMU":01 */
        return esp_base_mac_addr_set(ours);
    }
    return ESP_OK;
}

esp_err_t board_display_init(esp_lcd_panel_io_handle_t *io, esp_lcd_panel_handle_t *panel)
{
    RGB->size = (uint32_t)WIDTH << 16 | HEIGHT;
    RGB->bpp = 16;
    *io = NULL; /* no bus: draw() has drawn when it returns */
    *panel = &s_panel;
    return ESP_OK;
}

size_t board_display_max_transfer(void)
{
    return (size_t)WIDTH * HEIGHT * 2;
}

void board_backlight(uint8_t percent)
{
    (void)percent;
}

void board_key_backlight(uint8_t percent)
{
    (void)percent;
}

bool board_touch_read(uint16_t *x, uint16_t *y)
{
    (void)x;
    (void)y;
    return false; /* the console's "thermite touch" lines touch instead */
}

uint32_t board_keys_poll(board_key_event_t *events, size_t max, size_t *count)
{
    (void)events;
    (void)max;
    *count = 0; /* the console's "thermite key" lines press instead */
    return UINT32_MAX;
}

board_key_t board_wake_key(void)
{
    return BOARD_KEY_NONE;
}

int board_battery(bool *charging)
{
    *charging = false;
    return -1;
}

void board_sleep_prepare(void)
{
}

#endif
