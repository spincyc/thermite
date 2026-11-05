/*
 * The handheld remote (handheld/PROTOCOL.md): a thin client that draws the
 * bridge's screen model and reports input over ESP-NOW.
 *
 * One task (app_main's) runs everything that touches LVGL or thermite_remote:
 * lv_timer_handler, tr_poll, received frames, console lines, keys and touch.
 * Other contexts only queue data and set notification bits on it: the Wi-Fi
 * task (ESP-NOW receive), the console reader and the key interrupt.
 *
 * Boot: power hold and peripherals (board_init), the radio, the remote core,
 * the key that woke us sent at once, then the display. Sleep: dim at 10 s,
 * then "sleep" to the bridge, a short wait for its ack, deep sleep.
 */
#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "board.h"
#include "console.h"
#include "display.h"
#include "esp_app_desc.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_random.h"
#include "esp_rtc_time.h"
#include "esp_sleep.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "keymap.h"
#include "nvs.h"
#include "nvs_flash.h"
#include "power_policy.h"
#include "sdkconfig.h"
#include "thermite_remote.h"
#include "thermite_ui.h"

static const char *TAG = "remote";

#define NVS_NAMESPACE "thermite"
#define NVS_KEY "key"
#define NVS_BASE "base"
#define NVS_CHANNEL "channel"

/* Notification bits for the app task. */
#define WAKE_FRAME (1u << 0)
#define WAKE_LINE (1u << 1)
#define WAKE_KEYS (1u << 2)

#define FRAME_QUEUE 8
#define LINE_QUEUE 4
#define BATTERY_MS 30000
#define BRIGHT 100
#define DIM 15
#define KEY_LIGHT 40
/* Longest the loop sleeps when nothing asks for sooner. */
#define LOOP_MAX_MS 500

typedef struct {
    uint16_t length;
    uint8_t data[TL_FRAME_MAX];
} frame_t;

typedef struct {
    bool valid;
    uint8_t key[TL_KEY_LEN];
    uint8_t base[ESP_NOW_ETH_ALEN];
    uint8_t channel;
} enrolment_t;

/* Survives deep sleep (not power loss): the remote's session, and the clock. */
static RTC_DATA_ATTR tr_persist_t s_persist;
static RTC_DATA_ATTR struct {
    uint32_t magic;
    uint64_t clock_ms; /* our clock when we went to sleep */
    uint64_t rtc_us;   /* the RTC timer then */
} s_slept;
#define SLEPT_MAGIC 0x74687263u

static TaskHandle_t s_app;
static QueueHandle_t s_frames, s_lines;
static enrolment_t s_enrolment;
static uint8_t s_mac[ESP_NOW_ETH_ALEN];
static tr_t *s_remote;
static bool s_ui;
static unsigned s_changed_early;
static uint64_t s_clock_base_ms;
static key_repeat_t s_repeat;
static power_policy_t s_power;

/* ---- clock ---- */

/* A millisecond clock that keeps counting across deep sleep. esp_timer restarts at every
 * boot, and the RTC timer's conversion to microseconds depends on the slow clock's
 * calibration at the time of reading; so only the sleep's own length is taken from the
 * RTC, and our clock resumes where it stopped plus that length. */
static void clock_start(void)
{
    s_clock_base_ms = 0;
    if (board_woke() && s_slept.magic == SLEPT_MAGIC) {
        uint64_t rtc = esp_rtc_get_time_us();
        uint64_t slept = rtc > s_slept.rtc_us ? rtc - s_slept.rtc_us : 0;
        s_clock_base_ms = s_slept.clock_ms + slept / 1000;
    }
    s_clock_base_ms -= (uint64_t)(esp_timer_get_time() / 1000);
    s_slept.magic = 0;
}

static uint64_t clock_ms(void)
{
    return s_clock_base_ms + (uint64_t)(esp_timer_get_time() / 1000);
}

static void clock_stop(void)
{
    s_slept.clock_ms = clock_ms();
    s_slept.rtc_us = esp_rtc_get_time_us();
    s_slept.magic = SLEPT_MAGIC;
}

/* ---- enrolment ---- */

