/* The board's console (USB-Serial-JTAG, or UART0 behind a USB-UART bridge) as lines:
 * a reader task queues every complete line, and thermite lines go out whole on stdout
 * between the log output. */
#pragma once

#include "console_proto.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

typedef struct {
    char text[CP_LINE_MAX + 1];
} console_line_t;

/* Starts the reader; each line goes to lines, then notify gets bit set. */
void console_start(QueueHandle_t lines, TaskHandle_t notify, uint32_t bit);

/* Writes one whole line (with its newline) and flushes it. */
void console_put(const char *line);
