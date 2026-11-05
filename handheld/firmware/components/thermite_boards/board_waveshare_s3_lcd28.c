/*
 * Waveshare ESP32-S3-Touch-LCD-2.8 (ESP32-S3R8, 16 MB flash): the touch-only
 * prototype.
 *
 * Sources (read 2026-11-05):
 *   WIKI  https://www.waveshare.com/wiki/ESP32-S3-Touch-LCD-2.8
 *   SCH   https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-2.8/ESP32-S3-Touch-LCD-2.8.pdf
 *   DEMO  https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-2.8/ESP32-S3-Touch-LCD-2.8-Demo.zip,
 *         ESP-IDF/ESP32-S3-Touch-LCD-2.8-Test/main/<path>
 * Pins marked VERIFIED agree between the vendor schematic and the vendor demo;
 * nothing here has run on hardware yet.
 */
#include "sdkconfig.h"
#if CONFIG_THERMITE_BOARD_WAVESHARE_S3_LCD28

#include "board_priv.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "waveshare";

/* Display: ST7789(T3), 240x320 portrait, SPI.            DEMO LCD_Driver/ST7789.h:21-41, SCH. VERIFIED */
#define LCD_HOST SPI3_HOST
#define LCD_MOSI 45
#define LCD_SCLK 40
#define LCD_CS 42
#define LCD_DC 41
#define LCD_RST 39
/* Backlight: NPN driver, active high; demo LEDC at 5 kHz. DEMO ST7789.h:24,32, ST7789.c:62-100. VERIFIED */
#define LCD_BL 5
/* Touch: own I2C bus, CST328 at 0x1A (CST3530 at 0x58 on some later boards: third-party
 * report, UNVERIFIED). SDA 1, SCL 3; INT 4 active low; RST 2 active low.
 *                                                        DEMO Touch_Driver/CST328.h:12,44-49, SCH. VERIFIED */
#define TP_PORT I2C_NUM_1
#define TP_SDA 1
#define TP_SCL 3
#define TP_INT 4
#define TP_RST 2
/* Keys: BOOT on GPIO0 and PWR on GPIO6, both active low with external pull-ups.
 *                                                        SCH Key2/R20 and R1/D2; DEMO PWR_Key/PWR_Key.h:4. VERIFIED */
#define KEY_BOOT 0
#define KEY_PWR 6
/* Battery power latch: high keeps the battery connected; low powers off once PWR is
 * released. USB power bypasses it.                       DEMO PWR_Key/PWR_Key.h:5, PWR_Key.c:39-58, SCH. VERIFIED */
#define BAT_LATCH 7
/* Battery: ADC1 channel 7 on GPIO8, 200k/100k divider (x3), demo trims by 0.9945.
 *                                                        DEMO BAT_Driver/BAT_Driver.h:13-16, BAT_Driver.c:113, SCH
 * R5/R11. VERIFIED */
#define BAT_ADC_CHANNEL ADC_CHANNEL_7
/* Not used here: sensor I2C (QMI8658 0x6B, PCF85063 0x51) on SCL 10 / SDA 11; SD card; audio. */

#define LCD_W 240
#define LCD_H 320
#define LCD_MAX_TRANSFER (LCD_W * 40 * 2)

static const board_info_t INFO = {
    .name = "waveshare-s3-lcd28",
    .width = LCD_W,
    .height = LCD_H,
    .touch = true,
    .console = BOARD_CONSOLE_USB_SERIAL_JTAG,
    .swap_bytes = true,
};

/* The demo's ST7789T3 tuning after SLPOUT (DEMO LCD_Driver/Vernon_ST7789T/Vernon_ST7789T.c:160-202), minus
 * what esp_lcd sends itself (MADCTL, COLMOD, INVON, DISPON). Left out on purpose: RAMCTRL B0 00 E8,
 * which selects little-endian pixels; esp_lcd's B0 00 F0 keeps big-endian and LVGL swaps. The demo
 * sends D0 with length 1 (only A4); that is kept, to match what the vendor runs. */
static const lcd_command_t TUNING[] = {
    {0xB2, 5, {0x0C, 0x0C, 0x00, 0x33, 0x33}, 0}, /* porch */
    {0xB7, 1, {0x75}, 0},                         /* gate control */
    {0xBB, 1, {0x1A}, 0},                         /* VCOM */
    {0xC0, 1, {0x80}, 0},                         /* LCM control */
    {0xC2, 2, {0x01, 0xFF}, 0},                   /* VDV/VRH enable */
    {0xC3, 1, {0x13}, 0},                         /* VRH */
    {0xC4, 1, {0x20}, 0},                         /* VDV */
    {0xC6, 1, {0x0F}, 0},                         /* frame rate 60 Hz */
    {0xD0, 1, {0xA4}, 0},                         /* power control 1 */
    {0xE0, 14, {0xD0, 0x0D, 0x14, 0x0D, 0x0D, 0x09, 0x38, 0x44, 0x4E, 0x3A, 0x17, 0x18, 0x2F, 0x30}, 0},
    {0xE1, 14, {0xD0, 0x09, 0x0F, 0x08, 0x07, 0x14, 0x37, 0x44, 0x4D, 0x38, 0x15, 0x16, 0x2C, 0x2E}, 0},
};