static void enrolment_load(void)
{
    nvs_handle_t nvs;
    memset(&s_enrolment, 0, sizeof s_enrolment);
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &nvs) != ESP_OK) return;
    size_t key = sizeof s_enrolment.key, base = sizeof s_enrolment.base;
    s_enrolment.valid = nvs_get_blob(nvs, NVS_KEY, s_enrolment.key, &key) == ESP_OK && key == TL_KEY_LEN &&
                        nvs_get_blob(nvs, NVS_BASE, s_enrolment.base, &base) == ESP_OK && base == 6 &&
                        nvs_get_u8(nvs, NVS_CHANNEL, &s_enrolment.channel) == ESP_OK && s_enrolment.channel >= 1 &&
                        s_enrolment.channel <= 13;
    nvs_close(nvs);
}

static bool enrolment_store(const cp_command_t *c)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) != ESP_OK) return false;
    bool ok = nvs_set_blob(nvs, NVS_KEY, c->key, sizeof c->key) == ESP_OK &&
              nvs_set_blob(nvs, NVS_BASE, c->base, sizeof c->base) == ESP_OK &&
              nvs_set_u8(nvs, NVS_CHANNEL, c->channel) == ESP_OK && nvs_commit(nvs) == ESP_OK;
    nvs_close(nvs);
    return ok;
}

static bool enrolment_erase(void)
{
    nvs_handle_t nvs;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &nvs) != ESP_OK) return false;
    bool ok = nvs_erase_all(nvs) == ESP_OK && nvs_commit(nvs) == ESP_OK;
    nvs_close(nvs);
    return ok;
}

/* A new or no enrolment: restart clean, with no session from the old key. */
static void restart(void)
{
    memset(&s_persist, 0, sizeof s_persist);
    s_slept.magic = 0;
    vTaskDelay(pdMS_TO_TICKS(50)); /* let the answer leave */
    esp_restart();
}

static void handle_line(const char *text)
{
    static cp_command_t command;
    char out[160];
    cp_parse(text, strlen(text), &command);
    switch (command.kind) {
    case CP_IGNORE:
        return;
    case CP_ERROR:
        snprintf(out, sizeof out, "thermite error %s\n", command.error);
        console_put(out);
        return;
    case CP_HELLO:
        if (cp_format_remote(out, sizeof out, esp_app_get_description()->version, s_mac, board_info()->name,
                             s_enrolment.valid))
            console_put(out);
        return;
    case CP_ENROLL:
        if (!enrolment_store(&command)) {
            console_put("thermite error not stored\n");
            return;
        }
        memset(&command.key, 0, sizeof command.key);
        console_put("thermite enrolled\n");
        restart();
        return;
    case CP_FORGET:
        if (!enrolment_erase()) {
            console_put("thermite error not erased\n");
            return;
        }
        console_put("thermite forgotten\n");
        restart();
        return;
    }
}

static void drain_lines(void)
{
    static console_line_t line;
    while (xQueueReceive(s_lines, &line, 0) == pdTRUE) handle_line(line.text);
}

/* ---- radio ---- */

static void on_receive(const esp_now_recv_info_t *info, const uint8_t *data, int length)
{
    static frame_t frame; /* only the Wi-Fi task calls this */
    if (length <= 0 || length > TL_FRAME_MAX) return;
    if (memcmp(info->src_addr, s_enrolment.base, ESP_NOW_ETH_ALEN) != 0) return;
    frame.length = (uint16_t)length;
    memcpy(frame.data, data, (size_t)length);
    if (xQueueSend(s_frames, &frame, 0) == pdTRUE) xTaskNotify(s_app, WAKE_FRAME, eSetBits);
}

static void radio_start(uint8_t channel)
{
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
    memcpy(peer.peer_addr, s_enrolment.base, ESP_NOW_ETH_ALEN);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
}

/* ---- platform for thermite_remote ---- */

static void platform_send(void *context, const uint8_t *frame, size_t length)
{
    (void)context;
    esp_err_t err = esp_now_send(s_enrolment.base, frame, length);
    if (err != ESP_OK) ESP_LOGW(TAG, "send: %s", esp_err_to_name(err)); /* the core retries */
}

static void platform_channel(void *context, uint8_t channel)
{
    (void)context;
    esp_err_t err = esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
    if (err != ESP_OK) ESP_LOGW(TAG, "channel %u: %s", channel, esp_err_to_name(err));
}

static uint32_t platform_now(void *context)
{
    (void)context;
    return (uint32_t)clock_ms();
}

