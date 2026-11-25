#include "thermite_link.h"

#include <string.h>

#include "cJSON.h"
#include "mbedtls/ccm.h"
#include "mbedtls/platform_util.h"

#define NONCE_LEN 13
#define PARTS_MAX TL_PARTS_MAX

void tl_zeroize(void *buf, size_t len)
{
    if (buf && len) mbedtls_platform_zeroize(buf, len);
}

static void put64(uint8_t *at, uint64_t value)
{
    for (int i = 0; i < 8; i++) at[i] = (uint8_t)(value >> (8 * i));
}

static void put32(uint8_t *at, uint32_t value)
{
    for (int i = 0; i < 4; i++) at[i] = (uint8_t)(value >> (8 * i));
}

static uint64_t get64(const uint8_t *at)
{
    uint64_t value = 0;
    for (int i = 7; i >= 0; i--) value = (value << 8) | at[i];
    return value;
}

static uint32_t get32(const uint8_t *at)
{
    return (uint32_t)at[0] | (uint32_t)at[1] << 8 | (uint32_t)at[2] << 16 | (uint32_t)at[3] << 24;
}

static void nonce_of(tl_direction_t direction, const uint8_t *header, uint8_t nonce[NONCE_LEN])
{
    nonce[0] = (uint8_t)direction;
    memcpy(nonce + 1, header + 4, NONCE_LEN - 1);
}

size_t tl_seal(const uint8_t key[TL_KEY_LEN], tl_direction_t direction, const tl_header_t *header,
               const uint8_t *plaintext, size_t length, uint8_t *out, size_t capacity)
{
    if (length > TL_PART_MAX || capacity < TL_HEADER + length + TL_TAG || header->count < 1 || header->count > 16
        || header->index >= header->count) {
        return 0;
    }
    out[0] = TL_VERSION;
    out[1] = header->type;
    out[2] = header->message;
    out[3] = (uint8_t)(header->index << 4 | (header->count - 1));
    put64(out + 4, header->session);
    put32(out + 12, header->counter);
    uint8_t nonce[NONCE_LEN];
    nonce_of(direction, out, nonce);
    mbedtls_ccm_context ccm;
    mbedtls_ccm_init(&ccm);
    int failed = mbedtls_ccm_setkey(&ccm, MBEDTLS_CIPHER_ID_AES, key, TL_KEY_LEN * 8)
        || mbedtls_ccm_encrypt_and_tag(&ccm, length, nonce, NONCE_LEN, out, TL_HEADER, plaintext, out + TL_HEADER,
                                       out + TL_HEADER + length, TL_TAG);
    mbedtls_ccm_free(&ccm);
    return failed ? 0 : TL_HEADER + length + TL_TAG;
}

int tl_open(const uint8_t key[TL_KEY_LEN], tl_direction_t direction, const uint8_t *frame, size_t length,
            tl_header_t *header, uint8_t *out, size_t capacity)
{
    if (length < TL_HEADER + TL_TAG + 1 || length > TL_FRAME_MAX || frame[0] != TL_VERSION
        || frame[1] < TL_HELLO || frame[1] > TL_DATA) {
        return -1;
    }
    uint8_t index = frame[3] >> 4, count = (uint8_t)((frame[3] & 0x0f) + 1);
    size_t body = length - TL_HEADER - TL_TAG;
    if (index >= count || (frame[1] != TL_DATA && count != 1) || body > capacity) return -1;
    uint8_t nonce[NONCE_LEN];
    nonce_of(direction, frame, nonce);
    mbedtls_ccm_context ccm;
    mbedtls_ccm_init(&ccm);
    int failed = mbedtls_ccm_setkey(&ccm, MBEDTLS_CIPHER_ID_AES, key, TL_KEY_LEN * 8)
        || mbedtls_ccm_auth_decrypt(&ccm, body, nonce, NONCE_LEN, frame, TL_HEADER, frame + TL_HEADER, out,
                                    frame + TL_HEADER + body, TL_TAG);
    mbedtls_ccm_free(&ccm);
    if (failed) {
        tl_zeroize(out, body);
        return -1;
    }
    header->type = frame[1];
    header->message = frame[2];
    header->index = index;
    header->count = count;
    header->session = get64(frame + 4);
    header->counter = get32(frame + 12);
    return (int)body;
}

void tl_hex(uint64_t value, char out[17])
{
    static const char digits[] = "0123456789abcdef";
    for (int i = 15; i >= 0; i--, value >>= 4) out[i] = digits[value & 0x0f];
    out[16] = '\0';
}

void tl_remote_reset(tl_remote_t *remote)
{
    memset(remote, 0, sizeof *remote);
}

void tl_assembler_reset(tl_assembler_t *assembler)
{
    memset(assembler->offsets, 0, sizeof assembler->offsets);
    memset(assembler->lengths, 0, sizeof assembler->lengths);
    assembler->used = 0;
    assembler->received = 0;
    assembler->message = 0;
    assembler->count = 0;
    assembler->data[0] = '\0';
}

static void adopt(tl_remote_t *remote, uint64_t session)
{
    remote->session = session;
    remote->waiting = 0;
    remote->sent = 0;
    remote->heard = 0;
    remote->message = 0;
}

