/*
 * thermite-remote-sim: the handheld remote on the host, from the same
 * thermite_remote and thermite_ui the device runs.
 *
 * Interactive (needs SDL2):
 *   thermite-remote-sim --connect PATH --key-file FILE [--mac 02:00:00:00:00:01]
 *                       [--size 240x320] [--board NAME] [--keys LIST]
 *                       [--shot FILE] [--exit-after MS] [--script STEPS]
 *   connects to the bridge's Unix socket and plays both the base station (its
 *   lines, handheld/PROTOCOL.md) and the one remote it relays for. Keys:
 *   arrows, Enter and Backspace are the D-pad, OK and Back; + - m volume and
 *   Mute; p Play; [ ] channel down and up; s sleeps and wakes again, as deep
 *   sleep does: a new remote over the same persisted state. The mouse touches.
 *   --shot writes the screen as a PNG on exit; --exit-after ends the run.
 *   --script types keys unattended: "MS:KEY,..." presses an SDL key name
 *   (Up, Return, s, ...) MS after start; +KEY holds it down and -KEY lets go;
 *   down@X/Y, move@X/Y and up@X/Y press, drag and release the mouse there.
 *
 * Headless:
 *   thermite-remote-sim --render DIR --size WxH [--keys LIST] [--out DIR] [--check]
 *   renders each DIR/ *.json, in name order, to <name>.png. A fixture is one
 *   bridge message (or an array of them) applied with tr_apply to a fresh,
 *   connected remote; an optional "sim" member sets {"link": "connected" |
 *   "connecting" | "offline", "battery": 0-100 or -1, "charging": bool}.
 *   --check fails on any LVGL warning, any character the fonts cannot draw,
 *   or a blank render.
 *
 * LIST names the board's hard keys: dpad,volume,channel,playback.
 */
#define _DEFAULT_SOURCE

#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "lvgl.h"
#include "png.h"
#include "thermite_link.h"
#include "thermite_remote.h"
#include "thermite_ui.h"

#if LV_USE_SDL
#include LV_SDL_INCLUDE_PATH
#endif

#define LINE_MAX_BYTES 4096
#define FIXTURES_MAX 128
#define BASE_MAC "02:00:00:00:00:fe"

typedef struct {
    const char *connect, *key_file, *render, *out, *board, *shot, *script;
    char mac[18];
    int width, height;
    int exit_after;
    bool check;
    tu_keys_t keys;
} options_t;

static struct {
    options_t options;
    uint8_t key[TL_KEY_LEN];
    tr_persist_t persist;
    tr_identity_t identity;
    tr_platform_t platform;
    tr_t *remote;
    lv_display_t *display;
    int fd;
    int warnings;
    uint64_t seed;
    /* headless: the last frame the remote sent, to answer its HELLO */
    uint8_t frame[TL_FRAME_MAX];
    size_t frame_length;
} sim = { .fd = -1 };

/* ---- the platform ---------------------------------------------------------- */

static uint32_t clock_ms(void)
{
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (uint32_t)((uint64_t)now.tv_sec * 1000u + (uint64_t)now.tv_nsec / 1000000u);
}

static uint32_t now_ms(void *context)
{
    (void)context;
    return clock_ms();
}

/* splitmix64: seeded from the clock interactively, fixed for renders that repeat. */
static uint64_t random64(void *context)
{
    (void)context;
    uint64_t z = (sim.seed += 0x9e3779b97f4a7c15u);
    z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9u;
    z = (z ^ (z >> 27)) * 0x94d049bb133111ebu;
    return z ^ (z >> 31);
}

static void set_channel(void *context, uint8_t channel)
{
    (void)context;
    (void)channel; /* the simulated radio reaches the base on every channel */
}

