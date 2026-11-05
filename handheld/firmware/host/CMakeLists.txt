# Host builds of the firmware's portable parts: tests, and (with LVGL) the
# screen renders and the simulator. Sources come from fetch-deps.sh:
#   host/fetch-deps.sh DEPS && cmake -S host -B BUILD -DTHERMITE_DEPS=DEPS && cmake --build BUILD && ctest --test-dir BUILD
# THERMITE_UI=OFF leaves out LVGL and everything drawn. thermite-remote-sim
# renders fixtures without SDL2; with SDL2 found it also runs interactively.
cmake_minimum_required(VERSION 3.16)
project(thermite_handheld_host C)

set(THERMITE_DEPS "" CACHE PATH "Directory fetch-deps.sh filled")
if(NOT THERMITE_DEPS)
  message(FATAL_ERROR "set THERMITE_DEPS to the directory host/fetch-deps.sh filled")
endif()
set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(FIRMWARE ${CMAKE_CURRENT_SOURCE_DIR}/..)
set(HANDHELD ${FIRMWARE}/..)

set(MBEDTLS ${THERMITE_DEPS}/mbedtls)
add_library(tl_mbedcrypto STATIC
  ${MBEDTLS}/library/aes.c ${MBEDTLS}/library/ccm.c ${MBEDTLS}/library/cipher.c
  ${MBEDTLS}/library/cipher_wrap.c ${MBEDTLS}/library/platform_util.c ${MBEDTLS}/library/constant_time.c)
