"""Thermite's claim on Music Assistant and the configuration it keeps there.

Music Assistant is thermite's alone: the gateway is its only client. The
policy worker (oh_access.worker), the site's one supervised background
process, holds that claim; the gateway only reads the token it stores.

Claim. A fresh Music Assistant has no user. Once it reports running the
worker stores a random password in TOKEN_FILE, in the gateway's private
state, then creates Music Assistant's first admin, `thermite`, with it.
With the session it is answered with the worker mints a long-lived token
(Music Assistant's live a year), stores it beside the password, and
revokes every other token of the user, the session included. The password
stays: whenever Music Assistant refuses the stored token (it was revoked,
or Music Assistant's state was reset), or there is none yet (the answer to
the setup was lost), the worker signs in with it, or creates the user
again on a server with none, and mints the next token. Within RENEW_DAYS of
a token's end the worker mints the next, stores it, then revokes the rest.
A server that has a user when thermite has no password stored is someone
else's (music_unclaimed) until the owner resets Music Assistant: stops it,
deletes its state, starts it.

The password is never given up because Music Assistant refuses it: it
answers 401 alike to a wrong password, a disabled user and a login it rate
limits after failed ones (REFUSALS). The file counts the passes in a
row that reached Music Assistant and had the password refused, up to
REFUSALS, a day's; each pass tries again, and the first that signs in
clears the count. Only once the count is full does the gateway call the
server someone else's (music_unclaimed, problem()), and the worker still
tries.

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
from oh_access.files import Oversized, json_private, read_private, remove_temporaries

from . import MEDIA
from .client import SLOW, Client, Failed, NoUser, Stopped, Unauthorized, Unavailable

LOG = logging.getLogger(__name__)
#: The private file in the gateway's state that holds the claim.
TOKEN_FILE = "music-token.json"
TOKEN_BYTES = 8192
USERNAME = "thermite"
#: Days a long-lived token lives, and how many before its end the worker
#: replaces it.
TOKEN_DAYS = 365
RENEW_DAYS = 30
#: Music Assistant lists a user's newest tokens, this many at most
#: (auth/tokens' TOKEN_LIST_LIMIT), and the times revoke_others lists them
#: again while a list comes back full.
TOKEN_LIST = 100
REVOKE_ROUNDS = 20
DAY = 24 * 3600
#: The player providers thermite drives.
PLAYERS = ("sonos", "wiim", "airplay")
#: What the gateway names each library's filesystem_local instance; Music
#: Assistant never reports the path an instance was set up with.
LIBRARY_NAME = "thermite {}"
#: The private file in the gateway's state that records each library's
#: instance ID from the moment it is added, so that an instance whose
#: naming failed is named again, never replaced and rescanned. While it
#: cannot be trusted, as the claim, the libraries are left as they are.
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
#: Passes in a row whose sign-in Music Assistant refused, a day's, after
#: which the server is no longer thermite's. Passes are counted, not time:
#: Music Assistant down, or the clock stepped, adds none. Music Assistant
#: blocks a username's logins for 30 seconds after 3 failures in 30 minutes,
#: longer after more, so one refusal alone says little.
REFUSALS = 24 * 3600 // POLL
#: The problem a stored claim records when the server is someone else's,
#: and what read_claim() answers for a claim file it cannot trust.
UNCLAIMED = "music_unclaimed"
UNREADABLE = "unreadable"
#: Why a private record cannot be trusted: its owner or mode (or reading it
#: failed), or contents thermite did not write.
ACCESS, CONTENTS = "access", "contents"


class Untrusted(Exception):
    """A private record that is there but cannot be trusted; reason is
    ACCESS or CONTENTS."""

    def __init__(self, name, reason):
        super().__init__(name)
        self.reason = reason


def stored(path, maximum):
    """A private record's JSON object (version 1), or None when there is no
    file; Untrusted for one that is there but cannot be trusted. The claim
    and the library record are read alike."""
    try:
        data = read_private(path, maximum)
    except FileNotFoundError:
        return None
    except Oversized:
        raise Untrusted(Path(path).name, CONTENTS) from None
    except (AccessError, OSError):
        raise Untrusted(Path(path).name, ACCESS) from None
    try:
        data = json.loads(data)
    except ValueError:
        raise Untrusted(Path(path).name, CONTENTS) from None
    if not isinstance(data, dict) or data.get("version") != 1:
        raise Untrusted(Path(path).name, CONTENTS)
    return data


def token_file(state):
    return Path(state) / TOKEN_FILE


def secret(value):
    return isinstance(value, str) and 0 < len(value) <= 4096 and value.isprintable()


def read_claim(state):
    """The stored claim: {"password"} from before Music Assistant gives
    thermite its first token; {"password", "token", "issued_at",
    "expires_at"} once it has; either with "refusals", the passes in a row
    Music Assistant refused the password; or {"problem": UNCLAIMED}. None
    when there is no file. A file that is there but cannot be read or
    trusted is {"problem": UNREADABLE, "reason": ACCESS or CONTENTS}:
    nothing replaces it until the owner repairs it."""
    damaged = {"problem": UNREADABLE, "reason": CONTENTS}
    try:
        data = stored(token_file(state), TOKEN_BYTES)
    except Untrusted as error:
        return {"problem": UNREADABLE, "reason": error.reason}
    if data is None:
        return None
    if data.get("problem") == UNCLAIMED:
        return {"problem": UNCLAIMED}
    if not secret(data.get("password")):
        return damaged
    claim = {"password": data["password"]}
    if "token" in data:
        if not secret(data["token"]) or not all(type(data.get(each)) is int for each in ("issued_at", "expires_at")):
            return damaged
        claim |= {key: data[key] for key in ("token", "issued_at", "expires_at")}
    if type(data.get("refusals")) is int and data["refusals"] > 0:
        claim["refusals"] = min(data["refusals"], REFUSALS)
    return claim


def unreadable(claim):
    """Whether a claim is a file that cannot be trusted."""
    return claim is not None and claim.get("problem") == UNREADABLE


#: What the owner does about a claim file that cannot be trusted, by why.
REPAIR_CLAIM = {ACCESS: "give the file to the service account, mode 0600",
                CONTENTS: "thermite did not write it; restore it, or reset Music Assistant and delete it"}


def unclaimed_because(claim):
    """Why problem() calls a claim UNCLAIMED, and the owner's repair."""
    if claim.get("refusals", 0) >= REFUSALS:
        return ("it has refused thermite's password for a day; reset its state, or restore the claim file "
                "thermite stored")
    return "another user set it up; reset its state to let thermite claim it"