static void changed(void *context, unsigned what)
{
    (void)context;
    if ((what & TR_CHANGED_SCREEN) && tr_screen(sim.remote) && sim.options.connect) {
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(tr_screen(sim.remote), "id");
        fprintf(stderr, "sim: screen %s\n", cJSON_IsString(id) ? id->valuestring : "?");
    }
    if (what & TR_CHANGED_LINK && sim.options.connect) {
        static const char *const names[] = { "offline", "connecting", "connected" };
        fprintf(stderr, "sim: link %s\n", names[tr_link(sim.remote)]);
    }
    tu_changed(what);
}

static void log_cb(lv_log_level_t level, const char *text)
{
    if (level >= LV_LOG_LEVEL_WARN) sim.warnings++;
    fputs(text, stderr);
}

/* ---- options ------------------------------------------------------------- */

static int usage(void)
{
    fputs("usage: thermite-remote-sim --connect PATH --key-file FILE [--mac MAC] [--size WxH] [--board NAME]\n"
          "                           [--keys LIST] [--shot FILE] [--exit-after MS] [--script STEPS]\n"
          "       thermite-remote-sim --render DIR --size WxH [--keys LIST] [--out DIR] [--check]\n"
          "LIST: dpad,volume,channel,playback\n", stderr);
    return 2;
}

static bool parse_keys(const char *list, tu_keys_t *keys)
{
    char copy[128];
    snprintf(copy, sizeof copy, "%s", list);
    for (char *save = NULL, *name = strtok_r(copy, ",", &save); name; name = strtok_r(NULL, ",", &save)) {
        if (strcmp(name, "dpad") == 0) keys->dpad = true;
        else if (strcmp(name, "volume") == 0) keys->volume = true;
        else if (strcmp(name, "channel") == 0) keys->channel = true;
        else if (strcmp(name, "playback") == 0) keys->playback = true;
        else return false;
    }
    return true;
}

static bool parse(int argc, char **argv, options_t *options)
{
    *options = (options_t){ .board = "sim", .width = 240, .height = 320, .mac = "02:00:00:00:00:01" };
    for (int i = 1; i < argc; i++) {
        const char *name = argv[i], *value = i + 1 < argc ? argv[i + 1] : NULL;
        if (strcmp(name, "--check") == 0) {
            options->check = true;
            continue;
        }
        if (!value) return false;
        i++;
        if (strcmp(name, "--connect") == 0) options->connect = value;
        else if (strcmp(name, "--key-file") == 0) options->key_file = value;
        else if (strcmp(name, "--render") == 0) options->render = value;
        else if (strcmp(name, "--out") == 0) options->out = value;
        else if (strcmp(name, "--board") == 0) options->board = value;
        else if (strcmp(name, "--shot") == 0) options->shot = value;
        else if (strcmp(name, "--exit-after") == 0) options->exit_after = atoi(value);
        else if (strcmp(name, "--script") == 0) options->script = value;
        else if (strcmp(name, "--mac") == 0 && strlen(value) == 17) {
            for (int k = 0; k < 17; k++) options->mac[k] = (char)tolower((unsigned char)value[k]); /* MACs are lower-case */
        } else if (strcmp(name, "--size") == 0) {
            if (sscanf(value, "%dx%d", &options->width, &options->height) != 2 || options->width < 64
                || options->height < 64 || options->width > 1024 || options->height > 1024) {
                return false;
            }
        } else if (strcmp(name, "--keys") == 0) {
            if (!parse_keys(value, &options->keys)) return false;
        } else {
            return false;
        }
    }
    return (options->connect && options->key_file) != (options->render != NULL);
}

static void init_lvgl(void)
{
    lv_init();
    lv_tick_set_cb(clock_ms);
    lv_log_register_print_cb(log_cb);
}

/* ---- PNGs ---------------------------------------------------------------- */

