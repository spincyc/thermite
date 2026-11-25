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

    tr_destroy(remote);
    if (failures) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    puts("ui: all checks pass");
    return 0;
}
