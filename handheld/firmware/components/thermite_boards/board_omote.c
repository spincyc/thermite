/*
 * OMOTE Rev5 (ESP32-S3-WROOM-1-N16R8) and its fork OpenRemote Rev6: the
 * target remote. 2.8-inch 240x320 ILI9341 on an 8-bit i80 bus, FT6206 touch,
 * 25 keys on a TCA8418, LIS3DH lift-to-wake, MAX17048 gauge, CH340C console.
 *
 * Sources (facts only; OMOTE is GPL-3.0 and no code is taken from it):
 *   FW    github.com/OMOTE-Community/OMOTE-Firmware @ b99a41a6, hardware/ESP32/<file>
 *   HW    github.com/OMOTE-Community/OMOTE-Hardware @ 236b61ec, PCB/production/netlist.ipc, PCB/BOM.csv
 *   R6HW  github.com/LORDSn1per/OpenRemote-Hardware, PCB/Revision 6/OpenRemote.kicad_pcb
 * Rev6 uses the same ESP32 nets as Rev5 except GPIO45 (microphone power), which we do not use.
 * Nothing here has run on hardware yet.
 */
#include "sdkconfig.h"
#if CONFIG_THERMITE_BOARD_OMOTE_FAMILY

#include <string.h>

#include "board_priv.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "omote";

/* I2C, one bus for touch, keypad, accelerometer and gauge. Its 4k7 pull-ups hang on the
 * switched LCD rail, so the bus only works while LCD_EN is on.
 *                                        FW displaydriver.h:19-20; HW R11/R12 to +VSW. VERIFIED */
#define I2C_SDA 20
#define I2C_SCL 19
#define I2C_HZ 100000 /* what OMOTE's firmware runs (displaydriver.cpp:248) */
/* LCD rail (+VSW: LCD logic, touch, I2C pull-ups): P-FET, active low.
 *                                        FW displaydriver.h:23; HW Q4/R26/R28. VERIFIED */
#define LCD_EN 38
/* Backlight: P-FET, active low; firmware PWMs it at 640 Hz.
 *                                        FW displaydriver.h:22, displaydriver.cpp:12-43; HW Q3. VERIFIED */
#define LCD_BL 9
/* ILI9341, 8080 8-bit (IM[3:0] grounded), no reset line (RC), TE not connected.
 *                                        FW displaydriver.h:24-35; HW J2, U7. VERIFIED */
#define LCD_CS 39
#define LCD_DC 40
#define LCD_WR 41
#define LCD_RD 42
static const int LCD_DATA[8] = {48, 47, 21, 14, 13, 12, 11, 10};
/* Touch: FT6206 family at 0x38; INT not connected, so polled only.  HW J2-46 NC. VERIFIED */
/* Keypad: TCA8418 at 0x34, INT active low with a 10k pull-up to the always-on 3V3.
 *                                        FW keypad_keys_hal_esp32.cpp:22,85; HW U12, R36. VERIFIED */
#define KEY_INT 8
/* Key backlight LEDs, active high; firmware PWMs at 5 kHz. GPIO46 is a strapping pin, held low by
 * the LEDs at reset.                    FW keypad_keys_hal_esp32.cpp:23,99-101; HW D6-D16. VERIFIED */
#define KEY_BL 46
/* LIS3DH at 0x19 (SA0 pulled up), INT1 push-pull to GPIO2.  FW sleep_hal_esp32.cpp:22,39; HW U5. VERIFIED */
#define LIS3DH_ADDRESS 0x19
#define MOTION_INT 2
/* TP4056 ~CHRG: low while charging.     FW battery_hal_esp32.cpp:15,21,40; HW U11-7. VERIFIED */
#define CHARGING 1
/* Kept quiet in sleep: IR LED driver (active low, GPIO5), IR receiver power (GPIO6, high = on),
 * SD card power (GPIO16, active low).   FW infrared_*_hal_esp32.cpp, sd_card_hal_esp32.cpp; HW. VERIFIED */
#define IR_TX 5
#define IR_RX_POWER 6
#define SD_POWER 16

#define LCD_W 240
#define LCD_H 320
#define LCD_MAX_TRANSFER (LCD_W * 40 * 2)

