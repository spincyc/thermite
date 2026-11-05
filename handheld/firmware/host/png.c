#include "png.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static uint32_t crc_table[256];

static uint32_t crc(uint32_t value, const uint8_t *data, size_t size)
{
    if (!crc_table[1]) {
        for (uint32_t n = 0; n < 256; n++) {
            uint32_t c = n;
            for (int k = 0; k < 8; k++) c = c & 1 ? 0xedb88320u ^ (c >> 1) : c >> 1;
            crc_table[n] = c;
        }
    }
    value = ~value;
    for (size_t i = 0; i < size; i++) value = crc_table[(value ^ data[i]) & 0xff] ^ (value >> 8);
    return ~value;
}

static void be32(uint8_t *at, uint32_t value)
{
    at[0] = (uint8_t)(value >> 24);
    at[1] = (uint8_t)(value >> 16);
    at[2] = (uint8_t)(value >> 8);
    at[3] = (uint8_t)value;
}

static bool chunk(FILE *file, const char *type, const uint8_t *data, uint32_t size)
{
    uint8_t head[8], tail[4];
    be32(head, size);
    memcpy(head + 4, type, 4);
    be32(tail, crc(crc(0, head + 4, 4), data, size));
    return fwrite(head, 1, 8, file) == 8 && (size == 0 || fwrite(data, 1, size, file) == size)
        && fwrite(tail, 1, 4, file) == 4;
}

bool png_write(const char *path, const uint8_t *rgb, uint32_t width, uint32_t height)
{
    /* The raw stream: each row is a filter byte (0, none) and its pixels. */
    size_t row = (size_t)width * 3 + 1, raw_size = row * height;
    size_t blocks = (raw_size + 65534) / 65535;
    size_t zsize = 2 + raw_size + blocks * 5 + 4;
    uint8_t *z = malloc(zsize);
    if (!z) return false;
    size_t at = 0;
    z[at++] = 0x78; /* zlib: deflate, 32 KiB window */
    z[at++] = 0x01;
    uint32_t a = 1, b = 0;
    size_t done = 0;
    for (size_t block = 0; block < blocks; block++) {
        size_t size = raw_size - done < 65535 ? raw_size - done : 65535;
        z[at++] = block + 1 == blocks; /* BFINAL, stored */
        z[at++] = (uint8_t)size;
        z[at++] = (uint8_t)(size >> 8);
        z[at++] = (uint8_t)~size;
        z[at++] = (uint8_t)(~size >> 8);
        for (size_t i = 0; i < size; i++, done++) {
            size_t y = done / row, x = done % row;
            uint8_t byte = x == 0 ? 0 : rgb[y * width * 3 + x - 1];
            z[at++] = byte;
            a = (a + byte) % 65521;
            b = (b + a) % 65521;
        }
    }
    be32(z + at, b << 16 | a);
    at += 4;

    static const uint8_t signature[8] = { 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n' };
    uint8_t header[13];
    be32(header, width);
    be32(header + 4, height);
    header[8] = 8; /* bits per channel */
    header[9] = 2; /* RGB */
    header[10] = header[11] = header[12] = 0;
    FILE *file = fopen(path, "wb");
    bool ok = file && fwrite(signature, 1, 8, file) == 8 && chunk(file, "IHDR", header, 13)
        && chunk(file, "IDAT", z, (uint32_t)at) && chunk(file, "IEND", NULL, 0);
    if (file && fclose(file) != 0) ok = false;
    free(z);
    return ok;
}