target_include_directories(tl_mbedcrypto PUBLIC ${MBEDTLS}/include ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_definitions(tl_mbedcrypto PUBLIC MBEDTLS_CONFIG_FILE="tl_mbedtls_config.h")

add_library(cjson STATIC ${THERMITE_DEPS}/cJSON/cJSON.c)
target_include_directories(cjson PUBLIC ${THERMITE_DEPS}/cJSON)

set(STRICT -Wall -Wextra -Werror)
add_library(thermite_link STATIC ${FIRMWARE}/components/thermite_link/thermite_link.c)
target_include_directories(thermite_link PUBLIC ${FIRMWARE}/components/thermite_link/include)
target_link_libraries(thermite_link PRIVATE tl_mbedcrypto)
target_compile_options(thermite_link PRIVATE ${STRICT})

add_library(thermite_remote STATIC ${FIRMWARE}/components/thermite_remote/thermite_remote.c)
target_include_directories(thermite_remote PUBLIC ${FIRMWARE}/components/thermite_remote/include)
target_link_libraries(thermite_remote PUBLIC thermite_link cjson)
target_compile_options(thermite_remote PRIVATE ${STRICT})

enable_testing()
add_executable(test_link test_link.c)
target_link_libraries(test_link PRIVATE thermite_link cjson)
target_compile_options(test_link PRIVATE ${STRICT})
add_test(NAME link COMMAND test_link ${HANDHELD}/vectors.json)

# The core against a fake platform and a bridge built from tl_seal and tl_open.
add_executable(test_remote test_remote.c)
target_link_libraries(test_remote PRIVATE thermite_remote thermite_link tl_mbedcrypto cjson)
target_compile_options(test_remote PRIVATE ${STRICT})
add_test(NAME remote COMMAND test_remote)

# The device apps' and drivers' portable C, tested beside their sources, under
# the address and undefined-behaviour sanitizers.
set(SANITIZE -fsanitize=address,undefined)
function(device_test name)
  cmake_parse_arguments(T "" "" "SOURCES;INCLUDES;LIBRARIES" ${ARGN})
  add_executable(test_${name} ${T_SOURCES})
  target_include_directories(test_${name} PRIVATE ${T_INCLUDES})
  target_link_libraries(test_${name} PRIVATE ${T_LIBRARIES})
  target_compile_options(test_${name} PRIVATE ${STRICT} ${SANITIZE})
  target_link_options(test_${name} PRIVATE ${SANITIZE})
  add_test(NAME ${name} COMMAND test_${name})
endfunction()
set(BOARDS ${FIRMWARE}/components/thermite_boards)
set(BASE_PROTO ${FIRMWARE}/components/thermite_base_proto)
device_test(base_proto SOURCES ${BASE_PROTO}/test/test_base_proto.c ${BASE_PROTO}/base_proto.c
            INCLUDES ${BASE_PROTO}/include)
device_test(board_decode SOURCES ${BOARDS}/test/test_board_decode.c ${BOARDS}/board_decode.c
            INCLUDES ${BOARDS}/include)
device_test(remote_app SOURCES ${FIRMWARE}/remote/test/test_remote_app.c ${FIRMWARE}/remote/main/keymap.c
            ${FIRMWARE}/remote/main/console_proto.c ${FIRMWARE}/remote/main/power_policy.c
            INCLUDES ${FIRMWARE}/remote/main ${BOARDS}/include LIBRARIES cjson)

option(THERMITE_UI "Build thermite_ui, the fixture renders and thermite-remote-sim (needs LVGL)" ON)
if(THERMITE_UI)
  find_package(PkgConfig QUIET)
  if(PkgConfig_FOUND)
    pkg_check_modules(SDL2 QUIET IMPORTED_TARGET sdl2)
  endif()

  # LVGL from THERMITE_DEPS/lvgl with host/lv_conf.h; its own warnings are not ours.
  set(LVGL ${THERMITE_DEPS}/lvgl)
  file(GLOB_RECURSE LVGL_SOURCES CONFIGURE_DEPENDS ${LVGL}/src/*.c)
  add_library(lvgl STATIC ${LVGL_SOURCES})
  target_include_directories(lvgl PUBLIC ${LVGL} ${CMAKE_CURRENT_SOURCE_DIR})
  target_compile_definitions(lvgl PUBLIC LV_CONF_INCLUDE_SIMPLE)
  target_compile_options(lvgl PRIVATE -w)
  if(SDL2_FOUND)
    target_compile_definitions(lvgl PUBLIC THERMITE_SIM_SDL)
    target_link_libraries(lvgl PUBLIC PkgConfig::SDL2)
  else()
    message(STATUS "SDL2 not found: thermite-remote-sim renders only")
  endif()

  add_library(thermite_ui STATIC
    ${FIRMWARE}/components/thermite_ui/thermite_ui.c ${FIRMWARE}/components/thermite_ui/tu_text.c)
  target_include_directories(thermite_ui PUBLIC ${FIRMWARE}/components/thermite_ui/include)
  target_link_libraries(thermite_ui PUBLIC lvgl thermite_remote)
  target_compile_options(thermite_ui PRIVATE ${STRICT})

  add_executable(thermite-remote-sim sim.c png.c)
  target_link_libraries(thermite-remote-sim PRIVATE thermite_ui thermite_remote thermite_link lvgl m)
  target_compile_options(thermite-remote-sim PRIVATE ${STRICT})

  # Every fixture renders at each size without an LVGL warning or a glyph the
  # fonts lack, to a PNG that is not blank.
  set(FIXTURES ${CMAKE_CURRENT_SOURCE_DIR}/fixtures)
  foreach(case "240x320" "240x280" "320x480" "135x240" "240x320:dpad,volume,channel,playback")
    string(REPLACE ":" ";" parts "${case}")
    list(GET parts 0 size)
    set(name render-${size})
    set(keys "")
    if(case MATCHES ":")
      list(GET parts 1 keys)
      set(name ${name}-keys)
      set(keys --keys ${keys})
    endif()
    add_test(NAME ${name} COMMAND thermite-remote-sim --render ${FIXTURES} --size ${size} ${keys} --check
             --out ${CMAKE_CURRENT_BINARY_DIR}/renders/${name})
  endforeach()
endif()
