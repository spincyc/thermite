/*
 * thermite_remote against a fake platform and a fake bridge. The bridge is
 * built from tl_seal and tl_open with the bridge's direction, so every frame
 * the remote sends is checked the way handheld/link.js would read it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "thermite_link.h"
#include "thermite_remote.h"

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

static const uint8_t key[TL_KEY_LEN] = {
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f,
    0x20, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2a, 0x2b, 0x2c, 0x2d, 0x2e, 0x2f,
};

/* ---- the fake platform --------------------------------------------------- */

#define FRAMES_MAX 64

static struct {
    uint32_t clock;
    uint64_t randoms; /* how many random64 calls so far */
    bool zero_next;   /* the next random64 returns 0 */
    uint8_t channel;
    int channel_calls;
    uint8_t frames[FRAMES_MAX][TL_FRAME_MAX];
    size_t lengths[FRAMES_MAX];
    int count, read;  /* frames sent; frames the bridge has taken */
    unsigned changed; /* TR_CHANGED_* bits since the last changes() */
    int changed_calls;
} fake;

static void fake_send(void *context, const uint8_t *frame, size_t length)
{
    (void)context;
    if (fake.count < FRAMES_MAX) {
        memcpy(fake.frames[fake.count], frame, length);
        fake.lengths[fake.count++] = length;
    }
}

static void fake_set_channel(void *context, uint8_t channel)
{
    (void)context;
    fake.channel = channel;
    fake.channel_calls++;
}

static uint32_t fake_now(void *context)
{
    (void)context;
    return fake.clock;
}

static uint64_t random_value(uint64_t n)
{
    return 0x9e3779b97f4a7c15ull * n + 0x1234;
}

static uint64_t fake_random(void *context)
{
    (void)context;
    if (fake.zero_next) {
        fake.zero_next = false;
        return 0;
    }
    return random_value(++fake.randoms);
}

static void fake_changed(void *context, unsigned what)
{
    (void)context;
    fake.changed |= what;
    fake.changed_calls++;
}

static const tr_platform_t platform = { NULL, fake_send, fake_set_channel, fake_now, fake_random, fake_changed };
static const tr_identity_t identity = { "test-board", "1.2.3", 240, 320, 6 };

static void reset_fake(void)
{
    memset(&fake, 0, sizeof fake);
    fake.clock = 0xfffff000u; /* timers must survive the clock wrapping */
}

static unsigned changes(void)
{
    unsigned what = fake.changed;
    fake.changed = 0;
    return what;
}

static int unread(void)
{
    return fake.count - fake.read;
}

/* ---- the fake bridge ----------------------------------------------------- */

static struct {
    uint64_t session;
    uint64_t sessions; /* how many it has started */
    uint32_t sent, heard;
    uint8_t message;
} bridge;

static uint8_t last[TL_FRAME_MAX]; /* the last frame the bridge sealed, for replays */
static size_t last_length;

/* The next frame the remote sent, opened as the bridge opens it. */
static int take_frame(tl_header_t *header, char *plain, size_t capacity)
{
    if (fake.read >= fake.count) return -1;
    int length = tl_open(key, TL_FROM_REMOTE, fake.frames[fake.read], fake.lengths[fake.read], header,
                         (uint8_t *)plain, capacity - 1);
    fake.read++;
    if (length >= 0) plain[length] = '\0';
    return length;
}

/* The next frame must be a HELLO; returns its status (free with cJSON_Delete) and nonce. */
static cJSON *take_hello(uint64_t *nonce)
{
    tl_header_t header;
    char plain[TL_FRAME_MAX];
    if (take_frame(&header, plain, sizeof plain) < 0 || header.type != TL_HELLO || header.counter != 0) return NULL;
    if (nonce) *nonce = header.session;
    return cJSON_Parse(plain);
}

/* The next frame must be DATA in the bridge's session with a fresh counter; returns it parsed. */
static cJSON *take_data(void)
{
    tl_header_t header;
    char plain[TL_FRAME_MAX];
    if (take_frame(&header, plain, sizeof plain) < 0 || header.type != TL_DATA || header.session != bridge.session
        || header.counter <= bridge.heard) {
        return NULL;
    }
    bridge.heard = header.counter;
    return cJSON_Parse(plain);
}