static uint64_t platform_random(void *context)
{
    (void)context;
    /* The hardware RNG is a true random source while the radio runs. */
    return (uint64_t)esp_random() << 32 | esp_random();
}

static void platform_changed(void *context, unsigned what)
{
    (void)context;
    if (s_ui) {
        tu_changed(what);
    } else {
        s_changed_early |= what; /* before the display is up */
    }
}

static const tr_platform_t PLATFORM = {
    .send = platform_send,
    .set_channel = platform_channel,
    .now_ms = platform_now,
    .random64 = platform_random,
    .changed = platform_changed,
};

/* ---- input ---- */

static void on_key_interrupt(void *context)
{
    (void)context;
    BaseType_t woken = pdFALSE;
    xTaskNotifyFromISR(s_app, WAKE_KEYS, eSetBits, &woken);
    portYIELD_FROM_ISR(woken);
}

static void report_battery(void)
{
    bool charging = false;
    int percent = board_battery(&charging);
    tr_status(s_remote, percent, charging);
    if (s_ui) tu_battery(percent, charging);
}

/* One press of key: its operation. A held key presses again while the screen's
 * spec for its button repeats, read as each repeat falls due: a key that woke the
 * remote, before any screen arrived, repeats once one has. */
static void press(board_key_t key, bool first)
{
    const char *const *ids;
    size_t count = keymap_ids(key, &ids);
    if (count == 0) return; /* no entry (Record), or a wake-only button */
    if (!first && !keymap_repeats(tr_screen(s_remote), ids[0])) {
        key_repeat_up(&s_repeat, key);
        return;
    }
    if (!tr_key(s_remote, ids, count)) ESP_LOGW(TAG, "key refused: operations pending");
    if (first) key_repeat_down(&s_repeat, key, true, (uint32_t)clock_ms());
}

static void input(void)
{
    if (power_input(&s_power, (uint32_t)clock_ms()) == POWER_BRIGHTEN) {
        board_backlight(BRIGHT);
        board_key_backlight(KEY_LIGHT);
    }
}

static uint32_t poll_keys(void)
{
    board_key_event_t events[16];
    size_t count = 0;
    uint32_t wait = board_keys_poll(events, sizeof events / sizeof events[0], &count);
    for (size_t i = 0; i < count; i++) {
        input();
        if (events[i].down) {
            press(events[i].key, true);
        } else {
            key_repeat_up(&s_repeat, events[i].key);
        }
    }
    return wait;
}

/* ---- sleep ---- */

static void sleep_now(void)
{
    ESP_LOGI(TAG, "deep sleep");
    board_sleep_prepare();
    esp_now_deinit();
    esp_wifi_stop();
    clock_stop();
    esp_deep_sleep_start();
}

/* ---- the loop ---- */

static uint32_t min_u32(uint32_t a, uint32_t b)
{
    return a < b ? a : b;
}

static void wait_for(uint32_t ms, uint32_t *bits)
{
    if (ms > LOOP_MAX_MS) ms = LOOP_MAX_MS;
    TickType_t ticks = pdMS_TO_TICKS(ms);
    if (ticks == 0 && ms > 0) ticks = 1;
    *bits = 0;
    xTaskNotifyWait(0, UINT32_MAX, bits, ticks);
}

/* No enrolment: the enrol screen and the console, nothing else. */
static void run_unenrolled(void)
{
    char mac[18];
    snprintf(mac, sizeof mac, "%02x:%02x:%02x:%02x:%02x:%02x", s_mac[0], s_mac[1], s_mac[2], s_mac[3], s_mac[4],
             s_mac[5]);
    display_start();
    display_enrol_screen(mac, board_info()->name, esp_app_get_description()->version);
    lv_timer_handler();
    board_backlight(BRIGHT);
    ESP_LOGW(TAG, "not enrolled: waiting on the console (thermite enroll ...)");
    for (;;) {
        uint32_t bits;
        drain_lines();
        wait_for(lv_timer_handler(), &bits);
    }
}

