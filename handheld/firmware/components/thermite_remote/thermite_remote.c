/*
 * The remote's session, operations and screen model (handheld/PROTOCOL.md).
 *
 * An operation lives in a slot until the bridge acknowledges it. Each slot is
 * sent at most twice in a session; two silent sends renew the session with a
 * HELLO, after which it is sent once more and then given up. A HELLO that hears
 * no SYNC moves to the next channel; a whole scan without one gives up every
 * operation. Messages are composed when sent, so the status fields that are
 * due ride on whichever send goes out next.
 */
#include "thermite_remote.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define TR_MAGIC 0x31726d74u /* "tmr1" */
#define CHANNELS 13
#define FIELDS_MAX 192  /* an operation's own members, as JSON */
#define MESSAGE_MAX 448 /* an operation with every status member */
#define HAVE_MAX 64     /* the longest screen id echoed as have */

typedef struct {
    bool used;
    bool waiting;     /* not sent yet in the current session */
    bool last_chance; /* a HELLO renewed the session for it: one more send, then give up */
    uint8_t tries;    /* sends in the current session */
    uint32_t number;
    uint32_t due;     /* when the current send counts as silent */
    uint32_t status;  /* the status generation it carried, 0 for none */
    bool have;        /* it carried have */
    char fields[FIELDS_MAX];
} op_t;

struct tr {
    tr_persist_t *persist;
    uint8_t key[TL_KEY_LEN];
    tr_identity_t identity;
    tr_platform_t platform;
    op_t ops[TR_PENDING_MAX];

    int battery;
    bool charging;
    uint32_t status_made;  /* bumped by tr_status */
    uint32_t status_heard; /* the generation the bridge has */
    bool have_due;

    bool hello;            /* a HELLO waits for its SYNC */
    bool want_session;     /* a cold start: connect with no operation */
    uint8_t channel;       /* the channel in use now */
    uint8_t tried;         /* channels tried in this scan */
    uint32_t hello_due;
    uint32_t hello_status; /* the status generation the HELLO carried */
    tr_link_t link;

    cJSON *screen, *audio, *toast, *inputs;
    unsigned what; /* TR_CHANGED_* bits not yet reported */

    tl_assembler_t assembler;
};

/* ---- small helpers ------------------------------------------------------- */

static uint32_t now(const tr_t *r)
{
    return r->platform.now_ms(r->platform.context);
}

/* Wrap-safe: true once the clock has reached due. */
static bool reached(uint32_t clock, uint32_t due)
{
    return (int32_t)(clock - due) >= 0;
}

static void set_link(tr_t *r, tr_link_t link)
{
    if (r->link == link) return;
    r->link = link;
    r->what |= TR_CHANGED_LINK;
}

/* Reports what changed once per call into the remote, after its state is consistent. */
static void flush(tr_t *r)
{
    unsigned what = r->what;
    r->what = 0;
    if (what && r->platform.changed) r->platform.changed(r->platform.context, what);
}

static bool valid_channel(uint8_t channel)
{
    return channel >= 1 && channel <= CHANNELS;
}

/* A bounded JSON writer: once full, it stays full and the result is refused. */
typedef struct {
    char *at;
    size_t left;
    bool full;
} writer_t;

static void put(writer_t *w, const char *text)
{
    size_t length = strlen(text);
    if (w->full || length >= w->left) {
        w->full = true;
        return;
    }
    memcpy(w->at, text, length + 1);
    w->at += length;
    w->left -= length;
}

static void put_string(writer_t *w, const char *text)
{
    put(w, "\"");
    for (const unsigned char *c = (const unsigned char *)text; *c && !w->full; c++) {
        char one[8];
        if (*c == '"' || *c == '\\') snprintf(one, sizeof one, "\\%c", *c);
        else if (*c < 0x20) snprintf(one, sizeof one, "\\u%04x", *c);
        else snprintf(one, sizeof one, "%c", *c);
        put(w, one);
    }
    put(w, "\"");
}