/* The display's RGB565 frame as a PNG; *lit counts pixels that are not the black background. */
static bool write_frame(const char *path, const uint16_t *frame, int width, int height, size_t *lit, size_t *colors)
{
    size_t count = (size_t)width * height;
    uint8_t *rgb = malloc(count * 3);
    static uint8_t seen[65536 / 8];
    if (!rgb) return false;
    memset(seen, 0, sizeof seen);
    *lit = 0;
    *colors = 0;
    for (size_t i = 0; i < count; i++) {
        uint16_t c = frame[i];
        uint8_t r = c >> 11 & 0x1f, g = c >> 5 & 0x3f, b = c & 0x1f;
        rgb[3 * i] = (uint8_t)(r << 3 | r >> 2);
        rgb[3 * i + 1] = (uint8_t)(g << 2 | g >> 4);
        rgb[3 * i + 2] = (uint8_t)(b << 3 | b >> 2);
        if (c) (*lit)++;
        if (!(seen[c / 8] & 1 << c % 8)) (*colors)++;
        seen[c / 8] |= (uint8_t)(1 << c % 8);
    }
    bool ok = png_write(path, rgb, (uint32_t)width, (uint32_t)height);
    free(rgb);
    return ok;
}

/* ---- headless renders ---------------------------------------------------- */

static uint16_t *framebuffer;

static void flush_cb(lv_display_t *display, const lv_area_t *area, uint8_t *pixels)
{
    (void)area;
    (void)pixels; /* direct mode: the framebuffer is the draw buffer */
    lv_display_flush_ready(display);
}

static void capture(void *context, const uint8_t *frame, size_t length)
{
    (void)context;
    memcpy(sim.frame, frame, length);
    sim.frame_length = length;
}

/* Plays the bridge for one handshake: the remote's HELLO, answered with a SYNC. */
static void handshake(void)
{
    sim.frame_length = 0;
    tr_poll(sim.remote);
    tl_header_t header;
    uint8_t plain[TL_FRAME_MAX];
    if (!sim.frame_length || tl_open(sim.key, TL_FROM_REMOTE, sim.frame, sim.frame_length, &header, plain,
                                     sizeof plain) < 0 || header.type != TL_HELLO) {
        return;
    }
    char hex[17], text[64];
    tl_hex(header.session, hex);
    snprintf(text, sizeof text, "{\"hello\":\"%s\"}", hex);
    tl_header_t sync = { .type = TL_SYNC, .count = 1, .session = 0x5eed5eed5eed5eedu, .counter = 0 };
    uint8_t frame[TL_FRAME_MAX];
    size_t length = tl_seal(sim.key, TL_FROM_BRIDGE, &sync, (const uint8_t *)text, strlen(text), frame, sizeof frame);
    tr_receive(sim.remote, frame, length);
}

static int utf8_next(const char *text, size_t *at)
{
    const unsigned char *s = (const unsigned char *)text + *at;
    int extra = s[0] >= 0xF0 ? 3 : s[0] >= 0xE0 ? 2 : s[0] >= 0xC0 ? 1 : 0;
    uint32_t code = extra == 3 ? s[0] & 7u : extra == 2 ? s[0] & 15u : extra == 1 ? s[0] & 31u : s[0];
    for (int i = 1; i <= extra && s[i]; i++) code = code << 6 | (s[i] & 63u);
    *at += (size_t)extra + 1;
    return (int)code;
}

/* Counts characters in shown labels that their font cannot draw, naming each. */
static int missing_glyphs(lv_obj_t *obj, const char *fixture)
{
    int missing = 0;
    if (lv_obj_has_flag(obj, LV_OBJ_FLAG_HIDDEN)) return 0;
    if (lv_obj_check_type(obj, &lv_label_class)) {
        const char *text = lv_label_get_text(obj);
        const lv_font_t *font = lv_obj_get_style_text_font(obj, LV_PART_MAIN);
        for (size_t at = 0; text[at];) {
            int code = utf8_next(text, &at);
            lv_font_glyph_dsc_t glyph;
            if (code == '\n' || lv_font_get_glyph_dsc(font, &glyph, (uint32_t)code, 0)) continue;
            fprintf(stderr, "%s: no glyph for U+%04X in \"%s\"\n", fixture, (unsigned)code, text);
            missing++;
        }
    }
    for (uint32_t i = 0; i < lv_obj_get_child_count(obj); i++) {
        missing += missing_glyphs(lv_obj_get_child(obj, (int32_t)i), fixture);
    }
    return missing;
}

