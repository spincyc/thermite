/*
 * The remote's link to its base. On hardware, ESP-NOW (radio_espnow.c). On
 * the qemu board, which has no radio, the base station's own lines
 * (handheld/PROTOCOL.md) on UART1 (radio_lines.c): the firmware plays both
 * base and remote, as the host simulator does, against
 * thermite handheld serve --listen. Exactly one is built, by
 * CONFIG_THERMITE_RADIO_LINES.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "thermite_link.h"

#define RADIO_MAC_LEN 6

/* One received frame, as the radio queues it for the app task. */
typedef struct {
    uint16_t length;
    uint8_t data[TL_FRAME_MAX];
} radio_frame_t;

/* At boot, enrolled or not, with this remote's MAC. The line radio announces
 * its base here, so the bridge knows the base before enrolment. */
void radio_init(const uint8_t mac[RADIO_MAC_LEN]);

/* Starts the link to the enrolled base on channel: received frames go to frames,
 * then notify gets bit. */
void radio_start(const uint8_t base[RADIO_MAC_LEN], uint8_t channel, QueueHandle_t frames, TaskHandle_t notify,
                 uint32_t bit);

/* One frame to the base; a lost frame is the core's to retry. */
void radio_send(const uint8_t *frame, size_t length);

/* Listen and send on this channel, 1-13, from now on. */
void radio_set_channel(uint8_t channel);

/* Before deep sleep. */
void radio_stop(void);
