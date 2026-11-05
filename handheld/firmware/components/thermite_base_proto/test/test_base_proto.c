/* The base station's line protocol (handheld/PROTOCOL.md, "Base station lines"). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "base_proto.h"

static int failures;

#define CHECK(condition, ...)                                                                                          \
    do {                                                                                                               \
        if (!(condition)) {                                                                                            \
            failures++;                                                                                                \
            fprintf(stderr, "%s:%d: ", __FILE__, __LINE__);                                                            \
            fprintf(stderr, __VA_ARGS__);                                                                              \
            fputc('\n', stderr);                                                                                       \
        }                                                                                                              \
    } while (0)

static bp_command_t command;

static bp_kind_t parse(const char *line)
{
    bp_parse(line, strlen(line), &command);
    return command.kind;
}

static void test_base64(void)
{
    /* RFC 4648 section 10 vectors. */
    const char *plain[] = {"", "f", "fo", "foo", "foob", "fooba", "foobar"};
    const char *coded[] = {"", "Zg==", "Zm8=", "Zm9v", "Zm9vYg==", "Zm9vYmE=", "Zm9vYmFy"};
    for (int i = 0; i < 7; i++) {
        char text[16];
        uint8_t back[16];
        size_t n = bp_base64_encode((const uint8_t *)plain[i], strlen(plain[i]), text, sizeof text);
        CHECK(n == strlen(coded[i]) && strcmp(text, coded[i]) == 0, "encode %s gave %s", plain[i], text);
        int m = bp_base64_decode(coded[i], strlen(coded[i]), back, sizeof back);
        CHECK(m == (int)strlen(plain[i]) && memcmp(back, plain[i], (size_t)m) == 0, "decode %s", coded[i]);
    }
    uint8_t out[8];
    CHECK(bp_base64_decode("Zm9", 3, out, sizeof out) < 0, "length not a multiple of 4");
    CHECK(bp_base64_decode("Zm=v", 4, out, sizeof out) < 0, "data after padding");
    CHECK(bp_base64_decode("Z===", 4, out, sizeof out) < 0, "three pads");
    CHECK(bp_base64_decode("Zh==", 4, out, sizeof out) < 0, "non-canonical bits under padding");
    CHECK(bp_base64_decode("Zm9v-A==", 8, out, sizeof out) < 0, "url alphabet refused");
    CHECK(bp_base64_decode("Zm9vYmFy", 8, out, 5) < 0, "no room");
    char small[4];
    CHECK(bp_base64_encode((const uint8_t *)"foo", 3, small, sizeof small) == 0, "encode without room for NUL");
}

static void test_mac(void)
{
    uint8_t mac[6];
    CHECK(bp_mac_parse("aa:BB:0c:dd:ee:0f", 17, mac) && mac[1] == 0xbb && mac[5] == 0x0f, "mac parse");
    CHECK(!bp_mac_parse("aa:bb:cc:dd:ee:f", 16, mac), "short mac");
    CHECK(!bp_mac_parse("aa-bb-cc-dd-ee-ff", 17, mac), "dashes");
    CHECK(!bp_mac_parse("aa:bb:cc:dd:ee:fg", 17, mac), "bad hex");
    char text[18];
    bp_mac_format((const uint8_t[]){0xAA, 0xBB, 0x0C, 0xDD, 0xEE, 0x0F}, text);
    CHECK(strcmp(text, "aa:bb:0c:dd:ee:0f") == 0, "mac format %s", text);
}

