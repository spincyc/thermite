/*
 * Helpers and small drivers the board files share. Not part of board.h.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "board.h"
#include "board_decode.h"
#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_lcd_panel_dev.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "sdkconfig.h"

/* The bring-up overrides (Kconfig) as 0 or 1: sdkconfig.h leaves a disabled bool undefined. */
#ifdef CONFIG_THERMITE_LCD_INVERT
#define BOARD_LCD_INVERT 1
#else
#define BOARD_LCD_INVERT 0
#endif
#ifdef CONFIG_THERMITE_LCD_MIRROR_X
#define BOARD_LCD_MIRROR_X 1
#else
#define BOARD_LCD_MIRROR_X 0
#endif
#ifdef CONFIG_THERMITE_LCD_MIRROR_Y
#define BOARD_LCD_MIRROR_Y 1
#else
#define BOARD_LCD_MIRROR_Y 0
#endif
#ifdef CONFIG_THERMITE_TOUCH_SWAP_XY
#define BOARD_TOUCH_SWAP_XY 1
#else
#define BOARD_TOUCH_SWAP_XY 0
#endif
#ifdef CONFIG_THERMITE_TOUCH_MIRROR_X
#define BOARD_TOUCH_MIRROR_X 1
#else
#define BOARD_TOUCH_MIRROR_X 0
#endif
#ifdef CONFIG_THERMITE_TOUCH_MIRROR_Y
#define BOARD_TOUCH_MIRROR_Y 1
#else
#define BOARD_TOUCH_MIRROR_Y 0
#endif


#define I2C_TIMEOUT_MS 50

/* Milliseconds since boot (esp_timer), for debouncing. */
uint32_t board_ms(void);

/* I2C master, the v5.5 driver (driver/i2c_master.h). */
esp_err_t bi2c_bus(i2c_port_num_t port, gpio_num_t sda, gpio_num_t scl, bool internal_pullup,
                   i2c_master_bus_handle_t *bus);
esp_err_t bi2c_add(i2c_master_bus_handle_t bus, uint16_t address, uint32_t hz, i2c_master_dev_handle_t *dev);
esp_err_t bi2c_read(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *data, size_t length);
esp_err_t bi2c_write(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t value);
/* 16-bit register addresses, sent MSB first (CST328). */
esp_err_t bi2c_read16(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t *data, size_t length);
esp_err_t bi2c_write16(i2c_master_dev_handle_t dev, uint16_t reg, const uint8_t *data, size_t length);
/* Read-modify-write of the bits in mask. */
esp_err_t bi2c_update(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t mask, uint8_t value);

/* Backlight on one LEDC channel; percent 0 drives the pin to its off level. */
typedef struct {
    gpio_num_t pin;
    bool active_high;
    uint32_t hz;
    ledc_timer_t timer;
    ledc_channel_t channel;
} backlight_t;
esp_err_t backlight_init(const backlight_t *b);
void backlight_set(const backlight_t *b, uint8_t percent);

/* A controller command with its parameters, then a delay. */
typedef struct {
    uint8_t command;
    uint8_t length;
    uint8_t data[15];
    uint16_t delay_ms;
} lcd_command_t;
esp_err_t lcd_send(esp_lcd_panel_io_handle_t io, const lcd_command_t *commands, size_t count);

typedef enum { LCD_ST7789 = 0, LCD_ILI9341 } lcd_controller_t;

/* A MIPI-DCS panel, RGB565, on whichever bus. */
typedef struct {
    lcd_controller_t controller;
    int reset; /* -1: none wired, software reset */
    int x_gap, y_gap;
    bool bgr, invert, swap_xy, mirror_x, mirror_y;
    /* Sent after the controller's own init and before display on (vendor tuning). */
    const lcd_command_t *extra;
    size_t extra_count;
} lcd_panel_t;

/* SPI (4-wire, write only). Pixels go big-endian: LVGL must swap bytes. */
typedef struct {
    spi_host_device_t host;
    int mosi, sclk, cs, dc;
    uint32_t pclk_hz;
    size_t max_transfer; /* bytes */
} lcd_spi_bus_t;
esp_err_t lcd_spi_init(const lcd_spi_bus_t *bus, const lcd_panel_t *config, esp_lcd_panel_io_handle_t *io,
                       esp_lcd_panel_handle_t *panel);

/* Intel 8080 8-bit parallel (the S3's LCD_CAM). The peripheral swaps each pixel's bytes,
 * so LVGL sends its native little-endian RGB565. */
