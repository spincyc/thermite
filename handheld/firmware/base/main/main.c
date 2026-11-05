/*
 * The base station (handheld/PROTOCOL.md, "Base station lines"): a keyless
 * ESP-NOW relay on its native USB port. Protocol lines go only to the
 * USB-Serial-JTAG port; ESP_LOG goes to UART0 (sdkconfig.defaults), so the
 * protocol port carries nothing else after the ROM's boot banner.
 *
 * Tasks: the reader parses host lines; the sender owns the radio's transmit
 * side and works one queued command at a time (sends and channel changes, in
 * the order they arrived); the relay prints received frames. Every line goes
 * out whole under one mutex.
 */
#include <string.h>

#include "base_proto.h"
#include "driver/usb_serial_jtag.h"
#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "base";

#define NVS_NAMESPACE "thermite"
#define NVS_CHANNEL "channel"
#define SEND_QUEUE 8
#define RX_QUEUE 16
/* ESP-NOW reports every send through its callback; this only guards a lost one. */
#define SEND_TIMEOUT_MS 1000
#define USB_WRITE_MS 100

typedef struct {
    uint8_t mac[ESP_NOW_ETH_ALEN];
    int8_t rssi;
    uint16_t length;
    uint8_t frame[BP_FRAME_MAX];
} rx_frame_t;

static SemaphoreHandle_t s_out;
static QueueHandle_t s_commands, s_received;
static TaskHandle_t s_sender;
static uint8_t s_mac[ESP_NOW_ETH_ALEN];
static uint8_t s_channel = 1;
static bp_peers_t s_peers;
static uint32_t s_dropped;

/* One whole line to the host, or nothing when no host is listening. */
static void put_line(const char *line, size_t length)
{
    if (length == 0) return;
    xSemaphoreTake(s_out, portMAX_DELAY);
    if (usb_serial_jtag_is_connected()) {
        size_t done = 0;
        while (done < length) {
            int n = usb_serial_jtag_write_bytes(line + done, length - done, pdMS_TO_TICKS(USB_WRITE_MS));
            if (n <= 0) break; /* the host stopped reading: the line is lost, as on a closed port */
            done += (size_t)n;
        }
    }
    xSemaphoreGive(s_out);
}

static void put_base(void)
{
    char line[96];
    put_line(line, bp_format_base(line, sizeof line, esp_app_get_description()->version, s_mac, s_channel));
}

static void put_error(const char *words)
{
    char line[128];
    put_line(line, bp_format_error(line, sizeof line, words));
}

static uint8_t load_channel(void)
{
    nvs_handle_t nvs;
    uint8_t channel = 1;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) == ESP_OK) {
        if (nvs_get_u8(nvs, NVS_CHANNEL, &channel) != ESP_OK || channel < BP_CHANNEL_MIN || channel > BP_CHANNEL_MAX)
            channel = 1;
        nvs_close(nvs);
    }
    return channel;
}

static bool store_channel(uint8_t channel)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) != ESP_OK) return false;
    bool ok = nvs_set_u8(nvs, NVS_CHANNEL, channel) == ESP_OK && nvs_commit(nvs) == ESP_OK;
    nvs_close(nvs);
    return ok;
}

static void on_receive(const esp_now_recv_info_t *info, const uint8_t *data, int length)
{
    /* Wi-Fi task context: copy and hand over, never block. */
    static rx_frame_t frame; /* only the Wi-Fi task calls this */
    if (length <= 0 || length > BP_FRAME_MAX) return;
    memcpy(frame.mac, info->src_addr, ESP_NOW_ETH_ALEN);
    frame.rssi = (int8_t)info->rx_ctrl->rssi;
    frame.length = (uint16_t)length;
    memcpy(frame.frame, data, (size_t)length);
    if (xQueueSend(s_received, &frame, 0) != pdTRUE) s_dropped++;
}

static void on_sent(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    (void)info;
    xTaskNotify(s_sender, status == ESP_NOW_SEND_SUCCESS ? 1 : 2, eSetValueWithOverwrite);
}

/* Makes mac an ESP-NOW peer on the current channel, evicting the least recently used. */
static bool ensure_peer(const uint8_t mac[ESP_NOW_ETH_ALEN])
{
    uint8_t evicted[ESP_NOW_ETH_ALEN];
    bp_peer_action_t action = bp_peers_use(&s_peers, mac, evicted);
    if (action == BP_PEER_KNOWN) return true;
    if (action == BP_PEER_REPLACE) esp_now_del_peer(evicted);
    esp_now_peer_info_t peer = {.channel = 0, .ifidx = WIFI_IF_STA, .encrypt = false};
    memcpy(peer.peer_addr, mac, ESP_NOW_ETH_ALEN);
    esp_err_t err = esp_now_add_peer(&peer);
    if (err == ESP_ERR_ESPNOW_EXIST) return true;
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "add peer: %s", esp_err_to_name(err));
        bp_peers_forget(&s_peers, mac);
        return false;
    }
    return true;
}

