/*
 * The remote's console lines (handheld/PROTOCOL.md, "Remote console lines"),
 * and the line assembly for them. Pure C; tested on the host by ../test.
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "board_keys.h"

#ifdef __cplusplus
extern "C" {
#endif

#define CP_LINE_MAX 256
#define CP_KEY_LEN 32
#define CP_MAC_LEN 6

typedef enum {
    CP_IGNORE = 0, /* not a "thermite " line */
    CP_ERROR,      /* answer "thermite error <error>" */
    CP_HELLO,
    CP_ENROLL,
    CP_FORGET,
    CP_KEY,   /* test input: a hard key */
    CP_TOUCH, /* test input: the touch screen */
} cp_kind_t;

typedef struct {
    cp_kind_t kind;
    const char *error;
    uint8_t key[CP_KEY_LEN];  /* CP_ENROLL */
    uint8_t base[CP_MAC_LEN]; /* CP_ENROLL */
    uint8_t channel;          /* CP_ENROLL, 1-13 */
    board_key_t input;        /* CP_KEY */
    bool down, up;            /* CP_KEY: a press is both */
    bool touching;            /* CP_TOUCH: false lifts the finger */
    uint16_t x, y;            /* CP_TOUCH, display coordinates */
} cp_command_t;

/* One line without its newline; a trailing CR is ignored. */
void cp_parse(const char *line, size_t length, cp_command_t *command);

/* "thermite remote 1 <fw> <mac> <board> <0|1>\n"; returns the length, 0 when it does not fit. */
size_t cp_format_remote(char *out, size_t capacity, const char *fw, const uint8_t mac[CP_MAC_LEN], const char *board,
                        bool enrolled);

/* Bytes into lines; an overlong line is dropped whole and reported once. */
typedef struct {
    char data[CP_LINE_MAX + 1];
    size_t length;
    bool overflow, ready;
} cp_lines_t;
typedef enum { CP_LINE_NONE = 0, CP_LINE_READY, CP_LINE_TOO_LONG } cp_line_event_t;
cp_line_event_t cp_lines_push(cp_lines_t *lines, char byte);

#ifdef __cplusplus
}
#endif