size_t tl_remote_hello(tl_remote_t *remote, const uint8_t key[TL_KEY_LEN], uint64_t nonce,
                       const char *status, size_t length, uint8_t *out, size_t capacity)
{
    if (nonce == 0) return 0;
    tl_header_t header = { .type = TL_HELLO, .count = 1, .session = nonce };
    size_t size = tl_seal(key, TL_FROM_REMOTE, &header, (const uint8_t *)status, length, out, capacity);
    if (size) remote->waiting = nonce;
    return size;
}

int tl_remote_send(tl_remote_t *remote, const uint8_t key[TL_KEY_LEN], const char *message, size_t length,
                   tl_emit_t emit, void *context)
{
    size_t parts = (length + TL_PART_MAX - 1) / TL_PART_MAX;
    if (remote->session == 0 || length == 0 || length > TL_MESSAGE_MAX || remote->sent > UINT32_MAX - parts) {
        return -1;
    }
    uint8_t frame[TL_FRAME_MAX];
    tl_header_t header = { .type = TL_DATA, .message = remote->message++, .count = (uint8_t)parts,
                           .session = remote->session };
    for (size_t index = 0; index < parts; index++) {
        size_t at = index * TL_PART_MAX, size = length - at < TL_PART_MAX ? length - at : TL_PART_MAX;
        header.index = (uint8_t)index;
        header.counter = ++remote->sent;
        size_t sealed = tl_seal(key, TL_FROM_REMOTE, &header, (const uint8_t *)message + at, size, frame, sizeof frame);
        if (!sealed) return -1;
        emit(context, frame, sealed);
    }
    return (int)parts;
}

static void reverse_bytes(uint8_t *buf, size_t len)
{
    for (size_t i = 0, j = len; i + 1 < j; i++, j--) {
        uint8_t tmp = buf[i];
        buf[i] = buf[j - 1];
        buf[j - 1] = tmp;
    }
}

static bool gather(tl_assembler_t *assembler, const tl_header_t *header, const uint8_t *part, size_t length,
                   size_t *whole)
{
    if (header->count < 1 || header->count > PARTS_MAX || header->index >= header->count || length == 0) {
        return false;
    }
    if (assembler->received == 0 || assembler->message != header->message || assembler->count != header->count) {
        tl_assembler_reset(assembler);
        assembler->message = header->message;
        assembler->count = header->count;
    }
    uint16_t bit = (uint16_t)(1u << header->index);
    if (assembler->received & bit) return false;
    if (assembler->used + length > TL_MESSAGE_MAX) {
        tl_assembler_reset(assembler);
        return false;
    }
    memcpy(assembler->data + assembler->used, part, length);
    assembler->offsets[header->index] = (uint16_t)assembler->used;
    assembler->lengths[header->index] = (uint16_t)length;
    assembler->used += length;
    assembler->received |= bit;
    uint16_t expected = (uint16_t)(((uint32_t)1u << header->count) - 1u);
    if (assembler->received != expected) return false;
    /* Reorder variable-length parts in place if they arrived out of order. */
    size_t dest = 0;
    for (uint8_t i = 0; i < header->count; i++) {
        size_t at = assembler->offsets[i];
        size_t len = assembler->lengths[i];
        if (at > dest) {
            size_t prefix = at - dest;
            reverse_bytes(assembler->data + dest, prefix);
            reverse_bytes(assembler->data + at, len);
            reverse_bytes(assembler->data + dest, prefix + len);
            for (uint8_t j = (uint8_t)(i + 1); j < header->count; j++) {
                if (assembler->offsets[j] >= dest && assembler->offsets[j] < at) {
                    assembler->offsets[j] = (uint16_t)(assembler->offsets[j] + len);
                }
            }
            assembler->offsets[i] = (uint16_t)dest;
        }
        dest += len;
    }
    *whole = assembler->used;
    assembler->data[*whole] = '\0';
    assembler->used = 0;
    assembler->received = 0;
    return true;
}

tl_event_t tl_remote_receive(tl_remote_t *remote, tl_assembler_t *assembler, const uint8_t key[TL_KEY_LEN],
                             const uint8_t *frame, size_t length, const char **message, size_t *size)
{
    static uint8_t part[TL_PART_MAX];
    tl_header_t header;
    int opened = tl_open(key, TL_FROM_BRIDGE, frame, length, &header, part, sizeof part);
    if (opened < 0) return TL_NOTHING;
    if (header.type == TL_SYNC) {
        if (remote->waiting == 0 || header.counter != 0 || header.session == 0) return TL_NOTHING;
        cJSON *sync = cJSON_ParseWithLength((const char *)part, (size_t)opened);
        const cJSON *hello = cJSON_IsObject(sync) ? cJSON_GetObjectItemCaseSensitive(sync, "hello") : NULL;
        char hex[17];
        tl_hex(remote->waiting, hex);
        bool matched = cJSON_IsString(hello) && strcmp(hello->valuestring, hex) == 0;
        cJSON_Delete(sync);
        if (!matched) return TL_NOTHING;
        adopt(remote, header.session);
        tl_assembler_reset(assembler);
        return TL_SYNCED;
    }
    if (header.type != TL_DATA || remote->session == 0 || header.session != remote->session
        || header.counter <= remote->heard) {
        return TL_NOTHING;
    }
    remote->heard = header.counter;
    size_t whole;
    if (!gather(assembler, &header, part, (size_t)opened, &whole)) return TL_NOTHING;
    *message = (const char *)assembler->data;
    *size = whole;
    return TL_MESSAGE;
}
