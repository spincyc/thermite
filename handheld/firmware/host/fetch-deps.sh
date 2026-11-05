#!/bin/sh
# Fetch the host build's sources at the revisions ESP-IDF v5.5.6 and the
# firmware pin: mbedTLS and cJSON as ESP-IDF's submodules, LVGL as
# components/thermite_ui/idf_component.yml. Usage: fetch-deps.sh DIR
set -eu
dir=${1:?usage: fetch-deps.sh DIR}
mkdir -p "$dir"
fetch() { # name url revision
    if [ -d "$dir/$1/.git" ] && [ "$(git -C "$dir/$1" rev-parse HEAD)" = "$3" ]; then return; fi
    rm -rf "$dir/$1"
    git init -q "$dir/$1"
    git -C "$dir/$1" fetch -q --depth 1 "$2" "$3"
    git -C "$dir/$1" checkout -q FETCH_HEAD
}
fetch mbedtls https://github.com/espressif/mbedtls.git 2b96dd8eebe880f304c69976b3c2fa0c5100cbb6
fetch cJSON https://github.com/DaveGamble/cJSON.git b2890c8d76bbb64e710585ebc0a917196b9c67e7
fetch lvgl https://github.com/lvgl/lvgl.git 7f07a129e8d77f4984fff8e623fd5be18ff42e74
