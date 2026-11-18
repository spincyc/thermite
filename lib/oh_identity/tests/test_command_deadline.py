"""A command waits a bounded time for its locks: the gateway's own command
lock over real file locks in a temporary directory, held by another command
across its runtime call and by another process, as the policy worker's are."""

import inspect
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

from django.db import connections
from django.test import Client, SimpleTestCase, TransactionTestCase

from oh_access import service
from oh_identity import trail
from oh_identity.models import SecurityState
from oh_identity.runtime import install_adapter
from oh_identity.services import Problem
from oh_identity.tests.test_core import FakeRuntime, adapter
from oh_identity.tests.test_trail import Requests, lines
from oh_identity.transport import LoopbackTransport
from oh_room_policy.worker import FileLocks

AREA = Path(__file__).resolve().parents[3]
# Seconds a test's command waits for its locks.
DEADLINE = 0.3
# Holds the keys it is given until its stdin closes.
HOLDER = """
import sys
from oh_room_policy.worker import FileLocks
with FileLocks(sys.argv[1]).hold(sys.argv[2:]):
    print("held", flush=True)
    sys.stdin.read()
"""


class SlowRuntime(FakeRuntime):
    """The fake runtime, whose first command does not return until told."""

    def __init__(self):
        super().__init__()
        self.reached, self.answer = threading.Event(), threading.Event()

    def command(self, item, value, source=None):
        super().command(item, value, source)
        if len(self.commands) == 1:
            self.reached.set()
            self.answer.wait(10)


class CommandDeadline(Requests, TransactionTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "locks"
        # Authentication off: every principal is a Guest, who may command.
        SecurityState.objects.update_or_create(pk=1, defaults={"enabled": False})
        self.adapter = adapter()
        self.adapter.transport = SlowRuntime()
        self.adapter.command_lock = service.command_lock(FileLocks(self.directory), DEADLINE)
        install_adapter(self.adapter)
        self.client = Client(enforce_csrf_checks=True)
        self.request("get", "/api/v1/session")
        warnings = patch.object(trail, "warnings", trail.Limited())
        warnings.start()
        self.addCleanup(warnings.stop)

    def command(self, room="den", action="volume", value=56.5, client=None):
        return self.request("post", f"/api/v1/rooms/{room}/commands", {"action": action, "value": value}, client=client)

    def test_a_command_queued_behind_a_slow_runtime_call_is_refused_busy_and_never_sent(self):
        answers = []

        def first():
            try:
                client = Client(enforce_csrf_checks=True)
                self.request("get", "/api/v1/session", client=client)
                answers.append(self.command(client=client))
            finally:
                connections.close_all()

        holder = threading.Thread(target=first)
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(self.adapter.transport.answer.set)
        self.assertTrue(self.adapter.transport.reached.wait(5))
        started = time.monotonic()
        with self.assertLogs("oh_identity.trail", "INFO") as logs:
            refused = self.command(value=40)
        waited = time.monotonic() - started
        self.assertEqual((refused.status_code, refused.json()), (503, {"error": "runtime_busy"}))
        self.assertGreaterEqual(waited, DEADLINE)
        self.assertLess(waited, 5)
        [line] = lines(logs, "cmd")
        self.assertEqual((line["room"], line["value"], line["outcome"]), ("den", "40", "runtime_busy"))
        self.assertGreaterEqual(int(line["ms"]), DEADLINE * 1000)
        # The refused command is gone: the slow one's answer does not send it.
        self.adapter.transport.answer.set()
        holder.join(10)
        self.assertEqual([answer.status_code for answer in answers], [202])
        self.assertEqual(self.adapter.transport.commands, [("amp_volume", "56.5")])
        self.assertEqual(self.command(value=41).status_code, 202)
        self.assertEqual(self.adapter.transport.commands, [("amp_volume", "56.5"), ("amp_volume", "41")])

    def test_a_room_another_process_holds_is_busy_while_other_rooms_answer(self):
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.directory), "room:den"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True,
                                  env={**os.environ, "PYTHONPATH": str(AREA / "lib")})
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline(), "held\n")
        self.adapter.transport.answer.set()
        refused = self.command()
        self.assertEqual((refused.status_code, refused.json()), (503, {"error": "runtime_busy"}))
        self.assertEqual(self.command("desk", "source", "off").status_code, 202)
        # All off skips the held room and still turns the other off.
        answer = self.request("post", "/api/v1/all-off", {})
        self.assertEqual((answer.status_code, answer.json()),
                         (202, {"accepted": ["desk"], "skipped": [{"room": "den", "reason": "runtime_busy"}]}))
        self.assertEqual(self.adapter.transport.commands, [("loc_desk_source", "off")] * 2)

    def test_a_starting_service_waits_for_every_room_past_the_command_deadline(self):
        entered, leave = threading.Event(), threading.Event()

        def command():
            with self.adapter.command_lock(("desk",), ()):
                entered.set()
                leave.wait(10)

        holder = threading.Thread(target=command)
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(leave.set)
        self.assertTrue(entered.wait(5))
        with self.assertRaises(Problem) as busy:
            with self.adapter.command_lock(("den", "desk"), ()):
                self.fail("held a room another command holds")
        self.assertEqual((busy.exception.code, busy.exception.status), ("runtime_busy", 503))
        started = time.monotonic()
        threading.Timer(3 * DEADLINE, leave.set).start()
        self.assertEqual(service.rebase_timezone(self.adapter, "UTC"), 0)
        self.assertGreaterEqual(time.monotonic() - started, 2 * DEADLINE)


class ClientDeadline(SimpleTestCase):
    def test_the_wait_and_the_runtime_call_end_before_a_client_gives_the_command_up(self):
        access = (AREA / "web/remote/access.js").read_text()
        client = int(re.search(r"const COMMAND_DEADLINE_MS = (\d+);", access)[1]) / 1000
        runtime = inspect.signature(LoopbackTransport).parameters["timeout"].default
        self.assertEqual(inspect.signature(service.command_lock).parameters["deadline"].default,
                         service.COMMAND_LOCK_SECONDS)
        self.assertLess(service.COMMAND_LOCK_SECONDS + runtime, client)
