"""Thermite's side of Music Assistant below the gateway: the worker's claim
and the configuration it keeps (oh_music.provision), the HTTP client's
failures, the websocket the gateway reads events from, TIDAL's sign-in, and
the stand-in's own Music Assistant semantics (oh_music_sim). Fictional
speakers, documentation addresses and demo MACs only."""
import asyncio
import errno
import json
import os
import stat
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))
import httpx  # noqa: E402
import oh_music_sim as stand_in  # noqa: E402
from oh_access import AccessError  # noqa: E402
from oh_access.files import atomic_private  # noqa: E402
from oh_music import provision, tidal  # noqa: E402
from oh_music.client import SLOW, Client, Failed, NoUser, Stopped, Unauthorized, Unavailable  # noqa: E402
from oh_music.events import Closed, Socket  # noqa: E402

DAY = 24 * 3600


def mac(n):
    return bytes((2, 0, 0, 0, 0, n)).hex(":")


SONOS, AMP_PRO, MINI = "example_den_sonos", "example_desk_wiim", "example_kitchen_mini"
SPEAKERS = (
    {"id": SONOS, "label": "Sonos One", "room": "example_den", "room_label": "Example Den", "provider": "sonos",
     "udn": "RINCON_" + mac(21).replace(":", "").upper() + "01400", "ip": "192.0.2.21", "rooms": {}, "model": "One"},
    {"id": AMP_PRO, "label": "WiiM Amp Pro", "room": "example_desk", "room_label": "Example Desk", "provider": "wiim",
     "mac": mac(32), "ip": "192.0.2.32", "rooms": {}, "model": "Amp Pro"},
    {"id": MINI, "label": "WiiM Mini", "room": "example_kitchen", "room_label": "Example Kitchen", "provider": "wiim",
     "ip": "192.0.2.33", "rooms": {}, "model": "Mini"})


def spec(port, libraries=("flac",)):
    """The contract's music for the stand-in's speakers."""
    return {"port": port, "libraries": sorted(libraries),
            "speakers": {speaker["id"]: {key: value for key, value in speaker.items() if key != "model"}
                         for speaker in SPEAKERS}}


class Site(unittest.TestCase):
    """A stand-in Music Assistant on loopback and the gateway's private state."""

    def setUp(self):
        self.sim = stand_in.Sim(SPEAKERS)
        self.server = stand_in.Server(self.sim).start()
        self.addCleanup(lambda: self.server.stop())
        self.port = self.server.port
        self.state = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.state)
        os.chmod(self.state, 0o700)

    def provisioner(self, libraries=("flac",), **options):
        return provision.Provisioner(self.state, spec(self.port, libraries), **options)

    def reset(self):
        """Music Assistant with its state deleted, on the same port."""
        self.server.stop()
        self.sim = stand_in.Sim(SPEAKERS)
        self.server = stand_in.Server(self.sim, self.port).start()

    def token(self):
        return provision.read_claim(self.state)["token"]

    def domains(self):
        return sorted(entry["domain"] for entry in self.sim.providers.values())


