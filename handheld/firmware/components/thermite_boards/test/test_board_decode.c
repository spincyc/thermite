/* The board drivers' register decoding and arithmetic (board_decode.c), against the
 * datasheets' own examples where they give one. */
#include <stdio.h>
#include <string.h>

#include "board_decode.h"

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

static void test_tca8418(void)
{
    tca_event_t e = tca8418_decode(0x00);
    CHECK(e.kind == TCA_EVENT_NONE && !e.press, "empty FIFO");
    /* TI SCPS215 table 1: key 1 is ROW0 COL0; key number = row * 10 + column + 1. */
    e = tca8418_decode(0x80 | 1);
    CHECK(e.kind == TCA_EVENT_KEY && e.press && e.row == 0 && e.column == 0, "key 1 press");
    e = tca8418_decode(1);
    CHECK(e.kind == TCA_EVENT_KEY && !e.press, "key 1 release");
    e = tca8418_decode(0x80 | 45); /* row 4, column 4: OMOTE's Record */
    CHECK(e.row == 4 && e.column == 4, "key 45 is row 4 column 4 (%u, %u)", e.row, e.column);
    e = tca8418_decode(0x80 | 12);
    CHECK(e.row == 1 && e.column == 1, "key 12 is row 1 column 1");
    e = tca8418_decode(80);
    CHECK(e.kind == TCA_EVENT_KEY && e.row == 7 && e.column == 9, "key 80 is row 7 column 9");
    e = tca8418_decode(0x80 | 102); /* ROW5 GPI: OMOTE's SW_PWR */
    CHECK(e.kind == TCA_EVENT_GPI_ROW && e.pin == 5 && e.press, "GPI ROW5 press");
    e = tca8418_decode(97);
    CHECK(e.kind == TCA_EVENT_GPI_ROW && e.pin == 0 && !e.press, "GPI ROW0 release");
    e = tca8418_decode(0x80 | 114);
    CHECK(e.kind == TCA_EVENT_GPI_COL && e.pin == 9, "GPI COL9");
    e = tca8418_decode(0x80 | 90);
    CHECK(e.kind == TCA_EVENT_NONE && !e.press, "reserved code");
}

static void test_ft6x06(void)
{
    uint16_t x = 0, y = 0;
    const uint8_t touched[5] = {0x01, 0x80 | 0x00, 0xEF, 0x01, 0x3F}; /* contact at 239, 319 */
    CHECK(ft6x06_decode(touched, &x, &y) && x == 239 && y == 319, "point %u,%u", x, y);
    const uint8_t down[5] = {0x01, 0x01, 0x02, 0x10, 0x03}; /* put down at 258, 3; ID 1 */
    CHECK(ft6x06_decode(down, &x, &y) && x == 258 && y == 3, "put down %u,%u", x, y);
    const uint8_t lift[5] = {0x01, 0x40, 0x10, 0x00, 0x10};
    CHECK(!ft6x06_decode(lift, &x, &y), "lift-up event");
    const uint8_t none[5] = {0x00, 0x80, 0x10, 0x00, 0x10};
    CHECK(!ft6x06_decode(none, &x, &y), "no points");
    const uint8_t garbage[5] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF}; /* before the first touch, per OMOTE */
    CHECK(!ft6x06_decode(garbage, &x, &y), "0xFF before the first touch");
}

static void test_cst328(void)
{
    uint16_t x = 0, y = 0;
    /* X = 0x0EF (239): high 0x0E, low nibble 0xF; Y = 0x13F (319): high 0x13, low 0xF. */
    const uint8_t one[7] = {0x06, 0x0E, 0x13, 0xFF, 0x20, 0x01, 0xAB};
    CHECK(cst328_decode(one, &x, &y) && x == 239 && y == 319, "point %u,%u", x, y);
    const uint8_t origin[7] = {0x06, 0x00, 0x00, 0x00, 0x20, 0x01, 0xAB};
    CHECK(cst328_decode(origin, &x, &y) && x == 0 && y == 0, "origin");
    const uint8_t none[7] = {0x00, 0x0E, 0x13, 0xFF, 0x00, 0x00, 0xAB};
    CHECK(!cst328_decode(none, &x, &y), "no fingers");
    const uint8_t bad[7] = {0x06, 0x0E, 0x13, 0xFF, 0x20, 0x0F, 0xAB};
    CHECK(!cst328_decode(bad, &x, &y), "15 fingers is not a report");
}

