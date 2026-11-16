# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Commands that run in the background while both panes keep working (``&``).

A :class:`Command` is one program -- ``python3 -m http.server``, a dev
server, ``tail -f`` -- started in a directory and left running.  It has a
terminal of its own: a pty, in a session and process group of its own, and a
:class:`~meridian_commander.vt.Screen` that keeps its screen and up to
:data:`~meridian_commander.vt.SCROLLBACK` lines of history whether or not a
pane is showing it.  :class:`~meridian_commander.commandpane.CommandPane` is
the view that shows that screen in a pane; closing the view never stops the
command.  Only :meth:`Command.terminate` and :func:`stop_all` do.

**Its output is read on a thread of its own.**  A pty holds a few kilobytes;
a program writing to one nobody reads stops at its next write, so a hidden
``http.server`` would freeze on the next request it logged.  Reading on the
main loop's poll was not enough: a dialog, a full-screen viewer, the editor
and the full-screen shell (``!``) all hold the main thread for as long as
they are open.  So each command's reader thread takes its output in as it
comes and feeds the screen, under the command's lock, which the view holds
while it draws.  The main loop only notices when one has ended
(:meth:`CommandList.tick`), to say so on the status line.

**Stopping ends the whole process group**: ``SIGTERM`` (and ``SIGCONT``, so a
program stopped with ``Ctrl-Z`` can act on it), then ``SIGKILL`` for whatever
is left after a short grace.  The program is the leader of its own group, so
the children it starts -- a server's workers, the steps of ``make serve`` --
go with it, unless one has deliberately moved itself into a session of its
own.  Quitting the application stops every command the same way, and so does
an :mod:`atexit` safety net for a way out that skips the quit, so nothing
Meridian started is left running after it.
"""

from __future__ import annotations

import atexit
import errno
import os
import select
import signal
import threading
import time

from . import vt
from .plugins.terminal import CHUNK, set_size, spawn

#: How long the reader thread waits for output before looking again whether
#: it has been asked to finish.
POLL_SECONDS = 0.05

#: How long a stopped command has to end after ``SIGTERM`` before the rest of
#: its process group is sent ``SIGKILL``.
STOP_GRACE = 2.0

#: How long to wait for the reaping after ``SIGKILL``, which cannot be
#: refused; only a process stuck in the kernel takes longer.
KILL_WAIT = 1.0

#: How long a command's reader thread is given to finish when it is closed.
JOIN_SECONDS = 2.0

#: How often stopping looks again whether the group has gone.
STEP_SECONDS = 0.02

#: How long starting waits for the forked child to lead a process group of
#: its own, which it does a moment after the fork returns.
GROUP_SECONDS = 2.0

#: How long a closing reader waits for the last of the output: the kernel
#: hands a pty's output over a moment after the program writes it, and the
#: last words of a program that has just been stopped are worth having.
DRAIN_SECONDS = 0.2


def duration(seconds: float) -> str:
    """A running time, the way the jobs list shows it: 45s, 3m07s, 2h05m."""
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    minutes, whole = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m{whole:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def shell_argv(line: str) -> list[str]:
    """``line`` run by the user's shell, as a prompt would run it."""
    return [os.environ.get("SHELL") or "/bin/sh", "-c", line]


