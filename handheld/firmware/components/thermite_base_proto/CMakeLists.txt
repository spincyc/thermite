# The base station's lines (handheld/PROTOCOL.md): the base speaks them on its
# USB port, and the qemu board's radio speaks them on UART1. Pure C, tested on
# the host by test/.
idf_component_register(SRCS "base_proto.c"
                       INCLUDE_DIRS "include")
