"""Finite command batches; one lazy executor and admission budget per service."""

import atexit
from concurrent.futures import CancelledError, ThreadPoolExecutor
from queue import SimpleQueue
import threading


class Busy(Exception):
    """The service's finite command admission budget is already occupied."""


class CommandBatches:
    def __init__(self, *, workers=4, capacity=8, window=4):
        if not 1 <= workers <= capacity or not 1 <= window <= 4:
            raise ValueError("Invalid command batch bounds")
        self.workers, self.window = workers, window
        self._slots = threading.BoundedSemaphore(capacity)
        self._guard = threading.Lock()
        self._shutdown_guard = threading.Lock()
        self._executor = None
        self._closing = False

    def submit(self, action, item):
        if not self._slots.acquire(blocking=False):
            return None
        try:
            with self._guard:
                if self._closing:
                    self._slots.release()
                    return None
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="thermite-command")
                future = self._executor.submit(action, item)
            # Also release admission when a queued future is cancelled before
            # its action starts. Releasing only inside the job leaks capacity.
            future.add_done_callback(lambda _: self._slots.release())
            return future
        except BaseException:
            self._slots.release()
            raise

    def run(self, items, action, *, fatal):
        items = tuple(items)
        results, pending = [None] * len(items), {}
        # At most window completions can await consumption. Done callbacks
        # also wake on cancel_futures shutdown without polling cancelled jobs.
        completed = SimpleQueue()
        abort, failure_guard = threading.Event(), threading.Lock()
        failures = []

        def execute(item):
            # A failing worker fences the next queued job before the request
            # thread observes its Future, not merely after it starts draining.
            if abort.is_set():
                raise CancelledError()
            try:
                return action(item)
            except BaseException as error:
                if fatal(error):
                    with failure_guard:
                        if not failures:
                            failures.append(error)
                    abort.set()
                raise

        cursor = 0
        try:
            while cursor < len(items) or pending:
                while cursor < len(items) and len(pending) < self.window and not abort.is_set():
                    index, cursor = cursor, cursor + 1
                    future = self.submit(execute, items[index])
                    if future is None:
                        results[index] = Busy()
                    else:
                        pending[future] = index
                        future.add_done_callback(completed.put)
                if abort.is_set():
                    break
                if not pending:
                    continue
                future = completed.get()
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except CancelledError:
                    results[index] = Busy()
                except BaseException as error:
                    results[index] = error
            if failures:
                raise failures[0]
            return list(zip(items, results))
        finally:
            # A response, including an authorization error, must never leave
            # admitted effects running after the request has returned.
            abort.set()
            for future in pending:
                future.cancel()
            for future in pending:
                try:
                    future.result()
                except BaseException:
                    pass

    def shutdown(self):
        with self._shutdown_guard:
            with self._guard:
                self._closing = True
                executor, self._executor = self._executor, None
            try:
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
            finally:
                with self._guard:
                    self._closing = False


commands = CommandBatches()
atexit.register(commands.shutdown)