static const board_info_t INFO = {
#if CONFIG_THERMITE_BOARD_OPENREMOTE_REV6
    .name = "openremote-rev6",
#else
    .name = "omote-rev5",
#endif
    .width = LCD_W,
    .height = LCD_H,
    .touch = true,
    .console = BOARD_CONSOLE_UART0,
    .dpad = true,
    .volume = true,
    .channel = true,
    .playback = true,
    .swap_bytes = false, /* the i80 peripheral swaps */
};

/* The 5x5 matrix as OMOTE's firmware maps it (keypad_keys_hal_esp32.cpp:29-35), [row][column].
 * Row 0 column 0 ("?") is Home by our assignment; the schematic shows no switch there.
 * Row 1 column 1 ("off") is Power, though its switch (S17) is wired to ROW5 instead. */
static const board_key_t MATRIX[5][5] = {
    {BOARD_KEY_HOME, BOARD_KEY_PLAY, BOARD_KEY_SETTINGS, BOARD_KEY_REWIND, BOARD_KEY_STOP},
    {BOARD_KEY_FORWARD, BOARD_KEY_POWER, BOARD_KEY_BACK, BOARD_KEY_UP, BOARD_KEY_LEFT},
    {BOARD_KEY_BLUE, BOARD_KEY_CHANNEL_DOWN, BOARD_KEY_RED, BOARD_KEY_YELLOW, BOARD_KEY_GREEN},
    {BOARD_KEY_INFO, BOARD_KEY_RIGHT, BOARD_KEY_VOLUME_UP, BOARD_KEY_OK, BOARD_KEY_DOWN},
    {BOARD_KEY_SOURCE, BOARD_KEY_CHANNEL_UP, BOARD_KEY_VOLUME_DOWN, BOARD_KEY_MUTE, BOARD_KEY_RECORD},
};
/* SW_PWR grounds TCA8418 ROW5, a GPI.    HW S17, U12-3. VERIFIED */
#define POWER_ROW 5

static const backlight_t BACKLIGHT = {
    .pin = LCD_BL, .active_high = false, .hz = 640, .timer = LEDC_TIMER_0, .channel = LEDC_CHANNEL_0};
static const backlight_t KEY_BACKLIGHT = {
    .pin = KEY_BL, .active_high = true, .hz = 5000, .timer = LEDC_TIMER_1, .channel = LEDC_CHANNEL_1};

static i2c_master_dev_handle_t s_touch, s_motion, s_gauge;
static tca8418_t s_keys;
static bool s_keys_ok;
static esp_lcd_panel_handle_t s_panel;
static uint32_t s_rail_on_ms;
static touch_map_t s_touch_map = {
    .raw_width = LCD_W,
    .raw_height = LCD_H,
    .width = LCD_W,
    .height = LCD_H,
    .swap_xy = BOARD_TOUCH_SWAP_XY,
    .mirror_x = BOARD_TOUCH_MIRROR_X,
    .mirror_y = BOARD_TOUCH_MIRROR_Y,
};
/* Key edges read before board_keys_poll was first called (the wake read). */
static board_key_event_t s_pending[16];
static size_t s_pending_count;
static board_key_t s_wake_key;

const board_info_t *board_info(void)
{
    return &INFO;
}

static void output(gpio_num_t pin, int level)
{
    gpio_hold_dis(pin);
    gpio_set_level(pin, level);
    gpio_config_t config = {.pin_bit_mask = 1ULL << pin, .mode = GPIO_MODE_OUTPUT};
    gpio_config(&config);
    gpio_set_level(pin, level);
}

static board_key_t decode(uint8_t raw, bool *down)
{
    tca_event_t e = tca8418_decode(raw);
    *down = e.press;
    if (e.kind == TCA_EVENT_KEY && e.row < 5 && e.column < 5) return MATRIX[e.row][e.column];
    if (e.kind == TCA_EVENT_GPI_ROW && e.pin == POWER_ROW) return BOARD_KEY_POWER;
    return BOARD_KEY_NONE;
}

/* Drains the scanner into events; returns how many. */
static size_t read_keys(board_key_event_t *events, size_t max)
{
    uint8_t raw[10];
    size_t n = 0, count = 0;
    if (!s_keys_ok || tca8418_read(&s_keys, raw, sizeof raw, &n) != ESP_OK) return 0;
    for (size_t i = 0; i < n && count < max; i++) {
        bool down;
        board_key_t key = decode(raw[i], &down);
        if (key != BOARD_KEY_NONE || raw[i] != 0) events[count++] = (board_key_event_t){.key = key, .down = down};
    }
    return count;
}

