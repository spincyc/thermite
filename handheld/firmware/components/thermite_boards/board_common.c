/* I2C, backlight, SPI LCD bring-up and sleep helpers the board files share. */
#include <string.h>

#include "board_priv.h"
#include "driver/rtc_io.h"
#include "esp_check.h"
#include "esp_lcd_io_i80.h"
#include "esp_lcd_io_spi.h"
#include "esp_lcd_panel_commands.h"
#include "esp_lcd_panel_st7789.h"
#include "esp_sleep.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "board";

static void (*s_notify)(void *context);
static void *s_notify_context;

void board_set_notify(void (*notify)(void *context), void *context)
{
    s_notify_context = context;
    s_notify = notify;
}

static void board_isr(void *arg)
{
    (void)arg;
    if (s_notify) s_notify(s_notify_context);
}

esp_err_t board_irq_pin(gpio_num_t pin, bool pullup)
{
    static bool service;
    if (!service) {
        esp_err_t err = gpio_install_isr_service(0);
        if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) return err;
        service = true;
    }
    if (rtc_gpio_is_valid_gpio(pin)) rtc_gpio_deinit(pin); /* back from ext1 wake duty */
    gpio_config_t config = {
        .pin_bit_mask = 1ULL << pin,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = pullup ? GPIO_PULLUP_ENABLE : GPIO_PULLUP_DISABLE,
        .intr_type = GPIO_INTR_ANYEDGE,
    };
    ESP_RETURN_ON_ERROR(gpio_config(&config), TAG, "GPIO%d", pin);
    return gpio_isr_handler_add(pin, board_isr, NULL);
}

esp_err_t buttons_init(button_t *buttons, size_t n, bool pullup)
{
    for (size_t i = 0; i < n; i++) ESP_RETURN_ON_ERROR(board_irq_pin(buttons[i].pin, pullup), TAG, "button");
    return ESP_OK;
}

uint32_t buttons_poll(button_t *buttons, size_t n, board_key_event_t *events, size_t max, size_t *count)
{
    uint32_t now = board_ms();
    bool settling = false;
    for (size_t i = 0; i < n; i++) {
        bool down = gpio_get_level(buttons[i].pin) == 0;
        if (debounce_update(&buttons[i].state, down, now) && *count < max) {
            events[(*count)++] = (board_key_event_t){.key = buttons[i].key, .down = buttons[i].state.stable};
        }
        if (buttons[i].state.raw != buttons[i].state.stable) settling = true;
    }
    return settling ? 5 : UINT32_MAX;
}

board_key_t buttons_consume(button_t *buttons, size_t n, uint64_t wake_mask)
{
    for (size_t i = 0; i < n; i++) {
        if (wake_mask & (1ULL << buttons[i].pin)) {
            buttons[i].state = (debounce_t){.stable = true, .raw = true, .since = board_ms()};
            return buttons[i].key;
        }
    }
    return BOARD_KEY_NONE;
}

uint64_t board_ext1_wake_mask(void)
{
    if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_EXT1) return 0;
    return esp_sleep_get_ext1_wakeup_status();
}

bool board_woke(void)
{
    return esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_UNDEFINED;
}

uint32_t board_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

esp_err_t bi2c_bus(i2c_port_num_t port, gpio_num_t sda, gpio_num_t scl, bool internal_pullup,
                   i2c_master_bus_handle_t *bus)
{
    i2c_master_bus_config_t config = {
        .i2c_port = port,
        .sda_io_num = sda,
        .scl_io_num = scl,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = internal_pullup,
    };
    return i2c_new_master_bus(&config, bus);
}

esp_err_t bi2c_add(i2c_master_bus_handle_t bus, uint16_t address, uint32_t hz, i2c_master_dev_handle_t *dev)
{
    i2c_device_config_t config = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = address,
        .scl_speed_hz = hz,
    };
    return i2c_master_bus_add_device(bus, &config, dev);
}

esp_err_t bi2c_read(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *data, size_t length)
{
    return i2c_master_transmit_receive(dev, &reg, 1, data, length, I2C_TIMEOUT_MS);
}