static void deliver(tr_t *remote, const uint8_t *frame, size_t length)
{
    memcpy(last, frame, length);
    last_length = length;
    tr_receive(remote, frame, length);
}

/* Answers a HELLO nonce with a SYNC under a fresh session. */
static void sync_to(tr_t *remote, uint64_t nonce)
{
    char hex[17], plain[64];
    tl_hex(nonce, hex);
    snprintf(plain, sizeof plain, "{\"hello\":\"%s\"}", hex);
    bridge.session = 0x5e55000000000000ull + ++bridge.sessions;
    bridge.sent = bridge.heard = 0;
    bridge.message = 0;
    tl_header_t header = { .type = TL_SYNC, .count = 1, .session = bridge.session };
    uint8_t frame[TL_FRAME_MAX];
    size_t length = tl_seal(key, TL_FROM_BRIDGE, &header, (const uint8_t *)plain, strlen(plain), frame, sizeof frame);
    deliver(remote, frame, length);
}

/* Takes the next HELLO and answers it; false when there was none. */
static bool answer_hello(tr_t *remote)
{
    uint64_t nonce;
    cJSON *status = take_hello(&nonce);
    if (!status) return false;
    cJSON_Delete(status);
    sync_to(remote, nonce);
    return true;
}

/* Seals one bridge message as DATA under session (in parts when long) and delivers it. */
static void say_in(tr_t *remote, uint64_t session, const char *json)
{
    size_t length = strlen(json), parts = (length + TL_PART_MAX - 1) / TL_PART_MAX;
    tl_header_t header = { .type = TL_DATA, .message = bridge.message++, .count = (uint8_t)parts, .session = session };
    for (size_t index = 0; index < parts; index++) {
        size_t at = index * TL_PART_MAX, size = length - at < TL_PART_MAX ? length - at : TL_PART_MAX;
        uint8_t frame[TL_FRAME_MAX];
        header.index = (uint8_t)index;
        header.counter = ++bridge.sent;
        size_t sealed = tl_seal(key, TL_FROM_BRIDGE, &header, (const uint8_t *)json + at, size, frame, sizeof frame);
        deliver(remote, frame, sealed);
    }
}

static void say(tr_t *remote, const char *json)
{
    say_in(remote, bridge.session, json);
}

static void ack(tr_t *remote, double op)
{
    char json[48];
    snprintf(json, sizeof json, "{\"ack\":%.0f}", op);
    say(remote, json);
}

/* ---- helpers ------------------------------------------------------------- */

static tr_persist_t persist;

static void advance(tr_t *remote, uint32_t ms)
{
    fake.clock += ms;
    tr_poll(remote);
}

/* A cold start with a session: HELLO, SYNC, nothing left to read. */
static tr_t *connected(void)
{
    reset_fake();
    memset(&bridge, 0, sizeof bridge);
    memset(&persist, 0, sizeof persist);
    tr_t *remote = tr_create(&persist, key, &identity, &platform);
    tr_poll(remote);
    CHECK(answer_hello(remote), "no HELLO on a cold start");
    changes();
    return remote;
}

static double number(const cJSON *object, const char *name)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    return cJSON_IsNumber(item) ? item->valuedouble : -1;
}

static const char *string(const cJSON *object, const char *name)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    return cJSON_IsString(item) ? item->valuestring : NULL;
}

static bool has(const cJSON *object, const char *name)
{
    return cJSON_GetObjectItemCaseSensitive(object, name) != NULL;
}

static bool is_true(const cJSON *object, const char *name)
{
    return cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(object, name));
}

static int members(const cJSON *object)
{
    return cJSON_GetArraySize(object);
}

static bool same(const char *a, const char *b)
{
    return a && b && strcmp(a, b) == 0;
}

/* ---- tests --------------------------------------------------------------- */

