/*
 * M5Stack StickS3 (ESP32-S3-PICO-1-N8R8): the link test rig. 135x240 screen
 * without touch, two keys, M5PM1 power-management chip.
 *
 * Sources (read 2026-11-05):
 *   DOC   https://docs.m5stack.com/en/core/StickS3
 *   SCH   K150_Stick_S3_PRJ_V0.6 schematic (2025-11-11), linked from DOC
 *   M5GFX github.com/m5stack/M5GFX src/M5GFX.cpp @ 7cb26b4
 *   M5U   github.com/m5stack/M5Unified src/M5Unified.inl, src/utility/power/M5PM1_Class.inl,
 *         src/utility/Power_Class.inl @ 44952cb
 * Nothing here has run on hardware yet.
 */
#include "sdkconfig.h"
#if CONFIG_THERMITE_BOARD_M5STICK_S3

#include "board_priv.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "sticks3";

/* Display: ST7789P3 135x240, SPI3, 40 MHz; RAM offset 52/40 in portrait.
 *                                       M5GFX M5GFX.cpp:3030-3057, SCH p3. VERIFIED */
#define LCD_HOST SPI3_HOST
#define LCD_MOSI 39
#define LCD_SCLK 40
#define LCD_DC 45
#define LCD_CS 41
#define LCD_RST 21
#define LCD_X_GAP 52
#define LCD_Y_GAP 40
/* Backlight: N-FET on the LED cathode, active high; M5GFX PWMs it at 256 Hz.
 *                                       M5GFX M5GFX.cpp:3062, SCH p3. VERIFIED */
#define LCD_BL 38
/* Keys A and B: active low, external 10k pull-ups on the deep-sleep rail.
 *                                       M5U M5Unified.inl:2503-2505, SCH p3. VERIFIED */
#define KEY_A 11
#define KEY_B 12
/* Internal I2C: M5PM1 0x6E, BMI270 0x68, ES8311 0x18.   M5U M5Unified.inl:103, SCH p3. VERIFIED */
#define I2C_SDA 47
#define I2C_SCL 48
#define PM1_ADDRESS 0x6E

/* M5PM1 registers.                                      M5U M5PM1_Class.inl:25-50. VERIFIED */
enum {
    PM1_GPIO_MODE = 0x10, /* 1 = output */
    PM1_GPIO_OUT = 0x11,
    PM1_GPIO_IN = 0x12,
    PM1_GPIO_DRIVE = 0x13, /* 0 = push-pull */
    PM1_GPIO_FUNC = 0x16,  /* bit clear = plain GPIO */
    PM1_I2C_SLEEP = 0x09,
    PM1_WATCHDOG = 0x0A,
    PM1_VBAT = 0x22, /* mV, little-endian, 2 bytes */
};
#define PM1_LCD_RAIL (1u << 2) /* PMIC GPIO2 switches 3V3_L3B (LCD). M5GFX M5GFX.cpp:3016-3027, SCH p1 */
#define PM1_CHARGING (1u << 0) /* PMIC GPIO0: TP charger status, low = charging. M5U Power_Class.inl:443-446 */

#define LCD_W 135
#define LCD_H 240
#define LCD_MAX_TRANSFER (LCD_W * 60 * 2)

static const board_info_t INFO = {
    .name = "m5stick-s3",
    .width = LCD_W,
    .height = LCD_H,
    .touch = false,
    .console = BOARD_CONSOLE_USB_SERIAL_JTAG,
    .swap_bytes = true,
    .deep_sleep = true,
};

static const backlight_t BACKLIGHT = {
    .pin = LCD_BL, .active_high = true, .hz = 256, .timer = LEDC_TIMER_0, .channel = LEDC_CHANNEL_0};

/* Our choice for a test rig: A is Play (one press, visible), B is Volume + (exercises repeat). */
static button_t s_buttons[] = {
    {.pin = KEY_A, .key = BOARD_KEY_PLAY},
    {.pin = KEY_B, .key = BOARD_KEY_VOLUME_UP},
};
#define BUTTONS (sizeof s_buttons / sizeof s_buttons[0])

static i2c_master_dev_handle_t s_pm1;
static esp_lcd_panel_handle_t s_panel;
static uint64_t s_wake;

const board_info_t *board_info(void)
{
    return &INFO;
}

