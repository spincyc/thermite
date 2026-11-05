#include "console.h"

#include <stdio.h>
#include <string.h>

#include "driver/uart.h"
#include "esp_log.h"
#include "sdkconfig.h"

#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
#include "driver/usb_serial_jtag.h"
#include "driver/usb_serial_jtag_vfs.h"
#else
#include "driver/uart_vfs.h"
#endif

static const char *TAG = "console";

static QueueHandle_t s_lines;
static TaskHandle_t s_notify;
static uint32_t s_bit;

int console_uart_read(uart_port_t port, uint8_t *buffer, size_t size)
{
    int n = uart_read_bytes(port, buffer, 1, portMAX_DELAY);
    size_t held = 0;
    if (n == 1 && size > 1 && uart_get_buffered_data_len(port, &held) == ESP_OK && held) {
        int more = uart_read_bytes(port, buffer + 1, held < size - 1 ? held : size - 1, 0);
        if (more > 0) n += more;
    }
    return n;
}

static int read_some(uint8_t *buffer, size_t size)
{
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    return usb_serial_jtag_read_bytes(buffer, size, portMAX_DELAY);
#else
    return console_uart_read(CONFIG_ESP_CONSOLE_UART_NUM, buffer, size);
#endif
}

static void reader(void *arg)
{
    (void)arg;
    static cp_lines_t lines;
    static console_line_t line;
    uint8_t buffer[64];
    for (;;) {
        int n = read_some(buffer, sizeof buffer);
        for (int i = 0; i < n; i++) {
            cp_line_event_t event = cp_lines_push(&lines, (char)buffer[i]);
            if (event == CP_LINE_TOO_LONG) {
                console_put("thermite error line too long\n");
            } else if (event == CP_LINE_READY && strncmp(lines.data, "thermite ", 9) == 0) {
                memcpy(line.text, lines.data, lines.length + 1);
                if (xQueueSend(s_lines, &line, 0) == pdTRUE) {
                    xTaskNotify(s_notify, s_bit, eSetBits);
                } else {
                    console_put("thermite error busy\n");
                }
            }
        }
    }
}

void console_start(QueueHandle_t lines, TaskHandle_t notify, uint32_t bit)
{
    s_lines = lines;
    s_notify = notify;
    s_bit = bit;
    fflush(stdout);
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    usb_serial_jtag_driver_config_t config = {.rx_buffer_size = 512, .tx_buffer_size = 1024};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&config));
    usb_serial_jtag_vfs_use_driver();
#else
    ESP_ERROR_CHECK(uart_driver_install(CONFIG_ESP_CONSOLE_UART_NUM, 512, 1024, 0, NULL, 0));
    uart_vfs_dev_use_driver(CONFIG_ESP_CONSOLE_UART_NUM);
#endif
    xTaskCreate(reader, "console", 3072, NULL, 4, NULL);
    ESP_LOGI(TAG, "listening");
}

void console_put(const char *line)
{
    fputs(line, stdout);
    fflush(stdout);
}