static const backlight_t BACKLIGHT = {
    .pin = LCD_BL, .active_high = true, .hz = 5000, .timer = LEDC_TIMER_0, .channel = LEDC_CHANNEL_0};

static button_t s_buttons[] = {
    /* A touch-only board: both keys wake it and count as input, and send nothing. */
    {.pin = KEY_BOOT, .key = BOARD_KEY_NONE},
    {.pin = KEY_PWR, .key = BOARD_KEY_NONE},
};
#define BUTTONS (sizeof s_buttons / sizeof s_buttons[0])

static i2c_master_dev_handle_t s_touch;
static bool s_cst3530;
static touch_map_t s_touch_map;
static esp_lcd_panel_handle_t s_panel;
static adc_oneshot_unit_handle_t s_adc;
static adc_cali_handle_t s_cali;
static uint64_t s_wake;

const board_info_t *board_info(void)
{
    return &INFO;
}

static esp_err_t touch_init(void)
{
    gpio_config_t reset = {.pin_bit_mask = 1ULL << TP_RST, .mode = GPIO_MODE_OUTPUT};
    ESP_RETURN_ON_ERROR(gpio_config(&reset), TAG, "touch reset pin");
    /* The demo never drives RST; a clean reset costs 60 ms. */
    gpio_set_level(TP_RST, 0);
    vTaskDelay(pdMS_TO_TICKS(10));
    gpio_set_level(TP_RST, 1);
    vTaskDelay(pdMS_TO_TICKS(50));

    i2c_master_bus_handle_t bus;
    ESP_RETURN_ON_ERROR(bi2c_bus(TP_PORT, TP_SDA, TP_SCL, true, &bus), TAG, "touch bus");
    uint16_t range_x = 0, range_y = 0;
    if (i2c_master_probe(bus, CST328_ADDRESS, I2C_TIMEOUT_MS) == ESP_OK) {
        ESP_RETURN_ON_ERROR(bi2c_add(bus, CST328_ADDRESS, 400000, &s_touch), TAG, "CST328");
        cst328_init(s_touch, &range_x, &range_y);
    } else if (i2c_master_probe(bus, CST3530_ADDRESS, I2C_TIMEOUT_MS) == ESP_OK) {
        ESP_LOGW(TAG, "CST3530 touch (later board revision; untested driver)");
        ESP_RETURN_ON_ERROR(bi2c_add(bus, CST3530_ADDRESS, 400000, &s_touch), TAG, "CST3530");
        s_cst3530 = true;
    } else {
        ESP_LOGE(TAG, "no touch controller at 0x1A or 0x58");
        return ESP_ERR_NOT_FOUND;
    }
    /* Scale by the controller's own range when it reports a sane one. */
    bool sane = range_x >= 100 && range_x <= 4096 && range_y >= 100 && range_y <= 4096;
    s_touch_map = (touch_map_t){
        .raw_width = sane ? range_x : LCD_W,
        .raw_height = sane ? range_y : LCD_H,
        .width = LCD_W,
        .height = LCD_H,
        .swap_xy = BOARD_TOUCH_SWAP_XY,
        .mirror_x = BOARD_TOUCH_MIRROR_X,
        .mirror_y = BOARD_TOUCH_MIRROR_Y,
    };
    if (s_touch_map.swap_xy && sane) {
        s_touch_map.raw_width = range_y;
        s_touch_map.raw_height = range_x;
    }
    return board_irq_pin(TP_INT, true);
}

static void battery_init(void)
{
    adc_oneshot_unit_init_cfg_t unit = {.unit_id = ADC_UNIT_1};
    if (adc_oneshot_new_unit(&unit, &s_adc) != ESP_OK) return;
    adc_oneshot_chan_cfg_t channel = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT};
    adc_oneshot_config_channel(s_adc, BAT_ADC_CHANNEL, &channel);
    adc_cali_curve_fitting_config_t cali = {
        .unit_id = ADC_UNIT_1, .chan = BAT_ADC_CHANNEL, .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT};
    if (adc_cali_create_scheme_curve_fitting(&cali, &s_cali) != ESP_OK) s_cali = NULL;
}

