"""Bounded diagnostic capture for explicitly noninteractive install children.

Call on the main thread. Interactive commands must retain their normal
terminal streams instead. Normal completion follows the child
leader. On cancellation or a lingering output pipe, cleanup covers its
isolated process group. Commands remain responsible for their deliberately
detached services, including the sandbox runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import contextlib
from pathlib import Path
import os
import signal
import subprocess
import threading
import time
import unicodedata

TAIL_BYTES = 64 * 1024
READ_BYTES = 8192
DRAIN_GRACE = 1.0
# A sandbox stop can use 10 seconds before escalation and another 10 to
# verify retirement. Let its Python owner finish that cleanup and bookkeeping.
STOP_GRACE = 25.0
KILL_GRACE = 1.0


def _safe_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    clean = []
    index = 0
    while index < len(text):
        char = text[index]
        index += 1
        if char == "\x1b":
            if index == len(text):
                break
            kind = text[index]
            index += 1
            if kind == "[":
                while index < len(text):
                    end = text[index]
                    index += 1
                    if "@" <= end <= "~":
                        break
            elif kind in "]PX^_":
                while index < len(text):
                    if kind == "]" and text[index] == "\x07":
                        index += 1
                        break
                    if text.startswith("\x1b\\", index):
                        index += 2
                        break
                    index += 1
            elif kind in "()*+-./" and index < len(text):
                index += 1
            continue
        if char in "\n\t" or unicodedata.category(char) not in ("Cc", "Cf", "Cs"):
            clean.append(char)
    return "".join(clean)


class _Tail:
    def __init__(self) -> None:
        self.data = bytearray()
        self.truncated = False
        self.failed = False
        self.lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        with self.lock:
            excess = len(self.data) + len(chunk) - TAIL_BYTES
            if excess > 0:
                self.truncated = True
                del self.data[:excess]
            self.data.extend(chunk[-TAIL_BYTES:])

    def diagnostic(self) -> str:
        with self.lock:
            text = _safe_text(bytes(self.data)).strip("\n")
            truncated = self.truncated
        return (("[earlier child output omitted]\n" if truncated else "") + text).rstrip()


def tail(data: bytes) -> str:
    """The safe bounded tail of output a caller already read."""
    found = _Tail()
    found.append(data)
    return found.diagnostic()


def _drain(stream, tail: _Tail, done: threading.Event) -> None:
    try:
        while chunk := stream.read(READ_BYTES):
            tail.append(chunk)
    except (OSError, ValueError):
        tail.failed = True
    finally:
        try:
            stream.close()
        except OSError:
            tail.failed = True
        finally:
            done.set()


def _signal_group(pid: int, signum: int) -> bool | None:
    """Whether the group got the signal or is gone; None when this user may
    not signal it, as a root command under sudo."""
    try:
        os.killpg(pid, signum)
        return True
    except ProcessLookupError:
        return True
    except PermissionError:
        return None
    except OSError:
        return False


@contextlib.contextmanager
def _undisturbed():
    """Stopping a child, or waiting for one that cannot be stopped, runs to
    its end: Ctrl-C is ignored meanwhile, and a signal this program handles
    itself (SIGHUP or SIGTERM inside an install transaction, scope.Scope)
    is held back and delivered when the block ends."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    late: list[int] = []
    previous = {signal.SIGINT: signal.signal(signal.SIGINT, signal.SIG_IGN)}
    for number in (signal.SIGHUP, signal.SIGTERM):
        if callable(signal.getsignal(number)):
            previous[number] = signal.signal(number, lambda number, frame: late.append(number))
    try:
        yield
    finally:
        for number, handler in previous.items():
            if handler is not None:
                signal.signal(number, handler)
        if late:
            signal.raise_signal(late[0])


