"""Terminal presentation for lifecycle steps; the painter never runs work."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import threading
from collections import deque
from contextvars import ContextVar

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

    def __enter__(self):
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
            text = f"[ {symbol} ] {self.label}"
            # A single physical line is essential: clearing a wrapped frame
            # would leave fragments behind. Stage titles are plain text.
            text = text[:max(0, width - 1)]
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

    def detail(self, text: str):
        if self.verbose:
            self.message("   " + text)

    def working(self):
        if not self.announced:
            self.message(f"[ .. ] {self.label}")
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
        self.message(f"[ {shown} ] {self.label}" + (f": {detail}" if detail else ""), error=not success)

    def passed(self, detail: str):
        self.finish("OK", detail if self.verbose else "")

    def prompt_context(self):
        """Quiet mode still supplies the context of an error or a question."""
        if self.notes:
            print("\n".join(self.notes), file=sys.stderr, flush=True)
            self.notes.clear()


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
            if terminal and active and active.animate:
                active.working()
            yield
    finally:
        show_command_output.reset(output_token)
        terminal_command.reset(terminal_token)
        capture_command.reset(token)
