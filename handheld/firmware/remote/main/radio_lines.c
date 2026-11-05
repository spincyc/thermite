/*
 * radio.h for the qemu board: the base station's lines (base_proto.h) on
 * UART1, which QEMU connects to thermite handheld serve --listen. This
 * firmware is the base and the remote at once: it announces a base whose MAC
 * is its own made locally administered, answers each "send" addressed to
 * this remote with "sent ... ok", and sends its frames as "rx" lines.
 */
#include "sdkconfig.h"

#if CONFIG_THERMITE_RADIO_LINES

#include <string.h>

#include "base_proto.h"
#include "console.h"
#include "driver/uart.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "radio.h"

static const char *TAG = "radio";

#define UART UART_NUM_1
#define RSSI (-40)

static uint8_t s_mac[RADIO_MAC_LEN], s_base[RADIO_MAC_LEN];
static uint8_t s_channel = 1;
static QueueHandle_t s_frames;
static TaskHandle_t s_notify;
static uint32_t s_bit;

static void put(const char *line, size_t length)
{
    if (length) uart_write_bytes(UART, line, length);
}

static void announce(void)
{
    char line[96];
    put(line, bp_format_base(line, sizeof line, esp_app_get_description()->version, s_base, s_channel));
}

static void handle(const bp_command_t *command)
{
    static radio_frame_t frame;
    char line[64];
    switch (command->kind) {
    case BP_IGNORE:
        return;
    case BP_ERROR:
        put(line, bp_format_error(line, sizeof line, command->error));
        return;
    case BP_HELLO:
        announce();
        return;
    case BP_CHANNEL:
        s_channel = command->channel;
        announce();
        return;
    case BP_SEND: {
        /* Delivered when it is for this remote and the remote has started listening. */
        QueueHandle_t frames = __atomic_load_n(&s_frames, __ATOMIC_ACQUIRE);
        bool ours = frames && memcmp(command->mac, s_mac, RADIO_MAC_LEN) == 0;
        if (ours) {
            frame.length = (uint16_t)command->length;
            memcpy(frame.data, command->frame, command->length);
            ours = xQueueSend(frames, &frame, 0) == pdTRUE;
            if (ours) xTaskNotify(s_notify, s_bit, eSetBits);
        }
        put(line, bp_format_sent(line, sizeof line, command->id, ours));
        return;
    }
    }
}

static void reader(void *argument)
{
    (void)argument;
    static bp_lines_t lines;
    static bp_command_t command;
    uint8_t buffer[128];
    for (;;) {
        int n = console_uart_read(UART, buffer, sizeof buffer);
        for (int i = 0; i < n; i++) {
            bp_line_event_t event = bp_lines_push(&lines, (char)buffer[i]);
            if (event == BP_LINE_TOO_LONG) {
                char line[64];
                put(line, bp_format_error(line, sizeof line, "line too long"));
            } else if (event == BP_LINE_READY) {
                bp_parse(lines.data, lines.length, &command);
                handle(&command);
            }
        }
    }
}

void radio_init(const uint8_t mac[RADIO_MAC_LEN])
{
    memcpy(s_mac, mac, RADIO_MAC_LEN);
    memcpy(s_base, mac, RADIO_MAC_LEN);
    s_base[0] |= 0x02; /* locally administered */
    s_base[5] ^= 0xff; /* and not the remote's own */
    ESP_ERROR_CHECK(uart_driver_install(UART, 2 * BP_LINE_MAX, 2 * BP_LINE_MAX, 0, NULL, 0));
    xTaskCreate(reader, "radio", 4096, NULL, 5, NULL);
    announce();
    ESP_LOGI(TAG, "base lines on UART1");
}

void radio_start(const uint8_t base[RADIO_MAC_LEN], uint8_t channel, QueueHandle_t frames, TaskHandle_t notify,
                 uint32_t bit)
{
    (void)base;
    (void)channel;
    s_notify = notify;
    s_bit = bit;
    __atomic_store_n(&s_frames, frames, __ATOMIC_RELEASE); /* the reader delivers once this is set */
}

void radio_send(const uint8_t *frame, size_t length)
{
    static char line[BP_LINE_MAX + 2];
    put(line, bp_format_rx(line, sizeof line, s_mac, RSSI, frame, length));
}

void radio_set_channel(uint8_t channel)
{
    (void)channel; /* one wire, no channels */
}

void radio_stop(void)
{
}

#endif
