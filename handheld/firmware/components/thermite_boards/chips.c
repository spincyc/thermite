/*
 * Small drivers for the I2C chips on the boards: TCA8418 key scanner,
 * FT6x06, CST328 and CST3530 touch, LIS3DH accelerometer, MAX17048 gauge.
 * Register addresses and bit meanings are from each part's datasheet unless
 * a comment says otherwise. Decoding is in board_decode.c.
 */
#include "board_priv.h"
#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "chips";

/* ---- TCA8418 (TI SCPS215) ---- */
enum {
    TCA_CFG = 0x01,
    TCA_INT_STAT = 0x02,
    TCA_KEY_LCK_EC = 0x03,
    TCA_KEY_EVENT_A = 0x04,
    TCA_GPIO_INT_EN_1 = 0x1A,
    TCA_KP_GPIO_1 = 0x1D,
    TCA_KP_GPIO_2 = 0x1E,
    TCA_KP_GPIO_3 = 0x1F,
    TCA_GPI_EM_1 = 0x20,
    TCA_GPI_EM_2 = 0x21,
    TCA_GPI_EM_3 = 0x22,
};
/* CFG: KE_IEN (key events raise INT) | INT_CFG (INT re-asserts after 50 us while
 * events remain, so an edge-triggered input never misses one). */
#define TCA_CFG_VALUE 0x11

esp_err_t tca8418_init(tca8418_t *t, i2c_master_bus_handle_t bus, uint32_t hz, uint8_t rows, uint16_t cols,
                       uint8_t gpi_rows, uint16_t gpi_cols)
{
    ESP_RETURN_ON_ERROR(bi2c_add(bus, TCA8418_ADDRESS, hz, &t->dev), TAG, "TCA8418 device");
    /* After a deep-sleep wake the scanner still holds its configuration and the events
     * that woke us; rewriting the same values keeps both. */
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_KP_GPIO_1, rows), TAG, "KP_GPIO1");
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_KP_GPIO_2, (uint8_t)cols), TAG, "KP_GPIO2");
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_KP_GPIO_3, (uint8_t)(cols >> 8) & 0x03), TAG, "KP_GPIO3");
    /* GPIs default to inputs with pull-ups enabled (GPIO_DIR, GPIO_PULL = 0) and active-low
     * events (GPIO_INT_LVL = 0): a switch to ground presses on low. */
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_GPI_EM_1, gpi_rows), TAG, "GPI_EM1");
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_GPI_EM_2, (uint8_t)gpi_cols), TAG, "GPI_EM2");
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_GPI_EM_3, (uint8_t)(gpi_cols >> 8) & 0x03), TAG, "GPI_EM3");
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_GPIO_INT_EN_1, gpi_rows), TAG, "GPIO_INT_EN1");
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_GPIO_INT_EN_1 + 1, (uint8_t)gpi_cols), TAG, "GPIO_INT_EN2");
    ESP_RETURN_ON_ERROR(bi2c_write(t->dev, TCA_GPIO_INT_EN_1 + 2, (uint8_t)(gpi_cols >> 8) & 0x03), TAG,
                        "GPIO_INT_EN3");
    return bi2c_write(t->dev, TCA_CFG, TCA_CFG_VALUE);
}

esp_err_t tca8418_read(tca8418_t *t, uint8_t *events, size_t max, size_t *count)
{
    *count = 0;
    uint8_t lck_ec;
    ESP_RETURN_ON_ERROR(bi2c_read(t->dev, TCA_KEY_LCK_EC, &lck_ec, 1), TAG, "KEY_LCK_EC");
    size_t pending = lck_ec & 0x0f; /* up to 10 events */
    while (pending-- && *count < max) {
        uint8_t event;
        ESP_RETURN_ON_ERROR(bi2c_read(t->dev, TCA_KEY_EVENT_A, &event, 1), TAG, "KEY_EVENT_A");
        if (event == 0) break;
        events[(*count)++] = event;
    }
    /* Write 1s to clear K_INT, GPI_INT and the overflow and lock flags. */
    return bi2c_write(t->dev, TCA_INT_STAT, 0x1f);
}

esp_err_t tca8418_col_level(tca8418_t *t, unsigned column, bool *high)
{
    uint8_t reg = (uint8_t)(0x15 + column / 8); /* GPIO_DAT_STAT_2 holds COL0-7, _3 COL8-9 */
    uint8_t value;
    ESP_RETURN_ON_ERROR(bi2c_read(t->dev, reg, &value, 1), TAG, "GPIO_DAT_STAT");
    *high = (value >> (column % 8)) & 1;
    return ESP_OK;
}

/* ---- FT6x06 family (FocalTech) ---- */
esp_err_t ft6x06_read(i2c_master_dev_handle_t dev, uint16_t *x, uint16_t *y, bool *pressed)
{
    uint8_t regs[5];
    *pressed = false;
    ESP_RETURN_ON_ERROR(bi2c_read(dev, 0x02, regs, sizeof regs), TAG, "FT6x06 points");
    *pressed = ft6x06_decode(regs, x, y);
    return ESP_OK;
}

esp_err_t ft6x06_sleep(i2c_master_dev_handle_t dev)
{
    return bi2c_write(dev, 0xA5, 0x03); /* PWR_MODE: hibernate */
}

