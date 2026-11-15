"""Thermite's claim on Music Assistant and the configuration it keeps there.

Music Assistant is thermite's alone: the gateway is its only client. The
policy worker (oh_access.worker), the site's one supervised background
process, holds that claim; the gateway only reads the token it stores.

Claim. A fresh Music Assistant has no user. Once it reports running the
worker creates its first admin, `thermite`, with a random password nobody
keeps, and stores the setup session it is answered with in TOKEN_FILE, in
the gateway's private state, at once: a pending claim, which the worker
resumes from on its next pass however this one ends. With the session it
mints a long-lived token (Music Assistant's live a year), which replaces
the session in the file, and revokes every other token of the user. Within
RENEW_DAYS of a token's end the worker mints the next, stores it, then
revokes the rest.

A stored token or session is never given up because Music Assistant
refuses it: Music Assistant answers 401 whenever its authentication fails
inside, as it may while it starts, stops or upgrades. The file keeps the
token and records when the refusals began; each pass tries it again, and
the first that works clears the record. A server with no user (its state
was deleted) answers 503 instead, and is claimed afresh. Only after
REFUSED seconds of refusals in a row does the gateway call the server
someone else's (music_unclaimed, problem()); a server that had a user
before thermite ever claimed it is someone else's at once. Either waits
for the owner to reset Music Assistant: stop it, delete its state, start it.

Reconcile, idempotently, after a claim, whenever Music Assistant's server
ID changes (its state was reset) and every EVERY seconds: the sonos, wiim and airplay
player providers are present (MA groups a WiiM with a Sonos over AirPlay);
the sonos and wiim providers are given the house's speakers' addresses for
discovery; each library (oh_music.settings) is one filesystem_local instance
on MEDIA/<name>, named LIBRARY_NAME, and every other filesystem_local
instance is removed; the providers in UNWANTED are removed; remote access
stays off; onboarding is complete. Players are never renamed or disabled.
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
#: Seconds of refusals in a row after which a token is no longer thermite's.
REFUSED = 24 * 3600
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
#: Seconds one request of a pass may take: a provider's setup flow loads it.
SETUP = 20.0
#: The problem a stored claim records when the server is someone else's,
#: and what read_claim() answers for a claim file it cannot trust.
UNCLAIMED = "music_unclaimed"
UNREADABLE = "unreadable"


def token_file(state):
    return Path(state) / TOKEN_FILE


def read_claim(state):
    """The stored claim: {"token", "issued_at", "expires_at"} once claimed,
    {"session", "since"} while a claim is pending, either with
    "refused_since" while Music Assistant refuses it, or {"problem":
    UNCLAIMED}; None when there is no file. A file that is there but cannot
    be read or trusted (its owner, mode or contents) is {"problem":
    UNREADABLE}: nothing replaces it until the owner repairs it."""
    try:
        data = json.loads(read_private(token_file(state), TOKEN_BYTES))
    except FileNotFoundError:
        return None
    except (AccessError, OSError, ValueError):
        return {"problem": UNREADABLE}
    if not isinstance(data, dict) or data.get("version") != 1:
        return {"problem": UNREADABLE}
    if data.get("problem") == UNCLAIMED:
        return {"problem": UNCLAIMED}
    refused = {"refused_since": data["refused_since"]} if type(data.get("refused_since")) is int else {}
    for key, times in (("token", ("issued_at", "expires_at")), ("session", ("since",))):
        value = data.get(key)
        if (isinstance(value, str) and 0 < len(value) <= 4096 and value.isprintable()
                and all(type(data.get(each)) is int for each in times)):
            return {key: value, **{each: data[each] for each in times}, **refused}
    return {"problem": UNREADABLE}


def problem(claim, now):
    """Why the gateway cannot use a claim (read_claim's), as its problem
    code; None when it holds a token to try."""
    if claim is None or claim.get("problem") == UNREADABLE:
        return "music_unavailable"
    if claim.get("problem") == UNCLAIMED or now - claim.get("refused_since", now) >= REFUSED:
        return UNCLAIMED
    return None if "token" in claim else "music_unavailable"


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
        """One pass: claim, resume or renew once Music Assistant runs, then
        reconcile when due. "ready"; "unclaimed" when the server had a user
        before thermite claimed it; "refused" while it refuses the stored
        token or session; "unreadable" while the claim file cannot be
        trusted. Unavailable while it does not run."""
        with self.connect(self.spec["port"], timeout=SETUP) as client:
            info = client.info()
            if info.get("status") != "running":
                raise Unavailable("Music Assistant is starting")
            claim = read_claim(self.state)
            if claim == {"problem": UNREADABLE}:
                self.report("cannot trust its stored claim; give the file to the service account, mode 0600")
                return "unreadable"
            claimed = False
            if claim is None or "problem" in claim:
                claim = self.setup(client)
                if claim is None:
                    if read_claim(self.state) != {"problem": UNCLAIMED}:
                        write_claim(self.state, {"problem": UNCLAIMED})
                    self.report("unclaimed: another user set it up; reset its state to let thermite claim it")
                    return "unclaimed"
            try:
                try:
                    token, claimed = self.hold(client, claim)
                except NoUser:
                    # Its state was deleted: no user, so no one else's.
                    claim = self.setup(client)
                    if claim is None:
                        raise Unavailable("Music Assistant gained a user while thermite claimed it") from None
                    token, claimed = self.hold(client, claim)
            except Unauthorized:
                if "refused_since" not in claim:
                    write_claim(self.state, {**claim, "refused_since": int(self.clock())})
                self.report("refused: it refuses thermite's token; trying again")
                return "refused"
            client.token = token
            server, now, changed = info.get("server_id"), self.clock(), 0
            if claimed or self.reconciled is None or self.reconciled[0] != server or now - self.reconciled[1] >= self.every:
                changed = self.reconcile(client, info)
                self.reconciled = (server, now)
        self.report("ready", changed=changed)
        return "ready"

    def setup(self, client):
        """Create thermite's user on a server with none, storing its session
        at once (a pending claim); None when the server has a user."""
        session = client.setup(USERNAME, secrets.token_urlsafe(32), "thermite")
        if session is None:
            return None
        claim = {"session": session, "since": int(self.clock())}
        write_claim(self.state, claim)
        return claim

    def hold(self, client, claim):
        """(the token to use, whether it was just claimed): the pending
        claim's first long-lived token, or the stored one or its successor.
        Unauthorized when Music Assistant refuses what is stored, NoUser
        when it has no user."""
        if "session" in claim:
            client.token = claim["session"]
            token = self.mint(client)
            client.token = token
            self.revoke_others(client, token)
            LOG.info("Music Assistant claimed by thermite.")
            return token, True
        client.token = claim["token"]
        tokens = client.call("auth/tokens")  # Proves the token before anything else uses it.
        if claim["expires_at"] - self.clock() > RENEW_DAYS * DAY:
            if "refused_since" in claim:
                write_claim(self.state, {key: value for key, value in claim.items() if key != "refused_since"})
            self.revoke_others(client, claim["token"], tokens)
            return claim["token"], False
        token = self.mint(client)
        client.token = token
        self.revoke_others(client, token)
        LOG.info("Music Assistant token renewed.")
        return token, False

    def mint(self, client):
        token = client.call("auth/token/create", name=USERNAME)
        if not isinstance(token, str) or not token or len(token) > 4096 or not token.isprintable():
            raise Failed(200)
        issued = int(self.clock())
        write_claim(self.state, {"token": token, "issued_at": issued, "expires_at": issued + TOKEN_DAYS * DAY})
        return token

    def revoke_others(self, client, token, tokens=None):
        """Revoke every token of thermite's user but this one: the setup
        session, one a renewal replaced, one minted by a pass that never
        stored it."""
        mine = digest(token)
        for entry in (client.call("auth/tokens") if tokens is None else tokens) or ():
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
        if info.get("onboard_done") is False:
            client.call("config/onboard_complete")
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