esp_err_t board_init(void)
{
    /* Release what board_sleep_prepare held (the LCD bus would stay stuck low). */
    gpio_deep_sleep_hold_dis();
    for (int i = 0; i < 8; i++) gpio_hold_dis(LCD_DATA[i]);
    gpio_hold_dis(LCD_CS);
    gpio_hold_dis(LCD_DC);
    gpio_hold_dis(LCD_WR);
    /* Backlights off, then the LCD rail on: it also powers the I2C pull-ups. */
    output(LCD_BL, 1);
    output(KEY_BL, 0);
    output(LCD_EN, 0);
    s_rail_on_ms = board_ms();
    output(LCD_RD, 1); /* never read */
    output(SD_POWER, 1);
    output(IR_RX_POWER, 0);
    gpio_hold_dis(IR_TX);
    gpio_config_t charging = {
        .pin_bit_mask = 1ULL << CHARGING, .mode = GPIO_MODE_INPUT, .pull_up_en = GPIO_PULLUP_ENABLE};
    gpio_config(&charging);
    vTaskDelay(pdMS_TO_TICKS(5)); /* rail and pull-ups up */

    i2c_master_bus_handle_t bus;
    ESP_RETURN_ON_ERROR(bi2c_bus(I2C_NUM_0, I2C_SDA, I2C_SCL, false, &bus), TAG, "I2C");
    /* Keypad first: after a key wake, its FIFO holds the press that woke us. */
    esp_err_t err = tca8418_init(&s_keys, bus, I2C_HZ, 0x1F, 0x001F, 1u << POWER_ROW, 0);
    s_keys_ok = err == ESP_OK;
    if (!s_keys_ok) ESP_LOGE(TAG, "keypad: %s", esp_err_to_name(err));
    s_pending_count = read_keys(s_pending, sizeof s_pending / sizeof s_pending[0]);
    s_wake_key = BOARD_KEY_NONE;
    if (board_woke()) {
        for (size_t i = 0; i < s_pending_count; i++) {
            if (s_pending[i].down && s_pending[i].key != BOARD_KEY_NONE) {
                s_wake_key = s_pending[i].key;
                memmove(&s_pending[i], &s_pending[i + 1], (s_pending_count - i - 1) * sizeof s_pending[0]);
                s_pending_count--;
                break;
            }
        }
    } else {
        s_pending_count = 0; /* stale events from before a cold boot */
    }
    ESP_RETURN_ON_ERROR(board_irq_pin(KEY_INT, false), TAG, "keypad INT");

    if (bi2c_add(bus, LIS3DH_ADDRESS, I2C_HZ, &s_motion) == ESP_OK && lis3dh_init_motion_wake(s_motion) != ESP_OK) {
        ESP_LOGW(TAG, "LIS3DH not answering: no lift-to-wake");
        s_motion = NULL;
    }
    if (bi2c_add(bus, MAX17048_ADDRESS, I2C_HZ, &s_gauge) != ESP_OK) s_gauge = NULL;
    if (bi2c_add(bus, FT6X06_ADDRESS, I2C_HZ, &s_touch) != ESP_OK) s_touch = NULL;
    return ESP_OK;
}

esp_err_t board_display_init(esp_lcd_panel_io_handle_t *io, esp_lcd_panel_handle_t *panel)
{
    /* OMOTE's firmware waits 100 ms after LCD_EN before the first command. */
    uint32_t since = board_ms() - s_rail_on_ms;
    if (since < 100) vTaskDelay(pdMS_TO_TICKS(100 - since));
    ESP_RETURN_ON_ERROR(backlight_init(&BACKLIGHT), TAG, "backlight");
    ESP_RETURN_ON_ERROR(backlight_init(&KEY_BACKLIGHT), TAG, "key backlight");
    static lcd_i80_bus_t bus = {
        .cs = LCD_CS,
        .dc = LCD_DC,
        .wr = LCD_WR,
        /* OMOTE runs 40 MHz, beyond the ILI9341's ~15 MHz write cycle; start at 10. */
        .pclk_hz = CONFIG_THERMITE_LCD_PCLK_MHZ * 1000000,
        .max_transfer = LCD_MAX_TRANSFER,
    };
    memcpy(bus.data, LCD_DATA, sizeof LCD_DATA);
    /* BGR (FW displaydriver0 rgb_order false). OMOTE's default driver (LovyanGFX, offset
     * rotation 2) ends at MADCTL MY|MX|BGR; its Arduino_GFX path at MX|BGR: CONFLICT, settle on
     * hardware with CONFIG_THERMITE_LCD_MIRROR_Y. Inversion: off for the Adafruit 2770 panel,
     * on for BuyDisplay panels (FW displaydriver.h:69-78). */
    static const lcd_panel_t config = {
#if CONFIG_THERMITE_OMOTE_PANEL_ST7789
        .controller = LCD_ST7789,
#else
        .controller = LCD_ILI9341,
#endif
        .reset = -1,
        .bgr = true,
        .invert = BOARD_LCD_INVERT,
        .mirror_x = BOARD_LCD_MIRROR_X,
        .mirror_y = BOARD_LCD_MIRROR_Y,
    };
    ESP_RETURN_ON_ERROR(lcd_i80_init(&bus, &config, io, panel), TAG, "LCD");
    s_panel = *panel;
    return ESP_OK;
}