static void put_number(writer_t *w, long long value)
{
    char text[24];
    snprintf(text, sizeof text, "%lld", value);
    put(w, text);
}

/* The screen id to echo as have. Ids are short digests; one too long to fit a
 * message is echoed as "" (the bridge then resends the screen) rather than
 * leaving the operation unsendable. */
static const char *screen_id(const tr_t *r)
{
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(r->screen, "id");
    return cJSON_IsString(id) && strlen(id->valuestring) <= HAVE_MAX ? id->valuestring : "";
}

static void put_battery(writer_t *w, const tr_t *r)
{
    put(w, "\"battery\":");
    if (r->battery < 0) put(w, "null");
    else put_number(w, r->battery);
    put(w, r->charging ? ",\"charging\":true" : ",\"charging\":false");
}

/* ---- sending ------------------------------------------------------------- */

static void emit(void *context, const uint8_t *frame, size_t length)
{
    tr_t *r = context;
    r->platform.send(r->platform.context, frame, length);
}

static void send_hello(tr_t *r)
{
    char status[MESSAGE_MAX];
    writer_t w = { status, sizeof status, false };
    put(&w, "{\"have\":");
    put_string(&w, screen_id(r));
    put(&w, ",");
    put_battery(&w, r);
    put(&w, ",\"board\":");
    put_string(&w, r->identity.board ? r->identity.board : "");
    put(&w, ",\"fw\":");
    put_string(&w, r->identity.fw ? r->identity.fw : "");
    put(&w, ",\"w\":");
    put_number(&w, r->identity.width);
    put(&w, ",\"h\":");
    put_number(&w, r->identity.height);
    put(&w, "}");

    uint64_t nonce;
    do nonce = r->platform.random64(r->platform.context);
    while (nonce == 0);
    uint8_t frame[TL_FRAME_MAX];
    size_t length = w.full ? 0 : tl_remote_hello(&r->persist->link, r->key, nonce, status, strlen(status), frame,
                                                 sizeof frame);
    if (length) r->platform.send(r->platform.context, frame, length);
    r->hello_status = r->status_made;
    r->hello_due = now(r) + TR_HELLO_MS;
}

/* Starts a scan on the channel that last answered, unless one is running. */
static void connect(tr_t *r)
{
    if (r->hello) return;
    r->hello = true;
    r->tried = 1;
    if (r->channel != r->persist->channel) {
        r->channel = r->persist->channel;
        r->platform.set_channel(r->platform.context, r->channel);
    }
    send_hello(r);
    set_link(r, TR_LINK_CONNECTING);
}

/* Whatever is in flight waits for the next session's SYNC. */
static void hold_all(tr_t *r)
{
    for (int i = 0; i < TR_PENDING_MAX; i++) {
        if (r->ops[i].used) r->ops[i].waiting = true;
    }
}

static void send_op(tr_t *r, op_t *op)
{
    char message[MESSAGE_MAX];
    writer_t w = { message, sizeof message, false };
    bool status = r->status_made != r->status_heard;
    put(&w, "{\"op\":");
    put_number(&w, op->number);
    if (op->fields[0]) {
        put(&w, ",");
        put(&w, op->fields);
    }
    if (status) {
        put(&w, ",");
        put_battery(&w, r);
    }
    if (r->have_due) {
        put(&w, ",\"have\":");
        put_string(&w, screen_id(r));
    }
    put(&w, "}");
    if (w.full) return; /* cannot happen with the bounds above; it then times out and gives up */

    if (tl_remote_send(&r->persist->link, r->key, message, strlen(message), emit, r) < 0) {
        /* No session, or its counters ran out: a new one carries everything again. */
        tl_remote_reset(&r->persist->link);
        hold_all(r);
        connect(r);
        return;
    }
    op->waiting = false;
    op->tries++;
    op->status = status ? r->status_made : 0;
    op->have = r->have_due;
    op->due = now(r) + TR_RETRY_MS;
}

