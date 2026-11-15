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