esp_err_t bi2c_write(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t value)
{
    uint8_t buffer[2] = {reg, value};
    return i2c_master_transmit(dev, buffer, sizeof buffer, I2C_TIMEOUT_MS);
}

esp_err_t bi2c_read16(i2c_master_dev_handle_t dev, uint16_t reg, uint8_t *data, size_t length)
{
    uint8_t address[2] = {(uint8_t)(reg >> 8), (uint8_t)reg};
    return i2c_master_transmit_receive(dev, address, sizeof address, data, length, I2C_TIMEOUT_MS);
}

esp_err_t bi2c_write16(i2c_master_dev_handle_t dev, uint16_t reg, const uint8_t *data, size_t length)
{
    uint8_t buffer[2 + 8];
    if (length > sizeof buffer - 2) return ESP_ERR_INVALID_SIZE;
    buffer[0] = (uint8_t)(reg >> 8);
    buffer[1] = (uint8_t)reg;
    if (length) memcpy(buffer + 2, data, length);
    return i2c_master_transmit(dev, buffer, 2 + length, I2C_TIMEOUT_MS);
}

esp_err_t bi2c_update(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t mask, uint8_t value)
{
    uint8_t current;
    ESP_RETURN_ON_ERROR(bi2c_read(dev, reg, &current, 1), TAG, "read 0x%02x", reg);
    return bi2c_write(dev, reg, (uint8_t)((current & ~mask) | (value & mask)));
}

#define BACKLIGHT_BITS LEDC_TIMER_10_BIT
#define BACKLIGHT_MAX ((1u << 10) - 1u)

esp_err_t backlight_init(const backlight_t *b)
{
    ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = BACKLIGHT_BITS,
        .timer_num = b->timer,
        .freq_hz = b->hz,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    ESP_RETURN_ON_ERROR(ledc_timer_config(&timer), TAG, "backlight timer");
    ledc_channel_config_t channel = {
        .gpio_num = b->pin,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = b->channel,
        .timer_sel = b->timer,
        .duty = 0,
        .hpoint = 0,
        .flags.output_invert = b->active_high ? 0 : 1,
    };
    return ledc_channel_config(&channel);
}

void backlight_set(const backlight_t *b, uint8_t percent)
{
    if (percent > 100) percent = 100;
    uint32_t duty = BACKLIGHT_MAX * percent / 100u;
    ledc_set_duty(LEDC_LOW_SPEED_MODE, b->channel, duty);
    ledc_update_duty(LEDC_LOW_SPEED_MODE, b->channel);
}

esp_err_t lcd_send(esp_lcd_panel_io_handle_t io, const lcd_command_t *commands, size_t count)
{
    for (size_t i = 0; i < count; i++) {
        ESP_RETURN_ON_ERROR(esp_lcd_panel_io_tx_param(io, commands[i].command,
                                                      commands[i].length ? commands[i].data : NULL, commands[i].length),
                            TAG, "command 0x%02x", commands[i].command);
        if (commands[i].delay_ms) vTaskDelay(pdMS_TO_TICKS(commands[i].delay_ms));
    }
    return ESP_OK;
}

/* Creates the panel on io and brings it up, display on. */
static esp_err_t panel_up(const lcd_panel_t *c, esp_lcd_panel_io_handle_t io, esp_lcd_panel_handle_t *panel)
{
    esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = c->reset,
        .rgb_ele_order = c->bgr ? LCD_RGB_ELEMENT_ORDER_BGR : LCD_RGB_ELEMENT_ORDER_RGB,
        .data_endian = LCD_RGB_DATA_ENDIAN_BIG,
        .bits_per_pixel = 16,
    };
    if (c->controller == LCD_ST7789) {
        ESP_RETURN_ON_ERROR(esp_lcd_new_panel_st7789(io, &panel_config, panel), TAG, "ST7789 panel");
    } else {
        ESP_RETURN_ON_ERROR(lcd_new_panel_ili9341(io, &panel_config, panel), TAG, "ILI9341 panel");
    }
    ESP_RETURN_ON_ERROR(esp_lcd_panel_reset(*panel), TAG, "LCD reset");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_init(*panel), TAG, "LCD init");
    ESP_RETURN_ON_ERROR(lcd_send(io, c->extra, c->extra_count), TAG, "LCD tuning");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_invert_color(*panel, c->invert), TAG, "LCD invert");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_set_gap(*panel, c->x_gap, c->y_gap), TAG, "LCD gap");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_swap_xy(*panel, c->swap_xy), TAG, "LCD swap");
    ESP_RETURN_ON_ERROR(esp_lcd_panel_mirror(*panel, c->mirror_x, c->mirror_y), TAG, "LCD mirror");
    return esp_lcd_panel_disp_on_off(*panel, true);
}