class Claim(Site):
    def test_a_fresh_server_is_claimed_with_one_stored_token_and_onboarded(self):
        self.assertEqual(self.provisioner().step(), "ready")
        claim = provision.read_claim(self.state)
        self.assertEqual(claim["expires_at"] - claim["issued_at"], 365 * DAY)
        self.assertEqual(os.stat(provision.token_file(self.state)).st_mode & 0o777, 0o600)
        self.assertEqual(list(self.sim.users), ["thermite"])
        self.assertTrue(self.sim.onboard_done)
        # The setup session is revoked; the password is kept beside the token.
        [(token, entry)] = self.sim.tokens.items()
        self.assertEqual((token, entry["is_long_lived"]), (claim["token"], True))
        self.assertEqual(claim["password"], self.sim.users["thermite"])

    def test_a_token_near_its_end_is_replaced_and_the_old_one_revoked(self):
        now = [time.time()]
        provisioner = self.provisioner(clock=lambda: now[0])
        provisioner.step()
        first = self.token()
        now[0] += 300 * DAY
        provisioner.step()
        self.assertEqual(self.token(), first)
        now[0] += 40 * DAY
        provisioner.step()
        second = self.token()
        self.assertNotEqual(second, first)
        self.assertEqual(list(self.sim.tokens), [second])
        self.assertEqual(provision.read_claim(self.state)["issued_at"], int(now[0]))

    def test_a_server_someone_else_set_up_is_unclaimed_until_it_is_reset(self):
        self.sim.create_user("someone", "a long enough password")
        provisioner = self.provisioner()
        with self.assertLogs("oh_music.provision", "INFO") as logs:
            self.assertEqual(provisioner.step(), "unclaimed")
            self.assertEqual(provisioner.step(), "unclaimed")
        self.assertEqual(len(logs.records), 1)
        self.assertIn("reset its state", logs.records[0].getMessage())
        self.assertEqual(provision.read_claim(self.state), {"problem": provision.UNCLAIMED})
        self.assertEqual(self.sim.providers.keys(), {"chromecast", "dlna", "smart_fades"})
        self.reset()
        self.assertEqual(provisioner.step(), "ready")
        self.assertEqual(list(self.sim.users), ["thermite"])

    def test_a_reset_server_is_claimed_again_with_the_same_password(self):
        provisioner = self.provisioner()
        provisioner.step()
        first = provision.read_claim(self.state)
        self.reset()
        self.assertEqual(provisioner.step(), "ready")
        self.assertNotEqual(self.token(), first["token"])
        self.assertEqual(list(self.sim.tokens), [self.token()])
        self.assertEqual(self.sim.users, {"thermite": first["password"]})

    def test_a_refused_token_is_replaced_by_signing_in_with_the_password(self):
        provisioner = self.provisioner()
        provisioner.step()
        claim = provision.read_claim(self.state)
        for refuse in (lambda: setattr(self.sim, "refusing", 1), self.sim.tokens.clear):
            # Music Assistant's authentication failed inside, or someone revoked the token.
            refuse()
            self.assertEqual(provisioner.step(), "ready")
            self.assertEqual(list(self.sim.tokens), [self.token()])
            self.assertNotEqual(self.token(), claim["token"])
            self.assertEqual(provision.read_claim(self.state)["password"], claim["password"])
            claim = provision.read_claim(self.state)

    def test_a_refused_password_is_kept_and_counted_and_the_next_sign_in_clears_it(self):
        provisioner = self.provisioner()
        provisioner.step()
        claim = provision.read_claim(self.state)
        self.sim.tokens.clear()
        # Someone's failed logins made Music Assistant rate limit thermite's.
        self.sim.limiting = 2
        with self.assertLogs("oh_music.provision", "INFO") as logs:
            self.assertEqual(provisioner.step(), "refused")
            self.assertEqual(provisioner.step(), "refused")
            self.assertEqual(provisioner.step(), "ready")
        self.assertEqual([record.getMessage() for record in logs.records][:2],
                         ["Music Assistant: refused: it refuses thermite's password; trying again",
                          "Music Assistant claimed by thermite."])
        stored = provision.read_claim(self.state)
        self.assertEqual((stored["password"], "refusals" in stored), (claim["password"], False))
        self.assertEqual(list(self.sim.tokens), [stored["token"]])

    def test_a_password_refused_for_a_days_passes_is_unclaimed_but_kept_and_tried(self):
        provisioner = self.provisioner()
        provisioner.step()
        claim = provision.read_claim(self.state)
        self.sim.tokens.clear()
        self.sim.users["thermite"] = "someone else's"
        with patch.object(provision, "REFUSALS", 3):
            self.assertEqual(provisioner.step(), "refused")
            # Passes that do not reach Music Assistant are not counted, whatever the clock says.
            self.server.stop()
            for _ in range(3):
                with self.assertRaises(Unavailable):
                    provisioner.step()
            self.server = stand_in.Server(self.sim, self.port).start()
            self.assertEqual(provisioner.step(), "refused")
            stored = provision.read_claim(self.state)
            self.assertEqual((stored["password"], stored["token"], stored["refusals"]),
                             (claim["password"], claim["token"], 2))
            self.assertIsNone(provision.problem(stored))
            with self.assertLogs("oh_music.provision", "INFO") as logs:
                self.assertEqual(provisioner.step(), "refused")
            self.assertEqual(provision.problem(provision.read_claim(self.state)), provision.UNCLAIMED)
            # Said once, with the repair, as the gateway turns to music_unclaimed.
            self.assertEqual([record.getMessage() for record in logs.records],
                             ["Music Assistant: unclaimed: it has refused thermite's password for a day; reset its "
                              "state, or restore the claim file thermite stored"])
            # A full count is not written again on every pass, nor said again.
            before = os.stat(provision.token_file(self.state)).st_ino
            with self.assertNoLogs("oh_music.provision", "INFO"):
                self.assertEqual(provisioner.step(), "refused")
            self.assertEqual(os.stat(provision.token_file(self.state)).st_ino, before)
            # The owner resets Music Assistant: no user, so it is claimed afresh with the password.
            self.reset()
            self.assertEqual(provisioner.step(), "ready")
        self.assertEqual(self.sim.users, {"thermite": claim["password"]})
        self.assertIsNone(provision.problem(provision.read_claim(self.state)))

    def test_a_failure_after_a_renewal_keeps_the_new_token(self):
        now = [time.time()]
        provisioner = self.provisioner(clock=lambda: now[0])
        provisioner.step()
        first = self.token()
        now[0] += 340 * DAY

        def refuse(*_):
            # The old token's revocation is refused, the new one already stored.
            raise Unauthorized("Music Assistant refused the token")
        with patch.object(provisioner, "revoke_others", refuse), self.assertRaises(Unauthorized):
            provisioner.step()
        claim = provision.read_claim(self.state)
        self.assertNotEqual(claim["token"], first)
        self.assertNotIn("refusals", claim)
        self.assertEqual(provisioner.step(), "ready")
        self.assertEqual(list(self.sim.tokens), [claim["token"]])

    def test_a_minted_token_that_cannot_be_stored_is_logged_out_on_either_path(self):
        real = provision.write_claim

        def full(state, claim):
            if "token" in claim:
                raise OSError(28, "No space left on device")
            real(state, claim)
        now = [time.time()]
        provisioner = self.provisioner(clock=lambda: now[0])
        # Claiming: neither the token nor the session it was minted with is left.
        with patch.object(provision, "write_claim", full), self.assertRaises(OSError):
            provisioner.step()
        self.assertEqual(self.sim.tokens, {})
        self.assertEqual(provisioner.step(), "ready")
        first = self.token()
        # Renewing: the stored token stays, alone.
        now[0] += 340 * DAY
        with patch.object(provision, "write_claim", full):
            for _ in range(3):
                with self.assertRaises(OSError):
                    provisioner.step()
        self.assertEqual((list(self.sim.tokens), self.token()), ([first], first))

    def test_a_minted_token_in_place_but_not_synced_is_kept_on_either_path(self):
        from oh_access import files
        real = os.fsync

        def unsynced(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "Input/output error")
            real(fd)
        now = [time.time()]
        provisioner = self.provisioner(clock=lambda: now[0])
        for path in ("claiming", "renewing"):
            with patch.object(files.os, "fsync", unsynced), self.assertLogs("oh_music.provision", "WARNING") as logs:
                self.assertEqual(provisioner.step(), "ready", path)
            self.assertIn("WARNING:oh_music.provision:Music Assistant: music-token.json was written, but its directory "
                          "could not be synced (EIO); a crash may undo the write.", logs.output)
            self.assertEqual(list(self.sim.tokens), [self.token()], path)
            self.assertEqual(provisioner.step(), "ready")
            now[0] += 340 * DAY

    def test_a_token_the_claim_file_holds_is_never_logged_out(self):
        real = provision.write_claim

        def failed_after(state, claim):
            real(state, claim)
            if "token" in claim:
                raise OSError(5, "Input/output error")
        provisioner = self.provisioner()
        with patch.object(provision, "write_claim", failed_after), self.assertRaises(OSError):
            provisioner.step()
        stored = self.token()
        self.assertIn(stored, self.sim.tokens)
        self.assertEqual(provisioner.step(), "ready")
        self.assertEqual((list(self.sim.tokens), self.token()), ([stored], stored))

    def test_every_other_token_goes_past_the_hundred_music_assistant_lists(self):
        provisioner = self.provisioner()
        provisioner.step()
        with self.sim.lock:
            for _ in range(250):
                self.sim._token("thermite", "stray", long_lived=True)
        provisioner.reconciled = None
        self.assertEqual(provisioner.step(), "ready")
        self.assertEqual(list(self.sim.tokens), [self.token()])

    def test_a_session_whose_token_was_not_minted_is_logged_out_and_signed_in_again(self):
        self.sim.failing["auth/token/create"] = 1
        provisioner = self.provisioner()
        with self.assertRaises(Failed):
            provisioner.step()
        claim = provision.read_claim(self.state)
        self.assertEqual(set(claim), {"password"})
        self.assertEqual(provision.problem(claim), "music_unavailable")
        self.assertEqual((list(self.sim.users), self.sim.tokens), (["thermite"], {}))
        self.assertEqual(provisioner.step(), "ready")
        self.assertEqual(list(self.sim.tokens), [self.token()])
        self.assertTrue(self.sim.onboard_done)

    def test_a_worker_killed_after_setup_is_resumed_and_its_session_revoked(self):
        def killed(*_):
            raise KeyboardInterrupt()
        with patch.object(provision.Provisioner, "mint", killed), self.assertRaises(KeyboardInterrupt):
            self.provisioner().step()
        [session] = self.sim.tokens
        # The next worker process.
        self.assertEqual(self.provisioner().step(), "ready")
        self.assertFalse(self.sim.authorized(session))
        self.assertEqual(list(self.sim.tokens), [self.token()])

    def test_the_password_is_stored_before_music_assistant_is_asked(self):
        stored = []

        def killed(client, *_):
            # The worker dies while it waits for Music Assistant's answer.
            stored.append(provision.read_claim(self.state))
            raise KeyboardInterrupt()
        with patch.object(Client, "setup", killed), self.assertRaises(KeyboardInterrupt):
            self.provisioner().step()
        [pending] = stored
        self.assertEqual(set(pending), {"password"})
        self.assertEqual(self.sim.users, {})
        self.assertEqual(self.provisioner().step(), "ready")
        self.assertEqual(self.sim.users, {"thermite": pending["password"]})
        self.assertEqual(set(provision.read_claim(self.state)), {"password", "token", "issued_at", "expires_at"})

    def test_a_setup_whose_answer_was_lost_is_resumed_by_signing_in_with_its_password(self):
        self.sim.losing["/setup"] = 1
        with self.assertRaises(Unavailable):
            self.provisioner().step()
        self.assertEqual(list(self.sim.users), ["thermite"])
        self.assertEqual(set(provision.read_claim(self.state)), {"password"})
        self.assertEqual(self.provisioner().step(), "ready")
        self.assertEqual(list(self.sim.tokens), [self.token()])
        # Someone else's server: the pending password signs in to nothing, and is counted as any other.
        self.reset()
        self.sim.create_user("someone", "a long enough password")
        provision.write_claim(self.state, {"password": "xxx"})
        self.assertEqual(self.provisioner().step(), "refused")
        self.assertEqual(provision.read_claim(self.state), {"password": "xxx", "refusals": 1})

    def test_nothing_is_claimed_while_the_server_starts_or_is_down(self):
        self.sim.status = "starting"
        with self.assertRaises(Unavailable):
            self.provisioner().step()
        self.server.stop()
        with self.assertRaises(Unavailable):
            self.provisioner().step()
        self.assertIsNone(provision.read_claim(self.state))
        self.assertEqual(self.sim.users, {})

    def test_a_claim_file_that_cannot_be_trusted_is_reported_and_never_replaced(self):
        provisioner = self.provisioner()
        provisioner.step()
        os.chmod(provision.token_file(self.state), 0o644)
        unreadable = {"problem": provision.UNREADABLE, "reason": provision.ACCESS}
        self.assertEqual(provision.read_claim(self.state), unreadable)
        self.assertEqual(provision.problem(unreadable), "music_unavailable")
        before = provision.token_file(self.state).read_bytes()
        with self.assertLogs("oh_music.provision", "INFO") as logs:
            self.assertEqual(provisioner.step(), "unreadable")
            self.assertEqual(provisioner.step(), "unreadable")
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Music Assistant: cannot trust its stored claim; give the file to the service account, "
                          "mode 0600"])
        self.assertEqual(provision.token_file(self.state).read_bytes(), before)
        # Contents thermite did not write: another repair.
        damaged = {"problem": provision.UNREADABLE, "reason": provision.CONTENTS}
        provision.token_file(self.state).unlink()
        for claim in ({"password": "x", "token": "x", "issued_at": "soon", "expires_at": 1},
                      {"token": "x", "issued_at": 1, "expires_at": 1}, {"password": 5}):
            provision.write_claim(self.state, claim)
            self.assertEqual(provision.read_claim(self.state), damaged, claim)
        atomic_private(provision.token_file(self.state), b"{not json")
        self.assertEqual(provision.read_claim(self.state), damaged)
        # One past its bound holds what thermite never wrote, whoever may read it.
        atomic_private(provision.token_file(self.state), b" " * (provision.TOKEN_BYTES + 1))
        self.assertEqual(provision.read_claim(self.state), damaged)
        atomic_private(self.state / provision.LIBRARY_FILE, b" " * (provision.LIBRARY_BYTES + 1))
        with self.assertRaises(provision.Untrusted) as oversized:
            provision.read_libraries(self.state)
        self.assertEqual(oversized.exception.reason, provision.CONTENTS)
        atomic_private(provision.token_file(self.state), b"{not json")
        with self.assertLogs("oh_music.provision", "INFO") as logs:
            self.assertEqual(self.provisioner().step(), "unreadable")
        self.assertIn("thermite did not write it; restore it, or reset Music Assistant and delete it",
                      logs.records[0].getMessage())

    def test_the_worker_thread_first_removes_what_killed_writes_left(self):
        left = [self.state / ("." + name + "." + "0a" * 12) for name in (provision.TOKEN_FILE, provision.LIBRARY_FILE)]
        kept = [self.state / name for name in (".music-token.json." + "0a" * 11, ".music-token.json.tmp",
                                               ".other.json." + "0a" * 12, "music-token.json." + "0a" * 12)]
        for path in left + kept:
            path.write_text("x")
        elsewhere = self.state / "elsewhere"
        elsewhere.write_text("x")
        linked = self.state / (".music-token.json." + "0b" * 12)
        linked.symlink_to(elsewhere)
        stop = threading.Event()
        stop.set()
        provision.run(self.state, spec(self.port), stop)
        self.assertEqual([path.exists() for path in left], [False, False])
        self.assertEqual([path.exists() for path in kept], [True] * len(kept))
        self.assertTrue(linked.is_symlink())
        # One record's sweep failing leaves the other's to run.
        for path in left:
            path.write_text("x")
        real = provision.remove_temporaries

        def failing(path):
            if path.name == provision.TOKEN_FILE:
                raise PermissionError(13, "Permission denied")
            real(path)
        with patch.object(provision, "remove_temporaries", failing), \
                self.assertLogs("oh_music.provision", "WARNING") as logs:
            provision.run(self.state, spec(self.port), stop)
        self.assertEqual([path.exists() for path in left], [True, False])
        self.assertEqual(logs.output, ["WARNING:oh_music.provision:Music Assistant: music-token.json's leftover "
                                       "temporary files cannot be removed (PermissionError)."])

    def test_the_worker_thread_waits_for_the_server_then_claims_it(self):
        self.sim.status = "starting"
        stop = threading.Event()
        thread = threading.Thread(target=provision.run, args=(self.state, spec(self.port), stop),
                                  kwargs={"poll": 0.05, "every": 60})
        with self.assertLogs("oh_music.provision", "INFO") as logs:
            thread.start()
            time.sleep(0.2)
            self.sim.status = "running"
            until = time.monotonic() + 5
            # Stopping ends a pass at its next request, so the test waits for the pass to end.
            while not any(record.getMessage().startswith("Music Assistant: ready") for record in logs.records) \
                    and time.monotonic() < until:
                time.sleep(0.02)
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(messages[0], "Music Assistant: unavailable")
        self.assertIn("Music Assistant claimed by thermite.", messages)
        self.assertTrue(messages[-1].startswith("Music Assistant: ready changed="))


