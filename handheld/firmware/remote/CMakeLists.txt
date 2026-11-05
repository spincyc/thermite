# The handheld remote (handheld/PROTOCOL.md). ESP-IDF v5.5.6; see ../README.md.
# Build one board per build directory and sdkconfig, naming the board:
#   idf.py -B BUILD -D SDKCONFIG=BUILD/sdkconfig -D THERMITE_BOARD=omote-rev5 build
# Boards: waveshare-s3-lcd28, m5stick-s3, omote-rev5, openremote-rev6, and qemu
# for Espressif's QEMU (one sdkconfig.board.<name> each). The defaults apply only when SDKCONFIG does not
# exist yet: delete it, or use a fresh one, to switch boards.
# Bump PROJECT_VER for every build you hand out: it is the <fw> word of the console
# line and the status the bridge sees, so it must contain no spaces.
cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.1.0")

set(THERMITE_BOARD "" CACHE STRING "Board: waveshare-s3-lcd28, m5stick-s3, omote-rev5, openremote-rev6 or qemu")
if(NOT THERMITE_BOARD OR NOT EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.board.${THERMITE_BOARD}")
    message(FATAL_ERROR "set THERMITE_BOARD (-D THERMITE_BOARD=<name>) to a board with a sdkconfig.board.<name> here")
endif()
set(SDKCONFIG_DEFAULTS
    "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.defaults"
    "${CMAKE_CURRENT_LIST_DIR}/../components/thermite_ui/sdkconfig.lvgl"
    "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.board.${THERMITE_BOARD}")
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(thermite-remote)