esp_err_t board_init(void)
{
    /* Keep the battery connected first of all: on battery, the board is only on while
     * PWR is held until this pin goes high. */
    gpio_config_t latch = {.pin_bit_mask = 1ULL << BAT_LATCH, .mode = GPIO_MODE_OUTPUT};
    gpio_set_level(BAT_LATCH, 1);
    ESP_RETURN_ON_ERROR(gpio_config(&latch), TAG, "latch");
    gpio_set_level(BAT_LATCH, 1);
    gpio_hold_dis(BAT_LATCH);
    gpio_deep_sleep_hold_dis();

    s_wake = board_ext1_wake_mask();
    ESP_RETURN_ON_ERROR(buttons_init(s_buttons, BUTTONS, false), TAG, "keys");
    battery_init();
    esp_err_t err = touch_init();
    if (err != ESP_OK) ESP_LOGE(TAG, "touch unavailable: %s", esp_err_to_name(err));
    return ESP_OK;
}

esp_err_t board_display_init(esp_lcd_panel_io_handle_t *io, esp_lcd_panel_handle_t *panel)
{
    ESP_RETURN_ON_ERROR(backlight_init(&BACKLIGHT), TAG, "backlight");
    static const lcd_spi_bus_t bus = {
        .host = LCD_HOST,
        .mosi = LCD_MOSI,
        .sclk = LCD_SCLK,
        .cs = LCD_CS,
        .dc = LCD_DC,
        /* The demo runs 80 MHz; through the GPIO matrix 40 MHz is the safer default. */
        .pclk_hz = CONFIG_THERMITE_LCD_PCLK_MHZ * 1000000,
        .max_transfer = LCD_MAX_TRANSFER,
    };
    /* MADCTL as the demo ends up: BGR, MX (DEMO ST7789.c:38,47); inversion on (Vernon_ST7789T.c). */
    static const lcd_panel_t config = {
        .controller = LCD_ST7789,
        .reset = LCD_RST,
        .bgr = true,
        .invert = BOARD_LCD_INVERT,
        .mirror_x = BOARD_LCD_MIRROR_X,
        .mirror_y = BOARD_LCD_MIRROR_Y,
        .extra = TUNING,
        .extra_count = sizeof TUNING / sizeof TUNING[0],
    };
    ESP_RETURN_ON_ERROR(lcd_spi_init(&bus, &config, io, panel), TAG, "LCD");
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
    (void)percent;
}

bool board_touch_read(uint16_t *x, uint16_t *y)
{
    if (!s_touch) return false;
    uint16_t rx, ry;
    bool pressed = false;
    esp_err_t err = s_cst3530 ? cst3530_read(s_touch, &rx, &ry, &pressed) : cst328_read(s_touch, &rx, &ry, &pressed);
    if (err != ESP_OK || !pressed) return false;
    touch_map(&s_touch_map, rx, ry, x, y);
    return true;
}

uint32_t board_keys_poll(board_key_event_t *events, size_t max, size_t *count)
{
    *count = 0;
    return buttons_poll(s_buttons, BUTTONS, events, max, count);
}

board_key_t board_wake_key(void)
{
    buttons_consume(s_buttons, BUTTONS, s_wake);
    return BOARD_KEY_NONE; /* wake-only keys */
}

int board_battery(bool *charging)
{
    *charging = false; /* no charge-status pin on this board */
    if (!s_adc) return -1;
    int mv = 0;
    if (s_cali) {
        if (adc_oneshot_get_calibrated_result(s_adc, s_cali, BAT_ADC_CHANNEL, &mv) != ESP_OK) return -1;
    } else {
        int raw;
        if (adc_oneshot_read(s_adc, BAT_ADC_CHANNEL, &raw) != ESP_OK) return -1;
        mv = raw * 3100 / 4095; /* uncalibrated, 12 dB */
    }
    uint32_t battery = (uint32_t)mv * 3u * 10000u / 9945u;
    return lipo_percent(battery);
}

void board_sleep_prepare(void)
{
    board_backlight(0);
    if (s_panel) {
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_disp_sleep(s_panel, true);
    }
    /* Stay powered on battery through deep sleep. */
    gpio_set_level(BAT_LATCH, 1);
    gpio_hold_en(BAT_LATCH);
    gpio_deep_sleep_hold_en();
    uint64_t mask = 1ULL << KEY_BOOT | 1ULL << KEY_PWR;
    /* Touch INT only when idle-high now: a controller holding it low would wake us at once.
     * Whether a touch pulse is long enough for ext1 is UNVERIFIED. */
    if (s_touch && gpio_get_level(TP_INT) == 1) mask |= 1ULL << TP_INT;
    board_wake_low(mask, true);
}

#endif