/* A cold start: a random first op, the enrolled channel, a HELLO with every status field. */
static void handshake(void)
{
    reset_fake();
    memset(&bridge, 0, sizeof bridge);
    memset(&persist, 0xa5, sizeof persist); /* garbage, as RTC memory after power-up */
    tr_t *remote = tr_create(&persist, key, &identity, &platform);
    CHECK(persist.op == (uint32_t)random_value(1), "op does not start at random64's value: %u", persist.op);
    CHECK(persist.channel == 6 && fake.channel == 6, "not on the enrolled channel");
    CHECK(persist.link.session == 0, "a garbage session survived");
    CHECK(tr_link(remote) == TR_LINK_OFFLINE, "link not offline");
    CHECK(fake.count == 0, "sent before tr_poll");

    fake.zero_next = true; /* a zero nonce is not a nonce */
    CHECK(tr_poll(remote) == TR_HELLO_MS, "tr_poll does not wait for the SYNC");
    CHECK(tr_link(remote) == TR_LINK_CONNECTING && (changes() & TR_CHANGED_LINK), "link not connecting");
    uint64_t nonce;
    cJSON *status = take_hello(&nonce);
    CHECK(status && nonce != 0, "no HELLO");
    CHECK(same(string(status, "have"), "") && cJSON_IsNull(cJSON_GetObjectItem(status, "battery"))
          && cJSON_IsFalse(cJSON_GetObjectItem(status, "charging")) && same(string(status, "board"), "test-board")
          && same(string(status, "fw"), "1.2.3") && number(status, "w") == 240 && number(status, "h") == 320
          && members(status) == 7, "HELLO status fields");
    cJSON_Delete(status);

    fake.clock += 100;
    CHECK(tr_poll(remote) == TR_HELLO_MS - 100, "tr_poll's wait does not count down");
    sync_to(remote, nonce ^ 1);
    CHECK(persist.link.session == 0, "a SYNC for another HELLO was adopted");
    sync_to(remote, nonce);
    CHECK(persist.link.session == bridge.session, "session not adopted");
    CHECK(tr_link(remote) == TR_LINK_CONNECTED && (changes() & TR_CHANGED_LINK), "link not connected");
    CHECK(tr_idle(remote) && tr_poll(remote) == UINT32_MAX && unread() == 0, "not idle after the handshake");
    fake.clock += 10000;
    tr_poll(remote);
    CHECK(unread() == 0, "a connected idle remote sent something");
    tr_destroy(remote);
}

/* Numbered operations, their fields, and acks. */
static void operations(void)
{
    tr_t *remote = connected();
    uint32_t first = persist.op + 1;
    const char *up[] = { "up" };
    CHECK(tr_key(remote, up, 1) && !tr_idle(remote), "key refused");
    CHECK(tr_poll(remote) == TR_RETRY_MS, "tr_poll does not wait for the ack");
    cJSON *sent = take_data();
    CHECK(sent && number(sent, "op") == first && members(sent) == 2 && !has(sent, "have"), "first op");
    const cJSON *keys = cJSON_GetObjectItem(sent, "key");
    CHECK(cJSON_GetArraySize(keys) == 1 && same(cJSON_GetArrayItem(keys, 0)->valuestring, "up"), "key array");
    cJSON_Delete(sent);
    ack(remote, first + 5);
    CHECK(!tr_idle(remote), "an ack for another op removed it");
    ack(remote, first);
    CHECK(tr_idle(remote) && tr_link(remote) == TR_LINK_CONNECTED && tr_poll(remote) == UINT32_MAX, "ack");

    const char *play[] = { "play_pause", "play" };
    CHECK(tr_key(remote, play, 2), "two-choice key refused");
    sent = take_data();
    keys = cJSON_GetObjectItem(sent, "key");
    CHECK(number(sent, "op") == first + 1 && members(sent) == 2 && cJSON_GetArraySize(keys) == 2
          && same(cJSON_GetArrayItem(keys, 1)->valuestring, "play"), "two-choice key, no have once heard");
    cJSON_Delete(sent);
    ack(remote, first + 1);

    struct {
        bool (*run)(tr_t *, const char *);
        const char *value, *field;
    } single[] = {
        { tr_bubble, "apple_tv", "bubble" },
        { tr_room, "west \"main\" \\ room", "room" },
        { tr_input, "hdmi2", "input" },
    };
    uint32_t op = first + 2;
    for (size_t i = 0; i < sizeof single / sizeof single[0]; i++, op++) {
        CHECK(single[i].run(remote, single[i].value), "%s refused", single[i].field);
        sent = take_data();
        CHECK(number(sent, "op") == op && same(string(sent, single[i].field), single[i].value) && members(sent) == 2,
              "%s op", single[i].field);
        cJSON_Delete(sent);
        ack(remote, op);
    }
    CHECK(tr_all_off(remote), "all off refused");
    sent = take_data();
    CHECK(number(sent, "op") == op && is_true(sent, "allOff") && members(sent) == 2, "allOff op");
    cJSON_Delete(sent);
    ack(remote, op++);
    CHECK(tr_sleep(remote), "sleep refused");
    sent = take_data();
    CHECK(number(sent, "op") == op && is_true(sent, "sleep") && members(sent) == 2, "sleep op");
    cJSON_Delete(sent);
    ack(remote, op++);
    CHECK(!tr_key(remote, up, 0) && !tr_bubble(remote, NULL), "empty operations accepted");
    CHECK(persist.op == op - 1 && unread() == 0, "a refused operation used a number");
    tr_destroy(remote);
}