def problem(claim):
    """Why the gateway cannot use a claim (read_claim's), as its problem
    code; None when it holds a token to try."""
    if claim is None or unreadable(claim):
        return "music_unavailable"
    if claim.get("problem") == UNCLAIMED or claim.get("refusals", 0) >= REFUSALS:
        return UNCLAIMED
    return None if "token" in claim else "music_unavailable"


def write_claim(state, claim):
    json_private(token_file(state), {"version": 1, **claim})


def read_libraries(state):
    """{library: instance ID} as last recorded; empty when there is no
    record. Untrusted for one that cannot be trusted."""
    data = stored(Path(state) / LIBRARY_FILE, LIBRARY_BYTES)
    libraries = {} if data is None else data.get("libraries")
    if not isinstance(libraries, dict) or not all(
            isinstance(library, str) and isinstance(instance, str) for library, instance in libraries.items()):
        raise Untrusted(LIBRARY_FILE, CONTENTS)
    return libraries


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
        # What has been warned of, once until it changes.
        self.warned = set()
        # (server ID, when) of the last reconcile.
        self.reconciled = None

    def report(self, outcome, **fields):
        """One log line when the outcome changes; never a token or an address."""
        if outcome != self.reported:
            self.reported = outcome
            details = "".join(f" {key}={value}" for key, value in sorted(fields.items()))
            LOG.info("Music Assistant: %s%s", outcome, details)

    def step(self):
        """One pass: claim, sign in again or renew once Music Assistant runs,
        then reconcile when due. "ready"; "unclaimed" when the server had a
        user before thermite claimed it; "refused" while it refuses
        thermite's password; "unreadable" while the claim file cannot be
        trusted. Unavailable while it does not run."""
        # A provider's setup flow loads the provider before it answers.
        with self.connect(self.spec["port"], timeout=SLOW) as client:
            # Checked before each request: a stopping worker waits for the one in flight at most.
            client.stop = self.stop
            info = client.info()
            if info.get("status") != "running":
                raise Unavailable("Music Assistant is starting")
            claim = read_claim(self.state)
            if unreadable(claim):
                self.report("cannot trust its stored claim; " + REPAIR_CLAIM[claim["reason"]])
                return "unreadable"
            if claim is None or "password" not in claim:
                if client.has_user():
                    if claim is None:
                        write_claim(self.state, {"problem": UNCLAIMED})
                    self.report("unclaimed: " + unclaimed_because({"problem": UNCLAIMED}))
                    return "unclaimed"
                # Stored before Music Assistant is asked: a setup whose answer is lost is signed in to.
                claim = {"password": secrets.token_urlsafe(32)}
                write_claim(self.state, claim)
            token = self.kept(client, claim) if "token" in claim else None
            signed_in = token is None
            if signed_in:
                token = self.sign_in(client, claim)
                if token is None:
                    claim = self.count_refusal(claim)
                    # Once, as the gateway turns to music_unclaimed.
                    self.report("unclaimed: " + unclaimed_because(claim) if problem(claim) == UNCLAIMED else
                                "refused: it refuses thermite's password; trying again")
                    return "refused"
            client.token = token
            server, now, changed = info.get("server_id"), self.clock(), 0
            if signed_in or self.reconciled is None or self.reconciled[0] != server or now - self.reconciled[1] >= self.every:
                # Recorded first: a reconcile that fails waits as long as one that works.
                self.reconciled = (server, now)
                changed = self.reconcile(client, info)
        self.report("ready", changed=changed)
        return "ready"

    def kept(self, client, claim):
        """The stored token, or its successor once it is near its end; None
        when Music Assistant refuses it or has no user (its state was
        reset)."""
        client.token = claim["token"]
        try:
            tokens = client.call("auth/tokens")  # Proves the token before anything else uses it.
        except (Unauthorized, NoUser):
            return None
        if claim["expires_at"] - self.clock() > RENEW_DAYS * DAY:
            if "refusals" in claim:
                write_claim(self.state, {key: value for key, value in claim.items() if key != "refusals"})
            self.revoke_others(client, claim["token"], tokens)
            return claim["token"]
        token = self.mint(client, claim)
        LOG.info("Music Assistant token renewed.")
        return token

    def sign_in(self, client, claim):
        """A new long-lived token from a session signed in with thermite's
        password, or from thermite's user created with it on a server with
        none; None when Music Assistant refuses the password. Every other
        token of the user goes once the token is stored, the session
        included; a session whose token could not be minted is logged out,
        so that sessions never pile up."""
        try:
            session = client.login(USERNAME, claim["password"], "thermite")
        except NoUser:
            session = client.setup(USERNAME, claim["password"], "thermite")
            if session is None:
                raise Unavailable("Music Assistant gained a user while thermite claimed it") from None
        if session is None:
            return None
        client.token = session
        try:
            token = self.mint(client, claim)
        except Exception:
            if client.token == session:  # No token was stored.
                self.log_out(client)
            raise
        LOG.info("Music Assistant claimed by thermite.")
        return token

    def count_refusal(self, claim):
        """Count a pass whose password Music Assistant refused, up to
        REFUSALS; the claim as stored then. Nothing was written earlier in
        such a pass, so the claim read at its start is the one stored."""
        refusals = claim.get("refusals", 0)
        if refusals < REFUSALS:
            claim = {**claim, "refusals": refusals + 1}
            write_claim(self.state, claim)
        return claim

    def mint(self, client, claim):
        """A new long-lived token, minted with the client's token and stored
        with thermite's password; then the client uses it, and every other
        token of the user is revoked. One that cannot be stored is logged
        out at once, the client left on the token it had: held nowhere, a
        year-long admin token would be left behind by every pass whose
        write fails."""
        token = client.call("auth/token/create", name=USERNAME)
        if not secret(token):
            raise Failed(200)
        issued = int(self.clock())
        try:
            write_claim(self.state, {"password": claim["password"], "token": token, "issued_at": issued,
                                     "expires_at": issued + TOKEN_DAYS * DAY})
        except BaseException:
            minting, client.token = client.token, token
            try:
                self.log_out(client)
            finally:
                client.token = minting
            raise
        client.token = token
        self.revoke_others(client, token)
        return token

    @staticmethod
    def log_out(client):
        """Revoke the client's own token, as far as Music Assistant answers:
        one left goes with the others once a token is stored."""
        try:
            client.call("auth/logout")
        except Exception:
            pass

    def revoke_others(self, client, token, tokens=None):
        """Revoke every token of thermite's user but this one: a session it
        was minted with, one it replaced, one minted by a pass that never
        stored it. Music Assistant lists only the newest TOKEN_LIST, so a
        full list is read again once its others are revoked, REVOKE_ROUNDS
        times at most."""
        mine = digest(token)
        for _ in range(REVOKE_ROUNDS):
            listed = client.call("auth/tokens") if tokens is None else tokens
            tokens = None
            for entry in listed if isinstance(listed, list) else ():
                if isinstance(entry, dict) and entry.get("token_hash") != mine and isinstance(entry.get("token_id"), str):
                    client.call("auth/token/revoke", token_id=entry["token_id"])
            if not isinstance(listed, list) or len(listed) < TOKEN_LIST:
                return

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
            try:
                changed += apply(client, info)
            except (Unavailable, Stopped):
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
        instance, named: nothing is removed in the hope of replacing it. A
        new instance is named and recorded (keep). While the record cannot
        be trusted the libraries are left as they are."""
        try:
            recorded = read_libraries(self.state)
        except Untrusted as error:
            if "libraries" not in self.warned:
                self.warned.add("libraries")
                repair = ("give it to the service account, mode 0600" if error.reason == ACCESS else
                          "thermite did not write it; delete it to have the libraries found by name")
                LOG.warning("Music Assistant: its library record %s cannot be trusted; %s. Its libraries are left "
                            "as they are until then.", LIBRARY_FILE, repair)
            return 0
        self.warned.discard("libraries")
        names = {LIBRARY_NAME.format(library): library for library in self.spec["libraries"]}
        configs = {config["instance_id"]: config for config in self.configs(client)
                   if config.get("domain") == "filesystem_local"}
        found = {library: instance for library, instance in recorded.items()
                 if library in self.spec["libraries"] and instance in configs}
        for instance, config in configs.items():
            library = names.get(config.get("name"))
            if library and library not in found and instance not in found.values():
                found[library] = instance
        changed = 0
        for library in sorted(set(self.spec["libraries"]) - set(found)):
            instance = self.add(client, "filesystem_local", {"content_type": "music", "path": f"{MEDIA}/{library}"})
            if instance is not None:
                found[library] = instance
                changed += 1
                self.keep(client, library, instance, found)
        if found != recorded:
            write_libraries(self.state, found)
        for library, instance in sorted(found.items()):
            if instance in configs and configs[instance].get("name") != LIBRARY_NAME.format(library):
                self.name(client, library, instance)
                changed += 1
        if found.keys() == set(self.spec["libraries"]):
            for instance in configs.keys() - set(found.values()):
                client.call("config/providers/remove", instance_id=instance)
                changed += 1
        return changed

    def keep(self, client, library, instance, found):
        """Name a new library instance and record it ({library: instance}),
        each whatever the other does: either finds it again. One neither
        named nor recorded would be added again at every reconcile, so it
        is removed; if that fails too, it is an instance of no library, which
        the next reconcile removes once the library has another."""
        errors = []
        for keep in (lambda: self.name(client, library, instance), lambda: write_libraries(self.state, found)):
            try:
                keep()
            except Exception as error:
                errors.append(error)
        if len(errors) == 2:
            named, recorded = (type(error).__name__ for error in errors)
            try:
                client.call("config/providers/remove", instance_id=instance)
            except Exception as error:
                LOG.warning("Music Assistant: library %s's new instance could be neither named (%s) nor recorded "
                            "(%s), nor removed (%s): the next reconcile removes it once the library has another.",
                            library, named, recorded, type(error).__name__)
            else:
                LOG.warning("Music Assistant: library %s's new instance could be neither named (%s) nor recorded "
                            "(%s); it was removed.", library, named, recorded)
        if errors:
            raise errors[0]

    @staticmethod
    def name(client, library, instance):
        client.call("config/providers/save", provider_domain="filesystem_local", instance_id=instance,
                    values={"name": LIBRARY_NAME.format(library)})

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
    seconds (Provisioner.step), reconciling every `every`. First, what a
    write killed midway left beside the records goes."""
    try:
        for name in (TOKEN_FILE, LIBRARY_FILE):
            remove_temporaries(Path(state) / name)
    except OSError as error:
        LOG.warning("Music Assistant: the records' leftover temporary files cannot be removed (%s).",
                    type(error).__name__)
    provisioner = Provisioner(state, spec, connect=connect, every=every, stop=stop)
    while not stop.is_set():
        try:
            provisioner.step()
        except Stopped:
            break
        except Unavailable:
            provisioner.report("unavailable")
        except Exception as error:
            # A refused or failed command: tried again from the start.
            provisioner.report("failed", error=type(error).__name__)
        stop.wait(poll)
