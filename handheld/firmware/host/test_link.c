/* thermite_link against handheld/vectors.json, the frames handheld/link.js seals. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "thermite_link.h"

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

static size_t unhex(const char *text, uint8_t *out, size_t capacity)
{
    size_t length = strlen(text) / 2;
    if (length > capacity) return 0;
    for (size_t i = 0; i < length; i++) sscanf(text + 2 * i, "%2hhx", &out[i]);
    return length;
}

static char *slurp(const char *path)
{
    FILE *file = fopen(path, "rb");
    if (!file) return NULL;
    fseek(file, 0, SEEK_END);
    long size = ftell(file);
    rewind(file);
    char *text = malloc((size_t)size + 1);
    if (text && fread(text, 1, (size_t)size, file) != (size_t)size) {
        free(text);
        text = NULL;
    }
    if (text) text[size] = '\0';
    fclose(file);
    return text;
}

static const cJSON *frame_named(const cJSON *frames, const char *name)
{
    const cJSON *item;
    cJSON_ArrayForEach(item, frames) {
        if (strcmp(cJSON_GetObjectItem(item, "name")->valuestring, name) == 0) return item;
    }
    return NULL;
}

static int number(const cJSON *item, const char *name)
{
    return cJSON_GetObjectItem(item, name)->valueint;
}

static const char *text(const cJSON *item, const char *name)
{
    return cJSON_GetObjectItem(item, name)->valuestring;
}

static uint64_t session_of(const cJSON *item)
{
    return strtoull(text(item, "session"), NULL, 16);
}

static uint8_t key[TL_KEY_LEN];
static uint8_t frame[TL_FRAME_MAX], expected[TL_FRAME_MAX], plain[TL_FRAME_MAX];

/* Every vector opens to its fields and plaintext, and seals back to its bytes. */
static void vectors(const cJSON *frames)
{
    const cJSON *item;
    cJSON_ArrayForEach(item, frames) {
        const char *name = text(item, "name");
        size_t length = unhex(text(item, "frame"), expected, sizeof expected);
        tl_header_t header;
        int opened = tl_open(key, (tl_direction_t)number(item, "direction"), expected, length, &header, plain,
                             sizeof plain);
        const char *wanted = text(item, "plaintext");
        CHECK(opened == (int)strlen(wanted) && memcmp(plain, wanted, (size_t)opened) == 0, "%s: plaintext", name);
        CHECK(header.type == number(item, "type") && header.message == number(item, "message")
              && header.index == number(item, "index") && header.count == number(item, "count")
              && header.counter == (uint32_t)cJSON_GetObjectItem(item, "counter")->valuedouble
              && header.session == session_of(item), "%s: header", name);
        size_t sealed = tl_seal(key, (tl_direction_t)number(item, "direction"), &header, (const uint8_t *)wanted,
                                strlen(wanted), frame, sizeof frame);
        CHECK(sealed == length && memcmp(frame, expected, length) == 0, "%s: sealed bytes differ", name);
    }
}

static void rejects(const cJSON *list)
{
    const cJSON *item;
    cJSON_ArrayForEach(item, list) {
        size_t length = unhex(text(item, "frame"), expected, sizeof expected);
        tl_header_t header;
        CHECK(tl_open(key, (tl_direction_t)number(item, "direction"), expected, length, &header, plain,
                      sizeof plain) < 0, "%s: accepted", text(item, "name"));
    }
}

struct capture {
    uint8_t frames[4][TL_FRAME_MAX];
    size_t lengths[4];
    int count;
};

static void capture(void *context, const uint8_t *bytes, size_t length)
{
    struct capture *into = context;
    if (into->count < 4) {
        memcpy(into->frames[into->count], bytes, length);
        into->lengths[into->count++] = length;
    }
}

