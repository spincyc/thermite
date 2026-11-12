"""Run the application's tests in parallel worker processes.

    python -m tests            one worker per CPU (what `make check` runs)
    python -m tests -j 1       serially in this process, exactly as
                               `python -m unittest discover -s tests -t . -q`

Discovery runs once, here. Each worker is a fork of this process that runs
the tests it is handed one at a time, inside a single unittest suite run, so
unittest itself sets up and tears down class and module fixtures. A worker
keeps to one class while that class has tests left, so setUpClass runs once
per class per worker; an idle worker joins a class another worker is still
running only when no other class is left. Classes, and the tests in each,
start longest first by the durations the previous run in this checkout
recorded in .scratch/test-durations.json, else in discovery order.

Workers send back what each test did as text, and this process reports it
as `unittest -q` does: failures with their tracebacks, in discovery order,
then "Ran N tests" and OK or FAILED. The exit status is 1 when anything
failed. What tests print themselves appears as they print it.
"""

from __future__ import annotations

import argparse
import collections
import json
import multiprocessing
from multiprocessing import connection
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

AREA = Path(__file__).resolve().parents[1]
DURATIONS = AREA / ".scratch/test-durations.json"
#: The outcome lists of a unittest result that a report carries.
KINDS = ("errors", "failures", "skipped", "expectedFailures", "unexpectedSuccesses")


def cpus() -> int:
    counted = getattr(os, "process_cpu_count", None)  # Python 3.13+
    if counted is not None:
        return counted() or 1
    try:
        return len(os.sched_getaffinity(0)) or 1
    except AttributeError:  # pragma: no cover - not Linux
        return os.cpu_count() or 1