esp_err_t lcd_spi_init(const lcd_spi_bus_t *b, const lcd_panel_t *c, esp_lcd_panel_io_handle_t *io,
                       esp_lcd_panel_handle_t *panel)
{
    spi_bus_config_t bus = {
        .mosi_io_num = b->mosi,
        .miso_io_num = -1,
        .sclk_io_num = b->sclk,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = (int)b->max_transfer,
    };
    ESP_RETURN_ON_ERROR(spi_bus_initialize(b->host, &bus, SPI_DMA_CH_AUTO), TAG, "LCD SPI bus");
    esp_lcd_panel_io_spi_config_t io_config = {
        .cs_gpio_num = b->cs,
        .dc_gpio_num = b->dc,
        .spi_mode = 0,
        .pclk_hz = b->pclk_hz,
        .trans_queue_depth = 4,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)b->host, &io_config, io), TAG, "LCD IO");
    return panel_up(c, *io, panel);
}

esp_err_t lcd_i80_init(const lcd_i80_bus_t *b, const lcd_panel_t *c, esp_lcd_panel_io_handle_t *io,
                       esp_lcd_panel_handle_t *panel)
{
    esp_lcd_i80_bus_handle_t bus = NULL;
    esp_lcd_i80_bus_config_t bus_config = {
        .dc_gpio_num = b->dc,
        .wr_gpio_num = b->wr,
        .clk_src = LCD_CLK_SRC_DEFAULT,
        .bus_width = 8,
        .max_transfer_bytes = b->max_transfer,
        .dma_burst_size = 64,
    };
    for (int i = 0; i < 8; i++) bus_config.data_gpio_nums[i] = b->data[i];
    ESP_RETURN_ON_ERROR(esp_lcd_new_i80_bus(&bus_config, &bus), TAG, "LCD i80 bus");
    esp_lcd_panel_io_i80_config_t io_config = {
        .cs_gpio_num = b->cs,
        .pclk_hz = b->pclk_hz,
        .trans_queue_depth = 4,
        .lcd_cmd_bits = 8,
        .lcd_param_bits = 8,
        .dc_levels = {.dc_idle_level = 0, .dc_cmd_level = 0, .dc_dummy_level = 0, .dc_data_level = 1},
        .flags.swap_color_bytes = 1,
    };
    ESP_RETURN_ON_ERROR(esp_lcd_new_panel_io_i80(bus, &io_config, io), TAG, "LCD IO");
    return panel_up(c, *io, panel);
}

esp_err_t board_wake_low(uint64_t mask, bool pullup)
{
    for (int pin = 0; pin < 64; pin++) {
        if (!(mask & (1ULL << pin))) continue;
        ESP_RETURN_ON_FALSE(rtc_gpio_is_valid_gpio((gpio_num_t)pin), ESP_ERR_INVALID_ARG, TAG, "GPIO%d is not RTC",
                            pin);
        rtc_gpio_pulldown_dis((gpio_num_t)pin);
        if (pullup) {
            rtc_gpio_pullup_en((gpio_num_t)pin);
        } else {
            rtc_gpio_pullup_dis((gpio_num_t)pin);
        }
    }
    /* RTC IO pull-ups need the RTC peripherals powered in deep sleep. */
    if (pullup) ESP_RETURN_ON_ERROR(esp_sleep_pd_config(ESP_PD_DOMAIN_RTC_PERIPH, ESP_PD_OPTION_ON), TAG, "RTC domain");
    return esp_sleep_enable_ext1_wakeup_io(mask, ESP_EXT1_WAKEUP_ANY_LOW);
}
