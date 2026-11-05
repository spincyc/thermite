/*
 * The remote's screen. From the top:
 *
 *   room line      the room's label; arrows, a tap or a swipe change room
 *   status line    the room's LED and short text, the link and battery marks
 *   bubbles        Off, the sources and devices: a row that scrolls sideways
 *   body           notice, problem, then the layout on show: D-pad with its
 *                  corners, labelled rows, the keys beside Mute and the CH
 *                  rocker, then All off. It scrolls.
 *   volume line    Volume -, the audio device and its level, Volume +, Mute
 *
 * Toasts and the input list sit on the top layer. The compact layout (160 px
 * wide or less: a stick without touch) keeps the words and leaves out keys.
 *
 * Updates are deferred to one LVGL timer, so a change reported from inside a
 * key's event handler never deletes that key under it, and several changes
 * reported together redraw once. Each part rebuilds only when its share of
 * the model changed: an audio-only change sets two labels.
 */
#include "thermite_ui.h"

#include <stdio.h>
#include <string.h>

#include "tu_text.h"

/* web/remote/theme.css, dark. */
#define C_BG 0x000000
#define C_SURFACE 0x101111
#define C_RAISED 0x1b1c1d
#define C_LINE 0x353738
#define C_TEXT 0xe8e5df
#define C_MUTED 0xa6aaa8
#define C_FAINT 0x727876
#define C_ACCENT 0xc79a6a
#define C_INK 0x15120e
#define C_OK 0x99b39a
#define C_BUSY 0xd0ab6f
#define C_BAD 0xd98b86

#define REPEAT_FIRST_MS 420
#define REPEAT_EVERY_MS 150
#define TOAST_MS 2500
#define TEXT_MAX 256
#define ROOMS_MAX 24
#define COMPACT_WIDTH 160
#define ROOMY_HEIGHT 400 /* tall enough to show keys the board also has */

typedef struct {
    bool compact;
    int32_t margin, gap, key_h, cell_w, key_min_w;
    const lv_font_t *small, *body, *title, *value;
} metrics_t;

/* Copies of model strings that outlive the cJSON they came from, reset with their part. */
typedef struct {
    char data[2048];
    size_t used;
} arena_t;

static struct {
    lv_display_t *display;
    tr_t *remote;
    tu_keys_t keys;
    metrics_t m;
    int32_t width, height;
    bool roomy;

    lv_style_t key, key_pressed, key_lit, key_disabled, bubble, bubble_selected, chip;

    lv_obj_t *screen, *room_prev, *room_label, *room_next;
    lv_obj_t *led, *short_label, *link_mark, *battery_mark;
    lv_obj_t *bubbles, *body, *all_off, *all_off_label;
    lv_obj_t *footer, *volume_down, *volume_up, *mute, *audio_short, *audio_text;
    lv_obj_t *toast, *inputs;
    lv_timer_t *update, *toast_timer, *repeat;

    unsigned pending; /* TR_CHANGED_* bits waiting for the update timer */
    uint32_t bubbles_hash, body_hash;
    bool body_empty; /* the body shows the no-screen note */
    char repeat_id[48];
    char led_shown[8]; /* the status LED's state, so a new screen restarts no blink */
    const char *rooms[ROOMS_MAX];
    int room_count, room_index;
    arena_t body_ids, bubble_ids, room_ids, input_ids;
} ui;

/* Outside ui: the battery is reported whenever it changes, not again for a new screen. */
static int battery = -1;
static bool charging;

/* ---- small helpers ------------------------------------------------------- */

static const char *keep(arena_t *arena, const char *text)
{
    size_t size = strlen(text) + 1;
    if (arena->used + size > sizeof arena->data) return NULL;
    char *at = arena->data + arena->used;
    memcpy(at, text, size);
    arena->used += size;
    return at;
}

static const char *string_of(const cJSON *object, const char *name)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    return cJSON_IsString(item) ? item->valuestring : "";
}

static bool true_of(const cJSON *object, const char *name)
{
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(object, name));
}

static uint32_t fnv(uint32_t hash, const void *data, size_t size)
{
    const unsigned char *at = data;
    for (size_t i = 0; i < size; i++) hash = (hash ^ at[i]) * 16777619u;
    return hash;
}

/* A digest of a model subtree, to tell whether a part needs rebuilding. */
static uint32_t hash_of(uint32_t hash, const cJSON *item)
{
    int type = item ? item->type & 0xff : -1;
    hash = fnv(hash, &type, sizeof type);
    if (!item) return hash;
    if (item->string) hash = fnv(hash, item->string, strlen(item->string) + 1);
    if (cJSON_IsString(item)) hash = fnv(hash, item->valuestring, strlen(item->valuestring) + 1);
    if (cJSON_IsNumber(item)) hash = fnv(hash, &item->valuedouble, sizeof item->valuedouble);
    for (const cJSON *child = item->child; child; child = child->next) hash = hash_of(hash, child);
    return hash;
}

static uint32_t hash_members(const cJSON *object, const char *const *names, size_t count)
{
    uint32_t hash = 2166136261u;
    for (size_t i = 0; i < count; i++) {
        hash = hash_of(fnv(hash, names[i], strlen(names[i])), cJSON_GetObjectItemCaseSensitive(object, names[i]));
    }
    return hash;
}

/* Sets a label's text, normalized for the fonts, only when it differs: no redraw otherwise. */
static void set_text(lv_obj_t *label, const char *text)
{
    char normal[TEXT_MAX];
    tu_normalize(text, normal, sizeof normal);
    if (strcmp(lv_label_get_text(label), normal) != 0) lv_label_set_text(label, normal);
}

/* Each setter below changes nothing, so redraws nothing, when the value is already set. */
static void show(lv_obj_t *obj, bool shown)
{
    if (shown == lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) {
        if (shown) lv_obj_remove_flag(obj, LV_OBJ_FLAG_HIDDEN);
        else lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
    }
}

static void set_font(lv_obj_t *obj, const lv_font_t *font)
{
    if (lv_obj_get_style_text_font(obj, LV_PART_MAIN) != font) lv_obj_set_style_text_font(obj, font, 0);
}

static void set_ink(lv_obj_t *obj, uint32_t color)
{
    lv_color_t ink = lv_color_hex(color);
    if (!lv_color_eq(lv_obj_get_style_text_color(obj, LV_PART_MAIN), ink)) lv_obj_set_style_text_color(obj, ink, 0);
}

/* A bare container: no style, no scrolling, clicks pass to its parent. */
static lv_obj_t *box(lv_obj_t *parent)
{
    lv_obj_t *obj = lv_obj_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    return obj;
}