static char *slurp(const char *path)
{
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    fseek(file, 0, SEEK_END);
    long size = ftell(file);
    rewind(file);
    char *text = size >= 0 ? malloc((size_t)size + 1) : NULL;
    if (text && fread(text, 1, (size_t)size, file) != (size_t)size) {
        free(text);
        text = NULL;
    }
    if (text) text[size] = '\0';
    fclose(file);
    return text;
}

static int by_name(const void *a, const void *b)
{
    return strcmp(*(char *const *)a, *(char *const *)b);
}

/* One fixture into a fresh remote; returns the number of problems --check counts. */
static int render_one(const char *dir, const char *name, const char *out)
{
    char path[1024], png[1024];
    snprintf(path, sizeof path, "%s/%s", dir, name);
    snprintf(png, sizeof png, "%s/%.*s.png", out, (int)(strlen(name) - 5), name);
    char *text = slurp(path);
    cJSON *fixture = text ? cJSON_Parse(text) : NULL;
    free(text);
    if (!fixture) {
        fprintf(stderr, "%s: not JSON\n", path);
        return 1;
    }
    const cJSON *options = cJSON_GetObjectItemCaseSensitive(fixture, "sim");
    const char *link = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(options, "link"));
    const cJSON *battery = cJSON_GetObjectItemCaseSensitive(options, "battery");

    tr_t *previous = sim.remote;
    memset(&sim.persist, 0, sizeof sim.persist);
    sim.seed = 1;
    sim.remote = tr_create(&sim.persist, sim.key, &sim.identity, &sim.platform);
    tu_battery(cJSON_IsNumber(battery) ? battery->valueint : 80,
               cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(options, "charging")));
    tu_create(sim.display, sim.remote, &sim.options.keys);
    tr_destroy(previous);
    if (!link || strcmp(link, "connected") == 0) handshake();
    else if (strcmp(link, "connecting") == 0) tr_poll(sim.remote);

    int warnings = sim.warnings;
    const cJSON *message = cJSON_IsArray(fixture) ? fixture->child : fixture;
    for (; message; message = cJSON_IsArray(fixture) ? message->next : NULL) {
        char *one = cJSON_PrintUnformatted(message);
        tr_apply(sim.remote, one);
        free(one);
    }
    cJSON_Delete(fixture);
    lv_timer_handler();
    lv_obj_invalidate(lv_display_get_screen_active(sim.display));
    lv_refr_now(sim.display);

    int problems = sim.warnings - warnings;
    problems += missing_glyphs(lv_display_get_screen_active(sim.display), name);
    problems += missing_glyphs(lv_display_get_layer_top(sim.display), name);
    size_t lit, colors;
    if (!write_frame(png, framebuffer, sim.options.width, sim.options.height, &lit, &colors)) {
        fprintf(stderr, "%s: cannot write\n", png);
        return problems + 1;
    }
    /* Blank or nearly so: text and keys light far more than this. */
    if (lit < (size_t)sim.options.width * sim.options.height / 200 || colors < 16) {
        fprintf(stderr, "%s: blank render (%zu lit pixels, %zu colors)\n", png, lit, colors);
        problems++;
    }
    printf("%s\n", png);
    return problems;
}

/* mkdir -p: existing directories are fine; a failure shows when writing. */
static void make_dirs(const char *path)
{
    char copy[1024];
    snprintf(copy, sizeof copy, "%s", path);
    for (char *at = copy + 1; *at; at++) {
        if (*at != '/') continue;
        *at = '\0';
        mkdir(copy, 0777);
        *at = '/';
    }
    mkdir(copy, 0777);
}

