/* A PNG writer with no library: RGB, deflate's stored blocks. For the simulator's renders. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

/* Writes width x height RGB pixels (3 bytes each, rows top to bottom) to path. */
bool png_write(const char *path, const uint8_t *rgb, uint32_t width, uint32_t height);
