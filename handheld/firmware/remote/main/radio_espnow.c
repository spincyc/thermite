/* radio.h over ESP-NOW: Wi-Fi started but never associated, the base the only peer. */
#include "sdkconfig.h"

#if !CONFIG_THERMITE_RADIO_LINES

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_wifi.h"
#include "radio.h"

static const char *TAG = "radio";

static uint8_t s_base[RADIO_MAC_LEN];
static QueueHandle_t s_frames;
static TaskHandle_t s_notify;
static uint32_t s_bit;

static void on_receive(const esp_now_recv_info_t *info, const uint8_t *data, int length)
{
    static radio_frame_t frame; /* only the Wi-Fi task calls this */
    if (length <= 0 || length > TL_FRAME_MAX) return;
    if (memcmp(info->src_addr, s_base, RADIO_MAC_LEN) != 0) return;
    frame.length = (uint16_t)length;
    memcpy(frame.data, data, (size_t)length);
    if (xQueueSend(s_frames, &frame, 0) == pdTRUE) xTaskNotify(s_notify, s_bit, eSetBits);
}

void radio_init(const uint8_t mac[RADIO_MAC_LEN])
{
    (void)mac;
}

void radio_start(const uint8_t base[RADIO_MAC_LEN], uint8_t channel, QueueHandle_t frames, TaskHandle_t notify,
                 uint32_t bit)
{
    memcpy(s_base, base, RADIO_MAC_LEN);
    s_frames = frames;
    s_notify = notify;
    s_bit = bit;
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&config));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_start()); /* never associates */
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE));
    ESP_ERROR_CHECK(esp_now_init());
    ESP_ERROR_CHECK(esp_now_register_recv_cb(on_receive));
    /* Frames are sealed end to end (AES-256-CCM), so ESP-NOW's own encryption is off. */
    esp_now_peer_info_t peer = {.channel = 0, .ifidx = WIFI_IF_STA, .encrypt = false};
    memcpy(peer.peer_addr, s_base, RADIO_MAC_LEN);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
}

void radio_send(const uint8_t *frame, size_t length)
{
    esp_err_t err = esp_now_send(s_base, frame, length);
    if (err != ESP_OK) ESP_LOGW(TAG, "send: %s", esp_err_to_name(err));
}

void radio_set_channel(uint8_t channel)
{
    esp_err_t err = esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) ESP_LOGW(TAG, "channel %u: %s", channel, esp_err_to_name(err));
}

void radio_stop(void)
{
    esp_now_deinit();
    esp_wifi_stop();
}

#endif
