/* The remote app's key mapping and repeat, console lines and power policy. */
#include <stdio.h>
#include <string.h>

#include "console_proto.h"
#include "keymap.h"
#include "power_policy.h"

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

static int ids_are(board_key_t key, const char *a, const char *b)
{
    const char *const *ids;
    size_t n = keymap_ids(key, &ids);
    if (!a) return n == 0 && ids == NULL;
    if (b) return n == 2 && strcmp(ids[0], a) == 0 && strcmp(ids[1], b) == 0;
    return n == 1 && strcmp(ids[0], a) == 0;
}

static void test_keymap(void)
{
    /* PROTOCOL.md, "Hard keys", row by row. */
    CHECK(ids_are(BOARD_KEY_UP, "up", NULL) && ids_are(BOARD_KEY_DOWN, "down", NULL), "up/down");
    CHECK(ids_are(BOARD_KEY_LEFT, "left", NULL) && ids_are(BOARD_KEY_RIGHT, "right", NULL), "left/right");
    CHECK(ids_are(BOARD_KEY_OK, "ok", NULL) && ids_are(BOARD_KEY_BACK, "back", NULL), "ok/back");
    CHECK(ids_are(BOARD_KEY_VOLUME_UP, "volume_up", NULL) && ids_are(BOARD_KEY_VOLUME_DOWN, "volume_down", NULL),
          "volume");
    CHECK(ids_are(BOARD_KEY_MUTE, "mute", NULL), "mute");
    CHECK(ids_are(BOARD_KEY_CHANNEL_UP, "channel_up", NULL) && ids_are(BOARD_KEY_CHANNEL_DOWN, "channel_down", NULL),
          "channel");
    CHECK(ids_are(BOARD_KEY_PLAY, "play_pause", "play"), "play");
    CHECK(ids_are(BOARD_KEY_STOP, "stop", NULL), "stop");
    CHECK(ids_are(BOARD_KEY_REWIND, "rewind", "previous") && ids_are(BOARD_KEY_FORWARD, "fast_forward", "next"),
          "rewind/forward");
    CHECK(ids_are(BOARD_KEY_POWER, "power", NULL), "power");
    CHECK(ids_are(BOARD_KEY_HOME, "home", "menu") && ids_are(BOARD_KEY_SETTINGS, "settings", "menu"), "home/settings");
    CHECK(ids_are(BOARD_KEY_INFO, "info", "guide") && ids_are(BOARD_KEY_SOURCE, "input", NULL), "info/source");
    CHECK(ids_are(BOARD_KEY_RED, "red", NULL) && ids_are(BOARD_KEY_GREEN, "green", NULL) &&
              ids_are(BOARD_KEY_YELLOW, "yellow", NULL) && ids_are(BOARD_KEY_BLUE, "blue", NULL),
          "colours");
    CHECK(ids_are(BOARD_KEY_RECORD, NULL, NULL) && ids_are(BOARD_KEY_NONE, NULL, NULL) &&
              ids_are(BOARD_KEY_COUNT, NULL, NULL),
          "no entry");
    for (int k = BOARD_KEY_UP; k < BOARD_KEY_RECORD; k++) {
        const char *const *ids;
        CHECK(keymap_ids((board_key_t)k, &ids) > 0, "key %d has an entry", k);
    }

    cJSON *screen = cJSON_Parse("{\"buttons\":{\"volume_up\":{\"label\":\"+\",\"repeat\":true},"
                                "\"power\":{\"repeat\":false},\"mute\":{}}}");
    CHECK(keymap_repeats(screen, "volume_up"), "volume_up repeats");
    CHECK(!keymap_repeats(screen, "power") && !keymap_repeats(screen, "mute") && !keymap_repeats(screen, "up"),
          "others do not");
    CHECK(!keymap_repeats(NULL, "volume_up"), "no screen");
    cJSON_Delete(screen);
}