static void test_parse(void)
{
    CHECK(parse("") == BP_IGNORE, "empty line");
    CHECK(parse("I (123) boot: something") == BP_IGNORE, "log line");
    CHECK(parse("thermitehello") == BP_IGNORE, "no space");
    CHECK(parse("thermite hello") == BP_HELLO, "hello");
    CHECK(parse("thermite hello\r") == BP_HELLO, "hello with CR");
    CHECK(parse("thermite hello x") == BP_ERROR, "hello with argument");
    CHECK(parse("thermite ") == BP_ERROR, "empty command");
    CHECK(parse("thermite frob") == BP_ERROR && strcmp(command.error, "unknown command") == 0, "unknown");

    CHECK(parse("thermite channel 1") == BP_CHANNEL && command.channel == 1, "channel 1");
    CHECK(parse("thermite channel 13") == BP_CHANNEL && command.channel == 13, "channel 13");
    CHECK(parse("thermite channel 0") == BP_ERROR, "channel 0");
    CHECK(parse("thermite channel 14") == BP_ERROR, "channel 14");
    CHECK(parse("thermite channel 06") == BP_ERROR, "leading zero");
    CHECK(parse("thermite channel") == BP_ERROR, "no channel");
    CHECK(parse("thermite channel 6 7") == BP_ERROR, "two channels");
    CHECK(parse("thermite channel 6 ") == BP_ERROR, "trailing space");

    CHECK(parse("thermite send 7 aa:bb:cc:dd:ee:ff Zm9vYmFy") == BP_SEND, "send");
    CHECK(command.id == 7 && command.length == 6 && memcmp(command.frame, "foobar", 6) == 0 && command.mac[0] == 0xaa,
          "send fields");
    CHECK(parse("thermite send 65535 aa:bb:cc:dd:ee:ff Zm9v") == BP_SEND && command.id == 65535, "max id");
    CHECK(parse("thermite send 0 aa:bb:cc:dd:ee:ff Zm9v") == BP_ERROR, "id 0");
    CHECK(parse("thermite send 65536 aa:bb:cc:dd:ee:ff Zm9v") == BP_ERROR, "id too big");
    CHECK(parse("thermite send -1 aa:bb:cc:dd:ee:ff Zm9v") == BP_ERROR, "negative id");
    CHECK(parse("thermite send 7 aa:bb:cc:dd:ee Zm9v") == BP_ERROR, "short mac");
    CHECK(parse("thermite send 7 aa:bb:cc:dd:ee:ff Zm9") == BP_ERROR, "bad base64");
    CHECK(parse("thermite send 7 aa:bb:cc:dd:ee:ff") == BP_ERROR, "no frame");
    CHECK(parse("thermite send 7 aa:bb:cc:dd:ee:ff Zm9v x") == BP_ERROR, "extra word");
    CHECK(parse("thermite send 7  aa:bb:cc:dd:ee:ff Zm9v") == BP_ERROR, "double space");

    /* Frame size limits: 1470 bytes is the largest. */
    static uint8_t frame[1471];
    for (size_t i = 0; i < sizeof frame; i++) frame[i] = (uint8_t)i;
    static char line[BP_LINE_MAX + 2], text[2100];
    bp_base64_encode(frame, 1470, text, sizeof text);
    snprintf(line, sizeof line, "thermite send 65535 aa:bb:cc:dd:ee:ff %s", text);
    CHECK(strlen(line) <= BP_LINE_MAX, "largest send fits a line (%zu)", strlen(line));
    CHECK(parse(line) == BP_SEND && command.length == 1470 && memcmp(command.frame, frame, 1470) == 0, "1470 bytes");
    bp_base64_encode(frame, 1471, text, sizeof text);
    snprintf(line, sizeof line, "thermite send 1 aa:bb:cc:dd:ee:ff %s", text);
    CHECK(parse(line) == BP_ERROR, "1471 bytes refused");
}

static void test_format(void)
{
    char line[BP_LINE_MAX + 2];
    const uint8_t mac[6] = {0x24, 0x0a, 0xc4, 0x00, 0x01, 0xff};
    CHECK(bp_format_base(line, sizeof line, "0.1.0", mac, 6) > 0 &&
              strcmp(line, "thermite base 1 0.1.0 24:0a:c4:00:01:ff 6\n") == 0,
          "base line %s", line);
    CHECK(bp_format_sent(line, sizeof line, 65535, true) > 0 && strcmp(line, "thermite sent 65535 ok\n") == 0,
          "sent ok");
    CHECK(bp_format_sent(line, sizeof line, 1, false) > 0 && strcmp(line, "thermite sent 1 fail\n") == 0, "sent fail");
    CHECK(bp_format_rx(line, sizeof line, mac, -67, (const uint8_t *)"foobar", 6) > 0 &&
              strcmp(line, "thermite rx 24:0a:c4:00:01:ff -67 Zm9vYmFy\n") == 0,
          "rx %s", line);
    CHECK(bp_format_error(line, sizeof line, "line too long") > 0 &&
              strcmp(line, "thermite error line too long\n") == 0,
          "error");
    static uint8_t big[1470];
    size_t n = bp_format_rx(line, sizeof line, mac, -100, big, sizeof big);
    CHECK(n > 0 && n <= BP_LINE_MAX + 1 && line[n - 1] == '\n', "largest rx line fits (%zu)", n);
    CHECK(bp_format_rx(line, 40, mac, -1, big, sizeof big) == 0 && line[0] == '\0', "rx without room");
    CHECK(bp_format_sent(line, 10, 1, true) == 0, "sent without room");
}

