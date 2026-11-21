"""The scope of an install transaction.

Every run that begins, resolves or rolls back an install transaction
(lib/oh_install/changeover.py) works inside one Scope, from before it looks
for an earlier transaction until its own has committed or rolled back:

- One lock for the site, an flock on var/transaction.lock. A second run
  finds it held, says who holds it and changes nothing; without it, that
  run would roll the running transaction back under its owner
  (changeover.resolve). The holder writes its verb, process and start time
  into the file; thermite status reads them.
- SIGHUP and SIGTERM end the run once, as an interrupt raised in the main
  thread like Ctrl-C: a child that still runs is stopped, or waited for
  when this user may not signal it (output.run), before anything else
  happens, and the lock is released only when the scope ends. changeover
  then rolls an open transaction back in the same run, because nobody may
  be there to run the command again; root commands issued after the signal
  run under sudo -n and fail rather than ask (runner.perform).
- sudo's credentials are validated again every minute once the transaction
  has begun (keep_fresh), so the commit or rollback at the end of a long
  wait does not stop at a password prompt.

changeover refuses to work outside a held scope, and builds no engine
command that changes the host outside one (Transaction.argv): an owner
command cannot run a transaction without it.
"""
from __future__ import annotations

import dataclasses
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import threading
import time

from oh_install.system import System

#: The lock, in the site's var. Not in var/transaction: a purge removes that.
LOCK = "transaction.lock"
#: Seconds between validations of sudo's credentials. sudo forgets them
#: after its timestamp_timeout, five minutes unless sudoers says otherwise.
REFRESH = 60.0
#: How long taking the lock tries again while nobody says they hold it:
#: thermite status holds it for an instant to test it.
PATIENCE = 0.3
SIGNALS = (signal.SIGHUP, signal.SIGTERM)

#: The scopes this process holds, by the site's var.
_held: dict[Path, "Scope"] = {}

#: How the owner command of a transaction's verb is spelled now, where that
#: differs from the verb its record and scope keep: earlier releases read
#: those, and know a revert by that name.
SPELLED = {"revert": "rollback --restore-state"}


class InProgress(Exception):
    """Another run holds the site's scope; this one must change nothing."""

    def __init__(self, holder: Holder):
        super().__init__(f"{holder} is in progress")
        self.holder = holder


class Terminated(KeyboardInterrupt):
    """SIGHUP or SIGTERM reached a run inside a scope: an interrupt that
    nobody may be there to follow up with another command."""


@dataclasses.dataclass(frozen=True)
class Holder:
    """Who holds a site's scope, as the holder wrote it; None for what it
    did not write, or not readably."""

    verb: str | None = None
    pid: int | None = None
    since: str | None = None

    def __str__(self) -> str:
        who = f"thermite {SPELLED.get(self.verb, self.verb)}" if self.verb else "another thermite command"
        details = [f"process {self.pid}"] if self.pid else []
        if self.since:
            details.append(f"since {self.since}")
        return who + (f" ({', '.join(details)})" if details else "")


def _key(var: Path) -> Path:
    return Path(os.path.abspath(var))


def _holder(descriptor: int) -> Holder:
    try:
        found = json.loads(os.pread(descriptor, 4096, 0))
        verb, pid, since = found["verb"], found["pid"], found["since"]
    except (OSError, ValueError, TypeError, KeyError):
        return Holder()
    if not (isinstance(verb, str) and isinstance(pid, int) and isinstance(since, str)):
        return Holder()
    return Holder(verb, pid, since)