def leaves(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from leaves(test)
        else:
            yield test


def label(test) -> tuple[str, str | None]:
    """What a text result prints for a test: its name and docstring line."""
    return str(test), test.shortDescription()


class Collect(unittest.TestResult):
    """A worker's result; taken() is, as text, what it gained since last asked."""

    def __init__(self):
        super().__init__()
        self.reported = dict.fromkeys(("testsRun", *KINDS), 0)

    def taken(self) -> dict:
        report = {"testsRun": self.testsRun - self.reported["testsRun"]}
        self.reported["testsRun"] = self.testsRun
        for kind in KINDS:
            entries = getattr(self, kind)[self.reported[kind]:]
            self.reported[kind] += len(entries)
            report[kind] = [label(entry) if kind == "unexpectedSuccesses" else (label(entry[0]), entry[1])
                            for entry in entries]
        return report


class Feed(unittest.TestSuite):
    """The tests the parent hands one worker, as they arrive, as one suite."""

    _cleanup = False  # there is no list of tests to drop them from

    def __init__(self, pipe, tests, result):
        super().__init__()
        self.pipe, self.tests, self.result = pipe, tests, result

    def __iter__(self):
        while (index := self.pipe.recv()) is not None:
            yield self.tests[index]
            self.pipe.send(self.result.taken())


def work(pipe, tests) -> None:
    result = Collect()
    try:
        Feed(pipe, tests, result).run(result)
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    pipe.send(result.taken())  # the last class's and module's teardown


class Ran:
    """Stands in, in the printed result, for a test that ran in a worker."""

    def __init__(self, name: str, doc: str | None):
        self.name, self.doc = name, doc

    def __str__(self) -> str:
        return self.name

    def shortDescription(self) -> str | None:
        return self.doc


class Worker:
    def __init__(self, context, tests: list):
        self.pipe, theirs = context.Pipe()
        self.process = context.Process(target=work, args=(theirs, tests))
        self.process.start()
        theirs.close()
        self.group = None  # the class whose fixtures it holds
        self.index = None  # the test it is running
        self.last = -1
        self.started = 0.0


class Parallel:
    """The suite as `jobs` forked workers run it, reported into one result."""

    def __init__(self, suite: unittest.TestSuite, jobs: int):
        self.tests = list(leaves(suite))
        self.jobs = jobs
        self.durations = recorded()
        grouped: dict[type, list[int]] = {}
        for index, test in enumerate(self.tests):
            grouped.setdefault(type(test), []).append(index)
        self.groups = {group: collections.deque(sorted(indexes, key=lambda index: -self.estimate(index)))
                       for group, indexes in grouped.items()}
        self.order = sorted(self.groups, key=lambda group: -sum(map(self.estimate, self.groups[group])))

    def estimate(self, index: int) -> float:
        return self.durations.get(self.tests[index].id(), 0.0)

    def next_for(self, worker: Worker, workers: list[Worker]) -> int | None:
        if worker.group is not None and self.groups[worker.group]:
            return self.groups[worker.group].popleft()
        held = {other.group for other in workers if other is not worker}
        waiting = [group for group in self.order if self.groups[group]]
        for group in waiting:
            if group not in held:
                worker.group = group
                return self.groups[group].popleft()
        if not waiting:
            return None
        # Every class left is running elsewhere: help the one with most to do.
        worker.group = max(waiting, key=lambda group: (sum(map(self.estimate, self.groups[group])),
                                                       len(self.groups[group])))
        return self.groups[worker.group].popleft()

    def __call__(self, result: unittest.TestResult) -> None:
        context = multiprocessing.get_context("fork")
        reports: dict[tuple[int, int], dict] = {}
        measured: dict[str, float] = {}
        workers: list[Worker] = []

        def hand_out(worker: Worker) -> None:
            worker.index = self.next_for(worker, workers)
            worker.started = time.perf_counter()
            worker.pipe.send(worker.index)

        def retire(worker: Worker) -> None:
            workers.remove(worker)
            worker.pipe.close()
            worker.process.join()

        try:
            for _ in range(min(self.jobs, len(self.tests))):
                workers.append(Worker(context, self.tests))
            for worker in list(workers):
                hand_out(worker)
            while workers:
                for pipe in connection.wait([worker.pipe for worker in workers]):
                    worker = next(worker for worker in workers if worker.pipe is pipe)
                    try:
                        report = pipe.recv()
                    except EOFError:
                        retire(worker)
                        if worker.index is not None:
                            reports[worker.index, 0] = lost(self.tests[worker.index], worker.process.exitcode)
                        if any(self.groups.values()):
                            workers.append(Worker(context, self.tests))
                            hand_out(workers[-1])
                        continue
                    if worker.index is None:
                        reports[worker.last, 1] = report
                        retire(worker)
                        continue
                    measured[self.tests[worker.index].id()] = time.perf_counter() - worker.started
                    before, reports[worker.index, 0] = parted(report)
                    if worker.last >= 0:
                        reports[worker.last, 1] = before
                    worker.last = worker.index
                    hand_out(worker)
        finally:
            for worker in workers:
                worker.process.kill()
                worker.process.join()
        for key in sorted(reports):
            replay(reports[key], result)
        record(measured)


def parted(report: dict) -> tuple[dict, dict]:
    """`report` split into what tearing down the previous test's class or
    module did, which unittest does as it starts the next test, and what that
    test did; the first prints, as serially, after the previous test."""
    before = {"testsRun": 0, **{kind: [] for kind in KINDS}}
    after = {"testsRun": report["testsRun"], **{kind: [] for kind in KINDS}}
    for kind in KINDS:
        for entry in report[kind]:
            name = entry[0] if kind == "unexpectedSuccesses" else entry[0][0]
            (before if name.startswith(("tearDownClass (", "tearDownModule (")) else after)[kind].append(entry)
    return before, after


def lost(test, status) -> dict:
    report = dict.fromkeys(KINDS, ())
    return {**report, "testsRun": 1,
            "errors": [(label(test), f"The worker process running this test exited with status {status}.\n")]}


def replay(report: dict, result: unittest.TestResult) -> None:
    result.testsRun += report["testsRun"]
    for kind in KINDS:
        if kind == "unexpectedSuccesses":
            result.unexpectedSuccesses.extend(Ran(*test) for test in report[kind])
        else:
            getattr(result, kind).extend((Ran(*test), text) for test, text in report[kind])


def recorded() -> dict[str, float]:
    try:
        data = json.loads(DURATIONS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {name: seconds for name, seconds in data.items() if isinstance(seconds, (int, float))}


def record(durations: dict[str, float]) -> None:
    """Keep this run's durations for ordering the next, in a checkout only."""
    if not (AREA / ".git").exists():
        return
    try:
        DURATIONS.parent.mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=DURATIONS.parent, prefix=".test-durations-",
                                         delete=False, encoding="utf-8") as handle:
            json.dump({name: round(seconds, 3) for name, seconds in sorted(durations.items())}, handle, indent=0)
        os.replace(handle.name, DURATIONS)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests", description=__doc__.split("\n\n")[0])
    parser.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                        help="worker processes: 0 (the default) is one per CPU, 1 runs serially in this process")
    jobs = parser.parse_args(argv).jobs
    if jobs < 0:
        parser.error("--jobs must be 0 or more")
    suite = unittest.TestLoader().discover(str(AREA / "tests"), top_level_dir=str(AREA))
    runner = unittest.TextTestRunner(verbosity=0, warnings=None if sys.warnoptions else "default")
    if (jobs or cpus()) > 1 and "fork" in multiprocessing.get_all_start_methods():
        result = runner.run(Parallel(suite, jobs or cpus()))
    else:
        result = runner.run(suite)
    if not result.wasSuccessful():
        return 1
    return 5 if result.testsRun == 0 and not result.skipped else 0  # as unittest exits


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
