/* Text for LVGL's built-in fonts (thermite_ui private). */
#pragma once

#include <stddef.h>

/*
 * Copies text into out (always NUL-terminated), replacing what the built-in
 * Montserrat fonts cannot draw. They hold ASCII, the degree sign, the bullet
 * and LV_SYMBOL_*; the screen model's text uses a few more marks:
 *
 *   U+00B7 middle dot             -> U+2022 bullet
 *   U+2192 / U+2190 arrows        -> "->" / "<-"
 *   U+2212 minus, U+2013, U+2014  -> "-"
 *   U+2026 ellipsis               -> "..."
 *   U+2018 U+2019 U+201A, U+2032  -> "'"
 *   U+201C U+201D U+201E, U+2033  -> "\""
 *   U+00A0, U+2007, U+2009, U+202F spaces -> " "
 *   U+00D7 multiplication sign    -> "x"
 *   U+26A0 warning sign           -> LV_SYMBOL_WARNING
 *
 * Anything else passes through unchanged: accented letters have no glyph in
 * these fonts and LVGL leaves them out when drawing.
 */
void tu_normalize(const char *text, char *out, size_t capacity);

/* The LV_SYMBOL_* text for a button spec's icon name, or NULL when the fonts have none. */
const char *tu_icon(const char *name);
