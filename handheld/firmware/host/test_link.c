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

/* A new SYNC discards any incomplete multi-part message left from the previous session. */
static void stale_assembly(void)
{
    static tl_assembler_t assembler;
    tl_remote_t remote;
    tl_remote_reset(&remote);
    tl_assembler_reset(&assembler);

    const char *status = "{\"have\":\"\"}";
    const uint64_t nonce1 = 0x1111111111111111ull, sess1 = 0xaaaaaaaaaaaaaaaaull;
    const uint64_t nonce2 = 0x2222222222222222ull, sess2 = 0xbbbbbbbbbbbbbbbbull;
    char hex[17], sync_plain[64];
    const char *message = NULL;
    size_t size = 0;

    CHECK(tl_remote_hello(&remote, key, nonce1, status, strlen(status), frame, sizeof frame) > 0, "hello 1");
    tl_hex(nonce1, hex);
    snprintf(sync_plain, sizeof sync_plain, "{\"hello\":\"%s\"}", hex);
    tl_header_t hdr = { .type = TL_SYNC, .count = 1, .session = sess1 };
    size_t len = tl_seal(key, TL_FROM_BRIDGE, &hdr, (const uint8_t *)sync_plain, strlen(sync_plain), frame, sizeof frame);
    CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_SYNCED, "sync 1");

    /* Part 0 of a 2-part message 0 arrives in session 1, then the session renews. */
    static uint8_t part_old[TL_PART_MAX], part_new0[TL_PART_MAX];
    memset(part_old, 'A', sizeof part_old);
    memset(part_new0, 'B', sizeof part_new0);
    const char *part_new1 = "END_S2";

    tl_header_t old0 = { .type = TL_DATA, .message = 0, .index = 0, .count = 2, .session = sess1, .counter = 1 };
    len = tl_seal(key, TL_FROM_BRIDGE, &old0, part_old, sizeof part_old, frame, sizeof frame);
    CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_NOTHING, "s1 part 0 partial");

    CHECK(tl_remote_hello(&remote, key, nonce2, status, strlen(status), frame, sizeof frame) > 0, "hello 2");
    tl_hex(nonce2, hex);
    snprintf(sync_plain, sizeof sync_plain, "{\"hello\":\"%s\"}", hex);
    hdr.session = sess2;
    len = tl_seal(key, TL_FROM_BRIDGE, &hdr, (const uint8_t *)sync_plain, strlen(sync_plain), frame, sizeof frame);
    CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_SYNCED, "sync 2");

    /* Part 1 of message 0 in session 2 must NOT complete by splicing with session 1's part 0. */
    tl_header_t new1 = { .type = TL_DATA, .message = 0, .index = 1, .count = 2, .session = sess2, .counter = 1 };
    len = tl_seal(key, TL_FROM_BRIDGE, &new1, (const uint8_t *)part_new1, strlen(part_new1), frame, sizeof frame);
    CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_NOTHING,
          "session 1 part 0 spliced with session 2 part 1");

    /* Once session 2's own part 0 arrives, the assembled message contains only session 2 bytes. */
    tl_header_t new0 = { .type = TL_DATA, .message = 0, .index = 0, .count = 2, .session = sess2, .counter = 2 };
    len = tl_seal(key, TL_FROM_BRIDGE, &new0, part_new0, sizeof part_new0, frame, sizeof frame);
    CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_MESSAGE,
          "session 2 message did not complete");
    CHECK(size == sizeof part_new0 + strlen(part_new1)
          && memcmp(message, part_new0, sizeof part_new0) == 0
          && memcmp(message + sizeof part_new0, part_new1, strlen(part_new1)) == 0,
          "session 2 message corrupted by stale assembly");
}