static bool send_frame(const bp_command_t *command)
{
    if (!ensure_peer(command->mac)) return false;
    xTaskNotifyStateClear(NULL);
    ulTaskNotifyValueClear(NULL, UINT32_MAX);
    esp_err_t err = esp_now_send(command->mac, command->frame, command->length);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "send: %s", esp_err_to_name(err));
        return false;
    }
    uint32_t result = 0;
    if (xTaskNotifyWait(0, UINT32_MAX, &result, pdMS_TO_TICKS(SEND_TIMEOUT_MS)) != pdTRUE) {
        ESP_LOGW(TAG, "send callback timed out");
        return false;
    }
    return result == 1;
}

static void sender_task(void *arg)
{
    (void)arg;
    static bp_command_t command;
    for (;;) {
        xQueueReceive(s_commands, &command, portMAX_DELAY);
        if (command.kind == BP_SEND) {
            char line[40];
            put_line(line, bp_format_sent(line, sizeof line, command.id, send_frame(&command)));
        } else if (command.kind == BP_CHANNEL) {
            esp_err_t err = esp_wifi_set_channel(command.channel, WIFI_SECOND_CHAN_NONE);
            if (err != ESP_OK) {
                ESP_LOGW(TAG, "set channel: %s", esp_err_to_name(err));
                put_error("channel not set");
                continue;
            }
            s_channel = command.channel;
            if (!store_channel(s_channel)) ESP_LOGW(TAG, "channel not stored");
            put_base();
        }
    }
}

static void relay_task(void *arg)
{
    (void)arg;
    static rx_frame_t frame;
    static char line[BP_LINE_MAX + 2];
    uint32_t reported = 0;
    for (;;) {
        xQueueReceive(s_received, &frame, portMAX_DELAY);
        put_line(line, bp_format_rx(line, sizeof line, frame.mac, frame.rssi, frame.frame, frame.length));
        if (s_dropped != reported) {
            reported = s_dropped;
            ESP_LOGW(TAG, "%lu received frames dropped (queue full)", (unsigned long)reported);
        }
    }
}

static void reader_task(void *arg)
{
    (void)arg;
    static bp_lines_t lines;
    static bp_command_t command;
    uint8_t buffer[256];
    for (;;) {
        int n = usb_serial_jtag_read_bytes(buffer, sizeof buffer, portMAX_DELAY);
        for (int i = 0; i < n; i++) {
            bp_line_event_t event = bp_lines_push(&lines, (char)buffer[i]);
            if (event == BP_LINE_TOO_LONG) {
                put_error("line too long");
                continue;
            }
            if (event != BP_LINE_READY) continue;
            bp_parse(lines.data, lines.length, &command);
            switch (command.kind) {
            case BP_IGNORE:
                break;
            case BP_ERROR:
                put_error(command.error);
                break;
            case BP_HELLO:
                put_base();
                break;
            case BP_CHANNEL:
            case BP_SEND:
                /* Blocks while the sender is behind: the host's own buffering holds the rest. */
                xQueueSend(s_commands, &command, portMAX_DELAY);
                break;
            }
        }
    }
}

static void radio_start(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&config));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start());
    /* Never associates; listens all the time. */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(s_channel, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_now_init());
    uint32_t version = 0;
    esp_now_get_version(&version);
    ESP_LOGI(TAG, "ESP-NOW version %lu", (unsigned long)version);
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_receive));
    ESP_ERROR_CHECK(esp_now_register_send_cb(on_sent));
}

void app_main(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    s_channel = load_channel();
    bp_peers_init(&s_peers, ESP_NOW_MAX_TOTAL_PEER_NUM);

    s_out = xSemaphoreCreateMutex();
    s_commands = xQueueCreate(SEND_QUEUE, sizeof(bp_command_t));
    s_received = xQueueCreate(RX_QUEUE, sizeof(rx_frame_t));
    configASSERT(s_out && s_commands && s_received);

    usb_serial_jtag_driver_config_t usb = {.rx_buffer_size = 4096, .tx_buffer_size = 4096};
    ESP_ERROR_CHECK(usb_serial_jtag_driver_install(&usb));

    ESP_ERROR_CHECK(esp_read_mac(s_mac, ESP_MAC_WIFI_STA));
    xTaskCreate(sender_task, "sender", 4096, NULL, 6, &s_sender);
    radio_start();
    xTaskCreate(relay_task, "relay", 4096, NULL, 5, NULL);
    xTaskCreate(reader_task, "reader", 4096, NULL, 5, NULL);
    ESP_LOGI(TAG, "channel %u", (unsigned)s_channel);
    put_base();
}