/* Unacknowledged: again after TR_RETRY_MS, a HELLO after two, once more in the new session, then give up. */
static void retries(void)
{
    tr_t *remote = connected();
    uint64_t old = bridge.session;
    const char *ok[] = { "ok" };
    tr_key(remote, ok, 1);
    uint32_t op = persist.op;
    cJSON_Delete(take_data());
    advance(remote, TR_RETRY_MS - 1);
    CHECK(unread() == 0 && tr_link(remote) == TR_LINK_CONNECTED, "resent early");
    advance(remote, 1);
    cJSON *sent = take_data();
    CHECK(sent && number(sent, "op") == op, "not resent with the same op");
    CHECK(tr_link(remote) == TR_LINK_CONNECTING && (changes() & TR_CHANGED_LINK), "retry not shown as connecting");
    cJSON_Delete(sent);
    advance(remote, TR_RETRY_MS);
    uint64_t nonce;
    cJSON *status = take_hello(&nonce);
    CHECK(status && unread() == 0, "no HELLO after two silent sends");
    cJSON_Delete(status);
    CHECK(tr_poll(remote) == TR_HELLO_MS, "waits for the SYNC");
    sync_to(remote, nonce);
    CHECK(bridge.session != old && persist.link.session == bridge.session, "new session not adopted");
    sent = take_data();
    CHECK(sent && number(sent, "op") == op && !has(sent, "have") && has(sent, "key"), "not resent in the new session");
    cJSON_Delete(sent);
    CHECK(tr_link(remote) == TR_LINK_CONNECTING, "connected before the ack");
    changes();
    advance(remote, TR_RETRY_MS);
    CHECK(unread() == 0, "sent a fourth time");
    CHECK(tr_idle(remote) && tr_link(remote) == TR_LINK_OFFLINE && persist.link.session == 0, "did not give up");
    unsigned what = changes();
    const cJSON *toast = tr_toast(remote);
    CHECK((what & TR_CHANGED_TOAST) && (what & TR_CHANGED_LINK) && same(string(toast, "title"), "Not connected")
          && same(string(toast, "body"), "The remote's base did not answer") && same(string(toast, "tone"), "bad"),
          "no Not connected toast");
    tr_clear_toast(remote);
    CHECK(tr_toast(remote) == NULL && tr_poll(remote) == UINT32_MAX, "toast not cleared or timers left");

    /* Offline, the next press starts over with a HELLO and goes out after the SYNC. */
    CHECK(tr_key(remote, ok, 1) && persist.op == op + 1, "press after giving up");
    CHECK(answer_hello(remote), "no HELLO for a press while offline");
    sent = take_data();
    CHECK(sent && number(sent, "op") == op + 1, "press not sent after the SYNC");
    cJSON_Delete(sent);
    ack(remote, op + 1);
    CHECK(tr_idle(remote) && tr_link(remote) == TR_LINK_CONNECTED, "not connected again");

    /* One lost send: the resend's ack settles it, with no HELLO. */
    tr_key(remote, ok, 1);
    cJSON_Delete(take_data());
    advance(remote, TR_RETRY_MS);
    cJSON_Delete(take_data());
    ack(remote, persist.op);
    advance(remote, 1000);
    CHECK(tr_idle(remote) && unread() == 0 && tr_link(remote) == TR_LINK_CONNECTED, "ack after a resend");

    /* An ack in the old session that arrives while the HELLO waits still counts. */
    old = bridge.session;
    tr_key(remote, ok, 1);
    cJSON_Delete(take_data());
    advance(remote, TR_RETRY_MS);
    cJSON_Delete(take_data());
    advance(remote, TR_RETRY_MS);
    status = take_hello(&nonce);
    cJSON_Delete(status);
    say_in(remote, old, "{\"ack\":0}"); /* not this op */
    char json[32];
    snprintf(json, sizeof json, "{\"ack\":%u}", persist.op);
    say_in(remote, old, json);
    CHECK(tr_idle(remote) && tr_link(remote) == TR_LINK_CONNECTING, "late ack ignored, or connected before the SYNC");
    sync_to(remote, nonce);
    CHECK(unread() == 0 && tr_link(remote) == TR_LINK_CONNECTED, "resent an acknowledged op");
    tr_destroy(remote);
}

