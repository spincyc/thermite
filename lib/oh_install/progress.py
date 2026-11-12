"""Terminal presentation for lifecycle steps; the painter never runs work."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import threading
from collections import deque
from contextvars import ContextVar
from time import monotonic

import oh_cli

current = ContextVar("thermite_install_progress", default=None)
capture_command = ContextVar("thermite_install_capture", default=False)
terminal_command = ContextVar("thermite_install_terminal", default=False)
show_command_output = ContextVar("thermite_install_show_output", default=False)


class Progress:
    def __init__(self, system, label: str):
        self.system, self.label = system, label
        self.stream = sys.stdout
        self.quiet = oh_cli.switches.quiet
        self.verbose = oh_cli.switches.verbose
        self.terminal = self.stream.isatty() and os.environ.get("TERM") != "dumb"
        self.animate = self.terminal and not (self.quiet or self.verbose)
        self.color = self.terminal and oh_cli.color()
        self.unicode = True
        try:
            "✓".encode(self.stream.encoding or "ascii")
        except (UnicodeEncodeError, LookupError):
            self.unicode = False
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.paused = 0
        self.drawn = False
        self.finished = False
        self.frame = 0
        self.notes = deque(maxlen=20)
        self.announced = False
        self.started = None
        #: What a long wait currently reports, shown beside the spinner.
        self.detail = ""
        self.shown_detail = None
        self.shown_at = None

    def __enter__(self):
        self.started = monotonic()
        self.token = current.set(self)
        if self.animate:
            self._paint()
            self.thread = threading.Thread(target=self._spin, name="thermite-progress", daemon=True)
            self.thread.start()
        return self

    def __exit__(self, kind, value, traceback):
        try:
            if not self.finished:
                self.finish("STOP" if kind is KeyboardInterrupt else "FAIL")
        finally:
            self.stop.set()
            if self.thread is not None:
                self.thread.join()
            current.reset(self.token)

    def _clear(self):
        if self.drawn:
            self.stream.write("\r\x1b[2K")
            self.stream.flush()
            self.drawn = False

    def _paint(self):
        with self.lock:
            if self.paused or self.finished:
                return
            width = shutil.get_terminal_size((80, 24)).columns
            symbol = "|/-\\"[self.frame % 4]
            text = f"[ {symbol} ] {self.label}" + (f": {self.detail}" if self.detail else "")
            duration = f" ({self.elapsed()})"
            # A single physical line is essential: clearing a wrapped frame
            # would leave fragments behind. Retain elapsed time when a long
            # stage title needs truncating; stage titles are plain text.
            available = max(0, width - 1)
            text = (text[:available - len(duration)] + duration if available > len(duration)
                    else (text + duration)[:available])
            self.stream.write("\r\x1b[2K" + text)
            self.stream.flush()
            self.drawn = True
            self.frame += 1

    def _spin(self):
        # Only terminal repainting; no device, filesystem or service polling.
        while not self.stop.wait(0.125):
            self._paint()

    @contextlib.contextmanager
    def pause(self):
        with self.lock:
            self.paused += 1
            self._clear()
        try:
            yield
        finally:
            with self.lock:
                self.paused -= 1

    def message(self, text: str, *, error: bool = False):
        with self.pause():
            if error and self.quiet:
                print(text, file=sys.stderr, flush=True)
            elif not self.quiet:
                self.system.say(text)

    def elapsed_seconds(self) -> int:
        return max(0, int(monotonic() - self.started)) if self.started is not None else 0

    def elapsed(self) -> str:
        seconds = self.elapsed_seconds()
        minutes, seconds = divmod(seconds, 60)
        return f"{minutes}m {seconds:02d}s" if minutes else f"{seconds}s"

    #: Without animation, repeat an unchanged wait report at most this often.
    UPDATE_INTERVAL = 30

    def update(self, detail: str):
        """Report a long wait's current state without ending the stage.

        The spinner shows it in place. Plain and verbose output prints a
        change, rate-limited, so a redirected log stays readable.
        """
        detail = " ".join(str(detail).split())
        with self.lock:
            self.detail = detail
        if self.animate or self.quiet or not detail:
            return
        now = monotonic()
        if detail != self.shown_detail and (self.shown_at is None or now - self.shown_at >= self.UPDATE_INTERVAL):
            self.shown_detail, self.shown_at = detail, now
            self.message(f"   waiting: {detail} ({self.elapsed()})")

    def working(self, detail: str = ""):
        """Note that the stage acts, not only checks. The spinner already
        shows that; a log without one gets a line."""
        if not self.animate:
            self.announce(detail)

    def announce(self, detail: str = ""):
        """Name the stage once in the scrollback, before a password prompt
        that must say which stage asks."""
        if not self.announced:
            self.message(f"[ .. ] {self.label}" + (f": {detail}" if detail else ""))
            self.announced = True

    def finish(self, mark: str, detail: str = ""):
        with self.lock:
            self.finished = True
            self.stop.set()
            self._clear()
        success = mark == "OK"
        if not success and self.quiet:
            self.prompt_context()
        shown = "✓" if success and self.terminal and self.unicode else mark
        if self.color:
            shown = f"\x1b[1;{'32' if success else '31'}m{shown}\x1b[0m"
        duration = f" ({self.elapsed()})" if self.elapsed_seconds() else ""
        self.message(f"[ {shown} ] {self.label}" + duration + (f": {detail}" if detail else ""), error=not success)

    def passed(self, detail: str):
        self.finish("OK", detail if self.verbose else "")

    def prompt_context(self):
        """Quiet mode still supplies the context of an error or a question."""
        if self.notes:
            print("\n".join(self.notes), file=sys.stderr, flush=True)
            self.notes.clear()


@contextlib.contextmanager
def stage(system, title: str):
    """An unnumbered stage around work outside the runner's steps: it
    passes unless that work raises."""
    with Progress(system, title) as display:
        yield display
        display.passed("")


def update(detail: str) -> None:
    """Show a long wait's current state on the active stage, if any."""
    active = current.get()
    if active is not None:
        active.update(detail)


@contextlib.contextmanager
def paused():
    active = current.get()
    with active.pause() if active else contextlib.nullcontext():
        yield


@contextlib.contextmanager
def command(*, terminal: bool, show_output: bool = False):
    active = current.get()
    token = capture_command.set(bool(active and not active.verbose and not terminal))
    terminal_token = terminal_command.set(terminal)
    output_token = show_command_output.set(show_output)
    try:
        with paused() if terminal else contextlib.nullcontext():
            yield
    finally:
        show_command_output.reset(output_token)
        terminal_command.reset(terminal_token)
        capture_command.reset(token)