class Configuration(Site):
    def test_the_providers_are_what_thermite_needs_and_a_second_pass_changes_nothing(self):
        self.sim.remote_access = True
        provisioner = self.provisioner(("mp3", "flac"))
        provisioner.step()
        self.assertEqual(self.domains(), ["airplay", "filesystem_local", "filesystem_local", "sonos", "wiim"])
        values = {entry["name"] or entry["domain"]: entry["values"] for entry in self.sim.providers.values()}
        self.assertEqual(values, {
            "sonos": {"manual_discovery_ip_addresses": ["192.0.2.21"]},
            "wiim": {"manual_discovery_ip_addresses": ["192.0.2.32", "192.0.2.33"]}, "airplay": {},
            "thermite flac": {"content_type": "music", "path": "/media/flac"},
            "thermite mp3": {"content_type": "music", "path": "/media/mp3"}})
        self.assertFalse(self.sim.remote_access)
        calls = len(self.sim.calls)
        with Client(self.port, self.token()) as client:
            self.assertEqual(provisioner.reconcile(client, client.info()), 0)
        changes = [command for command, _ in self.sim.calls[calls:] if command.startswith("config/")
                   and command not in ("config/providers", "config/providers/get_value")]
        self.assertEqual(changes, [])
        # Players are Music Assistant's: never renamed or disabled.
        self.assertFalse([command for command, _ in self.sim.calls if command.startswith("config/players")])

    def test_libraries_follow_the_host_and_every_other_instance_is_removed(self):
        self.provisioner(("flac", "mp3")).step()
        with self.sim.lock:
            # One the owner added in Music Assistant's UI, and a speaker family thermite leaves alone.
            self.sim._create("filesystem_local", {"content_type": "music", "path": "/media/flac"})
            self.sim._create("heos")
        self.provisioner(("mp3",)).step()
        libraries = [(entry["name"], entry["values"]["path"]) for entry in self.sim.providers.values()
                     if entry["domain"] == "filesystem_local"]
        self.assertEqual(libraries, [("thermite mp3", "/media/mp3")])
        self.assertNotIn("heos", self.domains())

    def test_a_library_whose_naming_fails_is_named_again_and_never_replaced(self):
        self.sim.failing["config/providers/save"] = 100
        with self.sim.lock:
            foreign = self.sim._create("filesystem_local", {"content_type": "music", "path": "/media/flac"})
        with self.assertLogs("oh_music.provision", "WARNING"):
            for _ in range(3):
                # A new worker each time: the instance is known from the record alone.
                self.provisioner().step()
        [instance] = provision.read_libraries(self.state).values()
        self.assertEqual(self.sim.providers[instance]["name"], None)
        # Nothing was removed while a library had no named instance.
        self.assertEqual({entry for entry in self.sim.providers if entry.startswith("filesystem_local")},
                         {instance, foreign})
        self.assertFalse([args for command, args in self.sim.calls if command == "config/providers/remove"
                          and args["instance_id"].startswith("filesystem_local")])
        self.sim.failing.clear()
        self.provisioner().step()
        self.assertEqual([(entry["instance_id"], entry["name"]) for entry in self.sim.providers.values()
                          if entry["domain"] == "filesystem_local"], [(instance, "thermite flac")])

    def test_a_library_record_that_cannot_be_trusted_leaves_the_libraries_as_they_are(self):
        self.provisioner().step()
        record = self.state / provision.LIBRARY_FILE
        os.chmod(record, 0o644)
        provisioner = self.provisioner(("flac", "mp3"))
        with self.assertLogs("oh_music.provision", "WARNING") as logs:
            provisioner.step()
            provisioner.reconciled = None
            provisioner.step()
        self.assertEqual([record.getMessage() for record in logs.records if "library record" in record.getMessage()],
                         ["Music Assistant: its library record music-libraries.json cannot be trusted; give it to the "
                          "service account, mode 0600. Its libraries are left as they are until then."])
        self.assertEqual(self.domains().count("filesystem_local"), 1)
        os.chmod(record, 0o600)
        provisioner.reconciled = None
        provisioner.step()
        self.assertEqual(sorted(entry["name"] for entry in self.sim.providers.values()
                                if entry["domain"] == "filesystem_local"), ["thermite flac", "thermite mp3"])
        # A record thermite did not write: another repair.
        atomic_private(record, b'{"version": 1, "libraries": ["flac"]}')
        provisioner.reconciled = None
        with self.assertLogs("oh_music.provision", "WARNING") as logs:
            provisioner.step()
        self.assertIn("thermite did not write it; delete it to have the libraries found by name", logs.output[0])

    def test_a_library_record_that_cannot_be_written_finds_its_instance_by_name(self):
        def full(*_):
            raise OSError(28, "No space left on device")
        with patch.object(provision, "write_libraries", full), self.assertLogs("oh_music.provision", "WARNING"):
            for _ in range(3):
                self.provisioner().step()
        self.assertEqual([entry["name"] for entry in self.sim.providers.values()
                          if entry["domain"] == "filesystem_local"], ["thermite flac"])

    def test_a_library_neither_named_nor_recorded_is_removed_again(self):
        def full(*_):
            raise OSError(28, "No space left on device")

        def refused(*_):
            raise Failed(500)
        with patch.object(provision, "write_libraries", full), patch.object(provision.Provisioner, "name", refused), \
                self.assertLogs("oh_music.provision", "WARNING") as logs:
            for _ in range(3):
                self.provisioner().step()
        self.assertIn("could be neither named (Failed) nor recorded (OSError); it was removed.", logs.output[0])
        self.assertEqual(self.domains().count("filesystem_local"), 0)
        self.provisioner().step()
        self.assertEqual([entry["name"] for entry in self.sim.providers.values()
                          if entry["domain"] == "filesystem_local"], ["thermite flac"])

    def test_a_library_instance_that_cannot_even_be_removed_goes_at_the_next_reconcile(self):
        def full(*_):
            raise OSError(28, "No space left on device")

        def refused(*_):
            raise Failed(500)
        self.sim.failing["config/providers/remove"] = 1
        with patch.object(provision, "write_libraries", full), patch.object(provision.Provisioner, "name", refused), \
                self.assertLogs("oh_music.provision", "WARNING") as logs:
            self.provisioner().step()
        self.assertIn("could be neither named (Failed) nor recorded (OSError), nor removed (Failed): the next "
                      "reconcile removes it", logs.output[0])
        # The reconcile's own log names the first failure, not the removal's.
        self.assertIn("Failed", logs.output[1])
        stray = [instance for instance, entry in self.sim.providers.items() if entry["domain"] == "filesystem_local"]
        self.assertEqual(len(stray), 1)
        self.provisioner().step()
        kept = {instance: entry["name"] for instance, entry in self.sim.providers.items()
                if entry["domain"] == "filesystem_local"}
        self.assertEqual(list(kept.values()), ["thermite flac"])
        self.assertNotIn(stray[0], kept)

    def test_a_part_that_fails_is_logged_and_the_others_still_applied(self):
        self.sim.remote_access = True
        self.sim.failing["config/providers/get_value"] = 1
        now = [time.time()]
        provisioner = self.provisioner(clock=lambda: now[0])
        with self.assertLogs("oh_music.provision", "WARNING") as logs:
            self.assertEqual(provisioner.step(), "ready")
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Music Assistant: could not reconcile its speaker addresses (Failed); "
                          "trying again at the next reconcile."])
        self.assertEqual(self.sim.providers["sonos"]["values"], {})
        self.assertEqual(self.domains(), ["airplay", "filesystem_local", "sonos", "wiim"])
        self.assertFalse(self.sim.remote_access)
        # Tried again when the next reconcile is due, not on every pass.
        calls = len(self.sim.calls)
        provisioner.step()
        self.assertEqual([command for command, _ in self.sim.calls[calls:]], ["auth/tokens"])
        now[0] += 15 * 60
        provisioner.step()
        self.assertEqual(self.sim.providers["sonos"]["values"], {"manual_discovery_ip_addresses": ["192.0.2.21"]})

    def test_a_provider_music_assistant_will_not_add_is_logged_in_thermites_words(self):
        self.provisioner().step()
        provisioner = self.provisioner()
        with Client(self.port, self.token()) as client, self.assertLogs("oh_music.provision", "WARNING") as logs:
            # Already there; and a sign-in thermite does not answer, whose flow is ended.
            self.assertIsNone(provisioner.add(client, "sonos"))
            self.assertIsNone(provisioner.add(client, "tidal"))
        self.assertEqual([record.getMessage() for record in logs.records],
                         ["Music Assistant could not add the sonos provider: it refused.",
                          "Music Assistant could not add the tidal provider: it asked for more than thermite answers."])
        self.assertEqual(self.sim.flows, {})

    def test_a_speakers_new_address_is_given_to_its_provider(self):
        self.provisioner().step()
        provisioner = self.provisioner()
        provisioner.spec["speakers"][MINI]["ip"] = "192.0.2.43"
        provisioner.step()
        self.assertEqual(self.sim.providers["wiim"]["values"]["manual_discovery_ip_addresses"],
                         ["192.0.2.32", "192.0.2.43"])

    def test_a_pass_reconciles_after_a_claim_a_reset_or_fifteen_minutes(self):
        now = [time.time()]
        provisioner = self.provisioner(clock=lambda: now[0])
        provisioner.step()
        calls = len(self.sim.calls)
        with self.sim.lock:
            self.sim._create("heos")
        provisioner.step()
        self.assertIn("heos", self.domains())
        self.assertEqual([command for command, _ in self.sim.calls[calls:]], ["auth/tokens"])
        now[0] += 15 * 60
        provisioner.step()
        self.assertNotIn("heos", self.domains())
        self.reset()
        provisioner.step()
        self.assertEqual(self.domains(), ["airplay", "filesystem_local", "sonos", "wiim"])


