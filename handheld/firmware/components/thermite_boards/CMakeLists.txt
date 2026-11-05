# One handheld board behind include/board.h, chosen in Kconfig ("Thermite board").
# Every board file compiles to nothing unless its board is chosen. board_decode.c
# is pure C, tested on the host by test/.
idf_component_register(
    SRCS "board_common.c" "board_decode.c" "chips.c" "lcd_ili9341.c"
         "board_waveshare_s3_lcd28.c" "board_m5stick_s3.c" "board_omote.c" "board_qemu.c"
    INCLUDE_DIRS "include"
    REQUIRES esp_lcd
    PRIV_REQUIRES esp_driver_gpio esp_driver_i2c esp_driver_ledc esp_driver_spi esp_adc esp_timer
                  esp_hw_support log freertos)