/* HELLO unanswered: channels in turn from the one that last answered; the answering one is kept. */
static void channels(void)
{
    reset_fake();
    memset(&bridge, 0, sizeof bridge);
    memset(&persist, 0, sizeof persist);
    tr_t *remote = tr_create(&persist, key, &identity, &platform);
    tr_poll(remote);
    const uint8_t order[] = { 6, 7, 8, 9, 10, 11, 12, 13, 1, 2, 3, 4, 5 };
    for (size_t i = 0; i < sizeof order; i++) {
        CHECK(fake.channel == order[i], "HELLO %zu on channel %u, wanted %u", i, fake.channel, order[i]);
        cJSON *status = take_hello(NULL);
        CHECK(status && unread() == 0, "one HELLO on channel %u", order[i]);
        cJSON_Delete(status);
        advance(remote, TR_HELLO_MS - 1);
        CHECK(unread() == 0, "next channel early");
        advance(remote, 1);
    }
    CHECK(unread() == 0 && fake.channel == 6 && tr_link(remote) == TR_LINK_OFFLINE, "scan did not end on channel 6");
    CHECK(tr_toast(remote) == NULL && tr_poll(remote) == UINT32_MAX, "a scan with no op toasted or kept going");

    /* A press scans again; channel 9 answers and is kept. */
    const char *up[] = { "up" };
    tr_key(remote, up, 1);
    for (int i = 0; i < 3; i++) {
        cJSON_Delete(take_hello(NULL));
        advance(remote, TR_HELLO_MS);
    }
    CHECK(fake.channel == 9 && answer_hello(remote), "no HELLO on channel 9");
    CHECK(persist.channel == 9, "answering channel not kept");
    cJSON *sent = take_data();
    CHECK(sent && number(sent, "op") == persist.op, "press not sent after the scan");
    cJSON_Delete(sent);
    ack(remote, persist.op);
    tr_destroy(remote);

    /* Woken, it listens on channel 9 again; a scan from 12 wraps to 1. */
    reset_fake();
    remote = tr_create(&persist, key, &identity, &platform);
    CHECK(fake.channel == 9 && tr_link(remote) == TR_LINK_CONNECTED, "woke on another channel");
    tr_destroy(remote);
    persist.channel = 12;
    tl_remote_reset(&persist.link);
    reset_fake();
    remote = tr_create(&persist, key, &identity, &platform);
    tr_poll(remote);
    const uint8_t wrap[] = { 12, 13, 1, 2 };
    for (size_t i = 0; i < sizeof wrap; i++) {
        CHECK(fake.channel == wrap[i], "wrap: channel %u, wanted %u", fake.channel, wrap[i]);
        cJSON_Delete(take_hello(NULL));
        if (i + 1 < sizeof wrap) advance(remote, TR_HELLO_MS);
    }

    /* A whole scan with a press waiting gives it up with the toast. */
    tr_key(remote, up, 1);
    CHECK(unread() == 0, "a press restarted the running scan");
    for (int i = 0; i < 10; i++) advance(remote, TR_HELLO_MS);
    CHECK(tr_idle(remote) && tr_toast(remote) && fake.channel == 12, "scan with a press waiting did not give up");
    tr_destroy(remote);
}