static void run(void)
{
    static frame_t frame;
    uint32_t key_due = 0, battery_due = (uint32_t)clock_ms() + BATTERY_MS, bits = WAKE_KEYS;
    for (;;) {
        uint32_t now = (uint32_t)clock_ms();
        while (xQueueReceive(s_frames, &frame, 0) == pdTRUE) tr_receive(s_remote, frame.data, frame.length);
        drain_lines();
        uint32_t key_wait = UINT32_MAX;
        if ((bits & WAKE_KEYS) || (int32_t)(now - key_due) >= 0) {
            key_wait = poll_keys();
            key_due = key_wait == UINT32_MAX ? now + 0x7fffffffu : now + key_wait;
        } else {
            key_wait = key_due - now;
        }
        for (board_key_t key; (key = key_repeat_due(&s_repeat, now)) != BOARD_KEY_NONE;) press(key, false);
        uint32_t wait = tr_poll(s_remote);
        wait = min_u32(wait, lv_timer_handler());
        if (display_touched()) input();
        if ((int32_t)(now - battery_due) >= 0) {
            report_battery();
            battery_due = now + BATTERY_MS;
        }
        switch (power_step(&s_power, now, tr_idle(s_remote))) {
        case POWER_DIM:
            board_backlight(DIM);
            board_key_backlight(0);
            break;
        case POWER_BRIGHTEN:
            board_backlight(BRIGHT);
            break;
        case POWER_SEND_SLEEP:
            key_repeat_up(&s_repeat, s_repeat.key);
            tr_sleep(s_remote);
            break;
        case POWER_SLEEP_NOW:
            sleep_now();
            break;
        case POWER_NONE:
            break;
        }
        wait = min_u32(wait, key_wait);
        wait = min_u32(wait, key_repeat_wait(&s_repeat, now));
        wait = min_u32(wait, power_wait(&s_power, now));
        wait = min_u32(wait, battery_due - now);
        wait_for(wait, &bits);
    }
}

void app_main(void)
{
    s_app = xTaskGetCurrentTaskHandle();
    board_set_notify(on_key_interrupt, NULL);
    ESP_ERROR_CHECK(board_init());
    clock_start();
    const board_info_t *info = board_info();
#if CONFIG_ESP_CONSOLE_USB_SERIAL_JTAG
    if (info->console != BOARD_CONSOLE_USB_SERIAL_JTAG) ESP_LOGW(TAG, "console: build is USB, board says UART0");
#else
    if (info->console != BOARD_CONSOLE_UART0) ESP_LOGW(TAG, "console: build is UART, board says USB");
#endif

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    ESP_ERROR_CHECK(esp_read_mac(s_mac, ESP_MAC_WIFI_STA));
    enrolment_load();

    s_frames = xQueueCreate(FRAME_QUEUE, sizeof(frame_t));
    s_lines = xQueueCreate(LINE_QUEUE, sizeof(console_line_t));
    configASSERT(s_frames && s_lines);
    console_start(s_lines, s_app, WAKE_LINE);
    if (!s_enrolment.valid) run_unenrolled();

    /* The radio and the remote core before the display: the key that woke us goes first. */
    uint8_t channel = s_persist.channel >= 1 && s_persist.channel <= 13 ? s_persist.channel : s_enrolment.channel;
    radio_start(channel);
    const tr_identity_t identity = {
        .board = info->name,
        .fw = esp_app_get_description()->version,
        .width = info->width,
        .height = info->height,
        .channel = s_enrolment.channel,
    };
    s_remote = tr_create(&s_persist, s_enrolment.key, &identity, &PLATFORM);
    configASSERT(s_remote);
    power_init(&s_power, (uint32_t)clock_ms());
    bool charging = false;
    tr_status(s_remote, board_battery(&charging), charging);
    board_key_t woke_by = board_wake_key();
    if (woke_by != BOARD_KEY_NONE) {
        ESP_LOGI(TAG, "woken by key %d", woke_by);
        press(woke_by, true);
    }
    tr_wake(s_remote);

    lv_display_t *display = display_start();
    const tu_keys_t keys = {
        .dpad = info->dpad, .volume = info->volume, .channel = info->channel, .playback = info->playback};
    tu_create(display, s_remote, &keys);
    s_ui = true;
    tu_changed(s_changed_early | TR_CHANGED_SCREEN | TR_CHANGED_AUDIO | TR_CHANGED_LINK);
    report_battery();
    lv_timer_handler(); /* the first frame before the light comes on */
    board_backlight(BRIGHT);
    board_key_backlight(KEY_LIGHT);
    run();
}