static void push(bp_lines_t *lines, const char *text, int *ready, int *too_long, char *last, size_t last_size)
{
    for (const char *p = text; *p; p++) {
        bp_line_event_t event = bp_lines_push(lines, *p);
        if (event == BP_LINE_READY) {
            (*ready)++;
            snprintf(last, last_size, "%s", lines->data);
            CHECK(lines->length == strlen(lines->data), "length matches");
        }
        if (event == BP_LINE_TOO_LONG) (*too_long)++;
    }
}

static void test_lines(void)
{
    static bp_lines_t lines;
    int ready = 0, too_long = 0;
    char last[64];
    push(&lines, "thermite hello\nthermite chan", &ready, &too_long, last, sizeof last);
    CHECK(ready == 1 && strcmp(last, "thermite hello") == 0, "first line");
    push(&lines, "nel 3\r\n", &ready, &too_long, last, sizeof last);
    CHECK(ready == 2 && strcmp(last, "thermite channel 3\r") == 0, "split line");
    push(&lines, "\n", &ready, &too_long, last, sizeof last);
    CHECK(ready == 3 && last[0] == '\0', "empty line");

    static char huge[BP_LINE_MAX + 50];
    memset(huge, 'x', sizeof huge - 1);
    huge[sizeof huge - 1] = '\0';
    push(&lines, huge, &ready, &too_long, last, sizeof last);
    CHECK(too_long == 1 && ready == 3, "too long reported once (%d)", too_long);
    push(&lines, "\nthermite hello\n", &ready, &too_long, last, sizeof last);
    CHECK(ready == 4 && strcmp(last, "thermite hello") == 0 && too_long == 1, "recovers after an overlong line");

    memset(huge, 'y', BP_LINE_MAX);
    huge[BP_LINE_MAX] = '\n';
    huge[BP_LINE_MAX + 1] = '\0';
    push(&lines, huge, &ready, &too_long, last, sizeof last);
    CHECK(ready == 5 && too_long == 1 && lines.length == BP_LINE_MAX, "a line of exactly BP_LINE_MAX is accepted");
}

static void test_peers(void)
{
    bp_peers_t peers;
    uint8_t evicted[6], mac[6] = {0};
    bp_peers_init(&peers, 3);
    for (uint8_t i = 1; i <= 3; i++) {
        mac[5] = i;
        CHECK(bp_peers_use(&peers, mac, evicted) == BP_PEER_ADD, "add %u", i);
    }
    mac[5] = 1;
    CHECK(bp_peers_use(&peers, mac, evicted) == BP_PEER_KNOWN, "known");
    mac[5] = 4;
    CHECK(bp_peers_use(&peers, mac, evicted) == BP_PEER_REPLACE && evicted[5] == 2, "evicts least recent (2)");
    mac[5] = 5;
    CHECK(bp_peers_use(&peers, mac, evicted) == BP_PEER_REPLACE && evicted[5] == 3, "then 3");
    mac[5] = 1;
    CHECK(bp_peers_use(&peers, mac, evicted) == BP_PEER_KNOWN, "1 kept: it was used");
    bp_peers_forget(&peers, mac);
    CHECK(peers.count == 2, "forget");
    CHECK(bp_peers_use(&peers, mac, evicted) == BP_PEER_ADD, "re-add after forget");

    /* Clock wrap keeps least-recent order. */
    bp_peers_init(&peers, 2);
    peers.clock = UINT32_MAX - 1;
    mac[5] = 1;
    bp_peers_use(&peers, mac, evicted); /* used = UINT32_MAX */
    mac[5] = 2;
    bp_peers_use(&peers, mac, evicted); /* used = 0 */
    mac[5] = 3;
    CHECK(bp_peers_use(&peers, mac, evicted) == BP_PEER_REPLACE && evicted[5] == 1, "wrap");
    bp_peers_init(&peers, 0);
    CHECK(peers.capacity == BP_PEERS_MAX, "capacity defaults to the ESP-NOW limit");
}

int main(void)
{
    test_base64();
    test_mac();
    test_parse();
    test_format();
    test_lines();
    test_peers();
    if (failures) {
        fprintf(stderr, "%d failures\n", failures);
        return 1;
    }
    puts("base_proto: ok");
    return 0;
}