def running(var: Path) -> Holder | None:
    """Who holds the scope of this site now, if anyone. It writes nothing
    and never waits: it tests the lock shared, for an instant."""
    try:
        descriptor = os.open(Path(var) / LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _holder(descriptor)
        return None
    finally:
        os.close(descriptor)


def held(var: Path) -> Scope:
    """The scope this process holds for the site's var. RuntimeError
    outside one: the caller is an entry point that forgot to take it."""
    try:
        return _held[_key(var)]
    except KeyError:
        raise RuntimeError(f"an install transaction of {Path(var).parent} must run inside its scope "
                           "(oh_install.changeover.scoped)") from None


def ended() -> bool:
    """Whether a signal has ended a run this process holds a scope for."""
    return any(scope.signal is not None for scope in _held.values())


def _mute_dead_streams() -> None:
    """After a hangup every write to the terminal fails. What the run
    still says goes nowhere instead, so that saying it cannot stop a
    rollback; a stream that still works (a log file, a live terminal after
    kill -HUP) is left alone."""
    for descriptor in (1, 2):
        poller = select.poll()
        try:
            poller.register(descriptor, 0)
            dead = any(events & (select.POLLHUP | select.POLLERR | select.POLLNVAL) for _, events in poller.poll(0))
        except (OSError, ValueError):
            continue
        if dead:
            null = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(null, descriptor)
            finally:
                os.close(null)


class Scope:
    """One run's hold on a site's install transactions; a context manager.
    InProgress on entering when another run holds it."""

    def __init__(self, var: Path, system: System, verb: str):
        self.var, self.system, self.verb = Path(var), system, verb
        #: The signal that ended this run, once one has.
        self.signal: signal.Signals | None = None
        self._descriptor: int | None = None
        self._previous: dict[int, object] = {}
        self._over = threading.Event()
        self._refresher: threading.Thread | None = None

    def __enter__(self) -> Scope:
        if _key(self.var) in _held:
            raise RuntimeError(f"this run already holds the transaction scope of {self.var.parent}")
        self._descriptor = self._lock()
        _held[_key(self.var)] = self
        if threading.current_thread() is threading.main_thread():
            for number in SIGNALS:
                # Under nohup the run was told to outlive its terminal.
                if signal.getsignal(number) is not signal.SIG_IGN:
                    self._previous[number] = signal.signal(number, self._signalled)
        return self

    def __exit__(self, *exception) -> None:
        # First, so that no signal raises in the middle of what follows.
        for number, previous in self._previous.items():
            signal.signal(number, signal.SIG_DFL if previous is None else previous)
        self._over.set()
        if self._refresher is not None:
            self._refresher.join(timeout=5)
        del _held[_key(self.var)]
        try:
            os.ftruncate(self._descriptor, 0)
        finally:
            os.close(self._descriptor)

    def _lock(self) -> int:
        if not self.var.parent.is_dir():
            raise ValueError(f"{self.var.parent} is not a directory: --config must name an existing Thermite site "
                             "(thermite init DIR creates one)")
        self.var.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(self.var / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            deadline = time.monotonic() + PATIENCE
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    holder = _holder(descriptor)
                    if holder.pid is not None or time.monotonic() >= deadline:
                        raise InProgress(holder) from None
                    time.sleep(0.05)
            record = {"verb": self.verb, "pid": os.getpid(),
                      "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            os.ftruncate(descriptor, 0)
            os.pwrite(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode(), 0)
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _signalled(self, number: int, frame) -> None:
        if self.signal is not None:
            # Once is enough: the run is ending, and a hangup arrives both
            # from the shell and from the kernel.
            return
        self.signal = signal.Signals(number)
        _mute_dead_streams()
        raise Terminated(self.signal.name)

    def keep_fresh(self) -> None:
        """From now until the scope ends, validate sudo's credentials again
        every REFRESH seconds without asking, as `sudo -n -v` does: the
        owner authenticated to get here, and what is left may wait longer
        than sudo remembers that."""
        if self._refresher is None:
            self._refresher = threading.Thread(target=self._refresh, name="thermite-sudo-fresh", daemon=True)
            self._refresher.start()

    def _refresh(self) -> None:
        while not self._over.wait(REFRESH):
            code, _ = self.system.capture(("sudo", "-n", "-v"), timeout=REFRESH)
            if code:
                # Expired, or its terminal is gone: only the owner can
                # authenticate again, and asking again each minute would
                # only fill sudo's log.
                return