static bool sendable(const tr_t *r)
{
    return r->persist->link.session != 0 && !r->hello;
}

static int pending(const tr_t *r)
{
    int count = 0;
    for (int i = 0; i < TR_PENDING_MAX; i++) count += r->ops[i].used;
    return count;
}

/* Drops every operation and shows why: the base did not answer. */
static void give_up(tr_t *r)
{
    bool dropped = pending(r) > 0;
    memset(r->ops, 0, sizeof r->ops);
    r->hello = false;
    r->want_session = false;
    tl_remote_reset(&r->persist->link);
    if (r->channel != r->persist->channel) {
        r->channel = r->persist->channel;
        r->platform.set_channel(r->platform.context, r->channel);
    }
    set_link(r, TR_LINK_OFFLINE);
    if (dropped) {
        cJSON_Delete(r->toast);
        r->toast = cJSON_Parse("{\"title\":\"Not connected\",\"body\":\"The remote's base did not answer\","
                               "\"tone\":\"bad\"}");
        r->what |= TR_CHANGED_TOAST;
    }
}

/* One more operation; fields are its JSON members, already written. */
static bool operation(tr_t *r, const char *fields)
{
    op_t *op = NULL;
    for (int i = 0; i < TR_PENDING_MAX && !op; i++) {
        if (!r->ops[i].used) op = &r->ops[i];
    }
    if (!op || strlen(fields) >= FIELDS_MAX) return false;
    memset(op, 0, sizeof *op);
    op->used = true;
    op->waiting = true;
    op->number = ++r->persist->op;
    strcpy(op->fields, fields);
    if (sendable(r)) send_op(r, op);
    else connect(r);
    flush(r);
    return true;
}

/* ---- life ---------------------------------------------------------------- */

tr_t *tr_create(tr_persist_t *persist, const uint8_t key[TL_KEY_LEN], const tr_identity_t *identity,
                const tr_platform_t *platform)
{
    tr_t *r = calloc(1, sizeof *r);
    if (!r) return NULL;
    r->persist = persist;
    memcpy(r->key, key, TL_KEY_LEN);
    r->identity = *identity;
    r->platform = *platform;
    r->battery = -1;
    if (persist->magic != TR_MAGIC || !valid_channel(persist->channel)) {
        /* A cold start. The bridge applies each number once while it runs, so
         * counting from a random number keeps fresh presses from being
         * taken for repeats of the last life's. */
        memset(persist, 0, sizeof *persist);
        persist->magic = TR_MAGIC;
        tl_remote_reset(&persist->link);
        persist->op = (uint32_t)platform->random64(platform->context);
        persist->channel = valid_channel(identity->channel) ? identity->channel : 1;
    }
    r->channel = persist->channel;
    r->platform.set_channel(r->platform.context, r->channel);
    r->want_session = persist->link.session == 0;
    r->link = persist->link.session ? TR_LINK_CONNECTED : TR_LINK_OFFLINE;
    return r;
}

void tr_destroy(tr_t *r)
{
    if (!r) return;
    cJSON_Delete(r->screen);
    cJSON_Delete(r->audio);
    cJSON_Delete(r->toast);
    cJSON_Delete(r->inputs);
    free(r);
}

void tr_status(tr_t *r, int battery, bool charging)
{
    if (battery > 100) battery = 100;
    if (battery < 0) battery = -1;
    if (battery == r->battery && charging == r->charging) return;
    r->battery = battery;
    r->charging = charging;
    r->status_made++;
}

/* ---- operations ---------------------------------------------------------- */

/* Writes "name":"value" into a fields buffer; false when it does not fit. */
static bool member(char *fields, const char *name, const char *value)
{
    if (!value) return false;
    writer_t w = { fields, FIELDS_MAX, false };
    put(&w, "\"");
    put(&w, name);
    put(&w, "\":");
    put_string(&w, value);
    return !w.full;
}

