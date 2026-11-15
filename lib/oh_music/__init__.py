"""Music: a pinned Music Assistant (versions.json music_assistant) that plays
TIDAL and the owner's music libraries on the house's Sonos and WiiM speakers,
alone or grouped, driven from thermite's own pages through the gateway.

instance/host.yaml opts a host in:

    music:
      libraries:
        flac: /mnt/music/flac

Each library is a name and an absolute host directory the owner mounts
separately (a NAS share, a local disk); the container sees it read-only at
MEDIA/<name>. `port` and `stream_port` move Music Assistant's two thermite
listeners (oh_ports.MUSIC): its API, which only the gateway uses, on
loopback, and the audio streams speakers fetch, on the LAN. A host without
`music:` runs no Music Assistant, and the gateway serves no music.
"""

from __future__ import annotations

import os
import re

#: host.yaml's music: keys.
KEYS = frozenset(("libraries", "port", "stream_port"))
#: A library's name: also its directory under MEDIA and its label's key.
LIBRARY = re.compile(r"[a-z][a-z0-9_]{0,31}")
#: Pasted into a Quadlet Volume= line: no whitespace, quotes, colons, systemd
#: specifiers or other separators.
_PATH = re.compile(r"/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*")
#: Where the container sees the libraries.
MEDIA = "/media"
#: Music Assistant's state (its settings, library, credentials and caches),
#: below the installation's runtime state; the container's /data.
STATE = "music"
DATA = "/data"
#: The image's own entrypoint (versions.json music_assistant), which the
#: unit runs after SETTINGS has pinned the listeners.
ENTRYPOINT = ("/usr/local/bin/entrypoint.sh", "--data-dir", DATA, "--cache-dir", f"{DATA}/.cache")
#: The providers Music Assistant would set up by itself on its first start,
#: marked as done in a new settings.json so that it sets up none of them:
#: the gateway sets up the ones the house uses.
PROVIDERS = ("airplay", "chromecast", "dlna", "sonos", "bluesound", "heos", "wiim", "party", "smart_fades",
             "lastfm_recommendations", "playlist_metadata", "ambient_sounds")
#: Run by the container's Python before Music Assistant starts, so that
#: nothing changes the file under it: pins the API to loopback and the
#: streams to every address, on thermite's ports, in settings.json, and
#: leaves every other setting as it is. Arguments: the API's port, the
#: streams' port, then the command it runs once done (ENTRYPOINT). A new
#: state gets PROVIDERS too. It reads settings.json as Music Assistant
#: does, falling back to the copy Music Assistant rotates to .backup, and
#: writes it as Music Assistant does: whole, synced, then renamed over the
#: old one, which becomes the .backup. A file neither can read stops the
#: start, untouched, rather than start Music Assistant afresh over it.
SETTINGS = """\
import json, os, sys
api, streams, command = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3:]
data = command[command.index("--data-dir") + 1]
path = os.path.join(data, "settings.json")
def read(name):
    try:
        with open(name, "rb") as file:
            value = json.load(file)
    except FileNotFoundError:
        return None
    except ValueError:
        return False
    return value if isinstance(value, dict) else False
current = read(path)
settings = current if isinstance(current, dict) else read(path + ".backup")
if settings is None and current is None:
    settings = {"default_providers_setup": PROVIDERS}
if not isinstance(settings, dict):
    sys.exit(f"{path} and its .backup cannot be read; restore one, or move both aside to start afresh")
before = json.dumps(settings, sort_keys=True)
try:
    core = settings.setdefault("core", {})
    for domain, address, port in (("webserver", "127.0.0.1", api), ("streams", "0.0.0.0", streams)):
        entry = core.setdefault(domain, {})
        entry["domain"] = domain
        entry.setdefault("values", {}).update(bind_ip=address, bind_port=port)
except (AttributeError, TypeError):
    sys.exit(f"{path} has no core settings in the shape Music Assistant writes; restore it from its .backup")
if json.dumps(settings, sort_keys=True) != before or current is not settings:
    temporary = path + ".tmp"
    with open(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as file:
        json.dump(settings, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    if current is settings:
        os.replace(path, path + ".backup")
    os.replace(temporary, path)
    directory = os.open(data, os.O_RDONLY)
    os.fsync(directory)
    os.close(directory)
os.execv(command[0], command)
""".replace("PROVIDERS", repr(list(PROVIDERS)))


def settings(host: dict | None) -> dict | None:
    """host.yaml's music:, checked, as {"libraries": {name: host path}}; None
    when the host has no music. Ports are oh_ports.effective's."""
    if not host or "music" not in host:
        return None
    music = host["music"] if host["music"] is not None else {}
    if not isinstance(music, dict):
        raise ValueError("music: must be a mapping")
    unknown = sorted(str(key) for key in music if key not in KEYS)
    if unknown:
        raise ValueError("unknown music setting " + ", ".join(unknown) + " (allowed: " + ", ".join(sorted(KEYS)) + ")")
    libraries = music.get("libraries") or {}
    if not isinstance(libraries, dict):
        raise ValueError("music.libraries must map a name to a host directory")
    seen = {}
    for name, path in libraries.items():
        if not isinstance(name, str) or not LIBRARY.fullmatch(name):
            raise ValueError(f"music.libraries: {name!r} is not a name of lowercase letters, digits and _ "
                             "starting with a letter")
        if (not isinstance(path, str) or not _PATH.fullmatch(path) or os.path.normpath(path) != path):
            raise ValueError(f"music.libraries.{name} must be a normalized absolute path of letters, digits and ._@+-")
        if path in seen:
            raise ValueError(f"music.libraries.{name} and music.libraries.{seen[path]} are the same directory")
        seen[path] = name
    return {"libraries": dict(libraries)}
