# base_proto.c is pure C shared with the host tests (../test).
idf_component_register(SRCS "main.c" "base_proto.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_wifi esp_netif esp_event nvs_flash esp_driver_usb_serial_jtag
                                     esp_app_format esp_system)