class Command:
    """One command running in the background, its terminal, and its end.

    ``screen`` is shared with whichever pane shows it, and ``lock`` guards
    it: the reader thread feeds it under the lock, and the view draws and
    resizes it under the lock.  ``viewer`` is that view, or None while no
    pane shows the command.
    """

    def __init__(self, number: int, name: str, line: str, directory: str,
                 rows: int = 24, cols: int = 80) -> None:
        self.number = number
        self.name = name
        self.line = line
        self.directory = directory
        self.argv = shell_argv(line)
        self.screen = vt.Screen(rows, cols)
        self.lock = threading.RLock()
        #: The process group's leader, which is also the group's number.
        self.pid = 0
        self._fd: int | None = None
        #: Bumped on every read the screen is fed, so a view can tell that
        #: there is something new to draw.
        self.output = 0
        #: Whether the program has ended and been reaped.
        self.ended = False
        #: Its exit status: negative for the signal that ended it, None if
        #: it could not be collected.
        self.exit_status: int | None = None
        #: Whether it ended because it was stopped from here.
        self.stopped = False
        self.started = time.monotonic()
        self.finished: float | None = None
        self.viewer = None
        self._eof = False
        self._group_gone = False
        self._closing = threading.Event()
        self._thread = threading.Thread(
            target=self._pump, name=f"meridian-command-{number}", daemon=True)

    # -- starting ------------------------------------------------------------
    def start(self) -> None:
        """Start the program, and the thread that reads what it writes."""
        self.pid, self._fd = spawn(self.directory, self.argv)
        self._await_group()
        self.set_size(self.screen.rows, self.screen.cols)
        _LIVE.add(self)
        self._thread.start()

    def _await_group(self) -> None:
        """Wait for the child to lead a process group of its own.

        ``pty.fork()`` returns in the parent before the child has called
        ``setsid()``.  A stop sent in that moment found no group of that
        number, took the group for gone, and never signalled it again.
        """
        deadline = time.monotonic() + GROUP_SECONDS
        while time.monotonic() < deadline:
            try:
                if os.getpgid(self.pid) == self.pid:
                    return
            except OSError:
                return          # ended already: nothing left to wait for
            time.sleep(0.001)

    # -- what the screen asks -------------------------------------------------
    def running(self) -> bool:
        return not self.ended

    def state_text(self) -> str:
        """``running``, ``stopped``, ``exited (status N)`` or ``killed (SIG)``."""
        if not self.ended:
            return "running"
        if self.stopped:
            return "stopped"
        status = self.exit_status
        if status is None:
            return "exited"
        if status < 0:
            return f"killed ({signal.Signals(-status).name})"
        return f"exited (status {status})"

    def age(self) -> str:
        """How long it has run, or ran."""
        end = self.finished if self.finished is not None else time.monotonic()
        return duration(end - self.started)

    # -- the terminal -----------------------------------------------------------
    def send(self, data: bytes) -> None:
        """Write ``data`` to the program, as if typed at its terminal."""
        with self.lock:
            fd = self._fd
            if fd is None:
                return
            try:
                while data:
                    data = data[os.write(fd, data):]
            except OSError:
                # A full input buffer (the program is not reading what is
                # typed), or a terminal whose program has gone: the keys are
                # dropped, as the pane's own terminal drops them.
                pass

    def set_size(self, rows: int, cols: int) -> None:
        """Tell the program its terminal's size; it gets a SIGWINCH."""
        with self.lock:
            if self._fd is None:
                return
            try:
                set_size(self._fd, rows, cols)
            except OSError:
                # A pty whose other end has closed may refuse; the program
                # that would have heard it has gone anyway.
                pass

    def _pump(self) -> None:
        """The reader thread: take output in until there is no more.

        The master is closed here and only here, so no write can ever reach
        a descriptor number the system has since handed to something else.
        """
        fd = self._fd
        assert fd is not None
        while not self._closing.is_set():
            if self._eof:
                # Nothing more will come; wait for the program to be reaped.
                # (A program that closes its terminal and runs on is still
                # running, and keeps the pty until it ends or is stopped.)
                if self.poll():
                    break
                self._closing.wait(POLL_SECONDS)
            elif select.select([fd], [], [], POLL_SECONDS)[0]:
                self._read(fd)
        # Asked to finish (it has been stopped): take what it said last.
        deadline = time.monotonic() + DRAIN_SECONDS
        while not self._eof and time.monotonic() < deadline:
            if select.select([fd], [], [], POLL_SECONDS)[0]:
                self._read(fd)
        with self.lock:
            os.close(fd)
            self._fd = None

    def _read(self, fd: int) -> bool:
        """Feed the screen one read of output; whether there was any."""
        try:
            data = os.read(fd, CHUNK)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            data = b""          # EIO: every process has let go of the pty
        if not data:
            self._eof = True
            return False
        with self.lock:
            self.screen.feed(data)
            self.output += 1
            replies = self.screen.take_replies()
            if replies:
                self.send(replies)
        return True

    # -- the end --------------------------------------------------------------------
    def poll(self) -> bool:
        """Collect the exit status if the program has ended; whether it has."""
        with self.lock:
            if self.ended:
                return True
            status: int | None
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                # Collected elsewhere: it has ended, with a status unknown.
                pid, status = self.pid, None
            if pid == 0:
                return False
            self.exit_status = (None if status is None
                                else os.waitstatus_to_exitcode(status))
            self.ended = True
            self.finished = time.monotonic()
        # Whether its group went with it: once that is known to be empty
        # the number is never signalled again, as it could one day name
        # somebody else's group.
        self._group_alive()
        return True

    def _group_alive(self) -> bool:
        """Whether anything is left in the command's process group."""
        if self._group_gone:
            return False
        try:
            os.killpg(self.pid, 0)
        except ProcessLookupError:
            self._group_gone = True
            return False
        except PermissionError:
            return True         # there, but not ours to signal (setuid)
        return True

    def _signal(self, number: int) -> None:
        if self._group_gone:
            return
        try:
            os.killpg(self.pid, number)
        except ProcessLookupError:
            self._group_gone = True
        except PermissionError:
            pass

    def terminate(self) -> None:
        """Ask the whole process group to end: SIGTERM, then SIGCONT."""
        if not self.ended:
            self.stopped = True
        self._signal(signal.SIGTERM)
        # A program stopped with Ctrl-Z would hold a SIGTERM until it was
        # continued; this lets it act on the request now.
        self._signal(signal.SIGCONT)

    def settled(self) -> bool:
        """Whether the program has ended and nothing is left in its group."""
        return self.poll() and not self._group_alive()

    def kill(self) -> None:
        """SIGKILL to whatever is left of the group, and wait for the reaping."""
        self._signal(signal.SIGKILL)
        deadline = time.monotonic() + KILL_WAIT
        while not self.poll() and time.monotonic() < deadline:
            time.sleep(STEP_SECONDS)

    def stop(self, grace: float = STOP_GRACE) -> None:
        """End the command and everything it started, and let go of it."""
        stop_all([self], grace)

    def close(self) -> None:
        """Let go of the terminal: the reader thread closes it and ends.

        Its screen stays, so a stopped command can still be read.
        """
        self._closing.set()
        self._thread.join(timeout=JOIN_SECONDS)
        _LIVE.discard(self)


