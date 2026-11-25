/* Host UI tests for thermite_ui: waiting_link updates, unconfirmed bubbles,
 * disabled repeating keys, and safe input-list closing. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "lvgl.h"
#include "thermite_link.h"
#include "thermite_remote.h"
#include "thermite_ui.h"

static int failures;

#define CHECK(condition, ...)                                              \
    do {                                                                   \
        if (!(condition)) {                                                \
            failures++;                                                    \
            fprintf(stderr, "%s:%d: ", __FILE__, __LINE__);                \
            fprintf(stderr, __VA_ARGS__);                                  \
            fputc('\n', stderr);                                           \
        }                                                                  \
    } while (0)

static const uint8_t KEY[TL_KEY_LEN] = {
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
    0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f,
};

static uint32_t g_clock;
static uint64_t g_seed = 1;
static int g_sent;
static uint8_t g_last_frame[TL_FRAME_MAX];
static size_t g_last_len;

static void fake_send(void *ctx, const uint8_t *frame, size_t len)
{
    (void)ctx;
    g_sent++;
    if (len <= sizeof g_last_frame) {
        memcpy(g_last_frame, frame, len);
        g_last_len = len;
    }
}

static void fake_channel(void *ctx, uint8_t ch) { (void)ctx; (void)ch; }
static uint32_t fake_now(void *ctx) { (void)ctx; return g_clock; }
static uint64_t fake_rand(void *ctx) { (void)ctx; return 0x9e3779b97f4a7c15ull * ++g_seed + 1; }
static void fake_changed(void *ctx, unsigned what) { (void)ctx; tu_changed(what); }

static uint32_t lv_clock(void) { return g_clock; }
static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px)
{
    (void)area;
    (void)px;
    lv_display_flush_ready(disp);
}

static bool tree_has_label(lv_obj_t *obj, const char *needle)
{
    if (!obj || lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) return false;
    if (lv_obj_check_type(obj, &lv_label_class)) {
        const char *text = lv_label_get_text(obj);
        if (text && strstr(text, needle)) return true;
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(obj); i++) {
        if (tree_has_label(lv_obj_get_child(obj, (int32_t)i), needle)) return true;
    }
    return false;
}

static bool tree_has_bg_color(lv_obj_t *obj, uint32_t hex)
{
    if (!obj || lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) return false;
    if (lv_color_eq(lv_obj_get_style_bg_color(obj, LV_PART_MAIN), lv_color_hex(hex))) return true;
    for (uint32_t i = 0; i < lv_obj_get_child_count(obj); i++) {
        if (tree_has_bg_color(lv_obj_get_child(obj, (int32_t)i), hex)) return true;
    }
    return false;
}

static lv_obj_t *find_disabled_button(lv_obj_t *obj)
{
    if (!obj || lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) return NULL;
    if (lv_obj_check_type(obj, &lv_button_class) && lv_obj_has_state(obj, LV_STATE_DISABLED)) return obj;
    for (uint32_t i = 0; i < lv_obj_get_child_count(obj); i++) {
        lv_obj_t *found = find_disabled_button(lv_obj_get_child(obj, (int32_t)i));
        if (found) return found;
    }
    return NULL;
}

static lv_obj_t *find_slider(lv_obj_t *obj)
{
    if (!obj || lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) return NULL;
    if (lv_obj_check_type(obj, &lv_slider_class)) return obj;
    for (uint32_t i = 0; i < lv_obj_get_child_count(obj); i++) {
        lv_obj_t *found = find_slider(lv_obj_get_child(obj, (int32_t)i));
        if (found) return found;
    }
    return NULL;
}

static lv_obj_t *find_button_with_label(lv_obj_t *obj, const char *needle)
{
    if (!obj || lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) return NULL;
    if (lv_obj_check_type(obj, &lv_button_class) && tree_has_label(obj, needle)) return obj;
    for (uint32_t i = 0; i < lv_obj_get_child_count(obj); i++) {
        lv_obj_t *found = find_button_with_label(lv_obj_get_child(obj, (int32_t)i), needle);
        if (found) return found;
    }
    return NULL;
}

static cJSON *decode_last_data(void)
{
    tl_header_t hdr;
    uint8_t plain[TL_FRAME_MAX];
    int n = tl_open(KEY, TL_FROM_REMOTE, g_last_frame, g_last_len, &hdr, plain, sizeof plain);
    if (n <= 0 || hdr.type != TL_DATA) return NULL;
    plain[n] = '\0';
    return cJSON_Parse((const char *)plain);
}

static void answer_hello(tr_t *remote)
{
    tl_header_t hdr;
    uint8_t plain[TL_FRAME_MAX];
    int n = tl_open(KEY, TL_FROM_REMOTE, g_last_frame, g_last_len, &hdr, plain, sizeof plain);
    CHECK(n > 0 && hdr.type == TL_HELLO, "expected HELLO frame");
    if (n <= 0 || hdr.type != TL_HELLO) return;
    char hex[17], sync_json[64];
    tl_hex(hdr.session, hex);
    snprintf(sync_json, sizeof sync_json, "{\"hello\":\"%s\"}", hex);
    tl_header_t sh = { .type = TL_SYNC, .count = 1, .session = 0x5eed123456789abfull, .counter = 0 };
    uint8_t frame[TL_FRAME_MAX];
    size_t len = tl_seal(KEY, TL_FROM_BRIDGE, &sh, (const uint8_t *)sync_json, strlen(sync_json), frame, sizeof frame);
    tr_receive(remote, frame, len);
}

int main(void)
{
    lv_init();
    lv_tick_set_cb(lv_clock);
    lv_display_t *disp = lv_display_create(240, 320);
    static uint16_t fb[240 * 320];
    lv_display_set_color_format(disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(disp, fb, NULL, sizeof fb, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_flush_cb(disp, flush_cb);

    tr_persist_t persist;
    memset(&persist, 0, sizeof persist);
    const tr_identity_t id = { "test", "0.1.0", 240, 320, 1 };
    const tr_platform_t plat = { NULL, fake_send, fake_channel, fake_now, fake_rand, fake_changed };
    tr_t *remote = tr_create(&persist, KEY, &id, &plat);
    tu_keys_t keys = { 0 };
    tu_create(disp, remote, &keys);

    /* 1. waiting_link updates live across OFFLINE -> CONNECTING -> CONNECTED. */
    lv_obj_t *scr = lv_display_get_screen_active(disp);
    lv_timer_handler();
    CHECK(tree_has_label(scr, "Not connected"), "waiting screen missing OFFLINE text");

    tr_poll(remote);
    lv_timer_handler();
    CHECK(tree_has_label(scr, "Connecting"), "waiting screen did not update to CONNECTING");

    answer_hello(remote);
    lv_timer_handler();
    CHECK(tree_has_label(scr, "Connected"), "waiting screen did not update to CONNECTED");

    /* 2. "unconfirmed" bubble state renders with C_BAD (0xd98b86). */
    const char *unconfirmed_msg =
        "{\"screen\":{\"id\":\"u1\",\"room\":\"west\",\"label\":\"West\","
        "\"rooms\":[{\"id\":\"west\",\"label\":\"West\",\"led\":\"ok\",\"short\":\"Player\"}],"
        "\"led\":\"ok\",\"short\":\"Player\",\"problem\":\"Computer was not confirmed\","
        "\"notice\":\"\",\"locked\":false,"
        "\"bubbles\":["
        "{\"id\":\"off\",\"label\":\"Off\",\"state\":\"idle\",\"lock\":false,\"inert\":false},"
        "{\"id\":\"computer\",\"label\":\"Computer\",\"state\":\"unconfirmed\",\"lock\":false,\"inert\":false}],"
        "\"selected\":\"off\",\"hidden\":true,\"remote\":null,\"pad\":null,\"rows\":[],\"beside\":[],"
        "\"channel\":false,"
        "\"buttons\":{\"volume_up\":{\"label\":\"+\",\"repeat\":true},"
        "\"volume_down\":{\"label\":\"-\",\"repeat\":true},\"mute\":{\"label\":\"M\"}},"
        "\"allOff\":false},\"audio\":null}";
    tr_apply(remote, unconfirmed_msg);
    lv_timer_handler();
    CHECK(tree_has_bg_color(scr, 0xd98b86), "unconfirmed bubble did not render C_BAD dot");

    /* 3. Locked/disabled repeating key ignores LV_EVENT_PRESSED (no repeat spam). */
    const char *locked_msg =
        "{\"screen\":{\"id\":\"l1\",\"room\":\"west\",\"label\":\"West\","
        "\"rooms\":[{\"id\":\"west\",\"label\":\"West\",\"led\":\"ok\",\"short\":\"Player\"}],"
        "\"led\":\"ok\",\"short\":\"Player\",\"problem\":\"\",\"notice\":\"Locked\",\"locked\":true,"
        "\"bubbles\":[{\"id\":\"player\",\"label\":\"Player\",\"state\":\"current\",\"lock\":true,\"inert\":false}],"
        "\"selected\":\"player\",\"hidden\":false,\"remote\":{\"label\":\"Remote\",\"note\":\"\"},"
        "\"pad\":null,\"rows\":[{\"label\":\"\",\"keys\":[\"up\"],\"columns\":1}],\"beside\":[],\"channel\":false,"
        "\"buttons\":{\"up\":{\"label\":\"Up\",\"repeat\":true},"
        "\"volume_up\":{\"label\":\"+\",\"repeat\":true},"
        "\"volume_down\":{\"label\":\"-\",\"repeat\":true},\"mute\":{\"label\":\"M\"}},"
        "\"allOff\":false},\"audio\":null}";
    tr_apply(remote, locked_msg);
    lv_timer_handler();
    lv_obj_t *disabled_btn = find_disabled_button(scr);
    CHECK(disabled_btn != NULL, "locked screen has no disabled button");
    if (disabled_btn) {
        int before = g_sent;
        lv_obj_send_event(disabled_btn, LV_EVENT_PRESSED, NULL);
        g_clock += 1000;
        lv_timer_handler();
        CHECK(g_sent == before, "disabled repeating key sent %d frame(s)", g_sent - before);
    }

    /* 4. Inputs overlay closes safely on deferred timer without dangling input_ids. */
    tr_apply(remote, "{\"inputs\":{\"title\":\"TV input\",\"options\":[{\"value\":\"hdmi1\",\"label\":\"HDMI 1\",\"inUse\":false}]}}");
    lv_timer_handler();
    lv_obj_t *top = lv_display_get_layer_top(disp);
    CHECK(tree_has_label(top, "HDMI 1"), "inputs dialog did not open");
    /* Find the first button on top layer and click it twice before update_cb runs. */
    lv_obj_t *shade = lv_obj_get_child(top, 0);
    lv_obj_t *panel = shade ? lv_obj_get_child(shade, 0) : NULL;
    lv_obj_t *list = panel ? lv_obj_get_child(panel, 1) : NULL;
    lv_obj_t *item = list ? lv_obj_get_child(list, 0) : NULL;
    CHECK(item != NULL, "inputs option item not found");
    if (item) {
        int before = g_sent;
        lv_obj_send_event(item, LV_EVENT_CLICKED, NULL);
        lv_obj_send_event(item, LV_EVENT_CLICKED, NULL);
        CHECK(g_sent == before + 1, "double click before delete sent %d ops", g_sent - before);
        lv_timer_handler();
        CHECK(lv_obj_get_child_count(top) == 0, "inputs overlay not cleaned up by update timer");
    }

    /* Switching LED uses stepped 2 Hz timer (no lv_anim) and allOff state stays idempotent. */
    tr_apply(remote,
             "{\"screen\":{\"id\":\"s_busy\",\"room\":\"den\",\"label\":\"Den\",\"led\":\"busy\",\"short\":\"Switching\","
             "\"selected\":\"pc\",\"hidden\":false,\"allOff\":false,\"locked\":false,"
             "\"rooms\":[{\"id\":\"den\",\"label\":\"Den\"}],"
             "\"bubbles\":[{\"id\":\"pc\",\"label\":\"PC\",\"state\":\"switching\",\"lock\":false,\"inert\":false}],"
             "\"remote\":{\"label\":\"PC\",\"note\":\"Use PC\"},\"pad\":null,\"rows\":[],\"beside\":[],\"channel\":false,"
             "\"buttons\":{}}}");
    lv_timer_handler();
    CHECK(lv_anim_count_running() == 0, "switching blink should use stepped timer, not lv_anim");

    /* 5. Listen mode renders Now Playing (normalized), speaker bubbles, mode toggle, and touch volume slider. */
    const char *listen_msg =
        "{\"screen\":{\"id\":\"m1\",\"room\":\"kitchen\",\"label\":\"Kitchen\","
        "\"rooms\":[{\"id\":\"west\",\"label\":\"West\",\"led\":\"ok\",\"short\":\"Player\",\"mode\":\"watch\",\"locked\":true},"
        "{\"id\":\"kitchen\",\"label\":\"Kitchen\",\"led\":\"ok\",\"short\":\"Playing\",\"mode\":\"listen\"}],"
        "\"led\":\"ok\",\"short\":\"Playing\",\"problem\":\"\",\"notice\":\"\",\"locked\":false,"
        "\"bubbles\":["
        "{\"id\":\"kitchen_amp\",\"label\":\"Sonos Amp\",\"state\":\"current\",\"lock\":false,\"inert\":false},"
        "{\"id\":\"kitchen_mini\",\"label\":\"WiiM Mini\",\"state\":\"idle\",\"lock\":false,\"inert\":false}],"
        "\"selected\":\"kitchen_amp\",\"hidden\":false,\"remote\":{\"label\":\"Sonos Amp\",\"note\":\"\"},"
        "\"pad\":null,\"rows\":[{\"label\":\"\",\"keys\":[\"previous\",\"play_pause\",\"next\"],\"columns\":3}],"
        "\"beside\":[],\"channel\":false,"
        "\"buttons\":{\"previous\":{\"label\":\"Previous\",\"icon\":\"previous\"},"
        "\"play_pause\":{\"label\":\"Pause\",\"icon\":\"pause\"},\"next\":{\"label\":\"Next\",\"icon\":\"next\"},"
        "\"volume_up\":{\"label\":\"+\",\"icon\":\"plus\",\"repeat\":true},"
        "\"volume_down\":{\"label\":\"-\",\"icon\":\"minus\",\"repeat\":true},"
        "\"mute\":{\"label\":\"Mute\",\"icon\":\"mute\"}},"
        "\"allOff\":false,\"mode\":\"listen\",\"modes\":[\"watch\",\"listen\"],"
        "\"playing\":{\"title\":\"Harbour Lights\",\"artist\":\"The Lanterns \xC2\xB7 Harbour Lights\",\"state\":\"playing\"}},"
        "\"audio\":{\"short\":\"Sonos Amp\",\"value\":32,\"min\":0,\"max\":100,\"step\":1,"
        "\"muted\":false,\"disabled\":false,\"text\":\"32\",\"caption\":\"Sonos Amp \xC2\xB7 32\"}}";
    tr_apply(remote, listen_msg);
    lv_timer_handler();
    CHECK(tree_has_label(scr, "Harbour Lights"), "listen screen missing Now Playing title");
    CHECK(tree_has_label(scr, "The Lanterns"), "listen screen missing artist");
    CHECK(tree_has_label(scr, "Playing"), "listen screen missing Playing state");
    CHECK(tree_has_label(scr, "Sonos Amp"), "listen screen missing speaker bubble");

    /* Mode toggle button sends {"mode":"watch"}. */
    lv_obj_t *mode_btn = find_button_with_label(scr, "Listen");
    CHECK(mode_btn != NULL, "listen mode toggle button not shown");
    if (mode_btn) {
        int before = g_sent;
        lv_obj_send_event(mode_btn, LV_EVENT_CLICKED, NULL);
        CHECK(g_sent == before + 1, "mode button did not send op");
        cJSON *sent = decode_last_data();
        const cJSON *mode_field = sent ? cJSON_GetObjectItemCaseSensitive(sent, "mode") : NULL;
        CHECK(cJSON_IsString(mode_field) && strcmp(mode_field->valuestring, "watch") == 0,
              "mode toggle did not send mode=watch");
        cJSON_Delete(sent);
    }

    /* 5b. Tapping the room header opens the room picker showing locked indicator, then selects a room cleanly. */
    lv_obj_t *header_row = lv_obj_get_child(scr, 0);
    if (header_row) {
        lv_obj_send_event(header_row, LV_EVENT_CLICKED, NULL);
        lv_timer_handler();
        CHECK(tree_has_label(top, "Rooms") && tree_has_label(top, "West") && tree_has_label(top, "Kitchen"),
              "room picker did not list rooms");
        lv_obj_t *west_btn = find_button_with_label(top, "West");
        CHECK(west_btn != NULL, "West room button missing from room picker");
        if (west_btn) {
            int before = g_sent;
            lv_obj_send_event(west_btn, LV_EVENT_CLICKED, NULL);
            CHECK(g_sent == before + 1, "room picker selection did not send room op");
            cJSON *picked = decode_last_data();
            const cJSON *room_field = picked ? cJSON_GetObjectItemCaseSensitive(picked, "room") : NULL;
            CHECK(cJSON_IsString(room_field) && strcmp(room_field->valuestring, "west") == 0,
                  "room picker did not send room=west");
            cJSON_Delete(picked);
            lv_timer_handler();
            CHECK(lv_obj_get_child_count(top) == 0, "room picker overlay not closed after selection");
        }
    }

    /* 6. Interactive touch volume slider on 240x320 throttles at 250 ms and emits final value on release. */
    lv_obj_t *slider = find_slider(scr);
    CHECK(slider != NULL, "240x320 screen missing volume slider");
    if (slider) {
        CHECK(lv_slider_get_value(slider) == 32, "slider did not sync to audio.value=32");
        int before = g_sent;
        lv_obj_send_event(slider, LV_EVENT_PRESSED, NULL);
        lv_slider_set_value(slider, 40, LV_ANIM_OFF);
        lv_obj_send_event(slider, LV_EVENT_VALUE_CHANGED, NULL);
        CHECK(g_sent == before + 1, "first slider move should emit immediately");
        /* Rapid drags within 250 ms are throttled. */
        g_clock += 50;
        lv_slider_set_value(slider, 45, LV_ANIM_OFF);
        lv_obj_send_event(slider, LV_EVENT_VALUE_CHANGED, NULL);
        g_clock += 50;
        lv_slider_set_value(slider, 50, LV_ANIM_OFF);
        lv_obj_send_event(slider, LV_EVENT_VALUE_CHANGED, NULL);
        lv_timer_handler();
        CHECK(g_sent == before + 1, "slider drag within 250 ms was not throttled (%d sends)", g_sent - before);
        /* At 250 ms the throttled value (50) is emitted. */
        g_clock += 160;
        lv_timer_handler();
        CHECK(g_sent == before + 2, "slider throttle timer did not emit at 250 ms");
        cJSON *mid = decode_last_data();
        const cJSON *mid_vol = mid ? cJSON_GetObjectItemCaseSensitive(mid, "volume") : NULL;
        CHECK(cJSON_IsNumber(mid_vol) && mid_vol->valuedouble == 50.0, "throttled volume != 50");
        cJSON_Delete(mid);
        /* Final release emits final value (58). */
        g_clock += 40;
        lv_slider_set_value(slider, 58, LV_ANIM_OFF);
        lv_obj_send_event(slider, LV_EVENT_VALUE_CHANGED, NULL);
        lv_obj_send_event(slider, LV_EVENT_RELEASED, NULL);
        CHECK(g_sent == before + 3, "slider release did not emit final value");
        cJSON *fin = decode_last_data();
        const cJSON *fin_vol = fin ? cJSON_GetObjectItemCaseSensitive(fin, "volume") : NULL;
        CHECK(cJSON_IsNumber(fin_vol) && fin_vol->valuedouble == 58.0, "final volume != 58");
        cJSON_Delete(fin);
    }

    /* 7. Small screen (135x240) omits the touch volume slider. */
    lv_display_t *small_disp = lv_display_create(135, 240);
    static uint16_t small_fb[135 * 240];
    lv_display_set_color_format(small_disp, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(small_disp, small_fb, NULL, sizeof small_fb, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_flush_cb(small_disp, flush_cb);
    tu_create(small_disp, remote, &keys);
    lv_timer_handler();
    CHECK(find_slider(lv_display_get_screen_active(small_disp)) == NULL,
          "compact 135x240 screen should not show touch volume slider");

    tr_destroy(remote);
    if (failures) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    puts("ui: all checks pass");
    return 0;
}