static void test_repeat(void)
{
    key_repeat_t r = {0};
    key_repeat_down(&r, BOARD_KEY_VOLUME_UP, true, 1000);
    CHECK(key_repeat_wait(&r, 1000) == 420, "first wait 420 (%u)", key_repeat_wait(&r, 1000));
    CHECK(key_repeat_due(&r, 1419) == BOARD_KEY_NONE, "not before 420 ms");
    CHECK(key_repeat_due(&r, 1420) == BOARD_KEY_VOLUME_UP, "at 420 ms");
    CHECK(key_repeat_due(&r, 1420) == BOARD_KEY_NONE, "once");
    CHECK(key_repeat_wait(&r, 1420) == 150, "then 150");
    CHECK(key_repeat_due(&r, 1570) == BOARD_KEY_VOLUME_UP, "at 570 ms");
    CHECK(key_repeat_due(&r, 1720) == BOARD_KEY_VOLUME_UP, "at 720 ms");
    /* Late by more than a period: one press, then on schedule from now. */
    CHECK(key_repeat_due(&r, 2500) == BOARD_KEY_VOLUME_UP && key_repeat_due(&r, 2500) == BOARD_KEY_NONE, "no burst");
    CHECK(key_repeat_wait(&r, 2500) == 150, "rescheduled");
    key_repeat_up(&r, BOARD_KEY_MUTE);
    CHECK(r.key == BOARD_KEY_VOLUME_UP, "another key's release keeps it");
    key_repeat_up(&r, BOARD_KEY_VOLUME_UP);
    CHECK(key_repeat_due(&r, 9999) == BOARD_KEY_NONE && key_repeat_wait(&r, 9999) == UINT32_MAX, "stopped");

    key_repeat_down(&r, BOARD_KEY_POWER, false, 0);
    CHECK(key_repeat_due(&r, 5000) == BOARD_KEY_NONE, "non-repeating key");
    key_repeat_down(&r, BOARD_KEY_VOLUME_UP, true, 0);
    key_repeat_down(&r, BOARD_KEY_POWER, false, 100);
    CHECK(key_repeat_due(&r, 5000) == BOARD_KEY_NONE, "a second key down stops the first's repeat");

    /* Across the 32-bit wrap of now_ms. */
    key_repeat_down(&r, BOARD_KEY_DOWN, true, UINT32_MAX - 100);
    CHECK(key_repeat_due(&r, UINT32_MAX) == BOARD_KEY_NONE, "wrap: not yet");
    CHECK(key_repeat_due(&r, 319) == BOARD_KEY_DOWN, "wrap: due 420 ms later");
}

static cp_command_t cmd;

static cp_kind_t parse(const char *line)
{
    cp_parse(line, strlen(line), &cmd);
    return cmd.kind;
}

static void test_console(void)
{
    const char *key = "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1F";
    char line[200];
    CHECK(parse("thermite hello") == CP_HELLO, "hello");
    CHECK(parse("thermite hello\r") == CP_HELLO, "hello CR");
    CHECK(parse("thermite forget") == CP_FORGET, "forget");
    CHECK(parse("thermite forget now") == CP_ERROR, "forget with argument");
    CHECK(parse("I (10) main: hello") == CP_IGNORE, "log");
    CHECK(parse("thermite base 1 0.1.0 aa:bb:cc:dd:ee:ff 1") == CP_ERROR, "a base line is not ours");
    snprintf(line, sizeof line, "thermite enroll %s 24:0a:c4:00:01:ff 11", key);
    CHECK(parse(line) == CP_ENROLL && cmd.channel == 11 && cmd.key[0] == 0 && cmd.key[31] == 0x1f &&
              cmd.base[0] == 0x24 && cmd.base[5] == 0xff,
          "enroll");
    snprintf(line, sizeof line, "thermite enroll %s 24:0a:c4:00:01:ff 14", key);
    CHECK(parse(line) == CP_ERROR, "channel 14");
    snprintf(line, sizeof line, "thermite enroll %s 24:0a:c4:00:01:ff 0", key);
    CHECK(parse(line) == CP_ERROR, "channel 0");
    snprintf(line, sizeof line, "thermite enroll %.62s 24:0a:c4:00:01:ff 1", key);
    CHECK(parse(line) == CP_ERROR, "short key");
    snprintf(line, sizeof line, "thermite enroll %.63sg 24:0a:c4:00:01:ff 1", key);
    CHECK(parse(line) == CP_ERROR, "bad key digit");
    snprintf(line, sizeof line, "thermite enroll %s 24:0a:c4:00:01 1", key);
    CHECK(parse(line) == CP_ERROR, "short mac");
    snprintf(line, sizeof line, "thermite enroll %s 24:0a:c4:00:01:ff", key);
    CHECK(parse(line) == CP_ERROR, "no channel");
    snprintf(line, sizeof line, "thermite enroll %s 24:0a:c4:00:01:ff 1 x", key);
    CHECK(parse(line) == CP_ERROR, "extra word");

    const uint8_t mac[6] = {0xde, 0xad, 0xbe, 0xef, 0x00, 0x01};
    CHECK(cp_format_remote(line, sizeof line, "0.1.0", mac, "omote-rev5", true) > 0 &&
              strcmp(line, "thermite remote 1 0.1.0 de:ad:be:ef:00:01 omote-rev5 1\n") == 0,
          "remote line %s", line);
    CHECK(cp_format_remote(line, sizeof line, "0.1.0", mac, "m5stick-s3", false) > 0 &&
              strcmp(line, "thermite remote 1 0.1.0 de:ad:be:ef:00:01 m5stick-s3 0\n") == 0,
          "unenrolled %s", line);
    CHECK(cp_format_remote(line, 20, "0.1.0", mac, "x", false) == 0, "no room");

    static cp_lines_t lines;
    int ready = 0, too_long = 0;
    static char big[CP_LINE_MAX + 10];
    memset(big, 'x', sizeof big - 1);
    const char *parts[] = {"thermite he", "llo\n", big, "\nthermite forget\n"};
    for (int p = 0; p < 4; p++) {
        for (const char *c = parts[p]; *c; c++) {
            cp_line_event_t e = cp_lines_push(&lines, *c);
            if (e == CP_LINE_TOO_LONG) too_long++;
            if (e == CP_LINE_READY) {
                ready++;
                CHECK(strcmp(lines.data, ready == 1 ? "thermite hello" : "thermite forget") == 0, "line %d", ready);
            }
        }
    }
    CHECK(ready == 2 && too_long == 1, "lines %d/%d", ready, too_long);
}

