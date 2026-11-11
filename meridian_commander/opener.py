# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Handing a file to the desktop: ``w`` opens it with the system's application.

``open`` on macOS, ``xdg-open`` on Linux and the BSDs, ``wslview`` under WSL
when it is installed.  Nothing here chooses an application: the desktop's own
associations do, so a PDF goes to whatever opens PDFs.  The built-in viewers
stay on ``F3``.

A file on a server or inside an archive has no path the desktop could open,
so it is copied first, into a directory of its own under one private (0700)
directory made for this run of Meridian.  Each copy gets its own directory so
that opening two files of one name cannot overwrite a copy an application
still has open.  The run's directory is removed when Meridian exits, which an
application still holding a copy may not like: save anything worth keeping
somewhere else.  Edits to a copy are not sent back -- that is what ``F4``
with an outside editor does.

The opener is started detached, in a session of its own, with its output sent
nowhere, so it neither holds the terminal nor writes over the screen.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import tempfile

from .extedit import _copy_name

#: How much is read from a remote file at a time while copying it.
CHUNK = 256 * 1024


class NoOpener(Exception):
    """There is no desktop to hand a file to; the message says why."""


def _under_wsl(environ) -> bool:
    if environ.get("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.release().lower()


def opener_argv(system: str | None = None, environ=None,
                which=shutil.which) -> list[str]:
    """The command that opens a file on this desktop.

    Raises :class:`NoOpener` saying why when there is none: Linux with no
    display to open anything on, or no opener installed.
    """
    system = sys.platform if system is None else system
    environ = os.environ if environ is None else environ
    if system == "darwin":
        found = which("open")
        if found is None:
            raise NoOpener("open is not on the PATH")
        return [found]
    if _under_wsl(environ):
        found = which("wslview")
        if found is not None:
            return [found]
    if not (environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY")):
        raise NoOpener("there is no desktop here (neither DISPLAY nor "
                       "WAYLAND_DISPLAY is set)")
    found = which("xdg-open")
    if found is None:
        raise NoOpener("xdg-open is not on the PATH")
    return [found]


def start(argv: list[str]) -> None:
    """Run ``argv`` detached: no waiting, no terminal, no output."""
    subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)


class SystemOpener:
    """Opens files with the desktop's applications, keeping the copies it made.

    ``argv`` and ``launch`` are there for the tests: by default the opener is
    looked up on each use, so installing one while Meridian runs is enough.
    """

    def __init__(self, argv=opener_argv, launch=start) -> None:
        self._argv = argv
        self._launch = launch
        self.workdir: str | None = None

    def open(self, fs, path: str, is_dir: bool) -> str:
        """Hand ``path`` to the desktop; returns a line for the status bar."""
        name = fs.basename(path) or path
        try:
            argv = self._argv()
        except NoOpener as exc:
            return f"Cannot open {name}: {exc}"
        local = getattr(fs, "local_path", None)
        if local is not None:
            target = local(path)
        elif is_dir:
            return f"Cannot open {name}: not on a remote pane"
        else:
            try:
                target = self._copy(fs, path, name)
            except Exception as exc:
                return f"Cannot fetch {name}: {exc}"
        try:
            self._launch(argv + [target])
        except OSError as exc:
            return f"Could not start {os.path.basename(argv[0])}: {exc}"
        if local is None:
            return (f"Opened a copy of {name} -- changes to it are not "
                    f"sent back")
        return f"Opened {name}"

    def _copy(self, fs, path: str, name: str) -> str:
        """A private copy of a file that is not on this machine."""
        if self.workdir is None:
            self.workdir = tempfile.mkdtemp(prefix="meridian-open-")
        place = tempfile.mkdtemp(dir=self.workdir)
        copy = os.path.join(place, _copy_name(name))
        reader = fs.open_read(path)
        try:
            with open(copy, "wb") as out:
                while True:
                    block = reader.read(CHUNK)
                    if not block:
                        break
                    out.write(block)
        finally:
            try:
                reader.close()
            except Exception:
                pass
        return copy

    def cleanup(self) -> None:
        """Remove every copy made this run."""
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None