static int run_render(void)
{
    const options_t *o = &sim.options;
    const char *out = o->out ? o->out : o->render;
    make_dirs(out);
    char *names[FIXTURES_MAX];
    int count = 0;
    DIR *dir = opendir(o->render);
    if (!dir) {
        fprintf(stderr, "%s: %s\n", o->render, strerror(errno));
        return 1;
    }
    for (struct dirent *entry; (entry = readdir(dir)) && count < FIXTURES_MAX;) {
        size_t length = strlen(entry->d_name);
        if (length > 5 && strcmp(entry->d_name + length - 5, ".json") == 0) names[count++] = strdup(entry->d_name);
    }
    closedir(dir);
    qsort(names, (size_t)count, sizeof names[0], by_name);

    init_lvgl();
    sim.display = lv_display_create(o->width, o->height);
    size_t size = (size_t)o->width * o->height * 2;
    framebuffer = calloc(1, size);
    lv_display_set_color_format(sim.display, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(sim.display, framebuffer, NULL, (uint32_t)size, LV_DISPLAY_RENDER_MODE_DIRECT);
    lv_display_set_flush_cb(sim.display, flush_cb);
    sim.platform = (tr_platform_t){ .send = capture, .set_channel = set_channel, .now_ms = now_ms,
                                    .random64 = random64, .changed = changed };

    int problems = 0;
    for (int i = 0; i < count; i++) {
        problems += render_one(o->render, names[i], out);
        free(names[i]);
    }
    if (count == 0) fprintf(stderr, "%s: no *.json fixtures\n", o->render);
    if (o->check && (problems || count == 0)) {
        fprintf(stderr, "render check: %d problem(s)\n", problems);
        return 1;
    }
    return 0;
}

/* ---- interactive ----------------------------------------------------------- */

#if LV_USE_SDL

/* ---- base64 -------------------------------------------------------------- */

static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void base64_encode(const uint8_t *in, size_t length, char *out)
{
    for (size_t i = 0; i < length; i += 3) {
        uint32_t chunk = (uint32_t)in[i] << 16 | (i + 1 < length ? (uint32_t)in[i + 1] << 8 : 0)
            | (i + 2 < length ? in[i + 2] : 0);
        *out++ = alphabet[chunk >> 18 & 63];
        *out++ = alphabet[chunk >> 12 & 63];
        *out++ = i + 1 < length ? alphabet[chunk >> 6 & 63] : '=';
        *out++ = i + 2 < length ? alphabet[chunk & 63] : '=';
    }
    *out = '\0';
}

/* The decoded length, or -1 for text that is not padded base64 or does not fit. */
static int base64_decode(const char *in, uint8_t *out, size_t capacity)
{
    size_t length = strlen(in), used = 0;
    if (length % 4) return -1;
    for (size_t i = 0; i < length; i += 4) {
        uint32_t chunk = 0;
        int pad = 0;
        for (int k = 0; k < 4; k++) {
            const char *at = in[i + k] == '=' ? NULL : strchr(alphabet, in[i + k]);
            if (in[i + k] == '=' && i + 4 == length && k >= 2) pad++;
            else if (!at || !in[i + k] || pad) return -1;
            chunk = chunk << 6 | (at ? (uint32_t)(at - alphabet) : 0);
        }
        for (int k = 0; k < 3 - pad; k++) {
            if (used == capacity) return -1;
            out[used++] = (uint8_t)(chunk >> (16 - 8 * k));
        }
    }
    return (int)used;
}

static bool read_key(const char *path, uint8_t key[TL_KEY_LEN])
{
    char text[80] = "";
    FILE *file = fopen(path, "r");
    if (!file) return false;
    bool ok = fgets(text, sizeof text, file) != NULL;
    fclose(file);
    text[strcspn(text, "\r\n")] = '\0';
    if (!ok || strlen(text) != 2 * TL_KEY_LEN) return false;
    for (int i = 0; i < TL_KEY_LEN; i++) {
        if (sscanf(text + 2 * i, "%2hhx", &key[i]) != 1) return false;
    }
    return true;
}

/* ---- the base's lines ----------------------------------------------------- */

static bool write_all(const char *text)
{
    size_t length = strlen(text);
    while (length && sim.fd >= 0) {
        ssize_t sent = write(sim.fd, text, length);
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) return false;
        text += sent;
        length -= (size_t)sent;
    }
    return true;
}

