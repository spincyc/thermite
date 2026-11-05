#!/usr/bin/env bash
# archiso runs this once at boot, as root on tty1 (script= on the kernel
# command line): Thermite's installer instead of archinstall's menus. When
# it ends, this console's shell follows; thermite-installer starts it again.
set -euo pipefail
media=/run/archiso/bootmnt/thermite
[[ $EUID -eq 0 && -r /run/archiso/bootmnt/arch/version && -r "$media/payload.json" ]] || {
    echo 'Start this installer from the Thermite Arch installation medium.' >&2
    exit 1
}
python3 -I -B "$media/installer.py" launch || true
