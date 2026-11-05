# The handheld remote's base station: a keyless ESP-NOW relay on the native
# USB port (handheld/PROTOCOL.md, "Base station lines"). ESP-IDF v5.5.6; see
# ../README.md. Bump PROJECT_VER for every build you hand out: it is the
# <fw> word of the base line, so it must contain no spaces.
cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "0.1.0")
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components/thermite_base_proto")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(thermite-base)
