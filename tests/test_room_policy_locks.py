"""The service-state file locks the gateway and the policy worker share, with
real holders in a temporary directory: another process, as the other service
is, and another thread, as the gateway's other commands are.

Pure: no Django, network or device.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

AREA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AREA / "lib"))

from oh_room_policy.worker import Busy, FileLocks, ResourceLocks  # noqa: E402

# Holds the keys it is given until its stdin closes.
HOLDER = """
import sys
from oh_room_policy.worker import FileLocks
with FileLocks(sys.argv[1]).hold(sys.argv[2:]):
    print("held", flush=True)
    sys.stdin.read()
"""


class Deadline(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "locks"
        self.locks = FileLocks(self.directory)

    def process(self, *keys):
        """Another process holding keys, as the other service would."""
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(self.directory), *keys], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True,
                                  env={**os.environ, "PYTHONPATH": str(AREA / "lib")})
        self.addCleanup(self.release, holder)
        self.assertEqual(holder.stdout.readline(), "held\n")
        return holder

    def release(self, holder):
        if holder.poll() is None:
            holder.stdin.close()
            holder.wait(10)
        holder.stdout.close()

    def free(self, *keys):
        try:
            with self.locks.hold(keys, blocking=False):
                return True
        except BlockingIOError:
            return False

    def test_a_lock_another_process_keeps_past_the_deadline_is_busy_and_nothing_stays_held(self):
        self.process("room:example_west")
        started = time.monotonic()
        with self.assertRaises(Busy):
            # device:... sorts first, so it is taken, then given back.
            with self.locks.hold(["room:example_west", "device:example_matrix"], deadline=0.3):
                self.fail("held a lock another process holds")
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.3)
        self.assertLess(waited, 5)
        self.assertTrue(self.free("device:example_matrix"))
        self.assertFalse(self.free("room:example_west"))

    def test_a_lock_released_within_the_deadline_is_taken(self):
        holder = self.process("room:example_west")
        threading.Timer(0.2, self.release, [holder]).start()
        started = time.monotonic()
        with self.locks.hold(["room:example_west"], deadline=30):
            waited = time.monotonic() - started
            self.assertFalse(self.free("room:example_west"))
        self.assertGreaterEqual(waited, 0.2)
        self.assertTrue(self.free("room:example_west"))

    def test_two_waiters_behind_one_holder_each_keep_their_own_deadline(self):
        # A command holds a room across its device call; one queued behind it
        # gives up at its deadline while a more patient one gets its turn.
        entered, leave = threading.Event(), threading.Event()
        outcomes = {}

        def command():
            with self.locks.hold(["room:example_west"]):
                entered.set()
                leave.wait(10)

        def queued(name, deadline):
            try:
                with self.locks.hold(["room:example_west"], deadline=deadline):
                    outcomes[name] = "held"
            except Busy:
                outcomes[name] = "busy"

        holder = threading.Thread(target=command)
        holder.start()
        self.addCleanup(holder.join, 10)
        self.addCleanup(leave.set)
        self.assertTrue(entered.wait(5))
        waiters = [threading.Thread(target=queued, args=("hasty", 0.2)), threading.Thread(target=queued, args=("patient", 30))]
        for waiter in waiters:
            waiter.start()
        waiters[0].join(10)
        self.assertEqual(outcomes, {"hasty": "busy"})
        leave.set()
        waiters[1].join(10)
        self.assertEqual(outcomes, {"hasty": "busy", "patient": "held"})

    def test_one_processes_waiters_take_their_turn_in_the_order_they_came(self):
        # Commands queued behind a slow one must not overtake each other: the
        # last volume asked for is the one that stays.
        entered, leave = threading.Event(), threading.Event()
        order = []

        def command():
            with self.locks.hold(["room:example_west"]):
                entered.set()
                leave.wait(10)

        def queued(number):
            with self.locks.hold(["room:example_west", "device:example_matrix"], deadline=30):
                order.append(number)

        threads = [threading.Thread(target=command)]
        threads[0].start()
        self.addCleanup(leave.set)
        self.assertTrue(entered.wait(5))
        for number in range(5):
            threads.append(threading.Thread(target=queued, args=(number,)))
            threads[-1].start()
            time.sleep(0.1)  # Queued before the next one comes.
        leave.set()
        for thread in threads:
            thread.join(10)
        self.assertEqual(order, [0, 1, 2, 3, 4])

    def test_without_a_deadline_the_wait_has_no_limit_and_blocking_false_fails_at_once(self):
        holder = self.process("room-policy-worker")
        started = time.monotonic()
        with self.assertRaises(BlockingIOError):
            with self.locks.hold(["room-policy-worker"], blocking=False):
                self.fail("held the other process's lease")
        self.assertLess(time.monotonic() - started, 1)
        held = threading.Event()

        def wait():
            with self.locks.hold(["room-policy-worker"]):
                held.set()

        waiter = threading.Thread(target=wait)
        waiter.start()
        self.addCleanup(waiter.join, 10)
        self.assertFalse(held.wait(0.5))
        self.release(holder)
        self.assertTrue(held.wait(5))

    def test_resource_locks_match_non_reentrant_file_lock_semantics(self):
        locks = ResourceLocks()
        with locks.hold(["room:example_west"]):
            with self.assertRaises(BlockingIOError):
                with locks.hold(["room:example_west"], blocking=False):
                    self.fail("ResourceLocks must be non-reentrant")
            with self.assertRaises(Busy):
                with locks.hold(["room:example_west", "device:example_matrix"], deadline=0.05):
                    self.fail("ResourceLocks must time out with Busy")
            # Earlier sorted key released cleanly after Busy on later key.
            with locks.hold(["device:example_matrix"], blocking=False):
                pass
        with locks.hold(["room:example_west"], blocking=False):
            pass


if __name__ == "__main__":
    unittest.main()
