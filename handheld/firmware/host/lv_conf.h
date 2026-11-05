/*
 * LVGL 9.2 for the host build (tests, renders, the simulator). It mirrors
 * components/thermite_ui/sdkconfig.lvgl, which the device build uses through
 * Kconfig; keep the two in step. Everything not set here takes LVGL's default
 * (lv_conf_internal.h), as Kconfig does.
 */
#ifndef LV_CONF_H
#define LV_CONF_H /* lv_conf_internal.h checks that this file was found */

#define LV_COLOR_DEPTH 16

#define LV_USE_STDLIB_MALLOC LV_STDLIB_CLIB
#define LV_USE_STDLIB_STRING LV_STDLIB_CLIB
#define LV_USE_STDLIB_SPRINTF LV_STDLIB_CLIB
#define LV_USE_OS LV_OS_NONE

/* Warnings and errors reach the registered print callback; the render check fails on them. */
#define LV_USE_LOG 1
#define LV_LOG_LEVEL LV_LOG_LEVEL_WARN
#define LV_LOG_PRINTF 0

#define LV_FONT_MONTSERRAT_10 1
#define LV_FONT_MONTSERRAT_12 1
#define LV_FONT_MONTSERRAT_14 1
#define LV_FONT_MONTSERRAT_16 1
#define LV_FONT_MONTSERRAT_20 1
#define LV_FONT_DEFAULT &lv_font_montserrat_14

#define LV_USE_FLEX 1
#define LV_USE_THEME_DEFAULT 1
#define LV_THEME_DEFAULT_DARK 1

/* The simulator's window, mouse and keyboard; CMake sets THERMITE_SIM_SDL when SDL2 is found. */
#ifdef THERMITE_SIM_SDL
#define LV_USE_SDL 1
#define LV_SDL_INCLUDE_PATH <SDL2/SDL.h>
#define LV_SDL_RENDER_MODE LV_DISPLAY_RENDER_MODE_DIRECT
#define LV_SDL_BUF_COUNT 1
#define LV_SDL_ACCELERATED 0
#define LV_SDL_FULLSCREEN 0
#define LV_SDL_DIRECT_EXIT 1
#endif

#endif
