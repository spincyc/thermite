#!/usr/bin/env bash
# Runs only inside the Arch live medium. Native archinstall owns disk changes.
set -euo pipefail
media=/run/archiso/bootmnt/thermite
[[ $EUID -eq 0 && -r /run/archiso/bootmnt/arch/version && -r "$media/payload.json" ]] || {
    echo 'Start this installer from the Thermite Arch installation medium.' >&2
    exit 1
}
cat <<'GUIDE'
Thermite installation on Arch Linux

Connect this live system to the Internet before continuing. Wired DHCP is
normally automatic. For Wi-Fi, press Ctrl-C, use iwctl, then run:
  bash /run/archiso/bootmnt/thermite/arch-install.sh

The native Arch installer will ask you to choose a disk and review its
destructive storage confirmation. Create your own normal user with sudo
access and a password; do not use thermite as your owner account name.
Choose a hostname, mirrors, locale, timezone and bootloader for this machine.
Keep the preselected packages and configure the installed network connection.
No storage device, account or password is preselected by Thermite.

The application is installed before the final Reboot menu. Your private
house and runtime services are commissioned separately after first boot.
GUIDE
read -r -p 'Press Enter to check network/package access and open the installer: ' _
# Wait for the live environment's keyring initialization before pacman.
systemctl is-system-running --wait >/dev/null || true
systemctl start pacman-init.service
python3 -I -B - <<'PY'
from importlib.metadata import version
from pathlib import Path
from archinstall.lib.plugins import load_plugin, plugins
if tuple(map(int, version('archinstall').split('.')[:2])) != (4, 5):
    raise SystemExit('This medium requires bundled archinstall 4.5; do not upgrade it')
plugin = Path('/run/archiso/bootmnt/thermite/arch-plugin.py')
load_plugin(plugin)
# Native load_plugin logs import/initialization errors without raising.
# Require actual registration before native storage operations can begin.
if plugin.name not in plugins or not callable(getattr(plugins[plugin.name], 'on_genfstab', None)):
    raise SystemExit('Thermite finalization plugin did not load; installation stopped')
PY
# Only the disposable live environment changes here. Fail before entering
# the installer if repositories/DNS/time/signature setup cannot be reached.
pacman -Sy --noconfirm
pacman -Sp --noconfirm base linux linux-firmware mkinitcpio git make python python-yaml podman nodejs npm sudo networkmanager wpa_supplicant grub efibootmgr >/dev/null
exec archinstall --config "$media/arch-config.json" --plugin "$media/arch-plugin.py"
