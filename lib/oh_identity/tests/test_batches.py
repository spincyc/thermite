"""Bounded All Off concurrency with actual committed credentials and room locks."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import threading
from unittest.mock import patch

from django.db import connections, transaction
from django.test import Client, SimpleTestCase, TransactionTestCase

from oh_identity.batches import Busy, CommandBatches, commands
from oh_identity.models import PersonalToken, RoomPolicy
from oh_identity.runtime import RuntimeAdapter, RuntimeContract, install_adapter
from oh_identity.services import bootstrap, issue_token
from oh_identity.tests.test_core import FakeRuntime, PASSPHRASE, Policy
from oh_room_policy import ResourceLocks


class FiniteBatches(SimpleTestCase):
    def batch(self, **kwargs):
        batch = CommandBatches(**kwargs)
        self.addCleanup(batch.shutdown)
        return batch

    def test_global_admission_and_thread_count_are_bounded_and_reusable(self):
        batch = self.batch(workers=2, capacity=3)
        release, started = threading.Event(), threading.Event()
        lock = threading.Lock()
        active, peak = 0, 0
        def action(item):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    started.set()
            try:
                if not release.wait(3):
                    raise AssertionError("batch fixture was not released")
                return item
            finally:
                with lock:
                    active -= 1
        futures = []
        try:
            futures = [batch.submit(action, item) for item in range(3)]
            self.assertTrue(started.wait(2))
            self.assertIsNone(batch.submit(action, 3))
        finally:
            release.set()
        self.assertEqual([future.result(timeout=2) for future in futures], [0, 1, 2])
        self.assertEqual(peak, 2)
        again = batch.submit(lambda item: item, "reused")
        self.assertIsNotNone(again)
        self.assertEqual(again.result(timeout=2), "reused")

    def test_request_window_limits_admission_and_preserves_input_order(self):
        batch = self.batch(workers=4, capacity=8, window=2)
        entered, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        count = 0
        def action(item):
            nonlocal count
            with lock:
                count += 1
                if count == 2:
                    entered.set()
            if not release.wait(3):
                raise AssertionError("batch fixture was not released")
            return item * 2
        with patch.object(batch, "submit", wraps=batch.submit) as submit, ThreadPoolExecutor(max_workers=1) as requester:
            result = requester.submit(batch.run, range(9), action, fatal=lambda error: True)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(submit.call_count, 2)
            finally:
                release.set()
            self.assertEqual(result.result(timeout=2), [(item, item * 2) for item in range(9)])

    def test_fatal_error_fences_queued_jobs_and_drains_running_effects(self):
        batch = self.batch(workers=2, capacity=4)
        both_started, fail, failed, release, finished = (threading.Event() for _ in range(5))
        lock, called = threading.Lock(), []
        class Revoked(Exception):
            pass
        def action(item):
            with lock:
                called.append(item)
                if len(called) == 2:
                    both_started.set()
            if item == 0:
                if not fail.wait(3):
                    raise AssertionError("failure fixture was not released")
                failed.set()
                raise Revoked()
            if not release.wait(3):
                raise AssertionError("effect fixture was not released")
            return item
        def request():
            try:
                return batch.run(range(9), action, fatal=lambda error: isinstance(error, Revoked))
            finally:
                finished.set()
        with ThreadPoolExecutor(max_workers=1) as requester:
            result = requester.submit(request)
            try:
                self.assertTrue(both_started.wait(2))
                fail.set()
                self.assertTrue(failed.wait(2))
                self.assertFalse(finished.wait(.05))
            finally:
                release.set()
            with self.assertRaises(Revoked):
                result.result(timeout=2)
        self.assertCountEqual(called, [0, 1])
        # Cancelled queued jobs release their global admission permits too.
        futures = [batch.submit(lambda item: item, item) for item in range(4)]
        self.assertTrue(all(future is not None for future in futures))
        self.assertEqual([future.result(timeout=2) for future in futures], list(range(4)))

    def test_shutdown_cancels_queued_jobs_drains_running_jobs_and_releases_capacity(self):
        batch = self.batch(workers=1, capacity=2)
        entered, release, queued_done = (threading.Event() for _ in range(3))
        def action(item):
            entered.set()
            if not release.wait(3):
                raise AssertionError("shutdown fixture was not released")
            return item
        running = batch.submit(action, 0)
        self.assertTrue(entered.wait(2))
        queued = batch.submit(action, 1)
        queued.add_done_callback(lambda _: queued_done.set())
        with ThreadPoolExecutor(max_workers=1) as supervisor:
            closed = supervisor.submit(batch.shutdown)
            try:
                self.assertTrue(queued_done.wait(2))
                self.assertTrue(queued.cancelled())
                self.assertFalse(closed.done())
                self.assertIsNone(batch.submit(action, 2))
            finally:
                release.set()
            closed.result(timeout=2)
        self.assertEqual(running.result(timeout=2), 0)
        self.assertEqual(batch.submit(lambda item: item, "reopened").result(timeout=2), "reopened")

    def test_cancelled_admitted_command_is_busy_and_never_reported_accepted(self):
        batch = self.batch(workers=1, capacity=2, window=2)
        entered, admitted, release, cancelled = (threading.Event() for _ in range(4))
        futures, called = [], []
        original = batch.submit
        def submit(action, item):
            future = original(action, item)
            futures.append(future)
            if len(futures) == 2:
                future.add_done_callback(lambda _: cancelled.set())
                admitted.set()
            return future
        def action(item):
            called.append(item)
            entered.set()
            if not release.wait(3):
                raise AssertionError("cancelled command fixture was not released")
            return item
        with patch.object(batch, "submit", side_effect=submit), ThreadPoolExecutor(max_workers=2) as supervisor:
            result = supervisor.submit(batch.run, range(2), action, fatal=lambda error: True)
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(admitted.wait(2))
                closing = supervisor.submit(batch.shutdown)
                self.assertTrue(cancelled.wait(2))
            finally:
                release.set()
            value = result.result(timeout=2)
            closing.result(timeout=2)
        self.assertEqual(value[0], (0, 0))
        self.assertIsInstance(value[1][1], Busy)
        self.assertEqual(called, [0])


class AllOff(TransactionTestCase):
    def setUp(self):
        commands.shutdown()
        self.addCleanup(commands.shutdown)
        self.owner = bootstrap("owner", PASSPHRASE)
        self.token, self.raw = issue_token(self.owner, "batch fixture")
        self.client = Client(enforce_csrf_checks=True)

    def adapter(self, count=4, *, transport=None, shared=False, command_lock=None):
        rooms = {"room" + str(index): {"source_item": "loc_room" + str(index) + "_source", "sources": ["player"],
                  "actions": {"source": {"resources": ["device:shared"]}} if shared else {}}
                 for index in range(count)}
        adapter = RuntimeAdapter(RuntimeContract(rooms), transport or FakeRuntime(), Policy(), command_lock=command_lock)
        install_adapter(adapter)
        return adapter

    def call(self):
        return self.client.post("/api/v1/all-off", "{}", content_type="application/json", secure=True,
                                HTTP_AUTHORIZATION="Bearer " + self.raw)

    def test_skips_locked_room_without_creating_override(self):
        adapter = self.adapter(2)
        policy = RoomPolicy.objects.create(room_id="room1", config={"locked": True})
        response = self.call()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"accepted": ["room0"], "skipped": [{"room": "room1", "reason": "room_locked"}]})
        self.assertEqual(adapter.transport.commands, [("loc_room0_source", "off")])
        policy.refresh_from_db()
        self.assertEqual(policy.config, {"locked": True})

    def test_independent_rooms_dispatch_concurrently_with_ordered_results_and_db_cleanup(self):
        barrier = threading.Barrier(4)
        outer = self
        class Transport(FakeRuntime):
            def command(self, item, value, source=None):
                outer.assertFalse(connections["default"].in_atomic_block)
                barrier.wait(timeout=3)
                super().command(item, value, source)
        adapter = self.adapter(4, transport=Transport())
        closed = []
        original = connections.close_all
        def close():
            original()
            self.assertIsNone(connections["default"].connection)
            closed.append(threading.get_ident())
        with patch("oh_identity.views.connections.close_all", side_effect=close):
            response = self.call()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"accepted": ["room0", "room1", "room2", "room3"], "skipped": []})
        self.assertEqual(len(set(closed)), 4)
        self.assertEqual(len(adapter.transport.commands), 4)

    def test_exhausted_global_capacity_returns_finite_per_room_busy_results(self):
        release = threading.Event()
        futures = [commands.submit(lambda _: release.wait(3), item) for item in range(8)]
        self.assertTrue(all(future is not None for future in futures))
        self.adapter(3)
        try:
            response = self.call()
        finally:
            release.set()
            for future in futures:
                future.result(timeout=2)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"accepted": [], "skipped": [
            {"room": "room" + str(index), "reason": "runtime_busy"} for index in range(3)]})

    def test_revoked_credential_after_first_command_cannot_dispatch_queued_room(self):
        token = self.token
        class Transport(FakeRuntime):
            def command(self, item, value, source=None):
                super().command(item, value, source)
                PersonalToken.objects.filter(pk=token.pk).update(revoked=True)
        adapter = self.adapter(3, transport=Transport())
        batch = CommandBatches(workers=1, capacity=4)
        self.addCleanup(batch.shutdown)
        with patch("oh_identity.batches.commands", batch):
            response = self.call()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(adapter.transport.commands, [("loc_room0_source", "off")])

    def test_shared_hardware_remains_serialized_by_dispatch_locks(self):
        locks = ResourceLocks()
        both_attempted = threading.Event()
        guard, attempted, active, peak = threading.Lock(), 0, 0, 0
        @contextmanager
        def command_lock(rooms, resources):
            nonlocal attempted
            with guard:
                attempted += 1
                if attempted == 2:
                    both_attempted.set()
            with locks.hold([*("room:" + room for room in rooms), *resources]):
                yield
        class Transport(FakeRuntime):
            def command(self, item, value, source=None):
                nonlocal active, peak
                with guard:
                    active += 1
                    peak = max(peak, active)
                try:
                    if not both_attempted.wait(3):
                        raise AssertionError("independent room did not attempt its lock")
                    super().command(item, value, source)
                finally:
                    with guard:
                        active -= 1
        adapter = self.adapter(2, transport=Transport(), shared=True, command_lock=command_lock)
        response = self.call()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["accepted"], ["room0", "room1"])
        self.assertEqual(len(adapter.transport.commands), 2)
        self.assertEqual(peak, 1)

    def test_request_transaction_is_refused_before_any_transport_effect(self):
        adapter = self.adapter(2)
        with transaction.atomic():
            response = self.call()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.content), {"error": "runtime_busy"})
        self.assertEqual(adapter.transport.commands, [])
