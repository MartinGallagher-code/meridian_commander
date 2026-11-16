# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Saved commands to run in the background (``&``), kept like the presets.

A saved command is a name, a command line and, if it wants one, a directory
of its own.  It lives in an INI file of its own, one section per command::

    ~/.config/meridian-commander/commands.ini     ($XDG_CONFIG_HOME honoured)

    [http]
    command = python3 -m http.server 8000
    directory = /srv/site

A blank (or missing) ``directory`` means "the active pane's directory when it
is started", so one saved command can serve whichever directory you are
looking at.  The command line is run by your shell (``$SHELL -c``), so pipes,
``&&`` and ``$VARS`` work as they do at a prompt.

Everything here is handled as :mod:`~meridian_commander.presets` handles
``presets.ini``: the file is separate from ``config.ini`` because the
application rewrites it; it is plain text and safe to edit by hand, ``%``
included, since the parser is built with interpolation off; and the list is
kept in alphabetical order on the way in *and* on the way out, so each
command keeps its letter in the menu and a hand-edited file is tidied the
next time the application writes it.

The file runs whatever it says, with the same trust as ``config.ini``, and
nothing in it runs until it is picked from the menu.  A command line can
carry a token, so the file is created readable by its owner only.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass

from . import presets
from .config import _private_write, config_dir

HEADER = """\
; Meridian Commander saved commands, one section per command.
; Written by the app (press &), and safe to edit by hand.
; Each runs with your shell, in its directory, or in the active pane's
; directory when that is blank.  For example:
;
;   [http]
;   command = python3 -m http.server 8000
;   directory =

"""


def commands_path() -> str:
    return os.path.join(config_dir(), "commands.ini")


#: The rules a name must keep are the presets': an INI section name.
valid_name = presets.valid_name


@dataclass
class SavedCommand:
    """One saved command: what to run, and where."""

    name: str
    command: str
    #: The directory it runs in; blank for the active pane's.
    directory: str = ""

    def label(self) -> str:
        """The command line, and where it runs if it says, for a menu."""
        if self.directory:
            return f"{self.command}  (in {self.directory})"
        return self.command


def in_order(items: list[SavedCommand]) -> list[SavedCommand]:
    """``items`` sorted by name, case-insensitively, as the presets are."""
    return sorted(items, key=lambda item: (item.name.lower(), item.name))


def load(path: str | None = None) -> list[SavedCommand]:
    """Read the file.  A missing or broken one yields no commands."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path or commands_path())
    except (OSError, configparser.Error):
        return []
    return in_order([
        SavedCommand(name=name,
                     command=parser[name].get("command", "") or "",
                     directory=parser[name].get("directory", "") or "")
        for name in parser.sections()
    ])


def save(items: list[SavedCommand], path: str | None = None) -> str:
    """Write ``items`` out, replacing the file.  Returns the path written."""
    target = path or commands_path()
    parser = configparser.ConfigParser(interpolation=None)
    # Sections are written in the order they are added, so sorting here is
    # what keeps the file itself alphabetical.
    for item in in_order(items):
        parser[item.name] = {"command": item.command,
                             "directory": item.directory}
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _private_write(target) as handle:
        handle.write(HEADER)
        parser.write(handle)
    return target


def add(item: SavedCommand, items: list[SavedCommand]) -> list[SavedCommand]:
    """``items`` with ``item`` stored, replacing any entry of that name."""
    return in_order([i for i in items if i.name != item.name] + [item])


def replace(old_name: str, item: SavedCommand,
            items: list[SavedCommand]) -> list[SavedCommand]:
    """``items`` with the entry called ``old_name`` changed into ``item``.

    A rename onto a name already in use replaces that entry too, so the
    list never holds two of one name -- the file could not.
    """
    kept = [i for i in items if i.name not in (old_name, item.name)]
    return in_order(kept + [item])


def remove(name: str, items: list[SavedCommand]) -> list[SavedCommand]:
    """``items`` without the entry called ``name``."""
    return in_order([i for i in items if i.name != name])