/* screen, audio, toast and inputs replace what was held, and say what changed. */
static void model(void)
{
    tr_t *remote = connected();
    tr_apply(remote, "{\"screen\":{\"id\":\"s1\",\"label\":\"Example West\"}}");
    CHECK(changes() == TR_CHANGED_SCREEN && same(string(tr_screen(remote), "id"), "s1"), "screen");
    int calls = fake.changed_calls;
    tr_apply(remote, "{\"screen\":{\"id\":\"s1\",\"label\":\"Example West\"}}");
    CHECK(fake.changed_calls == calls && changes() == 0, "the same screen id reported a change");
    tr_apply(remote, "{\"screen\":{\"id\":\"s2\",\"label\":\"Example East\"}}");
    CHECK(changes() == TR_CHANGED_SCREEN && same(string(tr_screen(remote), "label"), "Example East"), "screen s2");

    tr_apply(remote, "{\"audio\":{\"short\":\"Receiver\",\"value\":35,\"muted\":false}}");
    CHECK(changes() == TR_CHANGED_AUDIO && number(tr_audio(remote), "value") == 35, "audio");
    tr_apply(remote, "{\"audio\":{\"short\":\"Receiver\",\"value\":35,\"muted\":false}}");
    CHECK(changes() == 0, "the same audio reported a change");
    tr_apply(remote, "{\"audio\":null}");
    CHECK(changes() == TR_CHANGED_AUDIO && tr_audio(remote) == NULL, "audio null");

    tr_apply(remote, "{\"toast\":{\"title\":\"Power\",\"body\":\"Press again\",\"tone\":\"\"}}");
    CHECK(changes() == TR_CHANGED_TOAST && same(string(tr_toast(remote), "title"), "Power"), "toast");
    tr_clear_toast(remote);
    tr_apply(remote, "{\"toast\":{\"title\":\"Power\",\"body\":\"Press again\",\"tone\":\"\"}}");
    CHECK(changes() == TR_CHANGED_TOAST && tr_toast(remote), "a repeated toast did not show again");

    tr_apply(remote, "{\"inputs\":{\"title\":\"TV input\",\"options\":[{\"value\":\"hdmi1\",\"label\":\"HDMI 1\","
                     "\"inUse\":true}]}}");
    CHECK(changes() == TR_CHANGED_INPUTS
          && cJSON_GetArraySize(cJSON_GetObjectItem(tr_inputs(remote), "options")) == 1, "inputs");
    tr_clear_inputs(remote);
    CHECK(tr_inputs(remote) == NULL, "inputs not cleared");

    /* All at once, through the radio, in parts: one changed call with every bit. */
    static char big[6000];
    int at = snprintf(big, sizeof big, "{\"screen\":{\"id\":\"s3\",\"pad\":\"");
    while (at < 5000) big[at++] = 'x';
    snprintf(big + at, sizeof big - (size_t)at, "\"},\"audio\":{\"value\":1},\"toast\":{\"title\":\"t\"},"
             "\"inputs\":{\"title\":\"i\",\"options\":[]}}");
    calls = fake.changed_calls;
    say(remote, big);
    CHECK(fake.changed_calls == calls + 1
          && changes() == (TR_CHANGED_SCREEN | TR_CHANGED_AUDIO | TR_CHANGED_TOAST | TR_CHANGED_INPUTS),
          "a message in parts");
    CHECK(same(string(tr_screen(remote), "id"), "s3") && strlen(string(tr_screen(remote), "pad")) > 4900, "parts");

    tr_apply(remote, "not json");
    tr_apply(remote, "[1,2]");
    tr_apply(remote, "{\"screen\":\"flat\",\"toast\":7,\"audio\":3}");
    CHECK(changes() == 0 && same(string(tr_screen(remote), "id"), "s3") && tr_audio(remote), "bad messages applied");
    tr_destroy(remote);
}