size_t board_display_max_transfer(void)
{
    return LCD_MAX_TRANSFER;
}

void board_backlight(uint8_t percent)
{
    backlight_set(&BACKLIGHT, percent);
}

void board_key_backlight(uint8_t percent)
{
    backlight_set(&KEY_BACKLIGHT, percent);
}

bool board_touch_read(uint16_t *x, uint16_t *y)
{
    uint16_t rx, ry;
    bool pressed = false;
    if (!s_touch || ft6x06_read(s_touch, &rx, &ry, &pressed) != ESP_OK || !pressed) return false;
    touch_map(&s_touch_map, rx, ry, x, y);
    return true;
}

uint32_t board_keys_poll(board_key_event_t *events, size_t max, size_t *count)
{
    *count = 0;
    while (s_pending_count && *count < max) {
        events[(*count)++] = s_pending[0];
        memmove(&s_pending[0], &s_pending[1], --s_pending_count * sizeof s_pending[0]);
    }
    *count += read_keys(events + *count, max - *count);
    /* INT re-asserts while events remain (CFG INT_CFG); a low line means more to read. */
    return gpio_get_level(KEY_INT) == 0 ? 10 : UINT32_MAX;
}

board_key_t board_wake_key(void)
{
    return s_wake_key;
}

int board_battery(bool *charging)
{
    *charging = gpio_get_level(CHARGING) == 0;
    int percent;
    if (!s_gauge || max17048_read(s_gauge, &percent, NULL, NULL) != ESP_OK) return -1;
    return percent;
}

void board_sleep_prepare(void)
{
    board_backlight(0);
    board_key_backlight(0);
    if (s_panel) {
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_disp_sleep(s_panel, true);
    }
    /* Release latched interrupts; a line still low would wake us at once. */
    uint64_t mask = 0;
    board_key_event_t ignored[10];
    read_keys(ignored, 10);
    if (gpio_get_level(KEY_INT) == 1) {
        mask |= 1ULL << KEY_INT;
    } else {
        ESP_LOGW(TAG, "keypad INT stuck low: keys will not wake");
    }
    if (s_motion) {
        lis3dh_clear(s_motion);
        if (gpio_get_level(MOTION_INT) == 1) mask |= 1ULL << MOTION_INT;
    }
    /* The LCD is about to lose power: drive its bus low so nothing back-powers it, as
     * OMOTE's firmware does, then switch the rail and backlight off and hold them so. */
    for (int i = 0; i < 8; i++) output(LCD_DATA[i], 0);
    output(LCD_CS, 0);
    output(LCD_DC, 0);
    output(LCD_WR, 0);
    output(LCD_RD, 0);
    output(LCD_BL, 1);
    output(KEY_BL, 0);
    output(LCD_EN, 1);
    output(SD_POWER, 1);
    output(IR_RX_POWER, 0);
    const gpio_num_t held[] = {LCD_BL, KEY_BL, LCD_EN, SD_POWER, IR_RX_POWER, LCD_CS, LCD_DC, LCD_WR, LCD_RD};
    for (size_t i = 0; i < sizeof held / sizeof held[0]; i++) gpio_hold_en(held[i]);
    for (int i = 0; i < 8; i++) gpio_hold_en(LCD_DATA[i]);
    gpio_deep_sleep_hold_en();
    /* External pull-up on KEY_INT; LIS3DH INT1 is push-pull. */
    board_wake_low(mask, false);
}

#endif