bool tr_wake(tr_t *r)
{
    r->have_due = true;
    return operation(r, "");
}

bool tr_key(tr_t *r, const char *const *ids, size_t count)
{
    char fields[FIELDS_MAX];
    writer_t w = { fields, sizeof fields, false };
    if (count == 0) return false;
    put(&w, "\"key\":[");
    for (size_t i = 0; i < count; i++) {
        if (!ids[i]) return false;
        if (i) put(&w, ",");
        put_string(&w, ids[i]);
    }
    put(&w, "]");
    return !w.full && operation(r, fields);
}

bool tr_bubble(tr_t *r, const char *id)
{
    char fields[FIELDS_MAX];
    return member(fields, "bubble", id) && operation(r, fields);
}

bool tr_room(tr_t *r, const char *id)
{
    char fields[FIELDS_MAX];
    return member(fields, "room", id) && operation(r, fields);
}

bool tr_all_off(tr_t *r)
{
    return operation(r, "\"allOff\":true");
}

bool tr_input(tr_t *r, const char *value)
{
    char fields[FIELDS_MAX];
    return member(fields, "input", value) && operation(r, fields);
}

bool tr_sleep(tr_t *r)
{
    return operation(r, "\"sleep\":true");
}

/* ---- receiving ----------------------------------------------------------- */

/* A SYNC: everything in flight goes again in the new session, on the channel that answered. */
static void synced(tr_t *r)
{
    r->hello = false;
    r->want_session = false;
    r->persist->channel = r->channel;
    /* The HELLO carried the status and have: the bridge has both. */
    r->status_heard = r->hello_status;
    r->have_due = false;
    bool any = false;
    for (int i = 0; i < TR_PENDING_MAX; i++) {
        op_t *op = &r->ops[i];
        if (!op->used) continue;
        any = true;
        op->tries = 0;
        send_op(r, op);
    }
    set_link(r, any ? TR_LINK_CONNECTING : TR_LINK_CONNECTED);
}

static void acknowledged(tr_t *r, uint32_t number)
{
    for (int i = 0; i < TR_PENDING_MAX; i++) {
        op_t *op = &r->ops[i];
        /* A late ack still counts while the op waits for a new session: it was applied. */
        if (!op->used || op->number != number) continue;
        if (op->status && op->status == r->status_made) r->status_heard = op->status;
        if (op->have) r->have_due = false;
        memset(op, 0, sizeof *op);
        if (!r->hello) set_link(r, TR_LINK_CONNECTED);
        return;
    }
}

/* Moves the member name, an object (or null when nullable), into *slot.
 * Returns -1 when absent, else whether *slot now differs. */
static int take(cJSON *message, const char *name, cJSON **slot, bool nullable)
{
    cJSON *item = cJSON_GetObjectItemCaseSensitive(message, name);
    if (!item || !(cJSON_IsObject(item) || (nullable && cJSON_IsNull(item)))) return -1;
    cJSON_DetachItemViaPointer(message, item);
    if (cJSON_IsNull(item)) {
        cJSON_Delete(item);
        item = NULL;
    }
    bool same = (*slot == NULL && item == NULL) || (*slot && item && cJSON_Compare(*slot, item, true));
    cJSON_Delete(*slot);
    *slot = item;
    return !same;
}