def _outlast(process: subprocess.Popen, deadline: float | None, report: Callable[[str], None]) -> None:
    """An interrupted run cannot stop a command this user may not signal: a
    root command under sudo, the install engine among them. Leaving while
    it still changes the host would let the next command, or a rollback,
    meet a host in motion; so wait for it, for as long as it was allowed
    to take."""
    if _signal_group(process.pid, 0) is not None or process.poll() is not None:
        return
    report("Waiting for the command to end: it runs as root, and this run may not stop it.")
    with _undisturbed():
        try:
            process.wait(timeout=None if deadline is None else max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass


def _stop(process: subprocess.Popen, reader: threading.Thread | None, signum: int) -> bool:
    """Allow cleanup, then fence every remaining process in this group."""
    with _undisturbed():
        stopped = _signal_group(process.pid, signum)
        deadline = time.monotonic() + STOP_GRACE
        try:
            process.wait(timeout=STOP_GRACE)
        except subprocess.TimeoutExpired:
            pass
        # The leader may exit before a descendant finishes cleanup. Give
        # the whole group its grace rather than using leader exit as proof.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        except OSError:
            stopped = False
        else:
            threading.Event().wait(max(0.0, deadline - time.monotonic()))
        stopped = _signal_group(process.pid, signal.SIGKILL) and stopped
        try:
            process.wait(timeout=KILL_GRACE)
        except subprocess.TimeoutExpired:
            stopped = False
        if reader is not None and reader.ident is not None:
            reader.join(timeout=KILL_GRACE)
        else:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        return bool(stopped) and (reader is None or not reader.is_alive())


def _report(tail: _Tail, report: Callable[[str], None]) -> None:
    text = tail.diagnostic()
    if text:
        report(text)


def run(argv: Sequence[str], cwd: Path, *, timeout: float | None = None,
        report: Callable[[str], None], keep_terminal: bool = False, show_output: bool | str = False) -> int:
    """Report a safe bounded tail on failure, or on successful opt-in;
    show_output "stderr" discards standard output and reports standard
    error alone, whenever there is any.

    FileNotFoundError, TimeoutExpired and KeyboardInterrupt retain their
    usual meaning. Cancellation cleans up the isolated child process group
    before reporting diagnostics and re-raising; an interrupt first waits
    for a child this user may not signal (_outlast). No output is written
    to disk.
    Preauthenticated sudo uses keep_terminal: its own process group retains
    this session's controlling terminal and the validation process's parent
    PID, so tty/ppid credential timestamps still match. Never read a password
    here: sudo must be invoked with -n.
    """
    tail = _Tail()
    done = threading.Event()
    process = None
    reader = None
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        isolation = {"process_group": 0} if keep_terminal else {"start_new_session": True}
        separate = show_output == "stderr"
        process = subprocess.Popen(list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL if separate else subprocess.PIPE,
                                   stderr=subprocess.PIPE if separate else subprocess.STDOUT,
                                   bufsize=0, **isolation)
        reader = threading.Thread(target=_drain, args=(process.stderr if separate else process.stdout, tail, done),
                                  name="thermite-child-output", daemon=True)
        reader.start()
        code = process.wait(timeout=timeout)
        drained = done.wait(DRAIN_GRACE)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        # Python boot checks own detached sandbox instances and stop those
        # in finally. SIGINT reaches that cleanup path for timeouts as well.
        if process is not None and isinstance(error, KeyboardInterrupt):
            _outlast(process, deadline, report)
        stopped = process is None or _stop(process, reader, signal.SIGINT)
        if not stopped:
            report("Child process group or output drain did not finish cleanup.")
        _report(tail, report)
        raise
    except BaseException:
        if process is not None:
            _stop(process, reader, signal.SIGTERM)
        raise
    if not drained:
        stopped = _stop(process, reader, signal.SIGTERM)
        if not stopped:
            report("Child exited but its process group or output drain did not finish cleanup.")
        else:
            report("Child exited with descendants holding its output pipe; the process group was stopped.")
        _report(tail, report)
        return code or 1
    reader.join()
    if tail.failed:
        report("Child output could not be read completely.")
    if code or tail.failed or show_output:
        _report(tail, report)
    return code or int(tail.failed)
