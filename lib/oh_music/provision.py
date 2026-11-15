"""Thermite's claim on Music Assistant and the configuration it keeps there.

Music Assistant is thermite's alone: the gateway is its only client. The
policy worker (oh_access.worker), the site's one supervised background
process, holds that claim; the gateway only reads the token it stores.

Claim. A fresh Music Assistant has no user. Once it reports running the
worker creates its first admin, `thermite`, with a random password nobody
keeps, mints a long-lived token (Music Assistant's live a year) with the
setup session, stores it in TOKEN_FILE in the gateway's private state and
completes onboarding. Within RENEW_DAYS of its end the worker mints the next,
stores it, then revokes every other token of the user. A server that
already has a user and refuses thermite's token (or one it holds none for)
is someone else's: the file records music_unclaimed and the gateway serves
no music until the owner resets Music Assistant (stop it, delete its state,
deploy again), after which the worker claims it again.

Reconcile, idempotently, after a claim, whenever Music Assistant's server
ID changes (its state was reset) and every EVERY seconds: the sonos, wiim and airplay
player providers are present (MA groups a WiiM with a Sonos over AirPlay);
the sonos and wiim providers are given the house's speakers' addresses for
discovery; each library (oh_music.settings) is one filesystem_local instance
on MEDIA/<name>, named LIBRARY_NAME, and every other filesystem_local
instance is removed; the providers in UNWANTED are removed; remote access
stays off. Players are never renamed or disabled.
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from pathlib import Path

from oh_access import AccessError
from oh_access.files import json_private, read_private

from . import MEDIA
from .client import Client, Failed, NoUser, Unauthorized, Unavailable

LOG = logging.getLogger(__name__)
#: The private file in the gateway's state that holds the claim.
TOKEN_FILE = "music-token.json"
TOKEN_BYTES = 8192
USERNAME = "thermite"
#: Days a long-lived token lives, and how many before its end the worker
#: replaces it.
TOKEN_DAYS = 365
RENEW_DAYS = 30
DAY = 24 * 3600
#: The player providers thermite drives.
PLAYERS = ("sonos", "wiim", "airplay")
#: What the gateway names each library's filesystem_local instance; Music
#: Assistant never reports the path an instance was set up with.
LIBRARY_NAME = "thermite {}"
#: Providers Music Assistant may load that thermite does not use: other
#: speaker families it would find on the LAN, and features that act on
#: their own.
UNWANTED = ("chromecast", "dlna", "heos", "bluesound", "party", "smart_fades", "lastfm_recommendations",
            "ambient_sounds")
#: Seconds between passes, and between reconciles of a server that has not
#: changed.
POLL = 30
EVERY = 15 * 60
#: The problem a stored claim records when the server is someone else's.
UNCLAIMED = "music_unclaimed"


def token_file(state):
    return Path(state) / TOKEN_FILE


def read_claim(state):
    """The stored claim: {"token", "issued_at", "expires_at"}, or {"problem":
    UNCLAIMED}; None when there is none or it cannot be read."""
    try:
        data = json.loads(read_private(token_file(state), TOKEN_BYTES))
    except FileNotFoundError:
        return None
    except (AccessError, OSError, ValueError):
        LOG.warning("The stored Music Assistant claim cannot be read; claiming again.")
        return None
    if not isinstance(data, dict) or data.get("version") != 1:
        return None
    if data.get("problem") == UNCLAIMED:
        return {"problem": UNCLAIMED}
    token = data.get("token")
    if (isinstance(token, str) and 0 < len(token) <= 4096 and token.isprintable()
            and type(data.get("issued_at")) is int and type(data.get("expires_at")) is int):
        return {"token": token, "issued_at": data["issued_at"], "expires_at": data["expires_at"]}
    return None


def write_claim(state, claim):
    json_private(token_file(state), {"version": 1, **claim})


def digest(token):
    """Music Assistant's name for a token in its token list."""
    return hashlib.sha256(token.encode()).hexdigest()


