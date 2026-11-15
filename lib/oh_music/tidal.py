"""TIDAL sign-in through Music Assistant's TIDAL provider, which signs in with
TIDAL's device flow: Music Assistant shows a code, the owner approves it at
link.tidal.com (or opens the link that carries it) from any device, and the
flow finishes by itself. An expired code is replaced with a fresh one.

Both the gateway's Admin endpoints (oh_identity.music) and `thermite access
music-tidal` drive it. Of each step only the flow's ID and the code are
kept, both checked; the link is rebuilt from the code, never forwarded.
"""

from __future__ import annotations

import json
import math
import re
import time

from oh_access import AccessError

from .client import Client, Failed, Unauthorized, Unavailable

LINK = "https://link.tidal.com/"
CODE = re.compile(r"[A-Z0-9]{4,16}")
FLOW = re.compile(r"[A-Za-z0-9_-]{1,64}")
#: Seconds between checks while the CLI waits for approval.
POLL = 3


def signed_in(client):
    """Whether a TIDAL provider instance is loaded and available."""
    return any(isinstance(entry, dict) and entry.get("domain") == "tidal" and entry.get("available") is True
               for entry in client.call("providers") or ())


def waiting(step):
    """A device-login step as {"state": "waiting", "flow", "code", "url",
    "expires_at"}; Failed for any other step."""
    if not isinstance(step, dict) or step.get("type") != "external":
        raise Failed(200)
    flow, url = step.get("flow_id"), step.get("url")
    code = url.rstrip("/").rsplit("/", 1)[-1] if isinstance(url, str) else ""
    if not isinstance(flow, str) or not FLOW.fullmatch(flow) or not CODE.fullmatch(code):
        raise Failed(200)
    expires = step.get("expires_at")
    expires = round(expires) if type(expires) in (int, float) and math.isfinite(expires) else None
    return {"state": "waiting", "flow": flow, "code": code, "url": LINK + code, "expires_at": expires}


def start(client):
    """A new sign-in: TIDAL's first, or a fresh one for the instance thermite
    has (a reconfigure, as Music Assistant re-authenticates)."""
    configs = [entry for entry in client.call("config/providers", provider_domain="tidal") or ()
               if isinstance(entry, dict) and isinstance(entry.get("instance_id"), str)]
    if configs:
        step = client.call("config/providers/reconfigure", instance_id=configs[0]["instance_id"])
    else:
        step = client.call("config/providers/setup", provider_domain="tidal")
    return waiting(step)


def poll(client, flow):
    """Where a sign-in stands: waiting (with its current code), done or
    failed. A flow Music Assistant no longer knows has ended: done when TIDAL
    is signed in."""
    try:
        step = client.call("config/flows/get", flow_id=flow)
    except Failed:
        return {"state": "done" if signed_in(client) else "failed"}
    kind = step.get("type") if isinstance(step, dict) else None
    if kind == "external":
        return waiting(step)
    return {"state": "done" if kind == "finish" else "failed"}


def cancel(client, flow):
    try:
        client.call("config/flows/abort", flow_id=flow)
    except Failed:
        pass  # Already ended.


def site(config_path):
    """(Music Assistant's port, thermite's token) for a site's service config."""
    from oh_access.files import read_private
    from oh_access.service import load_config
    from .provision import read_claim
    config = load_config(config_path, tls=False)
    music = json.loads(read_private(config["contract_file"])).get("music")
    if not music:
        raise AccessError("This site has no music: host.yaml has no music: section")
    claim = read_claim(config["state"])
    if not claim or "token" not in claim:
        raise AccessError("Music Assistant is not claimed yet: start it and the access worker"
                          if not claim else "Music Assistant belongs to another user: reset its state to let thermite claim it")
    return music["port"], claim["token"]


def sign_in(port, token, *, out=print, sleep=time.sleep, connect=Client):
    """The CLI's sign-in: print the code and link, wait for approval. 0 when
    TIDAL is signed in, 1 when the sign-in failed."""
    try:
        with connect(port, token) as client:
            step = start(client)
            shown = None
            while step["state"] == "waiting":
                if step["code"] != shown:
                    shown = step["code"]
                    out(f"Open {step['url']} (or enter {step['code']} at {LINK}) and approve thermite.")
                sleep(POLL)
                step = poll(client, step["flow"])
    except (Unavailable, Unauthorized):
        raise AccessError("Music Assistant is unavailable") from None
    except Failed:
        raise AccessError("Music Assistant could not start a TIDAL sign-in") from None
    if step["state"] == "done":
        out("TIDAL is signed in.")
        return 0
    out("TIDAL sign-in failed.")
    return 1