/* ---- CST328 (Hynitron; register use as Waveshare's ESP32-S3-Touch-LCD-2.8 demo) ---- */
esp_err_t cst328_init(i2c_master_dev_handle_t dev, uint16_t *range_x, uint16_t *range_y)
{
    *range_x = *range_y = 0;
    ESP_RETURN_ON_ERROR(bi2c_write16(dev, 0xD101, NULL, 0), TAG, "CST328 info mode");
    uint8_t res[4];
    if (bi2c_read16(dev, 0xD1F8, res, sizeof res) == ESP_OK) {
        *range_x = (uint16_t)(res[0] | res[1] << 8);
        *range_y = (uint16_t)(res[2] | res[3] << 8);
    }
    ESP_LOGI(TAG, "CST328 range %ux%u", *range_x, *range_y);
    return bi2c_write16(dev, 0xD109, NULL, 0); /* normal mode */
}

esp_err_t cst328_read(i2c_master_dev_handle_t dev, uint16_t *x, uint16_t *y, bool *pressed)
{
    uint8_t regs[7];
    *pressed = false;
    ESP_RETURN_ON_ERROR(bi2c_read16(dev, 0xD000, regs, sizeof regs), TAG, "CST328 point");
    *pressed = cst328_decode(regs, x, y);
    /* The vendor clears the count after every read. */
    return bi2c_write16(dev, 0xD005, (const uint8_t[]){0x00}, 1);
}

/* ---- CST3530 (Hynitron; command use as Waveshare's esp_lcd_touch_cst3530 component) ---- */
static esp_err_t cst3530_command(i2c_master_dev_handle_t dev, uint32_t command)
{
    uint8_t bytes[4] = {(uint8_t)(command >> 24), (uint8_t)(command >> 16), (uint8_t)(command >> 8), (uint8_t)command};
    return i2c_master_transmit(dev, bytes, sizeof bytes, I2C_TIMEOUT_MS);
}

esp_err_t cst3530_read(i2c_master_dev_handle_t dev, uint16_t *x, uint16_t *y, bool *pressed)
{
    uint8_t report[CST3530_REPORT];
    uint8_t command[4] = {0xD0, 0x07, 0x00, 0x00};
    *pressed = false;
    ESP_RETURN_ON_ERROR(
        i2c_master_transmit_receive(dev, command, sizeof command, report, sizeof report, I2C_TIMEOUT_MS), TAG,
        "CST3530 report");
    *pressed = cst3530_decode(report, x, y);
    return cst3530_command(dev, 0xD00002AB); /* acknowledge; releases INT */
}

/* ---- LIS3DH (ST DocID17530) ---- */
enum {
    LIS_CTRL_REG1 = 0x20,
    LIS_CTRL_REG2 = 0x21,
    LIS_CTRL_REG3 = 0x22,
    LIS_CTRL_REG4 = 0x23,
    LIS_CTRL_REG5 = 0x24,
    LIS_CTRL_REG6 = 0x25,
    LIS_REFERENCE = 0x26,
    LIS_INT1_CFG = 0x30,
    LIS_INT1_SRC = 0x31,
    LIS_INT1_THS = 0x32,
    LIS_INT1_DURATION = 0x33,
};

esp_err_t lis3dh_init_motion_wake(i2c_master_dev_handle_t dev)
{
    /* 10 Hz low-power mode, XYZ on: a few uA. */
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_CTRL_REG1, 0x2F), TAG, "CTRL_REG1");
    /* High-pass filter (normal mode) on the data feeding interrupt 1 only, so the
     * threshold measures change, not gravity. */
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_CTRL_REG2, 0x01), TAG, "CTRL_REG2");
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_CTRL_REG4, 0x00), TAG, "CTRL_REG4"); /* +-2 g */
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_CTRL_REG5, 0x08), TAG, "CTRL_REG5"); /* latch INT1 */
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_CTRL_REG6, 0x02), TAG, "CTRL_REG6"); /* INT pins active low */
    /* 8 x 16 mg = 128 mg for one sample (100 ms at 10 Hz). UNVERIFIED: tune on hardware. */
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_INT1_THS, 0x08), TAG, "INT1_THS");
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_INT1_DURATION, 0x01), TAG, "INT1_DURATION");
    uint8_t reference;
    ESP_RETURN_ON_ERROR(bi2c_read(dev, LIS_REFERENCE, &reference, 1), TAG, "REFERENCE"); /* resets the filter */
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_INT1_CFG, 0x2A), TAG, "INT1_CFG");           /* X, Y or Z high */
    ESP_RETURN_ON_ERROR(bi2c_write(dev, LIS_CTRL_REG3, 0x40), TAG, "CTRL_REG3");         /* IA1 on INT1 */
    lis3dh_clear(dev);
    return ESP_OK;
}

bool lis3dh_clear(i2c_master_dev_handle_t dev)
{
    uint8_t source = 0;
    if (bi2c_read(dev, LIS_INT1_SRC, &source, 1) != ESP_OK) return false;
    return (source & 0x40) != 0; /* IA */
}

/* ---- MAX17048 (Analog Devices 19-7029) ---- */
esp_err_t max17048_read(i2c_master_dev_handle_t dev, int *percent, uint16_t *millivolts, int *crate_tenths)
{
    uint8_t b[2];
    ESP_RETURN_ON_ERROR(bi2c_read(dev, 0x02, b, 2), TAG, "VCELL");
    if (millivolts) *millivolts = max17048_millivolts((uint16_t)(b[0] << 8 | b[1]));
    ESP_RETURN_ON_ERROR(bi2c_read(dev, 0x04, b, 2), TAG, "SOC");
    if (percent) *percent = max17048_percent((uint16_t)(b[0] << 8 | b[1]));
    if (crate_tenths) {
        ESP_RETURN_ON_ERROR(bi2c_read(dev, 0x16, b, 2), TAG, "CRATE");
        *crate_tenths = max17048_crate_tenths((uint16_t)(b[0] << 8 | b[1]));
    }
    return ESP_OK;
}