static void base_line(int channel)
{
    char line[96];
    snprintf(line, sizeof line, "thermite base 1 sim %s %d\n", BASE_MAC, channel);
    write_all(line);
}

/* The remote's radio: each frame goes to the bridge as the base received it. */
static void send_frame(void *context, const uint8_t *frame, size_t length)
{
    (void)context;
    char line[64 + (TL_FRAME_MAX + 2) / 3 * 4];
    int at = snprintf(line, sizeof line, "thermite rx %s -40 ", sim.options.mac);
    base64_encode(frame, length, line + at);
    strcat(line, "\n");
    write_all(line);
}

/* Deep sleep: SLEEPING waits for the sleep op's ack, ASLEEP has no remote at all. */
enum { AWAKE, SLEEPING, ASLEEP };
static int sleep_state;
static uint32_t sleep_until;

static void wake(void)
{
    sim.remote = tr_create(&sim.persist, sim.key, &sim.identity, &sim.platform);
    tu_create(sim.display, sim.remote, &sim.options.keys);
    tr_status(sim.remote, 80, false);
    tu_battery(80, false);
    tr_wake(sim.remote);
    sleep_state = AWAKE;
}

static void handle_line(char *line)
{
    char mac[32], data[LINE_MAX_BYTES];
    unsigned id;
    int channel;
    if (strcmp(line, "thermite hello") == 0) {
        base_line(1);
    } else if (sscanf(line, "thermite channel %d", &channel) == 1) {
        base_line(channel);
    } else if (sscanf(line, "thermite send %u %31s %4095s", &id, mac, data) == 3) {
        uint8_t frame[TL_FRAME_MAX];
        int length = base64_decode(data, frame, sizeof frame);
        bool ours = strcmp(mac, sim.options.mac) == 0 && length > 0 && sleep_state != ASLEEP;
        char answer[64];
        snprintf(answer, sizeof answer, "thermite sent %u %s\n", id, ours ? "ok" : "fail");
        write_all(answer);
        if (ours) tr_receive(sim.remote, frame, (size_t)length);
    } else if (strncmp(line, "thermite ", 9) == 0) {
        fprintf(stderr, "sim: ignored: %s\n", line);
    }
}

static char input[LINE_MAX_BYTES * 2];
static size_t input_used;

static bool read_lines(void)
{
    ssize_t got = read(sim.fd, input + input_used, sizeof input - input_used - 1);
    if (got == 0 || (got < 0 && errno != EINTR && errno != EAGAIN)) return false;
    if (got < 0) return true;
    input_used += (size_t)got;
    input[input_used] = '\0';
    char *start = input, *end;
    while ((end = strchr(start, '\n'))) {
        *end = '\0';
        if (end > start && end[-1] == '\r') end[-1] = '\0';
        handle_line(start);
        start = end + 1;
    }
    input_used -= (size_t)(start - input);
    memmove(input, start, input_used);
    if (input_used == sizeof input - 1) input_used = 0; /* an overlong line: drop it */
    return true;
}

/* The hard keys (PROTOCOL.md, Hard keys) on the host keyboard. */
static const struct {
    SDL_Keycode sym;
    const char *ids[2];
} hard_keys[] = {
    { SDLK_UP, { "up" } },           { SDLK_DOWN, { "down" } },         { SDLK_LEFT, { "left" } },
    { SDLK_RIGHT, { "right" } },     { SDLK_RETURN, { "ok" } },         { SDLK_KP_ENTER, { "ok" } },
    { SDLK_BACKSPACE, { "back" } },  { SDLK_PLUS, { "volume_up" } },    { SDLK_EQUALS, { "volume_up" } },
    { SDLK_KP_PLUS, { "volume_up" } }, { SDLK_MINUS, { "volume_down" } }, { SDLK_KP_MINUS, { "volume_down" } },
    { SDLK_m, { "mute" } },          { SDLK_p, { "play_pause", "play" } },
    { SDLK_LEFTBRACKET, { "channel_down" } }, { SDLK_RIGHTBRACKET, { "channel_up" } },
};