/* Status rides on the next send until heard, have on tr_wake; a HELLO carries both. */
static void status(void)
{
    tr_t *remote = connected();
    const char *up[] = { "up" };
    tr_key(remote, up, 1);
    cJSON *sent = take_data();
    CHECK(!has(sent, "have") && !has(sent, "battery"), "first op after the SYNC repeats what the HELLO said");
    cJSON_Delete(sent);
    ack(remote, persist.op);
    tr_apply(remote, "{\"screen\":{\"id\":\"abc\"}}");

    tr_status(remote, 80, true);
    CHECK(unread() == 0, "status sent on its own");
    tr_key(remote, up, 1);
    sent = take_data();
    CHECK(number(sent, "battery") == 80 && is_true(sent, "charging") && !has(sent, "have"), "status not on the op");
    cJSON_Delete(sent);
    advance(remote, TR_RETRY_MS); /* lost: the resend carries it again */
    sent = take_data();
    CHECK(number(sent, "battery") == 80, "status not on the resend");
    cJSON_Delete(sent);
    ack(remote, persist.op);
    tr_key(remote, up, 1);
    sent = take_data();
    CHECK(!has(sent, "battery") && !has(sent, "charging"), "status sent again once heard");
    cJSON_Delete(sent);
    ack(remote, persist.op);

    tr_status(remote, -5, false);
    tr_wake(remote);
    sent = take_data();
    CHECK(cJSON_IsNull(cJSON_GetObjectItem(sent, "battery")) && cJSON_IsFalse(cJSON_GetObjectItem(sent, "charging"))
          && same(string(sent, "have"), "abc") && number(sent, "op") == persist.op && members(sent) == 4,
          "wake with status");
    cJSON_Delete(sent);
    ack(remote, persist.op);
    tr_wake(remote);
    sent = take_data();
    CHECK(members(sent) == 2 && same(string(sent, "have"), "abc"), "wake carries have");
    cJSON_Delete(sent);
    ack(remote, persist.op);

    /* A screen id too long to echo is echoed as "" rather than leaving the op unsendable. */
    static char json[700];
    int at = snprintf(json, sizeof json, "{\"screen\":{\"id\":\"");
    while (at < 600) json[at++] = 'f';
    snprintf(json + at, sizeof json - (size_t)at, "\"}}");
    tr_apply(remote, json);
    tr_wake(remote);
    sent = take_data();
    CHECK(same(string(sent, "have"), ""), "an overlong have");
    cJSON_Delete(sent);
    ack(remote, persist.op);
    CHECK(tr_idle(remote), "op with an overlong have stuck");
    tr_apply(remote, "{\"screen\":{\"id\":\"abc\"}}");

    /* A HELLO carries the status and have, so neither rides after its SYNC. */
    tr_status(remote, 42, false);
    tl_remote_reset(&persist.link);
    tr_key(remote, up, 1);
    cJSON *hello = take_hello(NULL);
    CHECK(number(hello, "battery") == 42 && same(string(hello, "have"), "abc"), "HELLO status");
    cJSON_Delete(hello);
    fake.read--; /* answer that same HELLO */
    CHECK(answer_hello(remote), "no HELLO");
    sent = take_data();
    CHECK(!has(sent, "have") && !has(sent, "battery"), "after a new session");
    cJSON_Delete(sent);
    tr_destroy(remote);
}

/* Deep sleep: a new remote over the same persist keeps the session and the op number. */
static void deep_sleep(void)
{
    tr_t *remote = connected();
    const char *up[] = { "up" };
    tr_key(remote, up, 1);
    cJSON_Delete(take_data());
    ack(remote, persist.op);
    tr_apply(remote, "{\"screen\":{\"id\":\"s1\"}}");
    CHECK(tr_idle(remote), "not idle before sleep");
    CHECK(tr_sleep(remote), "sleep refused");
    cJSON_Delete(take_data());
    ack(remote, persist.op);
    uint32_t op = persist.op;
    uint64_t session = persist.link.session;
    tr_destroy(remote);

    fake.read = fake.count = 0;
    fake.clock += 60000;
    remote = tr_create(&persist, key, &identity, &platform);
    CHECK(persist.op == op && persist.link.session == session, "persist lost");
    CHECK(tr_link(remote) == TR_LINK_CONNECTED && tr_screen(remote) == NULL, "woke in the wrong state");
    CHECK(tr_poll(remote) == UINT32_MAX && unread() == 0, "a HELLO on wake");
    CHECK(tr_wake(remote), "wake refused");
    cJSON *sent = take_data();
    CHECK(sent && number(sent, "op") == op + 1 && same(string(sent, "have"), ""), "wake in the kept session");
    cJSON_Delete(sent);
    ack(remote, op + 1);
    CHECK(tr_idle(remote), "wake not acknowledged");
    tr_destroy(remote);

    /* Garbage magic is a cold start, even with a session in it. */
    persist.magic ^= 1;
    reset_fake();
    remote = tr_create(&persist, key, &identity, &platform);
    CHECK(persist.link.session == 0 && persist.op == (uint32_t)random_value(1) && tr_link(remote) == TR_LINK_OFFLINE,
          "invalid persist kept");
    tr_destroy(remote);
}