static esp_err_t lcd_rail(bool on)
{
    ESP_RETURN_ON_ERROR(bi2c_update(s_pm1, PM1_GPIO_FUNC, PM1_LCD_RAIL, 0), TAG, "PM1 func");
    ESP_RETURN_ON_ERROR(bi2c_update(s_pm1, PM1_GPIO_MODE, PM1_LCD_RAIL, PM1_LCD_RAIL), TAG, "PM1 mode");
    ESP_RETURN_ON_ERROR(bi2c_update(s_pm1, PM1_GPIO_DRIVE, PM1_LCD_RAIL, 0), TAG, "PM1 drive");
    return bi2c_update(s_pm1, PM1_GPIO_OUT, PM1_LCD_RAIL, on ? PM1_LCD_RAIL : 0);
}

esp_err_t board_init(void)
{
    s_wake = board_ext1_wake_mask();
    ESP_RETURN_ON_ERROR(buttons_init(s_buttons, BUTTONS, false), TAG, "keys");
    i2c_master_bus_handle_t bus;
    ESP_RETURN_ON_ERROR(bi2c_bus(I2C_NUM_0, I2C_SDA, I2C_SCL, true, &bus), TAG, "I2C");
    ESP_RETURN_ON_ERROR(bi2c_add(bus, PM1_ADDRESS, 100000, &s_pm1), TAG, "PM1");
    /* As M5Unified: keep the PMIC's I2C awake and its watchdog off (M5PM1_Class.inl:69-81). */
    if (bi2c_write(s_pm1, PM1_I2C_SLEEP, 0x00) != ESP_OK || bi2c_write(s_pm1, PM1_WATCHDOG, 0x00) != ESP_OK)
        ESP_LOGW(TAG, "PM1 not answering");
    return ESP_OK;
}

esp_err_t board_display_init(esp_lcd_panel_io_handle_t *io, esp_lcd_panel_handle_t *panel)
{
    ESP_RETURN_ON_ERROR(lcd_rail(true), TAG, "LCD rail");
    vTaskDelay(pdMS_TO_TICKS(10));
    ESP_RETURN_ON_ERROR(backlight_init(&BACKLIGHT), TAG, "backlight");
    static const lcd_spi_bus_t bus = {
        .host = LCD_HOST,
        .mosi = LCD_MOSI,
        .sclk = LCD_SCLK,
        .cs = LCD_CS,
        .dc = LCD_DC,
        .pclk_hz = CONFIG_THERMITE_LCD_PCLK_MHZ * 1000000,
        .max_transfer = LCD_MAX_TRANSFER,
    };
    /* BGR, inverted, portrait (M5GFX M5GFX.cpp:3053-3057; lgfx rgb_order false = BGR). */
    static const lcd_panel_t config = {
        .controller = LCD_ST7789,
        .reset = LCD_RST,
        .x_gap = LCD_X_GAP,
        .y_gap = LCD_Y_GAP,
        .bgr = true,
        .invert = BOARD_LCD_INVERT,
        .mirror_x = BOARD_LCD_MIRROR_X,
        .mirror_y = BOARD_LCD_MIRROR_Y,
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
    (void)x;
    (void)y;
    return false;
}

uint32_t board_keys_poll(board_key_event_t *events, size_t max, size_t *count)
{
    *count = 0;
    return buttons_poll(s_buttons, BUTTONS, events, max, count);
}

board_key_t board_wake_key(void)
{
    return buttons_consume(s_buttons, BUTTONS, s_wake);
}

int board_battery(bool *charging)
{
    *charging = false;
    uint8_t b[2];
    if (!s_pm1 || bi2c_read(s_pm1, PM1_VBAT, b, 2) != ESP_OK) return -1;
    uint32_t mv = (uint32_t)(b[0] | b[1] << 8);
    uint8_t in;
    if (bi2c_read(s_pm1, PM1_GPIO_IN, &in, 1) == ESP_OK) *charging = (in & PM1_CHARGING) == 0;
    if (mv < 2500) return -1; /* no cell */
    /* M5Unified's linear 3.3-4.1 V scale (Power_Class.inl:2585). */
    int percent = mv <= 3300 ? 0 : (int)((mv - 3300) * 100 / 800);
    return percent > 100 ? 100 : percent;
}

void board_sleep_prepare(void)
{
    board_backlight(0);
    if (s_panel) {
        esp_lcd_panel_disp_on_off(s_panel, false);
        esp_lcd_panel_disp_sleep(s_panel, true);
    }
    lcd_rail(false); /* cut the LCD and audio rail (3V3_L3B) */
    board_wake_low(1ULL << KEY_A | 1ULL << KEY_B, false);
}

#endif