static void test_cst3530(void)
{
    uint8_t r[CST3530_REPORT] = {0};
    uint16_t x = 0, y = 0;
    r[2] = 0xFF;
    r[3] = 0x01; /* one finger, no keys */
    /* X = 0x1EF (495): low 0xEF, high nibble 1 in byte 7's low nibble; Y = 0x2AB (683). */
    r[4] = 0xEF;
    r[5] = 0xAB;
    r[6] = 0x30;
    r[7] = 0x21;
    r[8] = 0x10; /* event 1, id 0 */
    uint16_t sum = 0x55;
    for (int i = 4; i < 9; i++) sum = (uint16_t)(sum + r[i]);
    r[0] = (uint8_t)sum;
    r[1] = (uint8_t)(sum >> 8);
    CHECK(cst3530_decode(r, &x, &y) && x == 0x1EF && y == 0x2AB, "point %u,%u", x, y);
    r[0] ^= 1;
    CHECK(!cst3530_decode(r, &x, &y), "bad checksum");
    r[0] ^= 1;
    r[2] = 0x00;
    CHECK(!cst3530_decode(r, &x, &y), "bad marker");
    r[2] = 0xFF;
    r[3] = 0x00;
    r[0] = 0x55;
    r[1] = 0;
    CHECK(!cst3530_decode(r, &x, &y), "no fingers");
}

static void test_max17048(void)
{
    /* Datasheet: VCELL 78.125 uV/LSB. 0xD000 = 53248 LSB = 4160 mV. */
    CHECK(max17048_millivolts(0xD000) == 4160, "4160 mV (%u)", max17048_millivolts(0xD000));
    CHECK(max17048_millivolts(0) == 0, "0 mV");
    CHECK(max17048_millivolts(0xFFFF) == 5120, "full scale 5.12 V (%u)", max17048_millivolts(0xFFFF));
    CHECK(max17048_percent(0x3280) == 50, "SOC 50.5 %% reads 50");
    CHECK(max17048_percent(0x6480) == 100, "100.5 %% reads 100");
    CHECK(max17048_percent(0xFF00) == 100, "clamped");
    CHECK(max17048_percent(0x0000) == 0, "empty");
    CHECK(max17048_crate_tenths(100) == 208, "100 LSB = 20.8 %%/h");
    CHECK(max17048_crate_tenths(0xFF9C) == -208, "-100 LSB = -20.8 %%/h");
}

static void test_lipo(void)
{
    CHECK(lipo_percent(4300) == 100 && lipo_percent(4200) == 100, "full");
    CHECK(lipo_percent(3000) == 0 && lipo_percent(3300) == 0, "empty");
    CHECK(lipo_percent(3800) == 50, "3.8 V is 50 %%");
    CHECK(lipo_percent(3775) == 45, "interpolates (%d)", lipo_percent(3775));
    int last = -1;
    for (uint32_t mv = 3200; mv <= 4300; mv += 5) {
        int p = lipo_percent(mv);
        CHECK(p >= last && p >= 0 && p <= 100, "monotonic at %u", (unsigned)mv);
        last = p;
    }
}

static void test_debounce(void)
{
    debounce_t d = {0};
    CHECK(!debounce_update(&d, true, 100), "first sample starts the timer");
    CHECK(!debounce_update(&d, true, 119), "not stable yet");
    CHECK(debounce_update(&d, true, 120) && d.stable, "pressed after 20 ms");
    CHECK(!debounce_update(&d, true, 200), "no repeat edge");
    CHECK(!debounce_update(&d, false, 210), "bounce");
    CHECK(!debounce_update(&d, true, 212), "bounce back");
    CHECK(!debounce_update(&d, true, 240) && d.stable, "still pressed: no edge");
    CHECK(!debounce_update(&d, false, 300) && !debounce_update(&d, false, 319), "release settling");
    CHECK(debounce_update(&d, false, 320) && !d.stable, "released");
    debounce_t w = {.stable = false, .raw = false, .since = UINT32_MAX - 5};
    CHECK(!debounce_update(&w, true, UINT32_MAX - 5) && debounce_update(&w, true, 15), "across the wrap");
}

static void test_touch_map(void)
{
    uint16_t x, y;
    touch_map_t same = {.raw_width = 240, .raw_height = 320, .width = 240, .height = 320};
    touch_map(&same, 10, 20, &x, &y);
    CHECK(x == 10 && y == 20, "identity");
    touch_map(&same, 500, 500, &x, &y);
    CHECK(x == 239 && y == 319, "clamped");
    touch_map_t mirrored = same;
    mirrored.mirror_x = mirrored.mirror_y = true;
    touch_map(&mirrored, 0, 0, &x, &y);
    CHECK(x == 239 && y == 319, "mirrored corner");
    touch_map_t swapped = {.raw_width = 240, .raw_height = 320, .width = 240, .height = 320, .swap_xy = true};
    touch_map(&swapped, 300, 5, &x, &y);
    CHECK(x == 5 && y == 300, "swap %u,%u", x, y);
    touch_map_t scaled = {.raw_width = 480, .raw_height = 640, .width = 240, .height = 320};
    touch_map(&scaled, 479, 320, &x, &y);
    CHECK(x == 239 && y == 160, "scaled %u,%u", x, y);
}

int main(void)
{
    test_tca8418();
    test_ft6x06();
    test_cst328();
    test_cst3530();
    test_max17048();
    test_lipo();
    test_debounce();
    test_touch_map();
    if (failures) {
        fprintf(stderr, "%d failures\n", failures);
        return 1;
    }
    puts("board_decode: ok");
    return 0;
}
