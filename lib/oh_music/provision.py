"""Thermite's claim on Music Assistant and the configuration it keeps there.

Music Assistant is thermite's alone: the gateway is its only client. The
policy worker (oh_access.worker), the site's one supervised background
process, holds that claim; the gateway only reads the token it stores.

Claim. A fresh Music Assistant has no user. Once it reports running the
worker stores a random password in TOKEN_FILE, in the gateway's private
state, then creates Music Assistant's first admin, `thermite`, with it, and
stores the setup session it is answered with beside it: a pending claim,
which the worker resumes from on its next pass however this one ends. A
setup whose answer was lost left a user thermite has the password of, so a
server with a user is signed in to with a pending password before it is
called someone else's. With the session the worker mints a long-lived token
(Music Assistant's live a year), which replaces the session and the
password in the file, and revokes every other token of the user. Within
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
ID changes (its state was reset) and every EVERY seconds, whether the last
one worked or not: the sonos, wiim and airplay player providers are present
(MA groups a WiiM with a Sonos over AirPlay); the sonos and wiim providers
are given the house's speakers' addresses for discovery; each library
(oh_music.settings) is one filesystem_local instance on MEDIA/<name>, named
LIBRARY_NAME, and once each has one every other filesystem_local instance
is removed; the providers in UNWANTED are removed; remote access stays off;
onboarding is complete. Each part is applied alone: one Music Assistant
fails is logged and tried at the next reconcile, and the others go on.
Players are never renamed or disabled.
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
from .client import SLOW, Client, Failed, NoUser, Unauthorized, Unavailable

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
#: The private file in the gateway's state that records each library's
#: instance ID from the moment it is added, so that an instance whose
#: naming failed is named again, never replaced and rescanned.
LIBRARY_FILE = "music-libraries.json"
LIBRARY_BYTES = 65536
#: Providers Music Assistant may load that thermite does not use: other
#: speaker families it would find on the LAN, and features that act on
#: their own.
UNWANTED = ("chromecast", "dlna", "heos", "bluesound", "party", "smart_fades", "lastfm_recommendations",
            "ambient_sounds")
#: Seconds between passes, and between reconciles of a server that has not
#: changed.
POLL = 30
EVERY = 15 * 60
#: The problem a stored claim records when the server is someone else's,
#: and what read_claim() answers for a claim file it cannot trust.
UNCLAIMED = "music_unclaimed"
UNREADABLE = "unreadable"


def token_file(state):
    return Path(state) / TOKEN_FILE


def secret(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and value.isprintable()


def read_claim(state):
    """The stored claim: {"token", "issued_at", "expires_at"} once claimed;
    while a claim is pending, {"password"} from before Music Assistant is
    asked for its user, then {"session", "password"}; a token or session
    with "refused_since" while Music Assistant refuses it; or {"problem":
    UNCLAIMED}. None when there is no file. A file that is there but cannot
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
    password = {"password": data["password"]} if secret(data.get("password")) else {}
    if secret(data.get("token")) and all(type(data.get(each)) is int for each in ("issued_at", "expires_at")):
        return {key: data[key] for key in ("token", "issued_at", "expires_at")} | refused
    if secret(data.get("session")) and password:
        return {"session": data["session"]} | password | refused
    if password and not {"token", "session"} & data.keys():
        return password
    return {"problem": UNREADABLE}


def held(claim):
    """Whether a claim holds a token or a session to try."""
    return claim is not None and ("token" in claim or "session" in claim)


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


def read_libraries(state):
    """{library: instance ID} as last recorded; empty when there is no record
    or it cannot be read (it is rewritten on the next change)."""
    try:
        data = json.loads(read_private(Path(state) / LIBRARY_FILE, LIBRARY_BYTES))
    except FileNotFoundError:
        return {}
    except (AccessError, OSError, ValueError):
        LOG.warning("Music Assistant's library record cannot be read; libraries are found by name.")
        return {}
    libraries = data.get("libraries") if isinstance(data, dict) else None
    return {library: instance for library, instance in (libraries or {}).items()
            if isinstance(library, str) and isinstance(instance, str)} if isinstance(libraries, dict) else {}