static tl_event_t feed(tl_remote_t *remote, tl_assembler_t *assembler, const cJSON *item, const char **message,
                       size_t *size)
{
    size_t length = unhex(text(item, "frame"), expected, sizeof expected);
    return tl_remote_receive(remote, assembler, key, expected, length, message, size);
}

/* The remote's end through one session: HELLO, SYNC, a key, its ack, a screen in parts. */
static void session(const cJSON *frames)
{
    static tl_assembler_t assembler;
    tl_remote_t remote;
    tl_remote_reset(&remote);
    const cJSON *hello = frame_named(frames, "hello");
    const char *status = text(hello, "plaintext");
    size_t length = tl_remote_hello(&remote, key, session_of(hello), status, strlen(status), frame, sizeof frame);
    size_t wanted = unhex(text(hello, "frame"), expected, sizeof expected);
    CHECK(length == wanted && memcmp(frame, expected, wanted) == 0, "hello bytes differ");

    const char *message = NULL;
    size_t size = 0;
    struct capture none = { .count = 0 };
    CHECK(tl_remote_send(&remote, key, "{}", 2, capture, &none) == -1 && none.count == 0, "sent before SYNC");
    CHECK(feed(&remote, &assembler, frame_named(frames, "ack"), &message, &size) == TL_NOTHING, "data before SYNC");
    CHECK(feed(&remote, &assembler, frame_named(frames, "sync"), &message, &size) == TL_SYNCED, "sync refused");
    CHECK(remote.session == session_of(frame_named(frames, "sync")), "session not adopted");
    CHECK(feed(&remote, &assembler, frame_named(frames, "sync"), &message, &size) == TL_NOTHING, "sync replayed");

    const cJSON *key_frame = frame_named(frames, "key");
    struct capture sent = { .count = 0 };
    const char *press = text(key_frame, "plaintext");
    CHECK(tl_remote_send(&remote, key, press, strlen(press), capture, &sent) == 1, "key not sent");
    wanted = unhex(text(key_frame, "frame"), expected, sizeof expected);
    CHECK(sent.count == 1 && sent.lengths[0] == wanted && memcmp(sent.frames[0], expected, wanted) == 0,
          "key bytes differ");

    const cJSON *ack = frame_named(frames, "ack");
    CHECK(feed(&remote, &assembler, ack, &message, &size) == TL_MESSAGE
          && size == strlen(text(ack, "plaintext")) && strcmp(message, text(ack, "plaintext")) == 0, "ack");
    CHECK(feed(&remote, &assembler, ack, &message, &size) == TL_NOTHING, "ack replayed");

    static char whole[TL_MESSAGE_MAX + 1];
    whole[0] = '\0';
    tl_event_t last = TL_NOTHING;
    const cJSON *item;
    cJSON_ArrayForEach(item, frames) {
        if (strncmp(text(item, "name"), "screen part", 11) != 0) continue;
        strcat(whole, text(item, "plaintext"));
        last = feed(&remote, &assembler, item, &message, &size);
    }
    CHECK(last == TL_MESSAGE && size == strlen(whole) && strcmp(message, whole) == 0, "screen not gathered");

    tl_remote_t fresh;
    tl_remote_reset(&fresh);
    CHECK(feed(&fresh, &assembler, frame_named(frames, "sync"), &message, &size) == TL_NOTHING,
          "sync adopted without a hello");
}

int main(int argc, char **argv)
{
    char *json = argc > 1 ? slurp(argv[1]) : NULL;
    cJSON *root = json ? cJSON_Parse(json) : NULL;
    if (!root) {
        fprintf(stderr, "usage: test_link handheld/vectors.json\n");
        return 2;
    }
    unhex(text(root, "key"), key, sizeof key);
    vectors(cJSON_GetObjectItem(root, "frames"));
    rejects(cJSON_GetObjectItem(root, "rejects"));
    session(cJSON_GetObjectItem(root, "frames"));
    cJSON_Delete(root);
    free(json);
    if (failures) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    puts("link: all vectors pass");
    return 0;
}