class Provisioner:
    """One site's claim and configuration. spec is the contract's music
    (oh_site_contract.music); connect(port, token) opens a Client."""

    def __init__(self, state, spec, *, connect=Client, clock=time.time, every=EVERY):
        self.state, self.spec, self.connect, self.clock = Path(state), spec, connect, clock
        self.every = every
        self.reported = None
        # (server ID, when) of the last reconcile.
        self.reconciled = None

    def report(self, outcome, **fields):
        """One log line when the outcome changes; never a token or an address."""
        if outcome != self.reported:
            self.reported = outcome
            details = "".join(f" {key}={value}" for key, value in sorted(fields.items()))
            LOG.info("Music Assistant: %s%s", outcome, details)

    def step(self):
        """One pass: claim (or renew) once Music Assistant runs, then
        reconcile when due. "ready" or "unclaimed"; Unavailable while it does
        not run."""
        claimed = False
        with self.connect(self.spec["port"]) as client:
            info = client.info()
            if info.get("status") != "running":
                raise Unavailable("Music Assistant is starting")
            claim = read_claim(self.state)
            if not claim or "token" not in claim:
                if self.claim(client) is None:
                    self.report("unclaimed: another user owns it; reset its state to claim it")
                    return "unclaimed"
                claim, claimed = read_claim(self.state), True
        with self.connect(self.spec["port"], claim["token"]) as client:
            try:
                token = self.renew(client, claim)
            except (Unauthorized, NoUser):
                # The server forgot thermite (its state was reset) or revoked the token.
                token, claimed = self.claim(client), True
                if token is None:
                    self.report("unclaimed: it refuses thermite's token; reset its state to claim it")
                    return "unclaimed"
            client.token = token
            server, now, changed = info.get("server_id"), self.clock(), 0
            if claimed or self.reconciled is None or self.reconciled[0] != server or now - self.reconciled[1] >= self.every:
                changed = self.reconcile(client, info)
                self.reconciled = (server, now)
        self.report("ready", changed=changed)
        return "ready"

    def claim(self, client):
        """thermite's long-lived token on a server with no user, stored; None,
        with music_unclaimed stored, when the server has a user."""
        session = client.setup(USERNAME, secrets.token_urlsafe(32), "thermite")
        if session is None:
            if read_claim(self.state) != {"problem": UNCLAIMED}:
                write_claim(self.state, {"problem": UNCLAIMED})
            return None
        with self.connect(self.spec["port"], session) as first:
            token = self.mint(first)
            first.token = token
            first.call("config/onboard_complete")
            self.revoke_others(first, token)
        LOG.info("Music Assistant claimed by thermite.")
        return token

    def mint(self, client):
        token = client.call("auth/token/create", name=USERNAME)
        if not isinstance(token, str) or not token or len(token) > 4096 or not token.isprintable():
            raise Failed(200)
        issued = int(self.clock())
        write_claim(self.state, {"token": token, "issued_at": issued, "expires_at": issued + TOKEN_DAYS * DAY})
        return token

    def renew(self, client, claim):
        """The token to use: the stored one, or its successor within RENEW_DAYS
        of its end. Unauthorized when the server refuses the stored one."""
        token = claim["token"]
        if claim["expires_at"] - self.clock() > RENEW_DAYS * DAY:
            client.call("auth/tokens")  # Proves the token before anything else uses it.
            return token
        fresh = self.mint(client)
        client.token = fresh
        self.revoke_others(client, fresh)
        LOG.info("Music Assistant token renewed.")
        return fresh

    def revoke_others(self, client, token):
        """Revoke every token of thermite's user but this one: the setup
        session's and the one a renewal replaced."""
        mine = digest(token)
        for entry in client.call("auth/tokens") or ():
            if isinstance(entry, dict) and entry.get("token_hash") != mine and isinstance(entry.get("token_id"), str):
                client.call("auth/token/revoke", token_id=entry["token_id"])

    # ---- configuration ----------------------------------------------------

    def reconcile(self, client, info):
        """Bring Music Assistant's providers to what thermite needs; the number
        of changes made."""
        changed = 0
        configs = [config for config in client.call("config/providers") or () if isinstance(config, dict)]
        domains = {config.get("domain") for config in configs}
        for domain in PLAYERS:
            if domain not in domains:
                changed += self.add(client, domain)
        configs = [config for config in client.call("config/providers") or () if isinstance(config, dict)]
        speakers = self.spec["speakers"].values()
        for domain in ("sonos", "wiim"):
            wanted = sorted({speaker["ip"] for speaker in speakers if speaker["provider"] == domain and "ip" in speaker})
            for config in configs:
                if config.get("domain") == domain:
                    key = "manual_discovery_ip_addresses"
                    current = client.call("config/providers/get_value", instance_id=config["instance_id"], key=key)
                    if sorted(current or ()) != wanted:
                        client.call("config/providers/save", provider_domain=domain, instance_id=config["instance_id"],
                                    values={key: wanted})
                        changed += 1
        libraries = {LIBRARY_NAME.format(name): name for name in self.spec["libraries"]}
        present = set()
        for config in configs:
            domain, instance = config.get("domain"), config.get("instance_id")
            if domain == "filesystem_local" and config.get("name") in libraries and config["name"] not in present:
                present.add(config["name"])
            elif domain == "filesystem_local" or domain in UNWANTED:
                # A library no longer listed, one the owner added in Music
                # Assistant's own UI, or one left unnamed by an interrupted pass.
                client.call("config/providers/remove", instance_id=instance)
                changed += 1
        for name, library in sorted(libraries.items()):
            if name not in present:
                changed += self.add(client, "filesystem_local", {"content_type": "music", "path": f"{MEDIA}/{library}"},
                                    name=name)
        if info.get("has_remote_access"):
            client.call("remote_access/configure", enabled=False)
            changed += 1
        return changed

    def add(self, client, domain, values=None, *, name=None):
        """Add one provider instance through its setup flow, answering its one
        form with values. 1 when it was added; 0 when Music Assistant says it
        exists or cannot be added (logged)."""
        step = client.call("config/providers/setup", provider_domain=domain)
        if isinstance(step, dict) and step.get("type") == "form" and values is not None:
            step = client.call("config/flows/submit", flow_id=step["flow_id"], values=values)
        kind = step.get("type") if isinstance(step, dict) else None
        if kind == "finish":
            instance = (step.get("result") or {}).get("instance_id")
            if name is not None and isinstance(instance, str):
                client.call("config/providers/save", provider_domain=domain, instance_id=instance,
                            values={"name": name})
            return 1
        if kind != "abort":
            # An unexpected step (a form thermite cannot answer): end the flow.
            if isinstance(step, dict) and isinstance(step.get("flow_id"), str):
                client.call("config/flows/abort", flow_id=step["flow_id"])
            LOG.warning("Music Assistant could not add the %s provider (%s step).", domain, kind or "no")
        return 0


def run(state, spec, stop, *, poll=POLL, every=EVERY, connect=Client):
    """The worker's music thread: until stop is set, a pass every `poll`
    seconds (Provisioner.step), reconciling every `every`."""
    provisioner = Provisioner(state, spec, connect=connect, every=every)
    while not stop.is_set():
        try:
            provisioner.step()
        except Unavailable:
            provisioner.report("unavailable")
        except Exception as error:
            # A refused or failed command: tried again from the start.
            provisioner.report("failed", error=type(error).__name__)
        stop.wait(poll)