static int held = -1;
static uint32_t held_next;

static void hard_press(int index)
{
    size_t count = hard_keys[index].ids[1] ? 2 : 1;
    if (sim.remote) tr_key(sim.remote, hard_keys[index].ids, count);
}

/* A key went down or up, from the keyboard or --script. A held key whose button
 * repeats presses again after 420 ms, then every 150 ms. */
static void key_event(SDL_Keycode sym, bool down)
{
    if (!down) {
        if (held >= 0 && hard_keys[held].sym == sym) held = -1;
        return;
    }
    if (sleep_state != AWAKE) return;
    if (sym == SDLK_s) {
        held = -1;
        if (tr_sleep(sim.remote)) {
            sleep_state = SLEEPING;
            sleep_until = clock_ms() + 1000;
        }
        return;
    }
    for (int i = 0; i < (int)(sizeof hard_keys / sizeof hard_keys[0]); i++) {
        if (hard_keys[i].sym != sym) continue;
        hard_press(i);
        const cJSON *buttons = cJSON_GetObjectItemCaseSensitive(tr_screen(sim.remote), "buttons");
        const cJSON *spec = cJSON_GetObjectItemCaseSensitive(buttons, hard_keys[i].ids[0]);
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(spec, "repeat"))) {
            held = i;
            held_next = clock_ms() + 420;
        }
        return;
    }
}

static int key_watch(void *data, SDL_Event *event)
{
    (void)data;
    if ((event->type == SDL_KEYDOWN && !event->key.repeat) || event->type == SDL_KEYUP) {
        key_event(event->key.keysym.sym, event->type == SDL_KEYDOWN);
    }
    return 0;
}

/* The next --script step, pushed as SDL key events when its time comes. */
static const char *script_at;
static uint32_t script_start;

static void script_step(uint32_t clock)
{
    unsigned at;
    char name[32];
    int used = 0;
    if (!script_at || !*script_at || sscanf(script_at, "%u:%31[^,]%n", &at, name, &used) != 2) return;
    if ((int32_t)(clock - script_start - at) < 0) return;
    script_at += used + (script_at[used] == ',');
    fprintf(stderr, "sim: script %s\n", name);
    int x, y;
    char button[8];
    if (sscanf(name, "%7[a-z]@%d/%d", button, &x, &y) == 3) {
        Uint32 window = SDL_GetWindowID(SDL_RenderGetWindow(lv_sdl_window_get_renderer(sim.display)));
        SDL_Event event;
        if (strcmp(button, "move") == 0) {
            event.motion = (SDL_MouseMotionEvent){ .type = SDL_MOUSEMOTION, .windowID = window,
                                                   .state = SDL_BUTTON_LMASK, .x = x, .y = y };
        } else {
            bool down = strcmp(button, "down") == 0;
            event.button = (SDL_MouseButtonEvent){ .type = down ? SDL_MOUSEBUTTONDOWN : SDL_MOUSEBUTTONUP,
                                                   .windowID = window, .button = SDL_BUTTON_LEFT, .state = down,
                                                   .x = x, .y = y };
        }
        SDL_PushEvent(&event);
        return;
    }
    /* Keys go straight to key_event: whether SDL's event watchers see pushed
     * events differs between SDL2 and sdl2-compat over SDL3. */
    const char *key = name[0] == '+' || name[0] == '-' ? name + 1 : name;
    SDL_Keycode sym = SDL_GetKeyFromName(key);
    if (sym == SDLK_UNKNOWN) {
        fprintf(stderr, "sim: script: no key %s\n", key);
        return;
    }
    if (name[0] != '-') key_event(sym, true);
    if (name[0] != '+') key_event(sym, false);
}