static void apply(tr_t *r, const char *text, size_t length)
{
    cJSON *message = cJSON_ParseWithLength(text, length);
    if (!cJSON_IsObject(message)) {
        cJSON_Delete(message);
        return;
    }
    const cJSON *ack = cJSON_GetObjectItemCaseSensitive(message, "ack");
    if (cJSON_IsNumber(ack) && ack->valuedouble >= 0 && ack->valuedouble <= UINT32_MAX) {
        acknowledged(r, (uint32_t)ack->valuedouble);
    }
    /* The id is the bridge's digest of the rest: the same id is the same screen. */
    const cJSON *screen = cJSON_GetObjectItemCaseSensitive(message, "screen");
    const cJSON *id = cJSON_GetObjectItemCaseSensitive(screen, "id");
    bool same = r->screen && cJSON_IsString(id) && id->valuestring[0] && strcmp(id->valuestring, screen_id(r)) == 0;
    if (take(message, "screen", &r->screen, false) >= 0 && !same) r->what |= TR_CHANGED_SCREEN;
    if (take(message, "audio", &r->audio, true) > 0) r->what |= TR_CHANGED_AUDIO;
    /* A toast or an input list shows each time it arrives, even when repeated. */
    if (take(message, "toast", &r->toast, false) >= 0) r->what |= TR_CHANGED_TOAST;
    if (take(message, "inputs", &r->inputs, false) >= 0) r->what |= TR_CHANGED_INPUTS;
    cJSON_Delete(message);
}

void tr_receive(tr_t *r, const uint8_t *frame, size_t length)
{
    const char *message;
    size_t size;
    switch (tl_remote_receive(&r->persist->link, &r->assembler, r->key, frame, length, &message, &size)) {
    case TL_SYNCED:
        synced(r);
        break;
    case TL_MESSAGE:
        apply(r, message, size);
        break;
    default:
        break;
    }
    flush(r);
}

void tr_apply(tr_t *r, const char *message)
{
    apply(r, message, strlen(message));
    flush(r);
}

/* ---- timers -------------------------------------------------------------- */

/* A HELLO went unanswered: the next channel, or, after all of them, give up. */
static void hello_silent(tr_t *r)
{
    if (r->tried >= CHANNELS) {
        give_up(r);
        return;
    }
    r->tried++;
    r->channel = (uint8_t)(r->channel % CHANNELS + 1);
    r->platform.set_channel(r->platform.context, r->channel);
    send_hello(r);
}

/* An operation went unanswered: send it again, renew the session, or give up. */
static void op_silent(tr_t *r, op_t *op)
{
    if (op->last_chance) {
        give_up(r);
    } else if (op->tries < 2) {
        send_op(r, op);
        set_link(r, TR_LINK_CONNECTING);
    } else {
        op->last_chance = true;
        hold_all(r);
        connect(r);
    }
}

static uint32_t until(uint32_t clock, uint32_t due)
{
    return reached(clock, due) ? 0 : due - clock;
}

uint32_t tr_poll(tr_t *r)
{
    uint32_t clock = now(r);
    if (r->hello && reached(clock, r->hello_due)) hello_silent(r);
    if (!r->hello && r->persist->link.session == 0 && r->want_session) connect(r);
    for (int i = 0; i < TR_PENDING_MAX; i++) {
        op_t *op = &r->ops[i];
        if (op->used && !op->waiting && reached(clock, op->due)) op_silent(r, op);
    }
    flush(r);

    clock = now(r);
    uint32_t wait = UINT32_MAX;
    if (r->hello) wait = until(clock, r->hello_due);
    for (int i = 0; i < TR_PENDING_MAX; i++) {
        const op_t *op = &r->ops[i];
        if (op->used && !op->waiting && until(clock, op->due) < wait) wait = until(clock, op->due);
    }
    return wait;
}

/* ---- what to show -------------------------------------------------------- */

const cJSON *tr_screen(const tr_t *r)
{
    return r->screen;
}

const cJSON *tr_audio(const tr_t *r)
{
    return r->audio;
}

const cJSON *tr_toast(const tr_t *r)
{
    return r->toast;
}

const cJSON *tr_inputs(const tr_t *r)
{
    return r->inputs;
}

void tr_clear_toast(tr_t *r)
{
    cJSON_Delete(r->toast);
    r->toast = NULL;
}

void tr_clear_inputs(tr_t *r)
{
    cJSON_Delete(r->inputs);
    r->inputs = NULL;
}

tr_link_t tr_link(const tr_t *r)
{
    return r->link;
}

bool tr_idle(const tr_t *r)
{
    return pending(r) == 0;
}
