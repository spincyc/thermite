"""Bounded diagnostic capture for explicitly noninteractive install children.

Call on the main thread. Interactive and privileged commands must retain
their normal terminal streams instead. Normal completion follows the child
leader. On cancellation or a lingering output pipe, cleanup covers its
isolated process group. Commands remain responsible for their deliberately
detached services, including the sandbox runtime.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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


def _signal_group(pid: int, signum: int) -> bool:
    try:
        os.killpg(pid, signum)
        return True
    except ProcessLookupError:
        return True
    except OSError:
        return False


def _stop(process: subprocess.Popen, reader: threading.Thread | None, signum: int) -> bool:
    """Allow cleanup, then fence every remaining process in this group."""
    previous = None
    if threading.current_thread() is threading.main_thread():
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
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
        elif process.stdout is not None:
            process.stdout.close()
        return stopped and (reader is None or not reader.is_alive())
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _report(tail: _Tail, report: Callable[[str], None]) -> None:
    text = tail.diagnostic()
    if text:
        report(text)


def run(argv: Sequence[str], cwd: Path, *, timeout: float | None = None,
        report: Callable[[str], None]) -> int:
    """Discard successful chatter; report a safe bounded tail on failure.

    FileNotFoundError, TimeoutExpired and KeyboardInterrupt retain their
    usual meaning. Cancellation cleans up the isolated child process group
    before reporting diagnostics and re-raising. No output is written to disk.
    """
    tail = _Tail()
    done = threading.Event()
    process = None
    reader = None
    try:
        process = subprocess.Popen(list(argv), cwd=cwd, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   bufsize=0, start_new_session=True)
        reader = threading.Thread(target=_drain, args=(process.stdout, tail, done),
                                  name="thermite-child-output", daemon=True)
        reader.start()
        code = process.wait(timeout=timeout)
        drained = done.wait(DRAIN_GRACE)
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        # Python boot checks own detached sandbox instances and stop those
        # in finally. SIGINT reaches that cleanup path for timeouts as well.
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
    if code or tail.failed:
        _report(tail, report)
    return code or int(tail.failed)