static uint32_t timers(void)
{
    uint32_t clock = clock_ms(), wait = 10; /* SDL's events do not wake poll() */
    script_step(clock);
    if (held >= 0 && (int32_t)(clock - held_next) >= 0) {
        hard_press(held);
        held_next += 150;
    }
    if (sleep_state == SLEEPING && (tr_idle(sim.remote) || (int32_t)(clock - sleep_until) >= 0)) {
        fprintf(stderr, "sim: asleep\n");
        tu_create(sim.display, NULL, &sim.options.keys); /* nothing may reach the old remote */
        tr_destroy(sim.remote);
        sim.remote = NULL;
        sleep_state = ASLEEP;
        sleep_until = clock + 1500;
    } else if (sleep_state == ASLEEP && (int32_t)(clock - sleep_until) >= 0) {
        fprintf(stderr, "sim: awake\n");
        wake();
    }
    return wait;
}

static void shot(void)
{
    if (!sim.options.shot) return;
    lv_refr_now(sim.display);
    lv_draw_buf_t *buffer = lv_display_get_buf_active(sim.display);
    size_t lit, colors;
    if (buffer && write_frame(sim.options.shot, (const uint16_t *)buffer->data, sim.options.width,
                              sim.options.height, &lit, &colors)) {
        fprintf(stderr, "sim: wrote %s\n", sim.options.shot);
    }
}

static int run_interactive(void)
{
    const options_t *o = &sim.options;
    if (!read_key(o->key_file, sim.key)) {
        fprintf(stderr, "%s: want 64 hex digits\n", o->key_file);
        return 1;
    }
    struct sockaddr_un address = { .sun_family = AF_UNIX };
    snprintf(address.sun_path, sizeof address.sun_path, "%s", o->connect);
    sim.fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sim.fd < 0 || connect(sim.fd, (struct sockaddr *)&address, sizeof address) < 0) {
        fprintf(stderr, "%s: %s\n", o->connect, strerror(errno));
        return 1;
    }
    init_lvgl();
    sim.seed = ((uint64_t)time(NULL) << 32) ^ (uint64_t)getpid() ^ clock_ms();
    sim.display = lv_sdl_window_create(o->width, o->height);
    lv_display_set_default(sim.display);
    lv_sdl_window_set_title(sim.display, "thermite remote");
    lv_sdl_mouse_create();
    SDL_AddEventWatch(key_watch, NULL);
    sim.platform = (tr_platform_t){ .send = send_frame, .set_channel = set_channel, .now_ms = now_ms,
                                    .random64 = random64, .changed = changed };
    base_line(1);
    wake();

    uint32_t end = clock_ms() + (uint32_t)o->exit_after;
    script_at = o->script;
    script_start = clock_ms();
    for (;;) {
        uint32_t wait = lv_timer_handler();
        if (sim.remote) {
            uint32_t remote = tr_poll(sim.remote);
            if (remote < wait) wait = remote;
        }
        uint32_t other = timers();
        if (other < wait) wait = other;
        if (o->exit_after > 0 && (int32_t)(clock_ms() - end) >= 0) break;
        struct pollfd ready = { .fd = sim.fd, .events = POLLIN };
        int polled = poll(&ready, 1, (int)wait);
        if (polled > 0 && !read_lines()) {
            fprintf(stderr, "sim: the bridge closed the connection\n");
            break;
        }
    }
    shot();
    close(sim.fd);
    return 0;
}

#else

static int run_interactive(void)
{
    fputs("thermite-remote-sim: built without SDL2; only --render works\n", stderr);
    return 1;
}

#endif

int main(int argc, char **argv)
{
    if (!parse(argc, argv, &sim.options)) return usage();
    sim.identity = (tr_identity_t){ .board = sim.options.board, .fw = "sim",
                                    .width = (uint16_t)sim.options.width, .height = (uint16_t)sim.options.height,
                                    .channel = 1 };
    return sim.options.render ? run_render() : run_interactive();
}
