idf_component_register(SRCS "main.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES thermite_base_proto esp_wifi esp_netif esp_event nvs_flash
                                     esp_driver_usb_serial_jtag esp_app_format esp_system)