/* A full-width row whose children wrap, centered. */
static lv_obj_t *row_box(lv_obj_t *parent)
{
    lv_obj_t *obj = box(parent);
    lv_obj_set_size(obj, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(obj, LV_FLEX_FLOW_ROW_WRAP);
    lv_obj_set_flex_align(obj, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_gap(obj, ui.m.gap, 0);
    return obj;
}

static lv_obj_t *label(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text)
{
    lv_obj_t *obj = lv_label_create(parent);
    lv_obj_set_style_text_font(obj, font, 0);
    lv_obj_set_style_text_color(obj, lv_color_hex(color), 0);
    lv_label_set_text(obj, "");
    set_text(obj, text);
    return obj;
}

/* A paragraph across the body's width. */
static lv_obj_t *paragraph(lv_obj_t *parent, const lv_font_t *font, uint32_t color, const char *text)
{
    lv_obj_t *obj = label(parent, font, color, text);
    lv_obj_set_width(obj, LV_PCT(100));
    lv_label_set_long_mode(obj, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_align(obj, LV_TEXT_ALIGN_CENTER, 0);
    return obj;
}

static void metrics_for(int32_t width, int32_t height)
{
    metrics_t *m = &ui.m;
    if (width <= COMPACT_WIDTH) {
        *m = (metrics_t){ .compact = true, .margin = 4, .gap = 3, .key_h = 32, .key_min_w = 32,
                          .small = &lv_font_montserrat_10, .body = &lv_font_montserrat_12,
                          .title = &lv_font_montserrat_14, .value = &lv_font_montserrat_20 };
    } else if (width < 300) {
        *m = (metrics_t){ .margin = 8, .gap = 4, .key_h = 40, .key_min_w = 48,
                          .small = &lv_font_montserrat_12, .body = &lv_font_montserrat_14,
                          .title = &lv_font_montserrat_16, .value = &lv_font_montserrat_20 };
    } else {
        *m = (metrics_t){ .margin = 10, .gap = 6, .key_h = 48, .key_min_w = 56,
                          .small = &lv_font_montserrat_14, .body = &lv_font_montserrat_16,
                          .title = &lv_font_montserrat_20, .value = &lv_font_montserrat_20 };
    }
    int32_t inner = width - 2 * m->margin;
    m->cell_w = LV_MIN((inner - 2 * m->gap) / 3, width < 300 ? 72 : 92);
    ui.roomy = height >= ROOMY_HEIGHT;
}

static void init_styles(void)
{
    lv_style_t *styles[] = { &ui.key, &ui.key_pressed, &ui.key_lit, &ui.key_disabled, &ui.bubble,
                             &ui.bubble_selected, &ui.chip };
    for (size_t i = 0; i < sizeof styles / sizeof styles[0]; i++) lv_style_init(styles[i]);
    lv_style_set_bg_color(&ui.key, lv_color_hex(C_RAISED));
    lv_style_set_bg_opa(&ui.key, LV_OPA_COVER);
    lv_style_set_border_color(&ui.key, lv_color_hex(C_LINE));
    lv_style_set_border_width(&ui.key, 1);
    lv_style_set_radius(&ui.key, 8);
    lv_style_set_text_color(&ui.key, lv_color_hex(C_TEXT));
    lv_style_set_text_font(&ui.key, ui.m.body);
    lv_style_set_pad_all(&ui.key, 2);

    lv_style_set_bg_color(&ui.key_pressed, lv_color_hex(C_LINE));
    lv_style_set_border_color(&ui.key_pressed, lv_color_hex(C_ACCENT));

    lv_style_set_bg_color(&ui.key_lit, lv_color_hex(C_BUSY));
    lv_style_set_border_color(&ui.key_lit, lv_color_hex(C_BUSY));
    lv_style_set_text_color(&ui.key_lit, lv_color_hex(C_INK));

    lv_style_set_opa(&ui.key_disabled, LV_OPA_40);

    lv_style_set_bg_color(&ui.bubble, lv_color_hex(C_SURFACE));
    lv_style_set_bg_opa(&ui.bubble, LV_OPA_COVER);
    lv_style_set_border_color(&ui.bubble, lv_color_hex(C_LINE));
    lv_style_set_border_width(&ui.bubble, 1);
    lv_style_set_radius(&ui.bubble, LV_RADIUS_CIRCLE);
    lv_style_set_text_color(&ui.bubble, lv_color_hex(C_TEXT));
    lv_style_set_text_font(&ui.bubble, ui.m.body);
    lv_style_set_pad_hor(&ui.bubble, ui.m.compact ? 6 : 12);
    lv_style_set_pad_column(&ui.bubble, 6);

    lv_style_set_bg_color(&ui.bubble_selected, lv_color_hex(C_RAISED));
    lv_style_set_border_color(&ui.bubble_selected, lv_color_hex(C_ACCENT));
    lv_style_set_border_width(&ui.bubble_selected, 2);

    lv_style_set_bg_color(&ui.chip, lv_color_hex(C_RAISED));
    lv_style_set_bg_opa(&ui.chip, LV_OPA_COVER);
    lv_style_set_radius(&ui.chip, 10);
    lv_style_set_border_width(&ui.chip, 2);
    lv_style_set_pad_all(&ui.chip, ui.m.compact ? 4 : 8);
    lv_style_set_pad_row(&ui.chip, 2);
}

static uint32_t led_color(const char *led)
{
    if (strcmp(led, "ok") == 0) return C_OK;
    if (strcmp(led, "busy") == 0) return C_BUSY;
    if (strcmp(led, "bad") == 0) return C_BAD;
    return C_LINE;
}

static lv_obj_t *led_dot(lv_obj_t *parent, int32_t size)
{
    lv_obj_t *dot = box(parent);
    lv_obj_set_size(dot, size, size);
    lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
    lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(dot, lv_color_hex(C_LINE), 0); /* off until told otherwise */
    return dot;
}

static void blink_cb(void *dot, int32_t value)
{
    lv_obj_set_style_bg_opa(dot, (lv_opa_t)value, 0);
}

/* Switching blinks, as the web remote's LED does. */
static void blink(lv_obj_t *dot)
{
    lv_anim_t anim;
    lv_anim_init(&anim);
    lv_anim_set_var(&anim, dot);
    lv_anim_set_exec_cb(&anim, blink_cb);
    lv_anim_set_values(&anim, LV_OPA_COVER, LV_OPA_20);
    lv_anim_set_duration(&anim, 500);
    lv_anim_set_playback_duration(&anim, 500);
    lv_anim_set_repeat_count(&anim, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&anim);
}

/* No padlock among the built-in symbols: a shackle (a box bordered on three sides) over a body. */
static lv_obj_t *padlock(lv_obj_t *parent, uint32_t color)
{
    int32_t unit = ui.m.compact ? 1 : 2;
    lv_obj_t *lock = box(parent);
    lv_obj_set_size(lock, 5 * unit + 2, 7 * unit + 2);
    lv_obj_t *shackle = box(lock);
    lv_obj_set_size(shackle, 3 * unit + 2, 4 * unit + 1);
    lv_obj_align(shackle, LV_ALIGN_TOP_MID, 0, 0);
    lv_obj_set_style_border_width(shackle, unit, 0);
    lv_obj_set_style_border_side(shackle, LV_BORDER_SIDE_TOP | LV_BORDER_SIDE_LEFT | LV_BORDER_SIDE_RIGHT, 0);
    lv_obj_set_style_border_color(shackle, lv_color_hex(color), 0);
    lv_obj_set_style_radius(shackle, 2 * unit, 0);
    lv_obj_t *body = box(lock);
    lv_obj_set_size(body, 5 * unit + 2, 4 * unit);
    lv_obj_align(body, LV_ALIGN_BOTTOM_MID, 0, 0);
    lv_obj_set_style_bg_opa(body, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(body, lv_color_hex(color), 0);
    lv_obj_set_style_radius(body, 1, 0);
    return lock;
}

/* ---- keys ---------------------------------------------------------------- */

static void press(const char *id)
{
    const char *ids[1] = { id };
    if (!ui.remote) return;
    tr_key(ui.remote, ids, 1);
}

static void stop_repeat(void)
{
    if (ui.repeat) lv_timer_delete(ui.repeat);
    ui.repeat = NULL;
}

static void repeat_cb(lv_timer_t *timer)
{
    press(ui.repeat_id);
    lv_timer_set_period(timer, REPEAT_EVERY_MS);
}

/* A key that repeats presses on touch, again 420 ms later, then every 150 ms. */
static void hold_cb(lv_event_t *event)
{
    stop_repeat();
    snprintf(ui.repeat_id, sizeof ui.repeat_id, "%s", (const char *)lv_event_get_user_data(event));
    press(ui.repeat_id);
    ui.repeat = lv_timer_create(repeat_cb, REPEAT_FIRST_MS, NULL);
}

static void let_go_cb(lv_event_t *event)
{
    (void)event;
    stop_repeat();
}

/* Any other key presses once on release, so a drag that scrolls the body sends nothing. */
static void click_cb(lv_event_t *event)
{
    press(lv_event_get_user_data(event));
}

static const cJSON *spec_of(const cJSON *screen, const char *id)
{
    return cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(screen, "buttons"), id);
}

/* The board has this key and the screen is short of room for it. */
static bool on_board(const char *id)
{
    static const char *const dpad[] = { "up", "down", "left", "right", "ok", "back", NULL };
    static const char *const volume[] = { "volume_up", "volume_down", "mute", NULL };
    static const char *const channel[] = { "channel_up", "channel_down", NULL };
    static const char *const playback[] = { "play_pause", "play", "stop", "rewind", "previous", "fast_forward",
                                            "next", NULL };
    const struct {
        bool has;
        const char *const *ids;
    } groups[] = { { ui.keys.dpad, dpad }, { ui.keys.volume, volume }, { ui.keys.channel, channel },
                   { ui.keys.playback, playback } };
    if (ui.roomy || !id) return false;
    for (size_t g = 0; g < sizeof groups / sizeof groups[0]; g++) {
        for (const char *const *at = groups[g].ids; groups[g].has && *at; at++) {
            if (strcmp(*at, id) == 0) return true;
        }
    }
    return false;
}

static const char *const color_keys[] = { "red", "green", "yellow", "blue" };
static const uint32_t color_values[] = { 0xc8574f, 0x5f9e62, 0xd6b84a, 0x5582c2 };

/* The face of a key: its icon, else its text, else its label. */
static void key_face(lv_obj_t *key, const char *id, const char *icon_name, const char *text, const char *name,
                     int32_t width)
{
    const char *icon = tu_icon(icon_name);
    char face[64];
    if (strcmp(id, "channel_up") == 0 || strcmp(id, "channel_down") == 0) {
        snprintf(face, sizeof face, "CH %s", id[8] == 'u' ? LV_SYMBOL_UP : LV_SYMBOL_DOWN);
    } else {
        snprintf(face, sizeof face, "%s", icon ? icon : text[0] ? text : name[0] ? name : id);
    }
    /* The face inherits the key's font and color, so a lit key's ink follows. */
    lv_obj_t *face_label = lv_label_create(key);
    set_text(face_label, face);
    lv_obj_set_style_text_align(face_label, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(face_label, LV_LABEL_LONG_WRAP);
    lv_obj_set_width(face_label, width - 6);
    /* A word too wide for the key takes the small font, and wraps if it must. */
    const char *shown = lv_label_get_text(face_label);
    if (lv_text_get_width(shown, (uint32_t)strlen(shown), ui.m.body, 0) > width - 8) {
        lv_obj_set_style_text_font(face_label, ui.m.small, 0);
    }
    lv_obj_center(face_label);
    for (size_t i = 0; i < 4; i++) {
        if (strcmp(id, color_keys[i]) == 0) {
            lv_obj_set_style_border_side(key, LV_BORDER_SIDE_BOTTOM, 0);
            lv_obj_set_style_border_width(key, 4, 0);
            lv_obj_set_style_border_color(key, lv_color_hex(color_values[i]), 0);
        }
    }
}

static lv_obj_t *key_button(lv_obj_t *parent, int32_t width, int32_t height)
{
    lv_obj_t *obj = lv_button_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_add_style(obj, &ui.key, 0);
    lv_obj_add_style(obj, &ui.key_pressed, LV_STATE_PRESSED);
    lv_obj_add_style(obj, &ui.key_lit, LV_STATE_CHECKED);
    lv_obj_add_style(obj, &ui.key_disabled, LV_STATE_DISABLED);
    lv_obj_set_size(obj, width, height);
    return obj;
}

/* One key; id must outlive it (a static string or one kept in ui.body_ids). */
static lv_obj_t *make_key(lv_obj_t *parent, const char *id, const char *icon, const char *text, const char *name,
                          bool repeat, int32_t width, int32_t height)
{
    lv_obj_t *obj = key_button(parent, width, height);
    key_face(obj, id, icon, text, name, width);
    if (repeat) {
        /* Held keys must not start a scroll: the hold would end in PRESS_LOST. */
        lv_obj_remove_flag(obj, LV_OBJ_FLAG_SCROLL_CHAIN);
        lv_obj_add_event_cb(obj, hold_cb, LV_EVENT_PRESSED, (void *)id);
        lv_obj_add_event_cb(obj, let_go_cb, LV_EVENT_RELEASED, NULL);
        lv_obj_add_event_cb(obj, let_go_cb, LV_EVENT_PRESS_LOST, NULL);
    } else {
        lv_obj_add_event_cb(obj, click_cb, LV_EVENT_CLICKED, (void *)id);
    }
    return obj;
}

/* A key as the screen's buttons describe it (REMOTE_LAYOUTS spec). */
static lv_obj_t *key(lv_obj_t *parent, const cJSON *screen, const char *id, int32_t width, int32_t height)
{
    const cJSON *spec = spec_of(screen, id);
    return make_key(parent, id, string_of(spec, "icon"), string_of(spec, "text"), string_of(spec, "label"),
                    true_of(spec, "repeat"), width, height);
}

/* A row of keys, columns across (0: as many as fit), centered. */
static void key_grid(lv_obj_t *parent, const cJSON *screen, const char *const *ids, int count, int columns)
{
    int32_t inner = ui.width - 2 * ui.m.margin;
    int fit = (int)((inner + ui.m.gap) / (ui.m.key_min_w + ui.m.gap));
    if (columns <= 0 || columns > fit) columns = LV_MIN(count, fit);
    if (columns <= 0) return;
    int32_t width = (inner - (columns - 1) * ui.m.gap) / columns;
    if (columns < 3 && count <= columns) width = LV_MIN(width, ui.m.cell_w + ui.m.cell_w / 2);
    lv_obj_t *grid = row_box(parent);
    for (int i = 0; i < count; i++) key(grid, screen, ids[i], width, ui.m.key_h);
}

/* The ids of a JSON array that the screen shows, kept in the body's arena. */
static int shown_ids(const cJSON *array, const char **ids, int capacity)
{
    int count = 0;
    const cJSON *item;
    cJSON_ArrayForEach(item, array) {
        if (count == capacity) break;
        if (!cJSON_IsString(item) || on_board(item->valuestring)) continue;
        const char *id = keep(&ui.body_ids, item->valuestring);
        if (id) ids[count++] = id;
    }
    return count;
}

/* ---- the body ------------------------------------------------------------ */

static void heading(lv_obj_t *parent, const char *text)
{
    lv_obj_t *obj = paragraph(parent, ui.m.small, C_MUTED, text);
    lv_obj_set_style_text_align(obj, LV_TEXT_ALIGN_LEFT, 0);
    lv_obj_set_style_pad_top(obj, ui.m.gap, 0);
}

static void note(const char *title, const char *text)
{
    lv_obj_t *obj = paragraph(ui.body, ui.m.title, C_TEXT, title);
    lv_obj_set_style_pad_top(obj, ui.m.key_h / 2, 0);
    paragraph(ui.body, ui.m.body, C_MUTED, text);
}

/* The D-pad on a 3x3 grid, OK at its heart and the corner keys in its corners. */
static void dpad(const cJSON *screen, const cJSON *corners)
{
    static const char *const cross[] = { "up", "left", "ok", "right", "down" };
    static const int cross_at[][2] = { { 1, 0 }, { 0, 1 }, { 1, 1 }, { 2, 1 }, { 1, 2 } };
    static const int corner_at[][2] = { { 0, 0 }, { 2, 0 }, { 0, 2 }, { 2, 2 } };
    const char *ids[4];
    if (ui.keys.dpad && !ui.roomy) {
        /* The board has the D-pad: keep the corners, as one row. */
        int count = shown_ids(corners, ids, 4);
        if (count) key_grid(ui.body, screen, ids, count, 4);
        return;
    }
    int32_t w = ui.m.cell_w, h = ui.m.key_h, gap = ui.m.gap;
    lv_obj_t *pad = box(ui.body);
    lv_obj_set_size(pad, 3 * w + 2 * gap, 3 * h + 2 * gap);
    for (int i = 0; i < 5; i++) {
        lv_obj_t *obj = key(pad, screen, cross[i], w, h);
        lv_obj_set_pos(obj, cross_at[i][0] * (w + gap), cross_at[i][1] * (h + gap));
        if (i == 2) lv_obj_add_state(obj, LV_STATE_CHECKED); /* OK stands out */
    }
    for (int i = 0; i < 4; i++) {
        const cJSON *item = cJSON_GetArrayItem(corners, i);
        const char *id = cJSON_IsString(item) ? keep(&ui.body_ids, item->valuestring) : NULL;
        if (!id) continue;
        lv_obj_t *obj = key(pad, screen, id, w, h);
        lv_obj_set_pos(obj, corner_at[i][0] * (w + gap), corner_at[i][1] * (h + gap));
    }
}

static void rows(const cJSON *screen)
{
    const cJSON *row;
    cJSON_ArrayForEach(row, cJSON_GetObjectItemCaseSensitive(screen, "rows")) {
        const char *ids[24];
        int count = shown_ids(cJSON_GetObjectItemCaseSensitive(row, "keys"), ids, 24);
        if (!count) continue;
        const char *title = string_of(row, "label");
        if (title[0]) heading(ui.body, title);
        const cJSON *columns = cJSON_GetObjectItemCaseSensitive(row, "columns");
        key_grid(ui.body, screen, ids, count, cJSON_IsNumber(columns) ? columns->valueint : 0);
    }
}

/* The playback keys that sit beside Mute, and the CH rocker. */
static void lower(const cJSON *screen)
{
    const char *ids[6];
    int count = shown_ids(cJSON_GetObjectItemCaseSensitive(screen, "beside"), ids, 3);
    if (true_of(screen, "channel")) {
        static const char *const channel[] = { "channel_down", "channel_up" };
        for (int i = 0; i < 2; i++) {
            if (!on_board(channel[i])) ids[count++] = channel[i];
        }
    }
    if (count) key_grid(ui.body, screen, ids, count, count);
}

static void all_off_cb(lv_event_t *event)
{
    (void)event;
    if (ui.remote) tr_all_off(ui.remote);
}

static void make_all_off(void)
{
    ui.all_off = key_button(ui.body, LV_PCT(100), ui.m.key_h);
    lv_obj_set_style_margin_top(ui.all_off, ui.m.gap * 2, 0);
    ui.all_off_label = label(ui.all_off, ui.m.body, C_BAD, "");
    lv_obj_center(ui.all_off_label);
    lv_obj_add_event_cb(ui.all_off, all_off_cb, LV_EVENT_CLICKED, NULL);
}

/* The first press arms All off; armed, it asks for the second (web remote.js showAllOff). */
static void show_all_off(const cJSON *screen)
{
    if (!ui.all_off) return;
    bool armed = true_of(screen, "allOff");
    lv_obj_set_style_bg_color(ui.all_off, lv_color_hex(armed ? C_BAD : C_RAISED), 0);
    lv_obj_set_style_text_color(ui.all_off_label, lv_color_hex(armed ? C_INK : C_BAD), 0);
    set_text(ui.all_off_label, armed ? LV_SYMBOL_POWER "  Press again to confirm" : LV_SYMBOL_POWER "  All off");
}

/* Controls are blocked while the room is locked or its bubbles are inert (web remote.js update). */
static bool blocked(const cJSON *screen)
{
    if (true_of(screen, "locked")) return true;
    const cJSON *bubble;
    cJSON_ArrayForEach(bubble, cJSON_GetObjectItemCaseSensitive(screen, "bubbles")) {
        if (true_of(bubble, "inert")) return true;
    }
    return false;
}

static void disable_keys(lv_obj_t *parent)
{
    for (uint32_t i = 0; i < lv_obj_get_child_count(parent); i++) {
        lv_obj_t *child = lv_obj_get_child(parent, (int32_t)i);
        if (child == ui.all_off) continue;
        if (lv_obj_check_type(child, &lv_button_class)) lv_obj_add_state(child, LV_STATE_DISABLED);
        else disable_keys(child);
    }
}

static void build_body(const cJSON *screen)
{
    stop_repeat();
    lv_obj_clean(ui.body);
    ui.all_off = NULL;
    ui.body_ids.used = 0;
    ui.body_empty = false;
    lv_obj_scroll_to_y(ui.body, 0, LV_ANIM_OFF);

    const char *notice = string_of(screen, "notice"), *problem = string_of(screen, "problem");
    if (notice[0]) paragraph(ui.body, ui.m.small, C_MUTED, notice);
    if (problem[0]) {
        char text[TEXT_MAX];
        snprintf(text, sizeof text, LV_SYMBOL_WARNING " %s", problem);
        paragraph(ui.body, ui.m.small, C_BAD, text);
    }
    const cJSON *remote = cJSON_GetObjectItemCaseSensitive(screen, "remote");
    if (true_of(screen, "hidden")) {
        note("Off", "Choose a source, or a device for its buttons");
    } else if (!cJSON_IsObject(remote)) {
        note("Nothing to control", "Choose a source above");
    } else if (string_of(remote, "note")[0]) {
        note(string_of(remote, "label"), string_of(remote, "note"));
    } else {
        paragraph(ui.body, ui.m.small, C_FAINT, string_of(remote, "label"));
        if (!ui.m.compact) {
            const cJSON *pad = cJSON_GetObjectItemCaseSensitive(screen, "pad");
            if (cJSON_IsObject(pad)) dpad(screen, cJSON_GetObjectItemCaseSensitive(pad, "corners"));
            rows(screen);
            lower(screen);
        }
    }
    if (!ui.m.compact) {
        make_all_off();
        show_all_off(screen);
    }
    if (blocked(screen)) disable_keys(ui.body);
}

/* The volume line follows the room's block, as every key on the web remote does. */
static void block_footer(const cJSON *screen)
{
    lv_obj_t *keys[] = { ui.volume_down, ui.volume_up, ui.mute };
    for (size_t i = 0; i < 3; i++) {
        if (!keys[i]) continue;
        if (blocked(screen)) lv_obj_add_state(keys[i], LV_STATE_DISABLED);
        else lv_obj_remove_state(keys[i], LV_STATE_DISABLED);
    }
}

/* ---- the header and bubbles ---------------------------------------------- */

static void room_cb(lv_event_t *event)
{
    int step = (int)(intptr_t)lv_event_get_user_data(event);
    if (lv_event_get_code(event) == LV_EVENT_GESTURE) {
        lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_active());
        if (dir != LV_DIR_LEFT && dir != LV_DIR_RIGHT) return;
        step = dir == LV_DIR_LEFT ? 1 : -1;
        lv_indev_wait_release(lv_indev_active());
    }
    if (ui.room_count < 2 || !ui.remote) return;
    int next = (ui.room_index + step + ui.room_count) % ui.room_count;
    tr_room(ui.remote, ui.rooms[next]);
}

static void show_rooms(const cJSON *screen)
{
    set_text(ui.room_label, string_of(screen, "label"));
    ui.room_ids.used = 0;
    ui.room_count = 0;
    ui.room_index = 0;
    const char *current = string_of(screen, "room");
    const cJSON *room;
    cJSON_ArrayForEach(room, cJSON_GetObjectItemCaseSensitive(screen, "rooms")) {
        const char *id = keep(&ui.room_ids, string_of(room, "id"));
        if (!id || ui.room_count == ROOMS_MAX) break;
        if (strcmp(id, current) == 0) ui.room_index = ui.room_count;
        ui.rooms[ui.room_count++] = id;
    }
    bool several = ui.room_count > 1 && !ui.m.compact;
    show(ui.room_prev, several);
    show(ui.room_next, several);
}

static void show_status(const cJSON *screen)
{
    const char *led = string_of(screen, "led");
    if (strcmp(led, ui.led_shown) != 0) {
        snprintf(ui.led_shown, sizeof ui.led_shown, "%s", led);
        lv_obj_set_style_bg_color(ui.led, lv_color_hex(led_color(led)), 0);
        lv_anim_delete(ui.led, blink_cb);
        lv_obj_set_style_bg_opa(ui.led, LV_OPA_COVER, 0);
        if (strcmp(led, "busy") == 0) blink(ui.led);
    }
    show(ui.led, true);
    set_text(ui.short_label, string_of(screen, "short"));
}

static void bubble_cb(lv_event_t *event)
{
    if (ui.remote) tr_bubble(ui.remote, lv_event_get_user_data(event));
}

/* Keeps what the room shows in sight, as the terminal remote's row does, starting
 * the row at a whole bubble: the one before it when both fit, else itself. */
static void reveal_bubble(lv_obj_t *bubble)
{
    lv_obj_update_layout(ui.bubbles);
    int32_t window = lv_obj_get_content_width(ui.bubbles);
    int32_t right = lv_obj_get_x(bubble) + lv_obj_get_width(bubble);
    int32_t index = lv_obj_get_index(bubble);
    lv_obj_t *before = index > 0 ? lv_obj_get_child(ui.bubbles, index - 1) : bubble;
    int32_t start = 0;
    if (right > window) start = right - lv_obj_get_x(before) <= window ? lv_obj_get_x(before) : lv_obj_get_x(bubble);
    lv_obj_scroll_to_x(ui.bubbles, start, LV_ANIM_OFF);
}

static void build_bubbles(const cJSON *screen)
{
    lv_obj_clean(ui.bubbles);
    ui.bubble_ids.used = 0;
    const char *selected = string_of(screen, "selected");
    lv_obj_t *reveal = NULL;
    const cJSON *item;
    cJSON_ArrayForEach(item, cJSON_GetObjectItemCaseSensitive(screen, "bubbles")) {
        const char *id = keep(&ui.bubble_ids, string_of(item, "id"));
        if (!id) break;
        const char *state = string_of(item, "state");
        bool current = strcmp(state, "current") == 0, switching = strcmp(state, "switching") == 0;
        lv_obj_t *bubble = lv_button_create(ui.bubbles);
        lv_obj_remove_style_all(bubble);
        lv_obj_add_style(bubble, &ui.bubble, 0);
        lv_obj_add_style(bubble, &ui.key_pressed, LV_STATE_PRESSED);
        if (strcmp(id, selected) == 0) lv_obj_add_style(bubble, &ui.bubble_selected, 0);
        lv_obj_set_size(bubble, LV_SIZE_CONTENT, ui.m.compact ? 22 : ui.m.key_h);
        lv_obj_set_flex_flow(bubble, LV_FLEX_FLOW_ROW);
        lv_obj_set_flex_align(bubble, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
        uint32_t color = current ? C_OK : switching ? C_BUSY : C_LINE;
        if (true_of(item, "lock")) {
            padlock(bubble, current || switching ? color : C_MUTED);
        } else {
            lv_obj_t *dot = led_dot(bubble, ui.m.compact ? 6 : 8);
            lv_obj_set_style_bg_color(dot, lv_color_hex(color), 0);
            if (switching) blink(dot);
        }
        set_text(lv_label_create(bubble), string_of(item, "label"));
        lv_obj_add_style(bubble, &ui.key_disabled, LV_STATE_DISABLED);
        if (true_of(item, "inert")) lv_obj_add_state(bubble, LV_STATE_DISABLED);
        lv_obj_add_event_cb(bubble, bubble_cb, LV_EVENT_CLICKED, (void *)id);
        if (current || switching || (!reveal && strcmp(id, selected) == 0)) reveal = bubble;
    }
    if (reveal && !ui.m.compact) reveal_bubble(reveal);
}

/* ---- the volume line ----------------------------------------------------- */

static void show_audio(void)
{
    const cJSON *audio = tr_audio(ui.remote);
    show(ui.footer, audio != NULL);
    if (!audio) return;
    /* The level large under the device's name; a longer text (no level yet) says it all alone. */
    const char *text = string_of(audio, "text");
    bool level = strlen(text) <= 6;
    set_text(ui.audio_short, !level ? "" : ui.m.compact ? string_of(audio, "caption") : string_of(audio, "short"));
    show(ui.audio_short, level);
    set_text(ui.audio_text, text);
    set_font(ui.audio_text, level ? ui.m.value : ui.m.small);
    lv_label_long_mode_t mode = level ? LV_LABEL_LONG_DOT : LV_LABEL_LONG_WRAP;
    if (lv_label_get_long_mode(ui.audio_text) != mode) lv_label_set_long_mode(ui.audio_text, mode);
    const cJSON *muted = cJSON_GetObjectItemCaseSensitive(audio, "muted");
    set_ink(ui.audio_text, cJSON_IsTrue(muted) ? C_BUSY : C_TEXT);
    if (ui.mute) {
        show(ui.mute, cJSON_IsBool(muted));
        if (cJSON_IsTrue(muted)) lv_obj_add_state(ui.mute, LV_STATE_CHECKED);
        else lv_obj_remove_state(ui.mute, LV_STATE_CHECKED);
    }
}

static void make_footer(lv_obj_t *parent)
{
    ui.footer = box(parent);
    lv_obj_set_size(ui.footer, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(ui.footer, ui.m.margin, 0);
    lv_obj_set_style_pad_ver(ui.footer, ui.m.gap, 0);
    lv_obj_set_style_border_side(ui.footer, LV_BORDER_SIDE_TOP, 0);
    lv_obj_set_style_border_width(ui.footer, 1, 0);
    lv_obj_set_style_border_color(ui.footer, lv_color_hex(C_LINE), 0);
    lv_obj_set_flex_flow(ui.footer, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(ui.footer, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(ui.footer, ui.m.gap, 0);

    bool keys = !ui.m.compact && !(ui.keys.volume && !ui.roomy);
    /* remotes.js's specs, which every screen repeats: the line exists before the first screen. */
    int32_t width = ui.m.key_min_w - 4;
    if (keys) ui.volume_down = make_key(ui.footer, "volume_down", "minus", "", "Volume down", true, width, ui.m.key_h);
    lv_obj_t *middle = box(ui.footer);
    lv_obj_set_flex_grow(middle, 1);
    lv_obj_set_height(middle, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(middle, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(middle, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    ui.audio_short = label(middle, ui.m.small, C_MUTED, "");
    lv_obj_set_width(ui.audio_short, LV_PCT(100));
    lv_obj_set_style_text_align(ui.audio_short, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(ui.audio_short, ui.m.compact ? LV_LABEL_LONG_WRAP : LV_LABEL_LONG_DOT);
    ui.audio_text = label(middle, ui.m.value, C_TEXT, "");
    lv_obj_set_width(ui.audio_text, LV_PCT(100));
    lv_obj_set_style_text_align(ui.audio_text, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(ui.audio_text, LV_LABEL_LONG_DOT);
    if (keys) {
        ui.volume_up = make_key(ui.footer, "volume_up", "plus", "", "Volume up", true, width, ui.m.key_h);
        ui.mute = make_key(ui.footer, "mute", "mute", "", "Mute", false, width, ui.m.key_h);
    }
    show(ui.footer, false);
}

/* ---- link and battery marks ---------------------------------------------- */

static void link_cb(lv_event_t *event)
{
    (void)event;
    if (ui.remote && tr_link(ui.remote) == TR_LINK_OFFLINE) tr_wake(ui.remote);
}

static void show_link(void)
{
    tr_link_t link = tr_link(ui.remote);
    show(ui.link_mark, link != TR_LINK_CONNECTED);
    set_text(ui.link_mark, link == TR_LINK_OFFLINE ? LV_SYMBOL_WARNING : LV_SYMBOL_REFRESH);
    lv_obj_set_style_text_color(ui.link_mark, lv_color_hex(link == TR_LINK_OFFLINE ? C_BAD : C_BUSY), 0);
}

static void show_battery(void)
{
    const char *symbol = battery < 0 ? "" : battery > 80 ? LV_SYMBOL_BATTERY_FULL
        : battery > 55 ? LV_SYMBOL_BATTERY_3 : battery > 30 ? LV_SYMBOL_BATTERY_2
        : battery > 10 ? LV_SYMBOL_BATTERY_1 : LV_SYMBOL_BATTERY_EMPTY;
    char text[16];
    snprintf(text, sizeof text, "%s%s", charging ? LV_SYMBOL_CHARGE : "", symbol);
    set_text(ui.battery_mark, text);
    bool low = battery >= 0 && battery <= 15 && !charging;
    lv_obj_set_style_text_color(ui.battery_mark, lv_color_hex(low ? C_BAD : C_MUTED), 0);
    show(ui.battery_mark, text[0] != '\0');
}

/* Before the first screen: what the link is doing, in the body. */
static void show_waiting(void)
{
    tr_link_t link = tr_link(ui.remote);
    lv_obj_clean(ui.body);
    ui.all_off = NULL;
    ui.body_empty = true;
    ui.body_hash = 0;
    ui.bubbles_hash = 0;
    lv_obj_clean(ui.bubbles);
    show(ui.led, false);
    ui.led_shown[0] = '\0';
    set_text(ui.room_label, "");
    set_text(ui.short_label, "");
    show(ui.room_prev, false);
    show(ui.room_next, false);
    if (link == TR_LINK_OFFLINE) note("Not connected", "Tap to try again");
    else if (link == TR_LINK_CONNECTING) note("Connecting", "Looking for the remote's base...");
    else note("Connected", "Waiting for the screen...");
}

/* ---- toasts and the input list ------------------------------------------- */

static void close_toast(void)
{
    if (ui.toast_timer) lv_timer_delete(ui.toast_timer);
    ui.toast_timer = NULL;
    if (ui.toast) lv_obj_delete(ui.toast);
    ui.toast = NULL;
}

static void toast_timer_cb(lv_timer_t *timer)
{
    (void)timer;
    ui.toast_timer = NULL; /* a one-shot timer deletes itself */
    close_toast();
}

static void toast_cb(lv_event_t *event)
{
    (void)event;
    if (ui.toast_timer) lv_timer_ready(ui.toast_timer);
}

static void show_toast(void)
{
    const cJSON *toast = tr_toast(ui.remote);
    if (!toast) return;
    close_toast();
    const char *tone = string_of(toast, "tone");
    uint32_t color = strcmp(tone, "bad") == 0 ? C_BAD : strcmp(tone, "muted") == 0 ? C_FAINT : C_ACCENT;
    ui.toast = lv_obj_create(lv_display_get_layer_top(ui.display));
    lv_obj_remove_style_all(ui.toast);
    lv_obj_add_style(ui.toast, &ui.chip, 0);
    lv_obj_set_style_border_color(ui.toast, lv_color_hex(color), 0);
    lv_obj_remove_flag(ui.toast, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_size(ui.toast, ui.width - 2 * ui.m.margin, LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(ui.toast, LV_FLEX_FLOW_COLUMN);
    lv_obj_update_layout(ui.screen);
    int32_t above = lv_obj_has_flag(ui.footer, LV_OBJ_FLAG_HIDDEN) ? 0 : lv_obj_get_height(ui.footer);
    lv_obj_align(ui.toast, LV_ALIGN_BOTTOM_MID, 0, -above - ui.m.margin);
    uint32_t ink = strcmp(tone, "muted") == 0 ? C_MUTED : color;
    lv_obj_t *title = label(ui.toast, ui.m.body, ink, string_of(toast, "title"));
    lv_obj_set_width(title, LV_PCT(100));
    lv_label_set_long_mode(title, LV_LABEL_LONG_WRAP);
    const char *text = string_of(toast, "body");
    if (text[0]) {
        lv_obj_t *body = label(ui.toast, ui.m.small, C_TEXT, text);
        lv_obj_set_width(body, LV_PCT(100));
        lv_label_set_long_mode(body, LV_LABEL_LONG_WRAP);
    }
    lv_obj_add_event_cb(ui.toast, toast_cb, LV_EVENT_CLICKED, NULL);
    ui.toast_timer = lv_timer_create(toast_timer_cb, TOAST_MS, NULL);
    lv_timer_set_repeat_count(ui.toast_timer, 1);
    tr_clear_toast(ui.remote);
}

static void close_inputs(void)
{
    if (ui.inputs) lv_obj_delete_async(ui.inputs);
    ui.inputs = NULL;
    ui.input_ids.used = 0;
}

static void input_cb(lv_event_t *event)
{
    const char *value = lv_event_get_user_data(event);
    if (!ui.remote) return;
    if (value) tr_input(ui.remote, value);
    tr_clear_inputs(ui.remote);
    close_inputs();
}

static lv_obj_t *list_item(lv_obj_t *parent, const char *text, bool in_use, const char *value)
{
    lv_obj_t *item = key_button(parent, LV_PCT(100), ui.m.key_h);
    lv_obj_set_style_pad_hor(item, ui.m.margin, 0);
    lv_obj_t *name = label(item, ui.m.body, C_TEXT, text);
    lv_label_set_long_mode(name, LV_LABEL_LONG_DOT);
    lv_obj_set_width(name, LV_PCT(in_use ? 70 : 100));
    lv_obj_align(name, LV_ALIGN_LEFT_MID, 0, 0);
    if (in_use) {
        lv_obj_t *mark = label(item, ui.m.small, C_OK, LV_SYMBOL_OK " in use");
        lv_obj_align(mark, LV_ALIGN_RIGHT_MID, 0, 0);
    }
    lv_obj_add_event_cb(item, input_cb, LV_EVENT_CLICKED, (void *)value);
    return item;
}

static void show_inputs(void)
{
    const cJSON *inputs = tr_inputs(ui.remote);
    if (ui.inputs) {
        lv_obj_delete(ui.inputs);
        ui.inputs = NULL;
        ui.input_ids.used = 0;
    }
    if (!inputs) return;
    lv_obj_t *shade = lv_obj_create(lv_display_get_layer_top(ui.display));
    ui.inputs = shade;
    lv_obj_remove_style_all(shade);
    lv_obj_set_size(shade, LV_PCT(100), LV_PCT(100));
    lv_obj_set_style_bg_color(shade, lv_color_hex(C_BG), 0);
    lv_obj_set_style_bg_opa(shade, LV_OPA_70, 0);
    lv_obj_remove_flag(shade, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *panel = lv_obj_create(shade);
    lv_obj_remove_style_all(panel);
    lv_obj_add_style(panel, &ui.chip, 0);
    lv_obj_set_style_border_color(panel, lv_color_hex(C_LINE), 0);
    lv_obj_set_style_bg_color(panel, lv_color_hex(C_SURFACE), 0);
    lv_obj_set_size(panel, ui.width - 2 * ui.m.margin, LV_SIZE_CONTENT);
    lv_obj_center(panel);
    lv_obj_set_flex_flow(panel, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(panel, ui.m.gap, 0);
    lv_obj_remove_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

    lv_obj_t *title = label(panel, ui.m.title, C_TEXT, string_of(inputs, "title"));
    lv_obj_set_width(title, LV_PCT(100));
    lv_label_set_long_mode(title, LV_LABEL_LONG_DOT);
    lv_obj_t *list = box(panel);
    lv_obj_add_flag(list, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_scroll_dir(list, LV_DIR_VER);
    lv_obj_set_size(list, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_flex_flow(list, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_style_pad_row(list, ui.m.gap, 0);
    const cJSON *option;
    cJSON_ArrayForEach(option, cJSON_GetObjectItemCaseSensitive(inputs, "options")) {
        const char *value = keep(&ui.input_ids, string_of(option, "value"));
        if (!value) break;
        list_item(list, string_of(option, "label"), true_of(option, "inUse"), value);
    }
    lv_obj_t *close = list_item(panel, "Close", false, NULL);
    lv_obj_set_style_text_color(lv_obj_get_child(close, 0), lv_color_hex(C_MUTED), 0);
    /* A list longer than the screen scrolls between the title and Close. */
    lv_obj_update_layout(panel);
    int32_t over = lv_obj_get_height(panel) - (ui.height - 2 * ui.m.margin);
    if (over > 0) lv_obj_set_height(list, lv_obj_get_height(list) - over);
}

/* ---- updates ------------------------------------------------------------- */

static void show_screen(void)
{
    static const char *const body_parts[] = { "hidden", "remote", "pad", "rows", "beside", "channel", "buttons",
                                              "notice", "problem", "locked" };
    static const char *const bubble_parts[] = { "bubbles", "selected", "locked" };
    const cJSON *screen = tr_screen(ui.remote);
    if (!screen) {
        if (!ui.body_empty) show_waiting();
        return;
    }
    show_rooms(screen);
    show_status(screen);
    uint32_t hash = hash_members(screen, bubble_parts, sizeof bubble_parts / sizeof bubble_parts[0]);
    if (hash != ui.bubbles_hash || ui.body_empty) build_bubbles(screen);
    ui.bubbles_hash = hash;
    /* The bubbles count for the body only through inert, which blocks its keys. */
    hash = hash_members(screen, body_parts, sizeof body_parts / sizeof body_parts[0]) ^ (blocked(screen) ? 1u : 0u);
    if (hash != ui.body_hash || ui.body_empty) build_body(screen);
    ui.body_hash = hash;
    show_all_off(screen);
    block_footer(screen);
}

static void update_cb(lv_timer_t *timer)
{
    unsigned what = ui.pending;
    ui.pending = 0;
    lv_timer_pause(timer);
    if (what & TR_CHANGED_LINK) show_link();
    if (what & (TR_CHANGED_SCREEN | TR_CHANGED_LINK)) show_screen();
    if (what & TR_CHANGED_AUDIO) show_audio();
    if (what & TR_CHANGED_TOAST) show_toast();
    if (what & TR_CHANGED_INPUTS) show_inputs();
}

void tu_changed(unsigned what)
{
    if (!ui.update) return;
    ui.pending |= what;
    lv_timer_resume(ui.update);
    lv_timer_ready(ui.update);
}

void tu_battery(int percent, bool is_charging)
{
    battery = percent < 0 ? -1 : percent > 100 ? 100 : percent;
    charging = is_charging;
    if (ui.battery_mark) show_battery();
}

/* ---- building the screen ------------------------------------------------- */

static lv_obj_t *arrow(lv_obj_t *parent, const char *symbol, int step)
{
    lv_obj_t *obj = lv_button_create(parent);
    lv_obj_remove_style_all(obj);
    lv_obj_set_size(obj, 32, LV_SIZE_CONTENT);
    lv_obj_set_ext_click_area(obj, 8);
    lv_obj_t *mark = label(obj, ui.m.body, C_MUTED, symbol);
    lv_obj_center(mark);
    lv_obj_add_event_cb(obj, room_cb, LV_EVENT_CLICKED, (void *)(intptr_t)step);
    return obj;
}

static void make_header(lv_obj_t *parent)
{
    lv_obj_t *top = box(parent);
    lv_obj_add_flag(top, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_remove_flag(top, LV_OBJ_FLAG_GESTURE_BUBBLE); /* swipes end here, not on the screen */
    lv_obj_set_size(top, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(top, ui.m.compact ? ui.m.margin : ui.m.margin / 2, 0);
    lv_obj_set_style_pad_top(top, ui.m.gap, 0);
    lv_obj_set_flex_flow(top, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(top, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    ui.room_prev = arrow(top, LV_SYMBOL_LEFT, -1);
    ui.room_label = label(top, ui.m.title, C_TEXT, "");
    lv_obj_set_flex_grow(ui.room_label, 1);
    lv_obj_set_style_text_align(ui.room_label, ui.m.compact ? LV_TEXT_ALIGN_LEFT : LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_long_mode(ui.room_label, LV_LABEL_LONG_DOT);
    ui.room_next = arrow(top, LV_SYMBOL_RIGHT, 1);
    lv_obj_add_event_cb(top, room_cb, LV_EVENT_CLICKED, (void *)(intptr_t)1);
    lv_obj_add_event_cb(top, room_cb, LV_EVENT_GESTURE, NULL);

    lv_obj_t *status = box(parent);
    lv_obj_set_size(status, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(status, ui.m.margin, 0);
    lv_obj_set_flex_flow(status, LV_FLEX_FLOW_ROW);
    lv_obj_set_flex_align(status, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_set_style_pad_column(status, 6, 0);
    ui.led = led_dot(status, ui.m.compact ? 6 : 8);
    ui.short_label = label(status, ui.m.compact ? ui.m.small : ui.m.body, C_MUTED, "");
    lv_obj_set_flex_grow(ui.short_label, 1);
    lv_label_set_long_mode(ui.short_label, LV_LABEL_LONG_DOT);
    ui.link_mark = label(status, ui.m.body, C_BUSY, "");
    lv_obj_add_flag(ui.link_mark, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_ext_click_area(ui.link_mark, 16);
    lv_obj_add_event_cb(ui.link_mark, link_cb, LV_EVENT_CLICKED, NULL);
    ui.battery_mark = label(status, ui.m.compact ? ui.m.small : ui.m.body, C_MUTED, "");
}

static void make_bubbles(lv_obj_t *parent)
{
    ui.bubbles = box(parent);
    lv_obj_set_size(ui.bubbles, LV_PCT(100), LV_SIZE_CONTENT);
    lv_obj_set_style_pad_hor(ui.bubbles, ui.m.margin, 0);
    lv_obj_set_style_pad_ver(ui.bubbles, ui.m.gap, 0);
    lv_obj_set_style_pad_gap(ui.bubbles, ui.m.gap, 0);
    if (ui.m.compact) {
        /* No touch to scroll with: the bubbles wrap. */
        lv_obj_set_flex_flow(ui.bubbles, LV_FLEX_FLOW_ROW_WRAP);
    } else {
        lv_obj_add_flag(ui.bubbles, LV_OBJ_FLAG_SCROLLABLE);
        lv_obj_set_scroll_dir(ui.bubbles, LV_DIR_HOR);
        lv_obj_set_scrollbar_mode(ui.bubbles, LV_SCROLLBAR_MODE_OFF);
        lv_obj_set_flex_flow(ui.bubbles, LV_FLEX_FLOW_ROW);
    }
}

static void make_body(lv_obj_t *parent)
{
    ui.body = box(parent);
    lv_obj_add_flag(ui.body, LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_CLICKABLE);
    lv_obj_set_scroll_dir(ui.body, LV_DIR_VER);
    lv_obj_set_scrollbar_mode(ui.body, LV_SCROLLBAR_MODE_ACTIVE);
    lv_obj_set_width(ui.body, LV_PCT(100));
    lv_obj_set_flex_grow(ui.body, 1);
    lv_obj_set_style_pad_hor(ui.body, ui.m.margin, 0);
    lv_obj_set_style_pad_ver(ui.body, ui.m.gap, 0);
    lv_obj_set_style_pad_row(ui.body, ui.m.gap, 0);
    lv_obj_set_flex_flow(ui.body, LV_FLEX_FLOW_COLUMN);
    lv_obj_set_flex_align(ui.body, LV_FLEX_ALIGN_START, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);
    lv_obj_add_event_cb(ui.body, link_cb, LV_EVENT_CLICKED, NULL);
}

/* Forgets a previous screen: the simulator builds one again over a new remote. */
static void reset(void)
{
    stop_repeat();
    close_toast();
    if (ui.inputs) lv_obj_delete(ui.inputs);
    if (ui.update) lv_timer_delete(ui.update);
    if (ui.screen) lv_obj_clean(ui.screen);
    lv_style_t *styles[] = { &ui.key, &ui.key_pressed, &ui.key_lit, &ui.key_disabled, &ui.bubble,
                             &ui.bubble_selected, &ui.chip };
    for (size_t i = 0; i < sizeof styles / sizeof styles[0]; i++) lv_style_reset(styles[i]);
    memset(&ui, 0, sizeof ui);
}

void tu_create(lv_display_t *display, tr_t *remote, const tu_keys_t *keys)
{
    reset();
    ui.display = display;
    ui.remote = remote;
    if (keys) ui.keys = *keys;
    ui.width = lv_display_get_horizontal_resolution(display);
    ui.height = lv_display_get_vertical_resolution(display);
    metrics_for(ui.width, ui.height);
    init_styles();

    ui.screen = lv_display_get_screen_active(display);
    lv_obj_clean(ui.screen);
    lv_obj_remove_style_all(ui.screen);
    lv_obj_remove_flag(ui.screen, LV_OBJ_FLAG_SCROLLABLE);
    lv_obj_set_style_bg_color(ui.screen, lv_color_hex(C_BG), 0);
    lv_obj_set_style_bg_opa(ui.screen, LV_OPA_COVER, 0);
    lv_obj_set_style_text_color(ui.screen, lv_color_hex(C_TEXT), 0);
    lv_obj_set_style_text_font(ui.screen, ui.m.body, 0);
    lv_obj_set_flex_flow(ui.screen, LV_FLEX_FLOW_COLUMN);

    make_header(ui.screen);
    make_bubbles(ui.screen);
    make_body(ui.screen);
    make_footer(ui.screen);

    if (!remote) return; /* asleep: the frame and nothing in it */
    ui.update = lv_timer_create(update_cb, 0, NULL);
    lv_timer_pause(ui.update);
    show_battery();
    tu_changed(TR_CHANGED_SCREEN | TR_CHANGED_AUDIO | TR_CHANGED_TOAST | TR_CHANGED_INPUTS | TR_CHANGED_LINK);
}