/* At most TR_PENDING_MAX outstanding. */
static void pending_limit(void)
{
    tr_t *remote = connected();
    const char *up[] = { "up" };
    uint32_t first = persist.op + 1;
    for (int i = 0; i < TR_PENDING_MAX; i++) CHECK(tr_key(remote, up, 1), "op %d refused", i);
    CHECK(!tr_key(remote, up, 1) && !tr_wake(remote) && !tr_sleep(remote), "more than TR_PENDING_MAX accepted");
    CHECK(unread() == TR_PENDING_MAX && persist.op == first + TR_PENDING_MAX - 1, "refused ops sent or numbered");
    ack(remote, first + 3);
    CHECK(tr_key(remote, up, 1) && persist.op == first + TR_PENDING_MAX, "a freed slot not reused");
    CHECK(!tr_key(remote, up, 1), "limit after reuse");
    tr_destroy(remote);
}

/* Replays, other sessions, other keys and the remote's own direction are dropped. */
static void replays(void)
{
    tr_t *remote = connected();
    say(remote, "{\"screen\":{\"id\":\"old\"}}");
    uint8_t first[TL_FRAME_MAX];
    size_t first_length = last_length;
    memcpy(first, last, last_length);
    say(remote, "{\"screen\":{\"id\":\"new\"}}");
    changes();
    tr_receive(remote, first, first_length);
    tr_receive(remote, last, last_length);
    CHECK(same(string(tr_screen(remote), "id"), "new") && changes() == 0, "replayed frame applied");

    uint64_t session = bridge.session;
    say_in(remote, session + 1, "{\"screen\":{\"id\":\"other\"}}");
    CHECK(same(string(tr_screen(remote), "id"), "new"), "frame of another session applied");

    /* A frame from the remote's direction, or under another key, is not the bridge's. */
    tl_header_t header = { .type = TL_DATA, .count = 1, .session = session, .counter = bridge.sent + 10 };
    const char *json = "{\"screen\":{\"id\":\"forged\"}}";
    uint8_t frame[TL_FRAME_MAX], other[TL_KEY_LEN] = { 1 };
    size_t length = tl_seal(key, TL_FROM_REMOTE, &header, (const uint8_t *)json, strlen(json), frame, sizeof frame);
    tr_receive(remote, frame, length);
    length = tl_seal(other, TL_FROM_BRIDGE, &header, (const uint8_t *)json, strlen(json), frame, sizeof frame);
    tr_receive(remote, frame, length);
    tr_receive(remote, frame, 10);
    CHECK(same(string(tr_screen(remote), "id"), "new") && changes() == 0, "forged frame applied");

    /* A SYNC replayed with no HELLO waiting does not move the session. */
    uint64_t nonce = 0;
    const char *up[] = { "up" };
    tl_remote_reset(&persist.link);
    tr_key(remote, up, 1);
    cJSON_Delete(take_hello(&nonce));
    sync_to(remote, nonce);
    uint8_t sync[TL_FRAME_MAX];
    size_t sync_length = last_length;
    memcpy(sync, last, last_length);
    cJSON_Delete(take_data());
    session = persist.link.session;
    sync_to(remote, nonce);
    tr_receive(remote, sync, sync_length);
    CHECK(persist.link.session == session && unread() == 0, "replayed SYNC adopted");
    tr_destroy(remote);
}

int main(void)
{
    handshake();
    operations();
    retries();
    channels();
    model();
    status();
    deep_sleep();
    pending_limit();
    replays();
    if (failures) {
        fprintf(stderr, "%d failure(s)\n", failures);
        return 1;
    }
    puts("remote: all scenarios pass");
    return 0;
}