def stop_all(commands: list[Command], grace: float = STOP_GRACE) -> None:
    """Stop every one of ``commands`` together, within one grace between them.

    All are asked first and then waited for at once, so quitting with five
    commands running takes one grace, not five.
    """
    for command in commands:
        command.terminate()
    deadline = time.monotonic() + grace
    waiting = list(commands)
    while True:
        waiting = [command for command in waiting if not command.settled()]
        if not waiting or time.monotonic() >= deadline:
            break
        time.sleep(STEP_SECONDS)
    for command in waiting:
        command.kill()
    for command in commands:
        command.close()


#: Every command started and not yet closed, for the safety net below.
_LIVE: set[Command] = set()

#: The grace the safety net gives: a way out that skipped the quit is often
#: one in a hurry.
EXIT_GRACE = 0.5


def stop_everything() -> None:
    """Stop every command still open: the safety net at interpreter exit."""
    stop_all(list(_LIVE), EXIT_GRACE)


atexit.register(stop_everything)


class CommandList:
    """Every background command of this run, running and ended."""

    def __init__(self) -> None:
        self.commands: list[Command] = []
        self._numbers = 0
        self._reported: set[int] = set()

    def start(self, name: str, line: str, directory: str) -> Command:
        """Start ``line`` in ``directory`` under ``name``.

        An ended command of the same name gives way to it, as starting it
        again is what replaces the last run's screen in the list.  Raises
        :class:`OSError` when no pty or process can be had.
        """
        for old in [c for c in self.commands if c.name == name and c.ended]:
            self.remove(old)
        command = Command(self._numbers + 1, name, line, directory)
        command.start()
        self._numbers += 1
        self.commands.append(command)
        return command

    def running(self) -> list[Command]:
        return [command for command in self.commands if command.running()]

    def find(self, name: str) -> Command | None:
        """The running command called ``name``, if there is one."""
        return next((c for c in self.running() if c.name == name), None)

    def tick(self) -> list[Command]:
        """The commands that have ended since the last call."""
        ended = []
        for command in self.commands:
            if command.number in self._reported or not command.poll():
                continue
            self._reported.add(command.number)
            ended.append(command)
        return ended

    def status(self) -> str:
        """The status line's indicator, or nothing with none running."""
        count = len(self.running())
        return f"Commands: {count} running" if count else ""

    def remove(self, command: Command) -> None:
        """Forget an ended command, after making sure nothing of it is left."""
        command.stop()
        self.commands.remove(command)

    def stop_all(self, grace: float = STOP_GRACE) -> None:
        stop_all(self.commands, grace)