def write_libraries(state, libraries):
    json_private(Path(state) / LIBRARY_FILE, {"version": 1, "libraries": libraries})


def digest(token):
    """Music Assistant's name for a token in its token list."""
    return hashlib.sha256(token.encode()).hexdigest()


class Provisioner:
    """One site's claim and configuration. spec is the contract's music
    (oh_site_contract.music); connect(port, token) opens a Client."""

    def __init__(self, state, spec, *, connect=Client, clock=time.time, every=EVERY, stop=None):
        self.state, self.spec, self.connect, self.clock = Path(state), spec, connect, clock
        self.every, self.stop = every, stop
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
        reconcile when due. "ready"; "unclaimed" when the server has a user
        thermite has neither a token, a session nor the password of;
        "refused" while it refuses the stored token or session; "unreadable"
        while the claim file cannot be trusted. Unavailable while it does
        not run."""
        # A provider's setup flow loads the provider before it answers.
        with self.connect(self.spec["port"], timeout=SLOW) as client:
            info = client.info()
            if info.get("status") != "running":
                raise Unavailable("Music Assistant is starting")
            claim = read_claim(self.state)
            if claim == {"problem": UNREADABLE}:
                self.report("cannot trust its stored claim; give the file to the service account, mode 0600")
                return "unreadable"
            claimed = False
            if not held(claim):
                claim = self.claim(client, claim)
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
                    claim = self.claim(client, None)
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
                # Recorded first: a reconcile that fails waits as long as one that works.
                self.reconciled = (server, now)
                changed = self.reconcile(client, info)
        self.report("ready", changed=changed)
        return "ready"

    def claim(self, client, claim):
        """A pending claim, {"session", "password"}: thermite's user created
        on a server with none, the password stored before Music Assistant
        is asked; or, on a server with a user, a session signed in with the
        pending password (a setup whose answer was lost). None when the
        server's user is not one thermite has the password of."""
        password = (claim or {}).get("password")
        session = None
        if not client.has_user():
            if password is None:
                password = secrets.token_urlsafe(32)
                write_claim(self.state, {"password": password})
            session = client.setup(USERNAME, password, "thermite")
        if session is None and password is not None:
            session = client.login(USERNAME, password, "thermite")
        if session is None:
            return None
        claim = {"session": session, "password": password}
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
        """Bring Music Assistant's configuration to what thermite needs, one
        part at a time; the number of changes made. A part that fails is
        logged, and the parts after it are still applied; one that loses
        Music Assistant, or the worker stopping, ends the pass."""
        changed = 0
        for part, apply in (("player providers", self.players), ("speaker addresses", self.discovery),
                            ("libraries", self.libraries), ("unwanted providers", self.unwanted),
                            ("remote access", self.remote_access), ("onboarding", self.onboarding)):
            if self.stop is not None and self.stop.is_set():
                break
            try:
                changed += apply(client, info)
            except Unavailable:
                raise
            except Exception as error:
                LOG.warning("Music Assistant: could not reconcile its %s (%s); trying again at the next reconcile.",
                            part, type(error).__name__)
        return changed

    @staticmethod
    def configs(client):
        return [config for config in client.call("config/providers") or ()
                if isinstance(config, dict) and isinstance(config.get("instance_id"), str)]

    def players(self, client, info):
        domains = {config.get("domain") for config in self.configs(client)}
        return sum(self.add(client, domain) is not None for domain in PLAYERS if domain not in domains)

    def discovery(self, client, info):
        changed, key = 0, "manual_discovery_ip_addresses"
        speakers = self.spec["speakers"].values()
        for config in self.configs(client):
            domain, instance = config.get("domain"), config["instance_id"]
            if domain in ("sonos", "wiim"):
                wanted = sorted({speaker["ip"] for speaker in speakers if speaker["provider"] == domain and "ip" in speaker})
                if sorted(client.call("config/providers/get_value", instance_id=instance, key=key) or ()) != wanted:
                    client.call("config/providers/save", provider_domain=domain, instance_id=instance, values={key: wanted})
                    changed += 1
        return changed

    def libraries(self, client, info):
        """One filesystem_local instance per library. An instance is a
        library's by the ID recorded when thermite added it, else by its
        name; one whose name was lost is named again. Every other instance
        (a library no longer listed, one the owner added in Music
        Assistant's own UI) is removed only once each library has its
        instance, named: nothing is removed in the hope of replacing it."""
        names = {LIBRARY_NAME.format(library): library for library in self.spec["libraries"]}
        configs = {config["instance_id"]: config for config in self.configs(client)
                   if config.get("domain") == "filesystem_local"}
        recorded = read_libraries(self.state)
        held = {library: instance for library, instance in recorded.items()
                if library in self.spec["libraries"] and instance in configs}
        for instance, config in configs.items():
            library = names.get(config.get("name"))
            if library and library not in held and instance not in held.values():
                held[library] = instance
        changed = 0
        for library in sorted(set(self.spec["libraries"]) - set(held)):
            instance = self.add(client, "filesystem_local", {"content_type": "music", "path": f"{MEDIA}/{library}"})
            if instance is not None:
                held[library] = instance
                write_libraries(self.state, held)
                changed += 1
        if held != recorded:
            write_libraries(self.state, held)
        for library, instance in sorted(held.items()):
            name = LIBRARY_NAME.format(library)
            if configs.get(instance, {}).get("name") != name:
                client.call("config/providers/save", provider_domain="filesystem_local", instance_id=instance,
                            values={"name": name})
                changed += instance in configs
        if held.keys() == set(self.spec["libraries"]):
            for instance in configs.keys() - set(held.values()):
                client.call("config/providers/remove", instance_id=instance)
                changed += 1
        return changed

    def unwanted(self, client, info):
        changed = 0
        for config in self.configs(client):
            if config.get("domain") in UNWANTED:
                client.call("config/providers/remove", instance_id=config["instance_id"])
                changed += 1
        return changed

    @staticmethod
    def remote_access(client, info):
        if info.get("has_remote_access"):
            client.call("remote_access/configure", enabled=False)
            return 1
        return 0

    @staticmethod
    def onboarding(client, info):
        if info.get("onboard_done") is False:
            client.call("config/onboard_complete")
            return 1
        return 0

    def add(self, client, domain, values=None):
        """Add one provider instance through its setup flow, answering its one
        form with values: the new instance's ID, or None (logged) when Music
        Assistant does not add it."""
        step = client.call("config/providers/setup", provider_domain=domain)
        if isinstance(step, dict) and step.get("type") == "form" and values is not None:
            step = client.call("config/flows/submit", flow_id=step["flow_id"], values=values)
        kind = step.get("type") if isinstance(step, dict) else None
        if kind == "finish":
            instance = (step.get("result") or {}).get("instance_id")
            if isinstance(instance, str) and instance:
                return instance
            why = "it named no instance"
        elif kind == "abort":
            why = "it refused"
        else:
            # A step thermite cannot answer: end the flow.
            if isinstance(step, dict) and isinstance(step.get("flow_id"), str):
                client.call("config/flows/abort", flow_id=step["flow_id"])
            why = "it asked for more than thermite answers"
        LOG.warning("Music Assistant could not add the %s provider: %s.", domain, why)
        return None


def run(state, spec, stop, *, poll=POLL, every=EVERY, connect=Client):
    """The worker's music thread: until stop is set, a pass every `poll`
    seconds (Provisioner.step), reconciling every `every`."""
    provisioner = Provisioner(state, spec, connect=connect, every=every, stop=stop)
    while not stop.is_set():
        try:
            provisioner.step()
        except Unavailable:
            provisioner.report("unavailable")
        except Exception as error:
            # A refused or failed command: tried again from the start.
            provisioner.report("failed", error=type(error).__name__)
        stop.wait(poll)
