#include "display.h"

#include "board.h"
#include "esp_check.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "display";

/* Rows per LVGL buffer, bounded by the bus's largest transfer. */
#define BUFFER_ROWS 40

static esp_lcd_panel_handle_t s_panel;
static bool s_swap, s_synchronous;
static bool s_touched;
static struct {
    bool down;
    uint16_t x, y;
} s_injected;

static uint32_t tick(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static bool flushed(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *event, void *context)
{
    (void)io;
    (void)event;
    lv_display_flush_ready((lv_display_t *)context);
    return false;
}

static void flush(lv_display_t *display, const lv_area_t *area, uint8_t *pixels)
{
    if (s_swap) lv_draw_sw_rgb565_swap(pixels, (uint32_t)lv_area_get_size(area));
    esp_lcd_panel_draw_bitmap(s_panel, area->x1, area->y1, area->x2 + 1, area->y2 + 1, pixels);
    /* A bus completes through flushed(), from its interrupt; a panel without one has drawn. */
    if (s_synchronous) lv_display_flush_ready(display);
}

static void read_touch(lv_indev_t *indev, lv_indev_data_t *data)
{
    (void)indev;
    uint16_t x = s_injected.x, y = s_injected.y;
    if (s_injected.down || board_touch_read(&x, &y)) {
        data->point.x = x;
        data->point.y = y;
        data->state = LV_INDEV_STATE_PRESSED;
        s_touched = true;
    } else {
        data->state = LV_INDEV_STATE_RELEASED;
    }
}

void display_touch(bool touching, uint16_t x, uint16_t y)
{
    s_injected.down = touching;
    s_injected.x = x;
    s_injected.y = y;
}

bool display_touched(void)
{
    bool touched = s_touched;
    s_touched = false;
    return touched;
}

lv_display_t *display_start(void)
{
    const board_info_t *info = board_info();
    esp_lcd_panel_io_handle_t io = NULL;
    ESP_ERROR_CHECK(board_display_init(&io, &s_panel));
    s_swap = info->swap_bytes;
    s_synchronous = io == NULL;

    lv_init();
    lv_tick_set_cb(tick);
    lv_display_t *display = lv_display_create(info->width, info->height);
    size_t rows = BUFFER_ROWS;
    size_t row_bytes = (size_t)info->width * 2;
    if (rows * row_bytes > board_display_max_transfer()) rows = board_display_max_transfer() / row_bytes;
    size_t size = rows * row_bytes;
    void *a = heap_caps_malloc(size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    void *b = heap_caps_malloc(size, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (!a || !b) {
        ESP_LOGE(TAG, "no DMA memory for %u-byte buffers", (unsigned)size);
        abort();
    }
    lv_display_set_buffers(display, a, b, (uint32_t)size, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display, flush);
    if (io) {
        const esp_lcd_panel_io_callbacks_t callbacks = {.on_color_trans_done = flushed};
        ESP_ERROR_CHECK(esp_lcd_panel_io_register_event_callbacks(io, &callbacks, display));
    }

    /* Every board gets the pointer: test input can touch a board that has no touch screen. */
    lv_indev_t *touch = lv_indev_create();
    lv_indev_set_type(touch, LV_INDEV_TYPE_POINTER);
    lv_indev_set_read_cb(touch, read_touch);
    lv_indev_set_display(touch, display);
    return display;
}

void display_enrol_screen(const char *mac, const char *board, const char *fw)
{
    lv_obj_t *screen = lv_screen_active();
    lv_obj_clean(screen);
    lv_obj_set_style_bg_color(screen, lv_color_black(), 0);
    lv_obj_t *label = lv_label_create(screen);
    lv_obj_set_width(label, lv_pct(90));
    lv_label_set_long_mode(label, LV_LABEL_LONG_WRAP);
    lv_obj_set_style_text_color(label, lv_color_white(), 0);
    lv_obj_set_style_text_align(label, LV_TEXT_ALIGN_CENTER, 0);
    lv_label_set_text_fmt(
        label, "Enrol this remote\n\n%s\n\n%s %s\n\nConnect it by USB to the Thermite server and enrol it there.", mac,
        board, fw);
    lv_obj_center(label);
}