/* Up to TL_PARTS_MAX (16) parts, in order and out of order, up to TL_MESSAGE_MAX bytes. */
static void sixteen_parts(void)
{
    CHECK(TL_PARTS_MAX == 16, "TL_PARTS_MAX must be 16");
    static tl_assembler_t assembler;
    tl_remote_t remote;
    tl_remote_reset(&remote);
    tl_assembler_reset(&assembler);

    const char *status = "{\"have\":\"\"}";
    const uint64_t nonce = 0x3333333333333333ull, sess = 0xccccccccccccccccull;
    char hex[17], sync_plain[64];
    const char *message = NULL;
    size_t size = 0;

    CHECK(tl_remote_hello(&remote, key, nonce, status, strlen(status), frame, sizeof frame) > 0, "hello 16");
    tl_hex(nonce, hex);
    snprintf(sync_plain, sizeof sync_plain, "{\"hello\":\"%s\"}", hex);
    tl_header_t hdr = { .type = TL_SYNC, .count = 1, .session = sess };
    size_t len = tl_seal(key, TL_FROM_BRIDGE, &hdr, (const uint8_t *)sync_plain, strlen(sync_plain), frame, sizeof frame);
    CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_SYNCED, "sync 16");

    /* 16 equal parts of 512 bytes = 8192 bytes (TL_MESSAGE_MAX), in order. */
    static uint8_t payload[TL_MESSAGE_MAX];
    for (size_t i = 0; i < sizeof payload; i++) payload[i] = (uint8_t)('a' + (i % 26));
    uint32_t counter = 0;
    tl_event_t ev = TL_NOTHING;
    for (uint8_t idx = 0; idx < TL_PARTS_MAX; idx++) {
        tl_header_t dh = { .type = TL_DATA, .message = 1, .index = idx, .count = TL_PARTS_MAX,
                           .session = sess, .counter = ++counter };
        len = tl_seal(key, TL_FROM_BRIDGE, &dh, payload + (size_t)idx * 512, 512, frame, sizeof frame);
        ev = tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size);
        CHECK(idx + 1 < TL_PARTS_MAX ? ev == TL_NOTHING : ev == TL_MESSAGE, "16-part in-order step %u", idx);
    }
    CHECK(size == TL_MESSAGE_MAX && memcmp(message, payload, TL_MESSAGE_MAX) == 0 && message[TL_MESSAGE_MAX] == '\0',
          "16-part in-order payload mismatch");

    /* 16 variable-length parts arriving out of order (interleaved reverse), plus a duplicate part ignored. */
    static uint8_t var_payload[TL_MESSAGE_MAX];
    size_t offsets[TL_PARTS_MAX], lengths[TL_PARTS_MAX], total = 0;
    for (uint8_t i = 0; i < TL_PARTS_MAX; i++) {
        offsets[i] = total;
        lengths[i] = (size_t)(17 + i * 31); /* sum = 16*17 + 31*120 = 3992 <= TL_MESSAGE_MAX */
        for (size_t k = 0; k < lengths[i]; k++) var_payload[total + k] = (uint8_t)('A' + ((i + k) % 26));
        total += lengths[i];
    }
    const uint8_t order[TL_PARTS_MAX] = { 15, 0, 14, 1, 13, 2, 12, 3, 11, 4, 10, 5, 9, 6, 8, 7 };
    for (size_t step = 0; step < TL_PARTS_MAX; step++) {
        uint8_t idx = order[step];
        tl_header_t dh = { .type = TL_DATA, .message = 2, .index = idx, .count = TL_PARTS_MAX,
                           .session = sess, .counter = ++counter };
        len = tl_seal(key, TL_FROM_BRIDGE, &dh, var_payload + offsets[idx], lengths[idx], frame, sizeof frame);
        ev = tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size);
        if (step == 3) {
            /* Duplicate of already-received part 15 with higher counter must be ignored cleanly. */
            tl_header_t dup = { .type = TL_DATA, .message = 2, .index = 15, .count = TL_PARTS_MAX,
                                .session = sess, .counter = ++counter };
            size_t dlen = tl_seal(key, TL_FROM_BRIDGE, &dup, var_payload + offsets[15], lengths[15], frame, sizeof frame);
            CHECK(tl_remote_receive(&remote, &assembler, key, frame, dlen, &message, &size) == TL_NOTHING,
                  "duplicate part completed message");
        }
        CHECK(step + 1 < TL_PARTS_MAX ? ev == TL_NOTHING : ev == TL_MESSAGE, "16-part ooo step %zu", step);
    }
    CHECK(size == total && memcmp(message, var_payload, total) == 0 && message[total] == '\0',
          "16-part out-of-order payload mismatch");

    /* Total > TL_MESSAGE_MAX across parts must be rejected cleanly. */
    static uint8_t big_part[TL_PART_MAX];
    memset(big_part, 'Z', sizeof big_part);
    for (uint8_t idx = 0; idx < 6; idx++) {
        tl_header_t dh = { .type = TL_DATA, .message = 3, .index = idx, .count = 6,
                           .session = sess, .counter = ++counter };
        len = tl_seal(key, TL_FROM_BRIDGE, &dh, big_part, sizeof big_part, frame, sizeof frame);
        CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_NOTHING,
              "oversized multi-part accepted at index %u", idx);
    }
}

/* SYNC payload parsed via cJSON: whitespace & extra fields accepted, malformed/wrong rejected. */
static void sync_json(void)
{
    static tl_assembler_t assembler;
    tl_remote_t remote;
    tl_remote_reset(&remote);
    tl_assembler_reset(&assembler);

    const char *status = "{\"have\":\"\"}";
    const uint64_t nonce = 0x0123456789abcdefull, sess = 0xfeedfacecafebeefull;
    const char *message = NULL;
    size_t size = 0;

    CHECK(tl_remote_hello(&remote, key, nonce, status, strlen(status), frame, sizeof frame) > 0, "hello sync_json");
    tl_header_t hdr = { .type = TL_SYNC, .count = 1, .session = sess };

    const char *bad[] = {
        "{bad json",
        "[\"0123456789abcdef\"]",
        "{\"hello\":123}",
        "{\"hello\":\"0123456789abcde0\"}",
        "{}"
    };
    for (size_t i = 0; i < sizeof bad / sizeof bad[0]; i++) {
        size_t len = tl_seal(key, TL_FROM_BRIDGE, &hdr, (const uint8_t *)bad[i], strlen(bad[i]), frame, sizeof frame);
        CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_NOTHING,
              "bad sync accepted: %s", bad[i]);
        CHECK(remote.waiting == nonce && remote.session == 0, "bad sync mutated state: %s", bad[i]);
    }

    const char *good = "{ \"hello\" : \"0123456789abcdef\" , \"extra\" : 1 }";
    size_t len = tl_seal(key, TL_FROM_BRIDGE, &hdr, (const uint8_t *)good, strlen(good), frame, sizeof frame);
    CHECK(tl_remote_receive(&remote, &assembler, key, frame, len, &message, &size) == TL_SYNCED,
          "valid spaced SYNC JSON rejected");
    CHECK(remote.session == sess && remote.waiting == 0, "SYNC session not adopted");

    uint8_t secret[32];
    memset(secret, 0xab, sizeof secret);
    tl_zeroize(secret, sizeof secret);
    for (size_t i = 0; i < sizeof secret; i++) {
        CHECK(secret[i] == 0, "tl_zeroize left byte %zu non-zero", i);
    }
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
    stale_assembly();
    sixteen_parts();
    sync_json();
    cJSON_Delete(root);
    free(json);
    if (failures) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    puts("link: all vectors pass");
    return 0;
}
