/*
 * A minimal ILI9341 esp_lcd panel. ESP-IDF v5.5.6 ships ST7789, NT35510 and
 * SSD1306 panels only; the ILI9341 takes the same MIPI-DCS window and RAM
 * commands but needs its own power-up sequence (ILITEK ILI9341 datasheet
 * V1.11, chapter 8). The power, VCOM, frame-rate and display-function
 * values are those OMOTE's firmware sends through LovyanGFX's ILI9341 table
 * (facts only; no code is taken from either); the extended power-control
 * commands and gamma tables are the values commonly used for 2.8-inch
 * ILI9341 modules. UNVERIFIED on our hardware.
 */
#include <stdlib.h>
#include <sys/cdefs.h>

#include "board_priv.h"
#include "esp_check.h"
#include "esp_lcd_panel_commands.h"
#include "esp_lcd_panel_interface.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ili9341";

typedef struct {
    esp_lcd_panel_t base;
    esp_lcd_panel_io_handle_t io;
    int reset_gpio;
    bool reset_high;
    int x_gap, y_gap;
    uint8_t madctl;
} ili9341_t;

static const lcd_command_t INIT[] = {
    {0xCF, 3, {0x00, 0xC1, 0x30}, 0},             /* power control B */
    {0xED, 4, {0x64, 0x03, 0x12, 0x81}, 0},       /* power-on sequence */
    {0xE8, 3, {0x85, 0x00, 0x78}, 0},             /* driver timing A */
    {0xCB, 5, {0x39, 0x2C, 0x00, 0x34, 0x02}, 0}, /* power control A */
    {0xF7, 1, {0x20}, 0},                         /* pump ratio */
    {0xEA, 2, {0x00, 0x00}, 0},                   /* driver timing B */
    {0xC0, 1, {0x23}, 0},                         /* power control 1: GVDD 4.6 V */
    {0xC1, 1, {0x10}, 0},                         /* power control 2 */
    {0xC5, 2, {0x3E, 0x28}, 0},                   /* VCOM control 1 */
    {0xC7, 1, {0x86}, 0},                         /* VCOM control 2 */
    {LCD_CMD_COLMOD, 1, {0x55}, 0},               /* RGB565 */
    {0xB1, 2, {0x00, 0x13}, 0},                   /* frame rate 100 Hz, as OMOTE's LovyanGFX setup */
    {0xB6, 3, {0x08, 0xC2, 0x27}, 0},             /* display function control, as OMOTE's LovyanGFX setup */
    {0xF2, 1, {0x00}, 0},                         /* 3-gamma off */
    {LCD_CMD_GAMSET, 1, {0x01}, 0},               /* gamma curve 1 */
    {0xE0, 15, {0x0F, 0x31, 0x2B, 0x0C, 0x0E, 0x08, 0x4E, 0xF1, 0x37, 0x07, 0x10, 0x03, 0x0E, 0x09, 0x00}, 0},
    {0xE1, 15, {0x00, 0x0E, 0x14, 0x03, 0x11, 0x07, 0x31, 0xC1, 0x48, 0x08, 0x0F, 0x0C, 0x31, 0x36, 0x0F}, 0},
    {LCD_CMD_SLPOUT, 0, {0}, 120},
};

static esp_err_t write_madctl(ili9341_t *p)
{
    return esp_lcd_panel_io_tx_param(p->io, LCD_CMD_MADCTL, (uint8_t[]){p->madctl}, 1);
}

static esp_err_t ili_reset(esp_lcd_panel_t *panel)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    if (p->reset_gpio >= 0) {
        gpio_set_level(p->reset_gpio, p->reset_high);
        vTaskDelay(pdMS_TO_TICKS(10));
        gpio_set_level(p->reset_gpio, !p->reset_high);
        vTaskDelay(pdMS_TO_TICKS(120));
        return ESP_OK;
    }
    ESP_RETURN_ON_ERROR(esp_lcd_panel_io_tx_param(p->io, LCD_CMD_SWRESET, NULL, 0), TAG, "reset");
    vTaskDelay(pdMS_TO_TICKS(120));
    return ESP_OK;
}

static esp_err_t ili_init(esp_lcd_panel_t *panel)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    ESP_RETURN_ON_ERROR(lcd_send(p->io, INIT, sizeof INIT / sizeof INIT[0]), TAG, "init");
    return write_madctl(p);
}

