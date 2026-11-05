# keymap.c, console_proto.c and power_policy.c are pure C shared with the host tests
# (../test). LVGL arrives through thermite_ui (its idf_component.yml).
idf_component_register(
    SRCS "main.c" "console.c" "display.c" "radio_espnow.c" "radio_lines.c" "keymap.c" "console_proto.c"
         "power_policy.c"
    INCLUDE_DIRS "."
    PRIV_REQUIRES thermite_boards thermite_remote thermite_ui thermite_link thermite_base_proto json
                  esp_wifi esp_netif esp_event nvs_flash esp_lcd esp_timer esp_hw_support
                  esp_driver_usb_serial_jtag esp_driver_uart esp_app_format esp_system)