class Errors(unittest.TestCase):
    """What Music Assistant answers becomes one of three exceptions, none
    with its text."""

    def client(self, status, body=b"", kind="application/json", token="example"):  # lint: allow (test fixture)
        seen = []

        def answer(request):
            seen.append(request)
            return httpx.Response(status, content=body, headers={"Content-Type": kind})
        client = Client(1024, token, transport=httpx.MockTransport(answer))
        self.addCleanup(client.close)
        return client, seen

    def test_a_stopped_client_makes_no_request(self):
        client, seen = self.client(200, b'{"ok": true}')
        client.stop = threading.Event()
        client.call("players/all")
        client.stop.set()
        for request in (client.info, lambda: client.call("players/all"), lambda: client.image("0" * 64, 80)):
            with self.assertRaises(Stopped):
                request()
        self.assertEqual(len(seen), 1)

    def test_a_command_is_one_post_with_the_bearer_token(self):
        client, seen = self.client(200, b'{"ok": true}')
        self.assertEqual(client.call("players/all", flag=True), {"ok": True})
        [request] = seen
        self.assertEqual((request.method, request.url.path, request.headers["authorization"]),
                         ("POST", "/api", "Bearer example"))
        body = json.loads(request.content)
        self.assertEqual((body["command"], body["args"]), ("players/all", {"flag": True}))

    def test_a_command_waits_seconds_and_a_slow_one_its_own_timeout(self):
        from oh_music import client as music_client
        client, seen = self.client(200, b"[]")
        client.call("players/all")
        client.call("music/search", music_client.SLOW, search_query="x")
        self.assertEqual([request.extensions["timeout"]["read"] for request in seen],
                         [music_client.TIMEOUT, music_client.SLOW])
        self.assertEqual(json.loads(seen[1].content)["args"], {"search_query": "x"})
        self.assertLessEqual(music_client.TIMEOUT, 5)

    def test_each_refusal_is_its_exception_without_music_assistants_text(self):
        detail = b"Traceback: KeyError 'secret detail'"
        for status, expected in ((500, Failed), (400, Failed), (404, Failed), (401, Unauthorized), (403, Unauthorized),
                                 (503, NoUser)):
            client, _ = self.client(status, detail, "text/plain")
            with self.assertRaises(expected) as raised:
                client.call("players/all")
            self.assertNotIn("secret", str(raised.exception))
            if expected is Failed:
                self.assertEqual((str(raised.exception), raised.exception.code), ("Music command failed", status))
        self.assertTrue(issubclass(NoUser, Unavailable))
        client, _ = self.client(200, b"not json")
        with self.assertRaises(Failed):
            client.call("players/all")
        client, _ = self.client(200, b"[" + b"0," * (4 * 1024 * 1024) + b"0]")
        with self.assertRaises(Failed):
            client.call("players/all")
        with self.assertRaises(Unauthorized):
            self.client(200, token=None)[0].call("players/all")
        with self.assertRaises(Unavailable):
            Client(1024, "example", transport=httpx.MockTransport(
                lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")))).call("players/all")

    def test_an_image_is_a_bounded_jpeg_or_png(self):
        jpeg = stand_in.jpeg((1, 2, 3), 80)
        client, seen = self.client(200, jpeg, "image/jpeg")
        self.assertEqual(client.image("0" * 64, 80), ("image/jpeg", jpeg))
        self.assertEqual((seen[0].url.path, dict(seen[0].url.params)), ("/imageproxy/" + "0" * 64, {"size": "80", "fmt": "jpg"}))
        # Music Assistant may fetch it from TIDAL first.
        self.assertEqual(seen[0].extensions["timeout"]["read"], SLOW)
        self.assertEqual(self.client(200, stand_in.png((1, 2, 3), 80), "image/png")[0].image("0" * 64, 80)[0], "image/png")
        for status, body, kind in ((200, b"<svg/>", "image/svg+xml"), (200, b"<html>", "text/html"),
                                   (200, stand_in.png((1, 2, 3), 80), "image/jpeg"), (404, b"", "text/plain"),
                                   (200, b"\xff\xd8\xff" + b"\0" * (2 * 1024 * 1024), "image/jpeg")):
            with self.assertRaises(Failed):
                self.client(status, body, kind)[0].image("0" * 64, 80)

    def test_a_server_says_whether_it_has_a_user(self):
        self.assertTrue(self.client(401)[0].has_user())
        self.assertFalse(self.client(503)[0].has_user())
        with self.assertRaises(Unavailable):
            self.client(200, b"[]")[0].has_user()

    def test_a_login_answers_a_session_or_none_when_refused(self):
        client, seen = self.client(200, b'{"success": true, "token": "abc"}')
        self.assertEqual(client.login("thermite", "x" * 12, "thermite"), "abc")
        self.assertEqual((seen[0].url.path, json.loads(seen[0].content)["credentials"]["username"]),
                         ("/auth/login", "thermite"))
        self.assertIsNone(self.client(401, b'{"success": false}')[0].login("thermite", "x" * 12, "thermite"))
        with self.assertRaises(NoUser):
            self.client(403, b'{"success": false}')[0].login("thermite", "x" * 12, "thermite")
        # Anything else is no answer, never a refusal.
        with self.assertRaises(Unavailable):
            self.client(500)[0].login("thermite", "x" * 12, "thermite")
        with self.assertRaises(Unavailable):
            self.client(200, b'{"success": false}')[0].login("thermite", "x" * 12, "thermite")

    def test_setup_once_a_user_exists_is_none(self):
        self.assertIsNone(self.client(400, b'{"success": false}')[0].setup("thermite", "x" * 12, "thermite"))
        self.assertEqual(self.client(200, b'{"success": true, "token": "abc"}')[0].setup("thermite", "x" * 12, "t"), "abc")
        with self.assertRaises(Unavailable):
            self.client(500)[0].setup("thermite", "x" * 12, "thermite")


class Events(Site):
    """The gateway's websocket to the stand-in."""

    def setUp(self):
        super().setUp()
        self.provisioner().step()

    def check(self, body):
        asyncio.run(asyncio.wait_for(body(), 10))

    async def answer(self, socket, message_id):
        while True:
            message = await socket.receive()
            if message.get("message_id") == message_id:
                return message

    def test_an_authenticated_socket_answers_commands_and_carries_events(self):
        async def body():
            socket = await Socket.open(self.port, self.token())
            try:
                answer = await self.answer(socket, await socket.command("players/all"))
                self.assertEqual(sorted(player["provider"] for player in answer["result"]), ["sonos", "wiim", "wiim"])
                sonos = next(player["player_id"] for player in answer["result"] if player["provider"] == "sonos")
                with Client(self.port, self.token()) as client:
                    await asyncio.to_thread(client.call, "players/cmd/volume_set", player_id=sonos, volume_level=35)
                while True:
                    message = await socket.receive()
                    if message.get("event") == "player_updated" and message["object_id"] == sonos:
                        break
                self.assertEqual(message["data"]["volume_level"], 35)
                failed = await self.answer(socket, await socket.command("players/get", player_id="example"))
                self.assertIn("error_code", failed)
            finally:
                await socket.close()
        self.check(body)

    def test_a_refused_token_or_a_server_with_no_user_opens_nothing(self):
        async def body():
            with self.assertRaises(Unauthorized):
                await Socket.open(self.port, "not-a-token")
            self.reset()
            with self.assertRaises(Closed):
                await Socket.open(self.port, self.token())
            self.server.stop()
            with self.assertRaises(Unavailable):
                await Socket.open(self.port, self.token())
        self.check(body)

    def test_a_message_longer_than_its_bytes_closes_the_socket(self):
        from oh_music import events

        async def body():
            socket = await Socket.open(self.port, self.token())
            try:
                with patch.object(events, "MESSAGE_BYTES", 1500):
                    # About 970 characters, 1870 bytes in UTF-8.
                    self.sim.emit("player_updated", "example", {"name": "\u00e9" * 900})
                    with self.assertRaises(Closed):
                        while True:
                            await socket.receive()
            finally:
                await socket.close()
        self.check(body)

    def test_pings_are_answered_and_the_connection_stays_open(self):
        self.sim.heartbeat = 0.05

        async def body():
            socket = await Socket.open(self.port, self.token())
            try:
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(socket.receive(), 0.5)
                [peer] = self.sim.peers
                self.assertGreaterEqual(peer.pongs, 3)
                answer = await self.answer(socket, await socket.command("providers"))
                self.assertIn("result", answer)
            finally:
                await socket.close()
        self.check(body)

    def test_a_revoked_token_or_a_silent_server_closes_the_socket(self):
        async def body():
            socket = await Socket.open(self.port, self.token(), quiet=0.2)
            started = time.monotonic()
            with self.assertRaises(Closed):
                while True:
                    await socket.receive()
            self.assertLess(time.monotonic() - started, 2)
            socket.abort()
            socket = await Socket.open(self.port, self.token())
            [entry] = self.sim.tokens.values()
            self.sim.revoke(entry["token_id"])
            with self.assertRaises(Closed):
                while True:
                    await socket.receive()
            socket.abort()
        self.check(body)


class Tidal(Site):
    def setUp(self):
        super().setUp()
        self.provisioner().step()

    def test_the_cli_shows_each_code_and_waits_for_approval(self):
        said, sleeps = [], iter((self.sim.expire_tidal, lambda: None, self.sim.approve_tidal))
        self.assertEqual(tidal.sign_in(self.port, self.token(), out=said.append, sleep=lambda _: next(sleeps)()), 0)
        codes = [line.split("link.tidal.com/")[1].split(" ")[0] for line in said[:-1]]
        self.assertEqual(len(set(codes)), 2)
        self.assertEqual(said[-1], "TIDAL is signed in.")
        self.assertIsNotNone(self.sim.tidal_instance())
        # Signing in again reconfigures the instance.
        said.clear()
        self.assertEqual(tidal.sign_in(self.port, self.token(), out=said.append,
                                       sleep=lambda _: self.sim.approve_tidal()), 0)
        self.assertIn("config/providers/reconfigure", [command for command, _ in self.sim.calls])
        self.assertEqual(len([entry for entry in self.sim.providers.values() if entry["domain"] == "tidal"]), 1)

    def test_a_sign_in_that_ends_unapproved_fails(self):
        said = []
        self.assertEqual(tidal.sign_in(self.port, self.token(), out=said.append,
                                       sleep=lambda _: self.sim.flows.clear()), 1)
        self.assertEqual(said[-1], "TIDAL sign-in failed.")
        with self.assertRaises(AccessError):
            tidal.sign_in(self.port, "not-a-token", out=said.append)

    def test_a_step_is_kept_only_as_its_flow_and_code(self):
        step = {"flow_id": "abc", "type": "external", "url": "https://phish.example/ABCDE", "expires_at": 1.6,
                "description": "anything"}
        self.assertEqual(tidal.waiting(step), {"state": "waiting", "flow": "abc", "code": "ABCDE",
                                               "url": "https://link.tidal.com/ABCDE", "expires_at": 2})
        for broken in ({**step, "type": "form"}, {**step, "url": "https://link.tidal.com/ab cd"},
                       {**step, "flow_id": "../x"}, None):
            with self.assertRaises(Failed):
                tidal.waiting(broken)

    def test_the_cli_finds_the_port_and_token_from_the_service_config(self):
        contract = self.state / "contract.json"
        bearer = self.state / "bearer"
        for path, text in ((contract, json.dumps({"music": spec(self.port)})), (bearer, "example")):
            path.write_text(text)
            os.chmod(path, 0o600)
        config = self.state / "config.json"
        config.write_text(json.dumps({
            "version": 2, "state": str(self.state), "contract_file": str(contract), "runtime_url": "http://127.0.0.1:13602",
            "runtime_token_file": str(bearer), "allowed_hosts": ["example.test"], "bind_host": "127.0.0.1",
            "port": 13600, "tls_cert": str(self.state / "cert.pem"), "tls_key": str(self.state / "key.pem"),
            "timezone": "UTC", "guest_http": None}))
        os.chmod(config, 0o600)
        self.assertEqual(tidal.site(config), (self.port, self.token()))
        os.chmod(provision.token_file(self.state), 0o640)
        with self.assertRaisesRegex(AccessError, "cannot be trusted"):
            tidal.site(config)
        provision.token_file(self.state).unlink()
        with self.assertRaisesRegex(AccessError, "not claimed yet"):
            tidal.site(config)
        provision.write_claim(self.state, {"problem": provision.UNCLAIMED})
        with self.assertRaisesRegex(AccessError, "another user set it up"):
            tidal.site(config)
        provision.write_claim(self.state, {"password": "x", "refusals": provision.REFUSALS})
        with self.assertRaisesRegex(AccessError, "refused thermite's password for a day; reset its state, or restore"):
            tidal.site(config)
        contract.write_text("{}")
        with self.assertRaisesRegex(AccessError, "no music"):
            tidal.site(config)


class StandIn(Site):
    """The stand-in groups and plays as Music Assistant does."""

    def setUp(self):
        super().setUp()
        self.provisioner().step()
        self.client = Client(self.port, self.token())
        self.addCleanup(self.client.close)
        labels = {speaker["label"]: speaker["id"] for speaker in SPEAKERS}
        self.ids = {labels[player["name"]]: player["player_id"] for player in self.client.call("players/all")}

    def player(self, speaker):
        return self.client.call("players/get", player_id=self.ids[speaker])

    def test_a_refusal_is_spent_only_on_a_request_with_a_token(self):
        self.sim.refusing = 1
        with Client(self.port) as anonymous:
            self.assertTrue(anonymous.has_user())
        with self.assertRaises(Unauthorized):
            self.client.call("players/all")
        self.assertTrue(self.client.call("players/all"))

    def test_a_wiim_without_airplay_is_dropped_from_a_sonos_group_without_an_error(self):
        self.client.call("players/cmd/set_members", target_player=self.ids[SONOS],
                         player_ids_to_add=[self.ids[AMP_PRO], self.ids[MINI]])
        self.assertEqual(self.player(SONOS)["group_members"], [self.ids[SONOS], self.ids[MINI]])
        self.assertIsNone(self.player(AMP_PRO)["synced_to"])
        # Only the Mini's AirPlay side is a (hidden) protocol player.
        everything = self.client.call("players/all", return_protocol_players=True)
        self.assertEqual([player["provider"] for player in everything if player["type"] == "protocol"], ["airplay"])

    def test_playing_to_a_member_takes_it_out_and_a_group_reports_its_loudest(self):
        self.client.call("players/cmd/set_members", target_player=self.ids[AMP_PRO], player_ids_to_add=[self.ids[MINI]])
        self.client.call("players/cmd/volume_set", player_id=self.ids[MINI], volume_level=60)
        self.assertEqual(self.player(AMP_PRO)["group_volume"], 60)
        self.client.call("player_queues/play_media", queue_id=self.ids[AMP_PRO], media="library://album/100")
        self.assertEqual(self.player(MINI)["current_media"]["title"], "Opening")
        self.client.call("player_queues/play_media", queue_id=self.ids[MINI], media="library://track/201")
        self.assertEqual((self.player(MINI)["synced_to"], self.player(AMP_PRO)["group_members"]), (None, []))
        self.assertEqual((self.player(MINI)["current_media"]["title"], self.player(AMP_PRO)["current_media"]["title"]),
                         ("Example Net", "Opening"))

    def test_a_leader_ungrouped_dissolves_its_group_and_an_unknown_command_is_refused(self):
        self.client.call("players/cmd/set_members", target_player=self.ids[AMP_PRO], player_ids_to_add=[self.ids[MINI]])
        self.client.call("players/cmd/ungroup", player_id=self.ids[AMP_PRO])
        self.assertEqual((self.player(AMP_PRO)["group_members"], self.player(MINI)["synced_to"]), ([], None))
        with self.assertRaises(Failed) as raised:
            self.client.call("players/cmd/explode", player_id=self.ids[MINI])
        self.assertEqual(raised.exception.code, 400)
        with self.assertRaises(Failed) as raised:
            self.client.call("player_queues/play_media", queue_id=self.ids[MINI], media="library://track/999")
        self.assertEqual(raised.exception.code, 500)

    def test_its_images_are_whole_jpegs_and_pngs_of_the_size_asked(self):
        image = stand_in.jpeg((10, 20, 30), 160)
        self.assertEqual((image[:2], image[-2:]), (b"\xff\xd8", b"\xff\xd9"))
        frame = image.index(b"\xff\xc0")
        self.assertEqual((int.from_bytes(image[frame + 5:frame + 7]), int.from_bytes(image[frame + 7:frame + 9])),
                         (160, 160))
        image = stand_in.png((10, 20, 30), 80)
        self.assertEqual((image[:8], int.from_bytes(image[16:20]), int.from_bytes(image[20:24])),
                         (b"\x89PNG\r\n\x1a\n", 80, 80))


if __name__ == "__main__":
    unittest.main()