static esp_err_t ili_draw(esp_lcd_panel_t *panel, int x0, int y0, int x1, int y1, const void *data)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    x0 += p->x_gap;
    x1 += p->x_gap;
    y0 += p->y_gap;
    y1 += p->y_gap;
    ESP_RETURN_ON_ERROR(
        esp_lcd_panel_io_tx_param(p->io, LCD_CMD_CASET,
                                  (uint8_t[]){(x0 >> 8) & 0xFF, x0 & 0xFF, ((x1 - 1) >> 8) & 0xFF, (x1 - 1) & 0xFF}, 4),
        TAG, "CASET");
    ESP_RETURN_ON_ERROR(
        esp_lcd_panel_io_tx_param(p->io, LCD_CMD_RASET,
                                  (uint8_t[]){(y0 >> 8) & 0xFF, y0 & 0xFF, ((y1 - 1) >> 8) & 0xFF, (y1 - 1) & 0xFF}, 4),
        TAG, "RASET");
    return esp_lcd_panel_io_tx_color(p->io, LCD_CMD_RAMWR, data, (size_t)(x1 - x0) * (size_t)(y1 - y0) * 2);
}

static esp_err_t ili_invert(esp_lcd_panel_t *panel, bool invert)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    return esp_lcd_panel_io_tx_param(p->io, invert ? LCD_CMD_INVON : LCD_CMD_INVOFF, NULL, 0);
}

static esp_err_t ili_mirror(esp_lcd_panel_t *panel, bool x, bool y)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    p->madctl = (uint8_t)((p->madctl & ~(LCD_CMD_MX_BIT | LCD_CMD_MY_BIT)) | (x ? LCD_CMD_MX_BIT : 0) |
                          (y ? LCD_CMD_MY_BIT : 0));
    return write_madctl(p);
}

static esp_err_t ili_swap(esp_lcd_panel_t *panel, bool swap)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    p->madctl = (uint8_t)((p->madctl & ~LCD_CMD_MV_BIT) | (swap ? LCD_CMD_MV_BIT : 0));
    return write_madctl(p);
}

static esp_err_t ili_gap(esp_lcd_panel_t *panel, int x, int y)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    p->x_gap = x;
    p->y_gap = y;
    return ESP_OK;
}

static esp_err_t ili_on_off(esp_lcd_panel_t *panel, bool on)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    return esp_lcd_panel_io_tx_param(p->io, on ? LCD_CMD_DISPON : LCD_CMD_DISPOFF, NULL, 0);
}

static esp_err_t ili_sleep(esp_lcd_panel_t *panel, bool sleep)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    ESP_RETURN_ON_ERROR(esp_lcd_panel_io_tx_param(p->io, sleep ? LCD_CMD_SLPIN : LCD_CMD_SLPOUT, NULL, 0), TAG,
                        "sleep");
    vTaskDelay(pdMS_TO_TICKS(120));
    return ESP_OK;
}

static esp_err_t ili_del(esp_lcd_panel_t *panel)
{
    ili9341_t *p = __containerof(panel, ili9341_t, base);
    if (p->reset_gpio >= 0) gpio_reset_pin(p->reset_gpio);
    free(p);
    return ESP_OK;
}

esp_err_t lcd_new_panel_ili9341(esp_lcd_panel_io_handle_t io, const esp_lcd_panel_dev_config_t *config,
                                esp_lcd_panel_handle_t *panel)
{
    ESP_RETURN_ON_FALSE(io && config && panel && config->bits_per_pixel == 16, ESP_ERR_INVALID_ARG, TAG, "arguments");
    ili9341_t *p = calloc(1, sizeof *p);
    ESP_RETURN_ON_FALSE(p, ESP_ERR_NO_MEM, TAG, "no memory");
    if (config->reset_gpio_num >= 0) {
        gpio_config_t reset = {.mode = GPIO_MODE_OUTPUT, .pin_bit_mask = 1ULL << config->reset_gpio_num};
        esp_err_t err = gpio_config(&reset);
        if (err != ESP_OK) {
            free(p);
            return err;
        }
    }
    p->io = io;
    p->reset_gpio = config->reset_gpio_num;
    p->reset_high = config->flags.reset_active_high;
    p->madctl = config->rgb_ele_order == LCD_RGB_ELEMENT_ORDER_BGR ? LCD_CMD_BGR_BIT : 0;
    p->base.reset = ili_reset;
    p->base.init = ili_init;
    p->base.del = ili_del;
    p->base.draw_bitmap = ili_draw;
    p->base.invert_color = ili_invert;
    p->base.mirror = ili_mirror;
    p->base.swap_xy = ili_swap;
    p->base.set_gap = ili_gap;
    p->base.disp_on_off = ili_on_off;
    p->base.disp_sleep = ili_sleep;
    *panel = &p->base;
    return ESP_OK;
}