static void test_power(void)
{
    power_policy_t p;
    power_init(&p, 0);
    CHECK(power_step(&p, 9999, true) == POWER_NONE, "awake before 10 s");
    CHECK(power_wait(&p, 9999) == 1, "wait to dim");
    CHECK(power_step(&p, 10000, true) == POWER_DIM, "dim at 10 s");
    CHECK(power_step(&p, 10001, true) == POWER_NONE, "dim once");
    CHECK(power_input(&p, 12000) == POWER_BRIGHTEN, "input brightens");
    CHECK(power_input(&p, 12001) == POWER_NONE, "already bright");
    /* The last input was at 12001. */
    CHECK(power_step(&p, 22001, true) == POWER_DIM, "dim 10 s after the last input");
    CHECK(power_step(&p, 32000, true) == POWER_NONE, "not yet asleep");
    CHECK(power_step(&p, 32001, false) == POWER_NONE, "an operation in flight keeps it awake");
    CHECK(power_step(&p, 32100, true) == POWER_SEND_SLEEP, "idle: say sleep");
    CHECK(power_step(&p, 32150, false) == POWER_NONE, "wait for the sleep ack");
    CHECK(power_wait(&p, 32150) == 250, "ack wait left (%u)", power_wait(&p, 32150));
    CHECK(power_step(&p, 32200, true) == POWER_SLEEP_NOW, "acked: sleep");

    power_init(&p, 0);
    power_step(&p, 10000, true);
    CHECK(power_step(&p, 20000, true) == POWER_SEND_SLEEP, "sleep sent");
    CHECK(power_step(&p, 20299, false) == POWER_NONE && power_step(&p, 20300, false) == POWER_SLEEP_NOW,
          "no ack: sleep after 300 ms");

    power_init(&p, 0);
    power_step(&p, 10000, true);
    power_step(&p, 20000, true);
    CHECK(power_input(&p, 20100) == POWER_BRIGHTEN && power_step(&p, 20500, false) == POWER_NONE,
          "input while waiting for the ack cancels sleep");

    power_init(&p, 0);
    power_step(&p, 10000, false);
    CHECK(power_step(&p, 59999, false) == POWER_NONE && power_step(&p, 60000, false) == POWER_SLEEP_NOW,
          "forced sleep after 60 s");
}

int main(void)
{
    test_keymap();
    test_repeat();
    test_console();
    test_power();
    if (failures) {
        fprintf(stderr, "%d failures\n", failures);
        return 1;
    }
    puts("remote app: ok");
    return 0;
}