typedef struct {
    int cs, dc, wr;
    int data[8];
    uint32_t pclk_hz;
    size_t max_transfer; /* bytes */
} lcd_i80_bus_t;
esp_err_t lcd_i80_init(const lcd_i80_bus_t *bus, const lcd_panel_t *config, esp_lcd_panel_io_handle_t *io,
                       esp_lcd_panel_handle_t *panel);

/* ILI9341 as an esp_lcd panel (ESP-IDF has no built-in one): reset, our init sequence,
 * windowed RAM writes, mirror, swap, invert, on/off and sleep. */
esp_err_t lcd_new_panel_ili9341(esp_lcd_panel_io_handle_t io, const esp_lcd_panel_dev_config_t *config,
                                esp_lcd_panel_handle_t *panel);

/* TCA8418 key scanner (TI SCPS215). */
#define TCA8418_ADDRESS 0x34
typedef struct {
    i2c_master_dev_handle_t dev;
} tca8418_t;
/* Rows and columns in the matrix (bit masks of ROW0-7 and COL0-9); gpi_rows/gpi_cols
 * are pins used as general-purpose inputs that report events into the FIFO. */
esp_err_t tca8418_init(tca8418_t *t, i2c_master_bus_handle_t bus, uint32_t hz, uint8_t rows, uint16_t cols,
                       uint8_t gpi_rows, uint16_t gpi_cols);
/* Pops up to max raw FIFO events and clears the interrupt; *count says how many. */
esp_err_t tca8418_read(tca8418_t *t, uint8_t *events, size_t max, size_t *count);
/* The GPIO_DAT_STAT level of COLn (n 0-9) configured as an input. */
esp_err_t tca8418_col_level(tca8418_t *t, unsigned column, bool *high);

/* FT6x06 family touch (FocalTech), address 0x38. */
#define FT6X06_ADDRESS 0x38
esp_err_t ft6x06_read(i2c_master_dev_handle_t dev, uint16_t *x, uint16_t *y, bool *pressed);
esp_err_t ft6x06_sleep(i2c_master_dev_handle_t dev);

/* CST328 touch (Hynitron), address 0x1A, 16-bit registers. */
#define CST328_ADDRESS 0x1A
/* Logs and returns the controller's own X/Y range (0 when unreadable). */
esp_err_t cst328_init(i2c_master_dev_handle_t dev, uint16_t *range_x, uint16_t *range_y);
esp_err_t cst328_read(i2c_master_dev_handle_t dev, uint16_t *x, uint16_t *y, bool *pressed);

/* CST3530 touch (Hynitron), address 0x58, 32-bit commands (UNVERIFIED part). */
#define CST3530_ADDRESS 0x58
esp_err_t cst3530_read(i2c_master_dev_handle_t dev, uint16_t *x, uint16_t *y, bool *pressed);

/* LIS3DH accelerometer (ST): low-power motion detection on INT1, active low. */
esp_err_t lis3dh_init_motion_wake(i2c_master_dev_handle_t dev);
/* Reads INT1_SRC, releasing a latched interrupt; true when motion was the cause. */
bool lis3dh_clear(i2c_master_dev_handle_t dev);

/* MAX17048 fuel gauge (Analog Devices), address 0x36. */
#define MAX17048_ADDRESS 0x36
esp_err_t max17048_read(i2c_master_dev_handle_t dev, int *percent, uint16_t *millivolts, int *crate_tenths);

/* The notify callback (board_set_notify) from a GPIO interrupt on pin, any edge. */
esp_err_t board_irq_pin(gpio_num_t pin, bool pullup);

/* Push buttons to ground, debounced by polling after their interrupts. */
typedef struct {
    gpio_num_t pin;
    board_key_t key; /* BOARD_KEY_NONE: wakes and counts as input, sends nothing */
    debounce_t state;
} button_t;
esp_err_t buttons_init(button_t *buttons, size_t n, bool pullup);
uint32_t buttons_poll(button_t *buttons, size_t n, board_key_event_t *events, size_t max, size_t *count);
/* The button on pin is already down (it woke the board): report only its release. */
board_key_t buttons_consume(button_t *buttons, size_t n, uint64_t wake_mask);

/* The ext1 pins that woke the board from deep sleep, 0 for any other start. */
uint64_t board_ext1_wake_mask(void);

/* Arms ext1 deep-sleep wake on mask, all active low, keeping the pins' pull-ups
 * on in sleep when pullup is set (pins without external pull-ups). */
esp_err_t board_wake_low(uint64_t mask, bool pullup);
