# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The Meridian Commander application: a two-pane terminal file manager.

This module wires the pieces together -- two :class:`~meridian_commander.panel.Panel`
objects, the filesystem backends, the viewer/editor, dialogs and the file
operations -- into a curses event loop with the keys of Midnight Commander and
the face of Turbo Vision.

The screen is laid out the way a Borland IDE laid one out: a grey menu bar
along the top with a clock in its right-hand corner, a shaded blue desktop
underneath it, two framed windows on that desktop (the active one drawn with a
double-line frame, the other single), a hint line, and the F-key bar along the
bottom.  :mod:`meridian_commander.theme` owns the colours and the chrome; this
module says what goes where.

Key bindings (also shown in the F1 help screen)::

    Tab            switch active pane          F1   help
    Up/Down        move cursor                 F2   open / connect location
    PgUp/PgDn      page                        F3   view (md/xlsx/tsv/docx/...)
    Home/End       first / last                F4   edit file
    Enter / Right  enter dir/archive/view      F5   copy  ->  other pane
    Backspace/Left parent directory            F6   move  ->  other pane
    Insert / Space tag file                    F7   make directory
    + / \\          tag / untag by pattern      F8   delete
    - / *          untag all / invert the tags
    Ctrl-U         swap panes                  F9   synchronize panes
    Ctrl-R         reload both panes           F10  quit
    Ctrl-G         go to path                  Esc  the menu bar
    Ctrl-T         change sort order           Alt+F/C/O/H  a menu by name
    x              extract archive -> other pane
    < / >          back / forward through this pane's history
    ~              home directory (this pane)
    T              the trash (this pane)
    =              other pane: same location as this one
    b              presets: saved locations to return to
    z              single pane: one pane at full width, the other kept
    /              search this pane: type a name, the cursor jumps to it
"""

from __future__ import annotations

import curses
import locale
import os
import posixpath
import shlex
import shutil
import stat as stat_mod
import subprocess
import sys
import time

from . import dialogs, extedit, presets, runner, shellinit, theme
from .attributes import AttributeChange, apply_change, common_bits
from .editor import Editor
from .filesystems import (
    FileSystem,
    FileSystemError,
    FTPFileSystem,
    LocalFileSystem,
    SFTPFileSystem,
    SSHFileSystem,
    _split_user_host,
)
from .operations import (
    OVERWRITE,
    SKIP,
    OperationCancelled,
    TransferReport,
    copy_contents,
    copy_path,
    count_tree,
    move_path,
    overlapping,
    same_place,
)
from .archive import ArchiveFileSystem, archive_stem, is_archive, open_archive
from .browsers import has_own_browser, viewer_for
from .compare import Comparison
from .panel import Panel, Search
from . import usage
from .peek import PeekPane
from .quickview import QuickViewPane
from .jobs import RULES, Job, JobQueue
from .jobspane import JobsPane
from .opener import SystemOpener
from .trash import (
    TrashUnavailable,
    forget,
    move_to_trash,
    trash_location,
    trash_root,
)
from .sync import (
    MTIME_TOLERANCE,
    build_sync_plan,
    execute_sync_plan,
    survey_directory,
)
from .util import human_size, human_time, ljust, read_key, rjust, typed_char
from .viewpane import ProgramPane, ViewPane

#: How long curses waits after Esc for the rest of a key's sequence, in ms.
ESCDELAY_MS = 25


def _cwd(fs: FileSystem) -> str:
    """The directory the command was run from, or the backend's home.

    A working directory can be deleted out from under a running shell, in which
    case asking for it raises rather than answering.  Falling back to home keeps
    that from being a crash on startup, which would be an absurd way to lose an
    application over a directory the user was not going to look at anyway.
    """
    try:
        return os.getcwd()
    except OSError:
        return fs.home()


# ---------------------------------------------------------------------------
# The menu bar
# ---------------------------------------------------------------------------
#
# Item keys: ``label`` is the caption with its ``~H~ot`` marker, ``name`` is
# the action :meth:`App._dispatch` looks up, ``key`` is the shortcut text shown
# right-aligned, and ``sep`` draws a rule.  Every entry names an action that a
# key binding also reaches, so the menu is a second way to everything rather
# than a place where features hide.

MENUS: list[dict] = [
    {
        "label": "≡", "name": "system",
        "items": [
            {"label": "~A~bout...", "name": "about"},
            {"sep": True},
            {"label": "~C~olours...", "name": "colours"},
        ],
    },
    {
        "label": "~F~ile", "name": "file",
        "items": [
            {"label": "~V~iew", "name": "view", "key": "F3"},
            {"label": "~E~dit", "name": "edit", "key": "F4"},
            {"label": "Ru~n~...", "name": "run", "key": "Enter"},
            {"label": "~C~opy", "name": "copy", "key": "F5"},
            {"label": "~M~ove", "name": "move", "key": "F6"},
            {"label": "Extr~a~ct archive", "name": "extract", "key": "x"},
            {"label": "~R~ename", "name": "rename", "key": "r"},
            {"sep": True},
            {"label": "Make ~d~irectory", "name": "mkdir", "key": "F7"},
            {"label": "Ne~w~ file", "name": "touch", "key": "n"},
            {"label": "Attri~b~utes...", "name": "attributes", "key": "a"},
            {"label": "Lin~k~...", "name": "link", "key": "l"},
            {"label": "~O~pen with system app", "name": "open_with",
             "key": "w"},
            {"label": "De~l~ete", "name": "delete", "key": "F8"},
            {"label": "~T~ag / untag", "name": "tag", "key": "Ins"},
            {"label": "Tag ~g~roup...", "name": "tag_group", "key": "+"},
            {"label": "~U~ntag group...", "name": "untag_group", "key": "\\"},
            {"label": "~I~nvert tags", "name": "invert", "key": "*"},
            {"sep": True},
            {"label": "~F~ind files...", "name": "find"},
            {"label": "E~x~it", "name": "quit", "key": "F10"},
        ],
    },
    {
        "label": "~C~ommand", "name": "command",
        "items": [
            {"label": "~O~pen location...", "name": "connect", "key": "F2"},
            {"label": "~P~resets...", "name": "presets", "key": "b"},
            {"label": "~G~o to path...", "name": "goto", "key": "Ctrl+G"},
            {"label": "~H~ome directory", "name": "home", "key": "~"},
            {"label": "Trash fol~d~er", "name": "open_trash", "key": "T"},
            {"label": "~B~ack", "name": "back", "key": "<"},
            {"label": "~F~orward", "name": "forward", "key": ">"},
            {"label": "Search by ~n~ame", "name": "search", "key": "/"},
            {"label": "Filt~e~r...", "name": "filter", "key": "F"},
            {"sep": True},
            {"label": "~S~wap panes", "name": "swap", "key": "Ctrl+U"},
            {"label": "~R~eload panes", "name": "reload", "key": "Ctrl+R"},
            {"label": "Sa~m~e location in other pane", "name": "mirror",
             "key": "="},
            {"label": "S~y~nchronize panes", "name": "sync", "key": "F9"},
            {"label": "~C~ompare files side by side", "name": "compare",
             "key": "D"},
            {"label": "He~a~d + tail in this pane", "name": "peek",
             "key": "h"},
            {"label": "~Q~uick view in the other pane", "name": "quick_view",
             "key": "Ctrl+Q"},
            {"label": "~J~obs in this pane", "name": "jobs", "key": "J"},
            {"sep": True},
            {"label": "~T~erminal in this pane", "name": "terminal", "key": "t"},
            {"label": "Full-screen she~l~l", "name": "shell", "key": "!"},
            {"label": "Plug-~i~ns...", "name": "plugins", "key": "p"},
        ],
    },
    {
        "label": "~O~ptions", "name": "options",
        "items": [
            {"label": "~S~ort order...", "name": "sort", "key": "Ctrl+T"},
            {"label": "Show ~h~idden files", "name": "hidden", "key": "."},
            {"label": "Director~y~ sizes", "name": "sizes", "key": "u"},
            {"label": "Single ~p~ane", "name": "single", "key": "z"},
            {"label": "~L~ong listing", "name": "long"},
            {"label": "~T~rash on delete", "name": "trash"},
            {"label": "View and ed~i~t in a pane", "name": "open_in_pane"},
            {"sep": True},
            {"label": "~C~olours...", "name": "colours"},
            {"label": "~E~ditor...", "name": "editor"},
            {"label": "~V~iewer...", "name": "viewer"},
            {"label": "Confi~g~uration...", "name": "config", "key": "C"},
        ],
    },
    {
        "label": "~H~elp", "name": "help",
        "items": [
            {"label": "~C~ontents", "name": "help", "key": "F1"},
            {"sep": True},
            {"label": "~A~bout...", "name": "about"},
        ],
    },
]


#: What Options > Editor offers, as (``[ui] editor`` value, label). The blank
#: one is the default and has to be on the list: choosing an outside editor
#: has to be undoable from the same menu that made the change.
EDITOR_CHOICES: list[tuple[str, str]] = [
    ("", "Built-in editor"),
    ("vi", "vi"),
    ("vim", "vim"),
    ("nano", "nano"),
    ("$EDITOR", "$EDITOR (whatever the environment names)"),
]

#: What Options > Viewer offers, as (``[ui] viewer`` value, label).  The same
#: shape as the editor's list, blank default included, because it answers the
#: same question about the other half of the pair.
VIEWER_CHOICES: list[tuple[str, str]] = [
    ("", "Built-in viewer"),
    ("less", "less"),
    ("less -R", "less -R (keep colour escapes)"),
    ("more", "more"),
    ("$PAGER", "$PAGER (whatever the environment names)"),
]


#: The answers to "the target is already there", as (label, key, rule, for
#: the rest of the transfer too).  ``older`` overwrites only a target older
#: than its source; ``None`` is Cancel.
CONFLICT_CHOICES: list[tuple[str, str, str | None, bool]] = [
    ("Overwrite", "o", OVERWRITE, False),
    ("Overwrite all", "a", OVERWRITE, True),
    ("Overwrite if older", "i", "older", False),
    ("Overwrite all older", "l", "older", True),
    ("Skip", "s", SKIP, False),
    ("Skip all", "n", SKIP, True),
    ("Cancel", "c", None, False),
]


class TransferConflicts:
    """Asks before a transfer replaces something, and remembers "all".

    One per transfer: an "all" answer holds for the rest of it -- every item
    tagged, and everything inside a directory being copied -- and for nothing
    after it.  Handed to :func:`~meridian_commander.operations.copy_path` and
    :func:`~meridian_commander.operations.move_path` as their ``on_conflict``.
    """

    def __init__(self, stdscr, verb: str) -> None:
        self.stdscr = stdscr
        self.verb = verb
        #: The answer given for all, once there is one.
        self.rule: str | None = None

    def __call__(self, target: str, source, existing) -> str:
        rule = self.rule
        if rule is None:
            rule = self._ask(target, source, existing)
        if rule == "older":
            return OVERWRITE if self.older(existing, source) else SKIP
        return rule

    @staticmethod
    def older(existing, source) -> bool:
        """Whether ``existing`` is older than ``source`` by more than slack.

        The same two seconds sync allows, for filesystems that keep times
        coarsely; and a time either side cannot report is not older, so the
        doubtful case keeps what is there.
        """
        if existing.mtime is None or source.mtime is None:
            return False
        return source.mtime - existing.mtime > MTIME_TOLERANCE

    def _ask(self, target: str, source, existing) -> str:
        name = os.path.basename(target.rstrip("/\\")) or target
        if existing.mtime is None or source.mtime is None:
            age = ""
        elif self.older(existing, source):
            age = "   older"
        elif self.older(source, existing):
            age = "   newer"
        else:
            age = "   same age"
        text = (f"{name} is already there.\n"
                f"Replace with: {human_size(source.size).strip():>7}  "
                f"{human_time(source.mtime)}\n"
                f"Existing:     {human_size(existing.size).strip():>7}  "
                f"{human_time(existing.mtime)}{age}")
        choice = dialogs.menu(self.stdscr, f"{self.verb}: already there",
                              [label for label, _, _, _ in CONFLICT_CHOICES],
                              keys=[key for _, key, _, _ in CONFLICT_CHOICES],
                              text=text)
        self._uncover()
        if choice is None:
            raise OperationCancelled()
        _, _, rule, for_all = CONFLICT_CHOICES[choice]
        if rule is None:
            raise OperationCancelled()
        if for_all:
            self.rule = rule
        return rule

    def _uncover(self) -> None:
        """Repaint what the question covered.

        It is taller than the progress window it opened over, which redraws
        only itself, so the question's top and bottom rows would otherwise
        stay on the screen until the transfer ended.
        """
        try:
            self.stdscr.touchwin()
            self.stdscr.noutrefresh()
        except (AttributeError, curses.error):
            pass   # no screen to repaint: a stand-in in a test


def _is_local(fs) -> bool:
    return getattr(fs, "scheme", "local") == "local"


def menu_layout() -> list[tuple[int, int]]:
    """Where each menu's caption sits on the bar, as (start column, width).

    Worked out from the captions alone so a mouse click can be turned back
    into a menu index without having drawn anything first.
    """
    spans = []
    col = 1
    for entry in MENUS:
        width = len(theme.strip_hotkey(entry["label"])) + 2
        spans.append((col, width))
        col += width
    return spans


class App:
    def __init__(self, stdscr, left_path: str | None = None,
                 right_path: str | None = None, single: bool = False) -> None:
        self.stdscr = stdscr
        self._backends: list[FileSystem] = []
        from . import config as _config

        #: Whether panes show a mode and an owner column (Options).
        self.long_listing = _config.long_listing()
        #: Whether F8 on a local pane offers the trash first (Options).
        self.use_trash = _config.trash_enabled()
        #: Whether F3 and F4 open in a pane rather than full-screen (Options).
        self.open_in_pane = _config.open_in_pane()
        #: Whether the last draw showed the cursor, for the editor in a pane.
        self._cursor_shown = False
        #: Hands files to the desktop (``w``), keeping its copies for the run.
        self.opener = SystemOpener()
        #: Copies and moves running in the background (MC-020).
        self.jobs = JobQueue()
        #: Quit once the last background job has ended.
        self._quit_when_done = False

        left_fs = LocalFileSystem()
        right_fs = LocalFileSystem()
        self._backends += [left_fs, right_fs]
        # With no paths given, the left pane opens where the command was run
        # from: you cd somewhere, type meridian, and the directory you were
        # already thinking about is the one under the cursor.  The right pane
        # stays at home, so the pair is "here" and "somewhere to put things"
        # rather than the same directory twice.
        self.left = Panel(left_fs, left_fs.normpath(left_path or _cwd(left_fs)))
        self.right = Panel(right_fs, right_fs.normpath(right_path or right_fs.home()))
        self.active = self.left
        self.message = ("Esc menu   Tab switch pane   F1 Help   "
                        "F9 Sync   F10 Quit   right-click: actions")
        self.running = True
        # (panel, y, x, h, w) rectangles, refreshed on every draw for the mouse.
        self._panel_boxes: list[tuple] = []
        # The menu whose caption is highlighted while its drop-down is open.
        self._open_menu: int | None = None
        # Single mode: the active pane fills the desktop and the other is kept
        # out of sight -- not closed, so its place, connection, history, tags
        # and any plug-in are all still there when it comes back.
        self.single = single
        # A pane single mode keeps on screen beside the active one: a
        # terminal that handed the keyboard over with Ctrl-], until the
        # keyboard comes back to it.  See _panes_on_screen.
        self._beside: Panel | None = None
        # The pane whose plug-in turned single mode on (the terminal's
        # Ctrl-] z), or None.  That was the plug-in's zoom, so closing the
        # plug-in ends it; z or the menu takes it over, and then it stays.
        self._zoomed_by: Panel | None = None
        # The search mode "/" turns on, or None.  Only the active pane can be
        # searching, and every key that is not the mode's own ends it before
        # doing what it always does, so it never outlives the pane's focus.
        self.search: Search | None = None
        # The hint line from before the search, put back when it ends.
        self._message_before_search = ""

    # -- helpers ----------------------------------------------------------
    @property
    def other(self) -> Panel:
        return self.right if self.active is self.left else self.left

    def _set_message(self, text: str) -> None:
        self.message = text

    def _reload_after_change(self, panel: Panel, keep_name: str | None = None,
                             gone: tuple[str, ...] = ()) -> None:
        """Reload ``panel``, and the other pane when the change reached it too.

        Both panes on one directory is not a corner case: it is what ``=``
        makes, and what a preset reusing a live connection makes.  Only the
        pane doing the work was reloaded, so a file deleted in one pane stayed
        listed in the other -- where Enter on it then failed, and F5 offered to
        copy something that was no longer there -- and a file created here
        never appeared there at all.

        ``gone`` names paths that have just been removed: a pane standing
        *inside* one of them is looking at a directory that no longer exists,
        which is worth the reload as much as sharing the directory is.  A pane
        somewhere else, or on another backend, is not touched -- the check is
        the same one a transfer uses to refuse copying into itself, so a remote
        pane costs no round trip for a change on this machine.
        """
        panel.refresh(keep_name=keep_name)
        other = self.right if panel is self.left else self.left
        for path in (panel.path, *gone):
            if overlapping(panel.fs, path, other.fs, other.path):
                other.refresh()
                return

    # -- the layout -------------------------------------------------------
    def _pairing_plugin(self):
        """An open plug-in that works on the other pane, or None.

        Such a plug-in is half of a pair -- head + tail follows the other
        pane's cursor, Grep searches the other pane's directory -- so single
        mode shows both panes for as long as one is open, rather than leave
        it working on a pane nobody can see.  ``uses_other_pane`` defaults to
        true, so a plug-in that does not say is given both panes: the safe
        answer for user plug-ins, and one that asks nothing new of them.
        """
        for panel in (self.active, self.other):
            plugin = panel.plugin
            if plugin is not None and getattr(plugin, "uses_other_pane", True):
                return plugin
        return None

    def _panes_on_screen(self) -> list[Panel]:
        """The panes drawn, left to right: both, or the active one alone.

        Single mode still draws both while something needs the pair: a
        plug-in working on the other pane, or a terminal kept beside the
        pane it handed the keyboard to.  Worked out afresh at every call
        rather than switched on and off, so no way of closing a plug-in or
        moving the keyboard can leave the layout stuck in the wrong state.
        """
        if self._beside is self.active:
            # The keyboard is back in the pane that was kept in view, so the
            # errand in the other pane is over and this one fills the screen.
            self._beside = None
        if (not self.single or self._beside is not None
                or self._pairing_plugin() is not None):
            return [self.left, self.right]
        return [self.active]

    def _hidden(self, panel: Panel) -> bool:
        """Whether single mode is keeping ``panel`` off the screen."""
        return all(shown is not panel for shown in self._panes_on_screen())

    def _keep_beside(self, panel: Panel) -> None:
        """Keep ``panel`` on screen while the keyboard is in the other pane.

        Called when a plug-in hands the keyboard over (the terminal's
        Ctrl-]): in single mode the pane taking the keyboard would otherwise
        replace the terminal on screen, and the point of Ctrl-] is to work in
        one pane with the shell still in sight.  Two panes show both anyway.
        """
        if self.single:
            self._beside = panel

    def _toggle_single(self, zoomed_by: Panel | None = None) -> None:
        """Switch between two panes and one, keeping the other in reserve.

        ``zoomed_by`` is the pane whose plug-in asked, from inside a plug-in
        that takes every key (the terminal's Ctrl-] z): one pane turned on
        that way goes back to two when that plug-in closes.
        """
        self.single = not self.single
        self._beside = None
        self._zoomed_by = zoomed_by if self.single else None
        if not self.single:
            self._set_message("Two panes")
            return
        pairing = self._pairing_plugin()
        if pairing is not None:
            name = getattr(pairing, "name", "") or "a plug-in"
            self._set_message(f"Single pane -- both stay on screen while "
                              f"{name} works on the other pane")
        elif zoomed_by is not None:
            # Tab and z belong to the program in there; only Ctrl-] is not.
            self._set_message("Single pane -- Ctrl-] z shows both")
        else:
            # Short enough to sit beside the hidden pane's note on an
            # 80-column hint line without being cut off.
            self._set_message("Single pane -- Tab flips, z shows both")

    def _hidden_note(self, hidden: Panel, width: int) -> str:
        """The hint line's note of where the pane out of sight is.

        Half the line at most, so the message keeps the rest; the location is
        trimmed from the left, as a caption is, because the deepest directory
        is the part that says which one it is.  On a very narrow screen it
        comes down to which side is hidden.
        """
        side = "left" if hidden is self.left else "right"
        prefix = f"{side} pane hidden: "
        where = f"{hidden.fs.label()}:{hidden.path}"
        room = min(width - 2, max((width - 2) // 2, 12))
        if len(prefix) + len(where) <= room:
            return prefix + where
        keep = room - len(prefix) - 1
        if keep >= 8:
            return prefix + "~" + where[-keep:]
        return f"{side} hidden"[:room]

    def _destination(self, panel: Panel) -> str:
        """How a prompt names a destination pane that is out of sight.

        Empty for a pane on screen, whose caption already says where it is.
        Otherwise it names the pane and its backend, in front of the path the
        prompt is filled with, so the two read as the location the way a
        caption does: ``the hidden pane, sftp://web1:`` then ``/srv/www``.
        """
        if not self._hidden(panel):
            return ""
        return f" the hidden pane, {panel.fs.label()}"

    # -- drawing ----------------------------------------------------------
    def draw(self) -> None:
        stdscr = self.stdscr
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 8 or width < 24:
            # Through paint(), which trims to the width and swallows the
            # error curses raises at the bottom-right cell: writing this
            # eighteen-character sentence straight into a four-column
            # terminal ended the application with a traceback, while it was
            # explaining that the terminal was too small.
            theme.paint(stdscr, 0, 0, "Terminal too small", "panel", width)
            stdscr.noutrefresh()
            curses.doupdate()
            return

        # Row 0 is the menu bar, the last row the F-key bar, and the one above
        # it the hint line; the desktop is everything in between.
        panel_y = 1
        panel_h = height - 3

        self._draw_desktop(panel_y, height - 2, width)
        self._draw_menu_bar(width)

        # Remember each pane's screen rectangle so the mouse handler can map a
        # click back to a pane and a row.  Only panes on screen get one, so a
        # click in single mode can only ever reach the pane it landed on.
        shown = self._panes_on_screen()
        if len(shown) == 2:
            left_w = width // 2
            self._panel_boxes = [
                (self.left, panel_y, 0, panel_h, left_w),
                (self.right, panel_y, left_w, panel_h, width - left_w),
            ]
        else:
            self._panel_boxes = [(shown[0], panel_y, 0, panel_h, width)]

        for panel, py, px, ph, pw in self._panel_boxes:
            if panel.plugin is not None:
                try:
                    panel.plugin.draw(stdscr, py, px, ph, pw)
                except Exception as exc:
                    # A broken draw() must not take down the whole UI.
                    theme.paint(stdscr, py + 1, px + 1,
                                f"plugin draw error: {exc}"[: pw - 2], "panel")
            else:
                self._draw_panel(panel, py, px, ph, pw,
                                 active=self.active is panel)

        # The hint line sits on the desktop rather than in a bar of its own,
        # which is where Turbo Vision put anything that was not chrome.  In
        # single mode its right-hand end says that a second pane exists, and
        # where it is: a pane nobody can see is easy to forget is there.
        room = width - 2
        right = width - 1
        if len(shown) == 1:
            note = self._hidden_note(self.other, width)
            right -= len(note)
            theme.paint(stdscr, height - 2, right, note, "hint")
            room -= len(note) + 1
        status = self.jobs.status()
        if status and len(status) + 2 < room // 2:
            right -= len(status) + 1
            theme.paint(stdscr, height - 2, right, status, "hint")
            room -= len(status) + 1
        theme.paint(stdscr, height - 2, 1, self.message[:room], "hint")
        self._draw_function_bar(height - 1, width)
        # The editor in a pane has a cursor; everything else draws its own
        # or has none.  Last, so nothing written after it moves it.  The
        # cursor is hidden again only after being shown here: a draw that
        # never shows one leaves the cursor, and the library, alone.
        where = getattr(self.active.plugin, "cursor", None)
        spot = where() if where is not None else None
        if spot is not None:
            stdscr.move(*spot)
            curses.curs_set(1)
            self._cursor_shown = True
        elif self._cursor_shown:
            curses.curs_set(0)
            self._cursor_shown = False
        stdscr.noutrefresh()
        curses.doupdate()

    def _draw_desktop(self, top: int, bottom: int, width: int) -> None:
        """Fill the desktop with the light-shade texture, as the IDEs did."""
        for row in range(top, bottom + 1):
            theme.fill(self.stdscr, row, 0, width, "desktop",
                       theme.glyph("shade"))

    def _draw_menu_bar(self, width: int) -> None:
        """The grey bar along the top: the menus, then the clock."""
        stdscr = self.stdscr
        theme.fill(stdscr, 0, 0, width, "menu")
        for index, ((start, span), entry) in enumerate(
                zip(menu_layout(), MENUS)):
            if start + span > width:
                break
            selected = index == self._open_menu
            role = "menusel" if selected else "menu"
            hot = "menuselhot" if selected else "menuhot"
            theme.fill(stdscr, 0, start, span, role)
            theme.paint_caption(stdscr, 0, start + 1, entry["label"], role, hot)
        clock = time.strftime("%H:%M")
        if width > 30:
            theme.paint(stdscr, 0, width - len(clock) - 2, clock, "clock")

    def _draw_panel(self, panel: Panel, y: int, x: int, h: int, w: int,
                    active: bool) -> None:
        """One pane, drawn as a Turbo Vision window.

        The active pane gets the double-line frame and the yellow caption; the
        other is drawn single and grey.  Which pane has the keyboard is
        therefore legible from the shape of the border alone, which is what
        the IDEs were doing before anyone could rely on colour.
        """
        stdscr = self.stdscr
        body_h = h - 3            # caption row, column titles, footer row
        inner_w = w - 2
        if body_h < 1 or inner_w < 8:
            return
        panel.ensure_visible(body_h)

        # Caption: filesystem label + current path.  Long paths keep their
        # tail visible (the deepest directories matter most), and are trimmed
        # before the frame sees them so the frame can still centre them.
        header = f"{panel.fs.label()}:{panel.path}"
        note = self._filter_note(panel)
        room = max(8, inner_w - 8 - len(note))
        if len(header) > room:
            header = "~" + header[-(room - 1):]
        header += note

        search = self.search
        if search is not None and search.panel is panel:
            # The frame writes the footer between two caps and a space either
            # side, from two columns in, so this is what fits inside it.
            footer, footer_role = self._search_footer(search, w - 7)
        else:
            footer = self._panel_footer(panel)
            footer_role = "panelerror" if panel.error else "panelinfo"
        theme.frame(stdscr, y, x, h, w,
                    "frame" if active else "framenc",
                    title=header, title_role="title" if active else "titlenc",
                    double=active, close=False,
                    footer=footer, footer_role=footer_role)

        # The long listing puts a mode and an owner between the size and the
        # time, and the time gives way first when the pane is too narrow for
        # all of them: at 80 columns and two panes, a name is worth more than
        # a date.
        long = self.long_listing
        show_last = not long or inner_w - 27 - 13 >= 16
        name_w = max(4, inner_w - (27 if long else 7) - (13 if show_last else 0))
        # With sizes on, the Modify column gives its room to a bar: hunting
        # for space is the one job where a file's age is the least useful
        # thing on the row and its share of the directory is the most.
        last_col = "Share" if panel.show_sizes else "Modify time"
        title = (f" {'Name'.ljust(name_w)}{'Size':>6}"
                 + (f" {'Mode':<10} {'Owner':<8}" if long else "")
                 + (f" {last_col:>12}" if show_last else ""))
        theme.paint(stdscr, y + 1, x + 1, title, "panelhead", inner_w)
        biggest = panel.biggest() if panel.show_sizes else 0

        for row in range(body_h):
            idx = panel.top + row
            ry = y + 2 + row
            if idx >= len(panel.entries):
                theme.fill(stdscr, ry, x + 1, inner_w, "panel")
                continue
            entry = panel.entries[idx]
            is_cursor = active and idx == panel.cursor
            tagged = entry.name in panel.selected

            name = entry.name
            if name == Panel.PARENT:
                display = ".."
            elif entry.is_dir:
                display = name + "/"
            else:
                display = name
            marker = " "
            if entry.is_symlink:
                marker = theme.glyph("link")
            if tagged:
                marker = theme.glyph("tag")

            size = panel.size_of(entry)
            if entry.is_dir and size is None:
                size_s = " <DIR>"
            else:
                size_s = human_size(size)
            if panel.show_sizes:
                tail = usage.bar(size or 0, biggest, 12,
                                 theme.glyph("block"), theme.glyph("shade"))
            else:
                tail = f"{human_time(entry.mtime)[:12]:>12}"
            line = f"{marker}{ljust(display, name_w)}{rjust(size_s, 6)}"
            if long:
                line += f" {self._mode_text(entry)} {ljust(entry.owner, 8)}"
            if show_last:
                line += f" {tail}"

            if is_cursor:
                role = ("panelcursortag" if tagged else
                        "panelcursordir" if entry.is_dir else "panelcursor")
            elif tagged:
                role = "paneltag"
            elif entry.is_symlink:
                role = "panellink"
            elif entry.is_dir:
                role = "paneldir"
            else:
                role = "panel"
            if panel.show_sizes and not is_cursor and panel.sizing(entry):
                role = "panelinfo"
            theme.paint(stdscr, ry, x + 1, line, role, inner_w)

        # The scrollbar rides the right-hand border, which is where a Turbo
        # Vision window kept it.
        theme.scrollbar(stdscr, y + 2, x + w - 1, body_h, panel.top, body_h,
                        len(panel.entries),
                        role="scroll" if active else "framenc")

    @staticmethod
    def _mode_text(entry) -> str:
        """``-rwxr-xr-x`` for the long listing, blank where nothing is known."""
        if entry.name == Panel.PARENT or not entry.mode:
            return " " * 10
        return stat_mod.filemode(entry.mode)

    def _panel_footer(self, panel: Panel) -> str:
        """The text along a pane's bottom frame.

        Whatever is worth knowing about the pane right now, in order: the
        error that stopped it being listed, what is tagged, what is under the
        cursor, or how much is in the directory.
        """
        if panel.error:
            return f"! {panel.error}"
        if panel.selected:
            total = sum(e.size or 0 for e in panel.listed
                        if e.name in panel.selected)
            return f"{len(panel.selected)} tagged, {human_size(total).strip()}"
        cur = panel.current()
        if cur and cur.name != Panel.PARENT and not cur.is_dir:
            return f"{cur.name}  {human_size(cur.size).strip()}"
        return f"{len(panel.entries) - (0 if panel._at_root() else 1)} items"

    @staticmethod
    def _search_footer(search: Search, room: int) -> tuple[str, str]:
        """The pane's footer while it is being searched, and its colour.

        The pattern and where the cursor stands among its matches, or that
        there are none.  Too long for the pane, the pattern loses its start
        rather than its end -- the end is what was just typed, and the count
        after it is the answer -- as a caption keeps the deepest directory.
        In a pane too narrow even for that, the end of the pattern alone.
        """
        pattern = search.pattern
        which, count = search.position()
        if not pattern:
            status, role = "type a name", "panelinfo"
        elif count:
            status, role = f"{which} of {count}", "panelinfo"
        else:
            status, role = "not found", "panelerror"
        text = f"/ {pattern}  {status}"
        if len(text) > room:
            keep = room - len(status) - 5         # "/ ~" and the two spaces
            if keep > 0:
                text = f"/ ~{pattern[-keep:]}  {status}"
            else:
                text = ("/" + pattern)[-room:]
        return text, role

    def _draw_function_bar(self, y: int, width: int) -> None:
        keys = [
            ("1", "Help"), ("2", "Conn"), ("3", "View"), ("4", "Edit"),
            ("5", "Copy"), ("6", "Move"), ("7", "Mkdir"), ("8", "Del"),
            ("9", "Sync"), ("10", "Quit"),
        ]
        theme.fill(self.stdscr, y, 0, width, "keybar")
        seg = max(1, width // len(keys))
        x = 0
        for num, label in keys:
            if x >= width:
                break
            room = max(0, seg - len(num))
            theme.paint(self.stdscr, y, x, str(num), "keybarkey")
            theme.paint(self.stdscr, y, x + len(num),
                        label.ljust(room)[:room], "keybar")
            x += seg

    # -- main loop --------------------------------------------------------
    #: How long getch() waits for a key before the loop pumps work again.
    #: A terminal plug-in only needs its output collected often enough to look
    #: live; a directory walk is *paced* by this number, because it gets one
    #: slice of work per poll -- so waiting the plug-in's 120 ms between 15 ms
    #: slices would leave it working one eighth of the time.
    PLUGIN_POLL_MS = 120
    #: How often the status line's job counter moves while jobs run.
    JOBS_POLL_MS = 500
    #: How long quitting waits for cancelled jobs to stop.
    JOBS_GRACE = 5.0
    SIZING_POLL_MS = 5

    def _tick_plugins(self) -> int | None:
        """Pump plug-ins and directory walks that have work outstanding.

        Returns the milliseconds ``getch()`` should wait for a key, or None to
        block until one arrives.  Whoever wants the tighter loop wins: a pane
        that is still counting is doing real work between polls, while a
        terminal is only being drained.
        """
        poll: int | None = None
        jobs_running = bool(self.jobs.jobs) and self._tick_jobs()
        for panel in (self.left, self.right):
            # A pane counting what is under its subdirectories is work in
            # progress too, and wants the same polling loop: that is what
            # keeps the totals growing while nobody presses anything.
            if panel.step_sizing():
                poll = self.SIZING_POLL_MS
            plugin = panel.plugin
            if plugin is not None and getattr(plugin, "wants_timer", False):
                if poll is None:
                    poll = self.PLUGIN_POLL_MS
                tick = getattr(plugin, "tick", None)
                if tick is not None:
                    try:
                        tick()
                    except Exception as exc:
                        self._set_message(f"Plugin error: {exc}")
                if getattr(plugin, "wants_close", False):
                    # An outside pager or editor in a pane has exited: the
                    # pane closes, as the full-screen run would have ended.
                    self._end_plugin(panel)
                    continue
                # A plug-in that is busy -- the terminal while output flows or
                # a key has just gone to the shell -- may ask for a quicker
                # poll, so an echo shows at once rather than on the next
                # idle one.
                wanted = getattr(plugin, "poll_ms", None)
                if isinstance(wanted, int) and wanted < poll:
                    poll = wanted
        if jobs_running and poll is None:
            poll = self.JOBS_POLL_MS
        return poll

    def _tick_jobs(self) -> bool:
        """Take in what the background jobs have done; whether any are left.

        The jobs run on threads of their own and never draw: this is where the
        main thread notices one has ended, reloads the panes it changed and
        says so on the status line.  Errors are not shown here -- they stay
        with the job, in the jobs list (``J``), rather than popping up over
        whatever is on the screen when they happen.
        """
        for job in self.jobs.tick():
            for panel in (self.left, self.right):
                if panel.fs is job.src_pane_fs or panel.fs is job.dst_pane_fs:
                    panel.refresh()
            text = f"Job {job.number} {job.state}: {job.title()}"
            if job.errors:
                count = len(job.errors)
                text += f" -- {count} error{'' if count == 1 else 's'}, J to read"
            elif job.note:
                text += f" -- {job.note}"
            self._set_message(text)
        if self.jobs.active():
            return True
        if self._quit_when_done:
            self.running = False
        return False

    def run(self) -> None:
        curses.curs_set(0)
        while self.running:
            poll = self._tick_plugins()
            if not self.running:          # the last job ended: quit, as asked
                break
            self.stdscr.timeout(-1 if poll is None else poll)
            self.draw()
            try:
                key = read_key(self.stdscr)
            except KeyboardInterrupt:
                if not self._may_quit():
                    continue
                if self.jobs.active():
                    self._quit_with_jobs()
                    if not self.running:
                        break
                    continue
                if dialogs.confirm(self.stdscr, "Quit", "Exit Meridian Commander?"):
                    break
                continue
            if key == -1:      # poll timeout: loop to pump plugin output
                continue
            self.handle_key(key)
        self.stdscr.timeout(-1)
        self._close_backends()

    # -- the menu bar -------------------------------------------------------
    def _menu_items(self, index: int) -> list[dict]:
        """One menu's entries, with the state-dependent ticks filled in."""
        items = []
        for item in MENUS[index]["items"]:
            entry = dict(item)
            if entry.get("name") == "hidden":
                entry["checked"] = self.active.show_hidden
            elif entry.get("name") == "single":
                entry["checked"] = self.single
            elif entry.get("name") == "long":
                entry["checked"] = self.long_listing
            elif entry.get("name") == "trash":
                entry["checked"] = self.use_trash
            elif entry.get("name") == "open_in_pane":
                entry["checked"] = self.open_in_pane
            items.append(entry)
        return items

    def open_menu(self, index: int) -> None:
        """Open a drop-down and run whatever it comes back with.

        Left and right walk to the neighbouring menu without closing the bar,
        which is the behaviour that makes a menu bar feel like one rather than
        like a stack of unrelated pop-ups.
        """
        spans = menu_layout()
        while True:
            index %= len(MENUS)
            self._open_menu = index
            self.draw()
            chosen = dialogs.dropdown(self.stdscr, self._menu_items(index),
                                      1, spans[index][0])
            if chosen == dialogs.PREVIOUS_MENU:
                index -= 1
                continue
            if chosen == dialogs.NEXT_MENU:
                index += 1
                continue
            break
        self._open_menu = None
        if chosen:
            self._dispatch(chosen)

    def _actions(self) -> dict:
        """Every named action, as a table of name -> what to call.

        A table rather than a chain of ``if``s so that the menu tree can be
        checked against it: an entry naming an action nothing answers to is a
        dead entry, and the tests say so rather than the user finding out.
        """
        return {
            "view": self._view,
            "edit": self._edit,
            "run": self._run_current,
            "copy": self._copy,
            "move": self._move,
            "extract": self._extract,
            "rename": self._rename,
            "mkdir": self._mkdir,
            "touch": self._touch,
            "delete": self._delete,
            "tag": self.active.toggle_select,
            "tag_group": self._tag_group,
            "untag_group": self._untag_group,
            "invert": self._invert_tags,
            "attributes": self._attributes,
            "link": self._link,
            "open_with": self._open_with,
            "jobs": self._jobs_pane,
            "long": self._toggle_long,
            "trash": self._toggle_trash,
            "open_in_pane": self._toggle_open_in_pane,
            "find": self._find_files,
            "connect": self._open_location,
            "presets": self._presets_menu,
            "goto": self._go_to_path,
            "home": self._go_home,
            "open_trash": self._open_trash,
            "back": self._back,
            "forward": self._forward,
            "search": self._start_search,
            "filter": self._filter,
            "swap": self._swap_panes,
            "reload": self._reload,
            "mirror": self._mirror_to_other_pane,
            "sync": self._sync,
            "compare": self._compare,
            "peek": self._peek,
            "quick_view": self._quick_view,
            "terminal": self._open_terminal_pane,
            "shell": self._open_terminal,
            "plugins": self._plugin_mode,
            "sort": self._sort_menu,
            "hidden": self._toggle_hidden,
            "sizes": self._toggle_sizes,
            "single": self._toggle_single,
            "colours": self._colour_menu,
            "editor": self._editor_menu,
            "viewer": self._viewer_menu,
            "config": self._config_menu,
            "help": self._help,
            "about": self._about,
            "quit": self._quit,
        }

    def _dispatch(self, action: str) -> None:
        """Run a named action from the menu bar or the context menu."""
        handler = self._actions().get(action)
        if handler is not None:
            handler()

    def _menu_key(self, key: int, following: int | None = None) -> bool:
        """Handle Esc and the Alt+letter combinations that open a menu.

        A lone Esc and Alt+F arrive as the same first byte, so the only way to
        tell them apart is to look for a second one straight away: a real Alt
        combination has already sent its letter, while a user pressing Escape
        has not.  ``following`` is that second key when the caller has
        already looked for it.  Returns True when the key was the menu's.
        """
        if following is None:
            following = self._peek_key()
        if following == -1:
            self.open_menu(0)
            return True
        if 32 <= following < 127:
            letter = chr(following).lower()
            for index, entry in enumerate(MENUS):
                if theme.hotkey_letter(entry["label"]) == letter:
                    self.open_menu(index)
                    return True
        return True     # an escape sequence we do not use: swallow it

    def _peek_key(self) -> int:
        """The next key if one is already waiting, else -1."""
        getch = getattr(self.stdscr, "getch", None)
        if getch is None:
            return -1
        try:
            self.stdscr.timeout(40)
            return getch()
        except curses.error:
            return -1
        finally:
            self.stdscr.timeout(-1)

    def handle_key(self, key: int | str) -> None:
        panel = self.active
        body_h = self.stdscr.getmaxyx()[0] - 6

        # A plugin owning the active pane gets first crack at every key.
        if panel.plugin is not None and key != curses.KEY_RESIZE:
            try:
                res = panel.plugin.handle_key(key)
            except Exception as exc:
                self._set_message(f"Plugin error: {exc}")
                res = True
            if res is False:
                self._end_plugin(panel)
                return
            if res is not None:
                return
            # res is None: fall through so the app can handle the key (Tab).

        # The search mode comes before every hotkey: while it is on, a letter
        # is part of a name, and q asking to quit or d offering to delete in
        # the middle of typing one would be the worst surprise it could give.
        search = self.search
        if search is not None and self._search_key(search, key):
            return

        if key in (curses.KEY_UP, ord("k")):
            panel.move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            panel.move(1)
        elif key == curses.KEY_NPAGE:
            panel.move(body_h)
        elif key == curses.KEY_PPAGE:
            panel.move(-body_h)
        elif key == curses.KEY_HOME:
            panel.move_to(0)
        elif key == curses.KEY_END:
            panel.move_to(len(panel.entries) - 1)
        elif key == 9:  # Tab
            # In single mode too: the drawing shows whichever pane is active,
            # so this flips to the other one at full width, like a tab.
            self.active = self.other
        elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
            self._activate_entry()
        elif key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT):
            if not panel.go_parent():
                self._leave_archive()
        elif key in (curses.KEY_IC, ord(" ")):
            panel.toggle_select()
            panel.move(1)
        elif key == ord("+"):  # tag the names matching a pattern
            self._tag_group()
        elif key == ord("\\"):  # untag the names matching a pattern
            self._untag_group()
        elif key == ord("-"):  # untag everything
            panel.clear_selection()
        elif key == ord("*"):  # tag what is untagged, and the other way round
            self._invert_tags()
        elif key == ord("a"):  # permissions, owner and group
            self._attributes()
        elif key == ord("l"):  # make a symbolic link, or change one's target
            self._link()
        elif key == ord("w"):  # open with the desktop's own application
            self._open_with()
        elif key == ord("J"):  # the background jobs, in this pane
            self._jobs_pane()
        elif key == 17:  # Ctrl-Q: the other pane shows the cursor's file
            self._quick_view()
        elif key == 27:  # Esc: the menu bar; Alt+letter: a menu by name
            self._menu_key(key)
        elif key == 21:  # Ctrl-U swap panes
            self._swap_panes()
        elif key == 18:  # Ctrl-R reload
            self._reload()
        elif key == 7:  # Ctrl-G go to path
            self._go_to_path()
        elif key == ord("~"):  # home directory of this pane's filesystem
            self._go_home()
        elif key == ord("T"):  # the trash, in this pane
            self._open_trash()
        elif key == ord("<"):  # back through this pane's history
            self._back()
        elif key == ord(">"):  # and forward again
            self._forward()
        elif key == ord("="):  # other pane: same location as this one
            self._mirror_to_other_pane()
        elif key == ord("b"):  # presets: saved locations
            self._presets_menu()
        elif key == 20:  # Ctrl-T sort
            self._sort_menu()
        elif key == ord("."):  # toggle hidden files in the active pane
            self._toggle_hidden()
        elif key == ord("u"):  # subdirectory sizes in the active pane
            self._toggle_sizes()
        elif key == ord("z"):  # one pane at full width, or two
            self._toggle_single()
        elif key == ord("t"):  # terminal inside this pane
            self._open_terminal_pane()
        elif key == ord("!"):  # full-screen shell (for vim/htop and friends)
            self._open_terminal()
        elif key in (ord("p"), curses.KEY_F11):  # plug-in mode
            self._plugin_mode()
        elif key == ord("C"):  # configuration menu
            self._config_menu()
        elif key == ord("f"):  # find files under the current directory
            self._find_files()
        elif key == ord("n"):  # new empty file in the active pane
            self._touch()
        elif key == ord("r"):  # rename the entry under the cursor
            self._rename()
        elif key == ord("x"):  # extract the archive into the other pane
            self._extract()
        elif key == ord("D"):  # compare both panes' files side by side
            self._compare()
        elif key == ord("h"):  # head + tail of the other pane's file
            self._peek()
        elif key == ord("/"):  # search this pane's listing by name
            self._start_search()
        elif key == ord("F"):  # show only the names matching a pattern
            self._filter()
        elif key == curses.KEY_MOUSE:
            self._handle_mouse()
        # F-keys, with digit (1..0 -> F1..F10) and mnemonic-letter alternates
        # so the app is fully usable on terminals that swallow the F-keys.
        elif key in (curses.KEY_F1, ord("1"), ord("?")):
            self._help()
        elif key in (curses.KEY_F2, ord("2"), ord("o")):
            self._open_location()
        elif key in (curses.KEY_F3, ord("3"), ord("v")):
            self._view()
        elif key in (curses.KEY_F4, ord("4"), ord("e")):
            self._edit()
        elif key in (curses.KEY_F5, ord("5"), ord("c")):
            self._copy()
        elif key in (curses.KEY_F6, ord("6"), ord("m")):
            self._move()
        elif key in (curses.KEY_F7, ord("7")):
            self._mkdir()
        elif key in (curses.KEY_F8, curses.KEY_DC, ord("8"), ord("d")):
            self._delete()
        elif key in (curses.KEY_F9, ord("9"), ord("s")):
            self._sync()
        elif key in (curses.KEY_F10, ord("0"), ord("q"), 3):  # 3 = Ctrl-C
            self._quit()
        elif key == curses.KEY_RESIZE:
            pass

    # -- the search mode ----------------------------------------------------
    def _start_search(self) -> None:
        """Turn the search mode on in the active pane."""
        panel = self.active
        if panel.plugin is not None:
            # A plug-in draws the pane, so there is no listing to search, and
            # a cursor moving under it would be one nobody could see.
            self._set_message("Search works on a directory listing")
            return
        self.search = Search(panel)
        self._message_before_search = self.message
        self._set_message("Search: / or Esc ends   Down/Up next/previous   "
                          "Ins tag")

    def _filter(self) -> None:
        """Ask for a pattern and show only the names in the pane that match.

        Pre-filled with the pane's filter, so it can be edited, and an empty
        answer shows everything again.  It hides; it does not untag: the
        caption counts what it hides, tagged entries among them, and a copy
        or delete says how many of its items are not on the screen.
        """
        panel = self.active
        if panel.plugin is not None:
            self._set_message("Filter works on a directory listing")
            return
        pattern = dialogs.prompt(
            self.stdscr, "Filter",
            "Show only names matching (text or glob; empty shows all):",
            default=panel.filter)
        if pattern is None:
            return
        panel.set_filter(pattern)
        if not pattern:
            self._set_message("Filter off")
            return
        shown = len(panel.entries) - (0 if panel._at_root() else 1)
        self._set_message(f"Filter {pattern}: {shown} shown, "
                          f"{panel.hidden_count} hidden")

    @staticmethod
    def _filter_note(panel: Panel) -> str:
        """The caption's word on a filter: the pattern and what it hides."""
        if not panel.filter:
            return ""
        note = f" [{panel.filter}: {panel.hidden_count} hidden"
        tagged = panel.hidden_tagged
        return note + (f", {tagged} tagged]" if tagged else "]")

    @staticmethod
    def _not_shown(panel: Panel) -> str:
        """A copy, move or delete's word on tagged items a filter hides."""
        hidden = panel.hidden_tagged
        return f" ({hidden} not shown)" if hidden else ""

    def _tag_group(self) -> None:
        """Tag the shown names matching a pattern: Midnight Commander's ``+``."""
        self._group(tag=True)

    def _untag_group(self) -> None:
        """Untag the shown names matching a pattern: Midnight Commander's ``\\``."""
        self._group(tag=False)

    def _group(self, tag: bool) -> None:
        """Ask for a pattern and tag or untag what matches it.

        Pre-filled with ``*``, so ``+`` then Enter tags every file as ``+`` on
        its own used to -- every *file*: the "Files only" switch, on by
        default, keeps ``*.tsv`` from taking a directory called ``old.tsv``.
        A pattern that matches nothing says so and changes nothing.
        """
        panel = self.active
        if panel.plugin is not None:
            self._set_message("Tagging works on a directory listing")
            return
        verb = "Tag" if tag else "Untag"
        files_only = dialogs.Toggle("Files only", "f")
        pattern = dialogs.prompt(
            self.stdscr, f"{verb} group",
            f"{verb} the names matching (glob or text):", default="*",
            toggles=[files_only])
        if not pattern:
            return
        count = panel.select_matching(pattern, tag=tag,
                                      files_only=files_only.on)
        if not count:
            self._set_message(f"Nothing matches {pattern}")
            return
        self._set_message(f"{verb}ged {count}: {len(panel.selected)} tagged")

    def _invert_tags(self) -> None:
        """Tag what is untagged and untag what is tagged: mc's ``*``."""
        panel = self.active
        if panel.plugin is not None:
            self._set_message("Tagging works on a directory listing")
            return
        panel.invert_selection()
        self._set_message(f"Tags inverted: {len(panel.selected)} tagged")

    def _attributes(self) -> None:
        """Show and change the permissions, owner and group of what is tagged.

        The bits every entry agrees on are shown as they are, the others as
        mixed, and a mixed bit left alone stays each entry's own.  A change
        below directories is confirmed first, as a delete is, and can be
        cancelled; an entry that refuses -- a file that is not yours to give
        away -- is named at the end rather than stopping the rest.
        """
        panel = self.active
        if panel.plugin is not None:
            self._set_message("Attributes work on a directory listing")
            return
        targets = panel.selected_entries()
        if not targets:
            return
        fs = panel.fs
        if not fs.can_chmod:
            dialogs.message(self.stdscr, "Attributes",
                            f"{fs.label()} cannot change permissions.",
                            error=True)
            return
        on, off = common_bits([t.mode for t in targets])
        owners = {t.owner for t in targets}
        groups = {t.group for t in targets}
        owner = owners.pop() if len(owners) == 1 else ""
        group = groups.pop() if len(groups) == 1 else ""
        subject = (targets[0].name if len(targets) == 1
                   else f"{len(targets)} items{self._not_shown(panel)}")
        choice = dialogs.attributes(
            self.stdscr, "Attributes", subject, on, off, owner, group,
            can_chown=fs.can_chown,
            has_dirs=any(t.is_dir and not t.is_symlink for t in targets))
        if choice is None:
            return
        if choice.recursive and not dialogs.confirm(
                self.stdscr, "Attributes",
                f"Change {subject} and everything under it?"):
            return
        change = AttributeChange(
            on=choice.on, off=choice.off,
            owner=choice.owner if choice.owner != owner else "",
            group=choice.group if choice.group != group else "",
            recursive=choice.recursive)
        paths = [(fs.join(panel.path, t.name), t) for t in targets]
        dlg = dialogs.ProgressDialog(self.stdscr, "Attributes")
        try:
            done, failures = apply_change(
                fs, paths, change,
                progress=lambda count, label: dlg.update(count, 0, label),
                cancel=dlg.cancelled)
        except OperationCancelled:
            done, failures = -1, []
        finally:
            dlg.close()
        panel.refresh()
        if failures:
            more = len(failures) - 8
            dialogs.message(self.stdscr, "Attributes: not changed",
                            "\n".join(failures[:8])
                            + (f"\n... and {more} more" if more > 0 else ""),
                            error=True)
        elif done < 0:
            self._set_message("Attributes cancelled")
        else:
            self._set_message(f"Attributes changed on {done} item(s)")

    def _open_with(self) -> None:
        """Hand the entry under the cursor to the desktop's own application."""
        panel = self.active
        if panel.plugin is not None:
            self._set_message("Open with works on a directory listing")
            return
        entry = panel.current()
        if entry is None or entry.name == "..":
            self._set_message("Nothing to open")
            return
        path = panel.fs.join(panel.path, entry.name)
        self._set_message(self.opener.open(panel.fs, path, entry.is_dir))

    def _jobs_pane(self) -> None:
        """Show the background jobs in this pane, or stop showing them."""
        from .plugin_api import PluginContext

        for panel in (self.left, self.right):
            if isinstance(panel.plugin, JobsPane):
                self._close_plugin(panel)
                panel.refresh()
                self._set_message("Jobs closed")
                return
        panel = self.active
        if panel.plugin is not None:
            self._set_message("This pane is busy -- close it first")
            return
        ctx = PluginContext(app=self, own_panel=panel, other_panel=self.other)
        panel.plugin = JobsPane(ctx)
        self._set_message("Jobs -- p pauses, c cancels, Enter shows errors, "
                          "J closes")

    def _background(self, move: bool, src_panel: Panel, dst_panel: Panel,
                    sources: list, dest: str, keep_mode: bool) -> None:
        """Queue a copy or move to run while the panes stay usable.

        A job cannot stop to ask, so what to do with a target that is already
        there is asked now, once, for all of it.  A remote side gets a
        connection of the job's own -- browsing that host must not wait
        behind the copy, and one paramiko channel is not shared between
        threads -- dialled now, on this thread, so a password can be asked.
        """
        verb = "Move" if move else "Copy"
        for fs in (src_panel.fs, dst_panel.fs):
            if isinstance(fs, ArchiveFileSystem):
                dialogs.message(self.stdscr, f"Background {verb.lower()}",
                                "An archive is rewritten as a whole, so it "
                                "cannot take part in a background job.\n"
                                "Run this one without Background.",
                                error=True)
                return
        labels = ["Overwrite", "Overwrite if older", "Skip", "Cancel"]
        choice = dialogs.menu(self.stdscr, f"Background {verb.lower()}",
                              labels, keys=["o", "i", "s", "c"],
                              text="If a target is already there:")
        if choice is None or choice == 3:
            return
        rule = RULES[choice]
        closing: list[FileSystem] = []
        own: list[FileSystem] = []
        for fs, path in ((src_panel.fs, src_panel.path),
                         (dst_panel.fs, dest)):
            # Both sides on this machine share one backend, so a move between
            # local panes is a rename rather than a copy and a delete.
            mine = own[0] if own and _is_local(fs) and _is_local(own[0]) \
                else self._own_connection(fs, path)
            if mine is None:
                for opened in closing:
                    opened.close()
                return
            if mine is not fs:
                closing.append(mine)
            own.append(mine)
        job = Job(self.jobs.number(), move, own[0], src_panel.path,
                  [entry.name for entry in sources], own[1], dest, rule,
                  keep_mode=keep_mode, src_pane_fs=src_panel.fs,
                  dst_pane_fs=dst_panel.fs, closing=closing,
                  delete_sources=self._delete_sources)
        self.jobs.submit(job)
        src_panel.clear_selection()
        queued = "" if self.jobs.started(job) else ", after the one before it"
        self._set_message(f"Job {job.number}: {job.title()}{queued} "
                          f"-- J to watch it")

    def _own_connection(self, fs: FileSystem, path: str) -> FileSystem | None:
        """A connection to where ``fs`` is, for a background job alone.

        The local disk needs none: each job gets a fresh local backend, which
        holds nothing open.  A remote location is dialled again with the
        details the pane was opened with, asking for a password only if the
        agent and keys will not do, as a preset does.
        """
        if _is_local(fs):
            return LocalFileSystem()
        return self._dial(presets.from_location("job", fs, path))

    def _link(self) -> None:
        """Make symbolic links in the other pane, or change a link's target.

        On a link (and nothing tagged) it edits where that link points.
        Otherwise each tagged entry, or the one under the cursor, gets a link
        of its name in the other pane, relative by default -- a relative link
        keeps working when the tree it is in moves.  Both panes have to be on
        one machine: a link whose target is on another one is never what was
        meant, and is refused with that reason.
        """
        panel, other = self.active, self.other
        if panel.plugin is not None:
            self._set_message("Links work on a directory listing")
            return
        cursor = panel.current()
        if not panel.selected and cursor is not None and cursor.is_symlink:
            self._edit_link(panel, cursor.name)
            return
        targets = panel.selected_entries()
        if not targets:
            return
        if not same_place(panel.fs, other.fs):
            dialogs.message(self.stdscr, "Link",
                            "The two panes are on different machines, and a "
                            "link\ncan only point at a path on its own.",
                            error=True)
            return
        fs = other.fs
        if not fs.can_symlink:
            dialogs.message(self.stdscr, "Link",
                            f"{fs.label()}: links are not supported here.",
                            error=True)
            return
        relative = dialogs.Toggle("Relative", "r")
        if len(targets) == 1:
            where = dialogs.prompt(
                self.stdscr, "Link", f"Link to '{targets[0].name}' at:",
                default=fs.join(other.path, targets[0].name),
                toggles=[relative])
        else:
            where = dialogs.prompt(
                self.stdscr, "Link", f"Link {len(targets)} items into:",
                default=other.path, toggles=[relative])
        if not where:
            return
        where = fs.normpath(other.resolve(where))
        made, errors = 0, []
        for entry in targets:
            source = panel.fs.join(panel.path, entry.name)
            link = where if len(targets) == 1 else fs.join(where, entry.name)
            target = source
            if relative.on:
                rel = (os.path.relpath if fs.sep == os.sep else posixpath.relpath)
                target = rel(source, fs.dirname(link))
            try:
                if self._make_link(fs, target, link):
                    made += 1
            except Exception as exc:
                errors.append(f"{fs.basename(link)}: {exc}")
        other.refresh()
        if errors:
            dialogs.message(self.stdscr, "Link errors", "\n".join(errors[:8]),
                            error=True)
        else:
            self._set_message(f"Made {made} link(s)")

    def _make_link(self, fs: FileSystem, target: str, link: str) -> bool:
        """Make ``link`` point at ``target``; asks before replacing anything.

        False when the answer was to leave what is there.
        """
        try:
            existing = fs.stat(link)
        except Exception:
            existing = None
        if existing is not None:
            if existing.is_dir and not existing.is_symlink:
                raise FileSystemError("a directory of that name is in the way")
            if not dialogs.confirm(self.stdscr, "Link",
                                   f"'{fs.basename(link)}' already exists.\n"
                                   f"Replace it with a link?"):
                return False
            fs.remove(link)
        fs.symlink(target, link)
        return True

    def _edit_link(self, panel: Panel, name: str) -> None:
        """Point the link ``name`` somewhere else, keeping its name."""
        fs = panel.fs
        path = fs.join(panel.path, name)
        if not fs.can_symlink:
            dialogs.message(self.stdscr, "Link",
                            f"{fs.label()}: links are not supported here.",
                            error=True)
            return
        try:
            current = fs.readlink(path)
        except Exception as exc:
            dialogs.message(self.stdscr, "Link", str(exc), error=True)
            return
        target = dialogs.prompt(self.stdscr, "Link",
                                f"'{name}' points at:", default=current)
        if not target or target == current:
            return
        try:
            fs.remove(path)
            fs.symlink(target, path)
        except Exception as exc:
            dialogs.message(self.stdscr, "Link error", str(exc), error=True)
            return
        panel.refresh(keep_name=name)
        self._set_message(f"{name} -> {target}")

    def _toggle_trash(self) -> None:
        """Have F8 offer the trash on local panes, or not, and remember it."""
        from . import config as config_mod

        self.use_trash = not self.use_trash
        saved = config_mod.save_trash(self.use_trash)
        self._set_message(("F8 offers the trash" if self.use_trash
                           else "F8 deletes permanently")
                          + ("" if saved else "  (could not be saved)"))

    def _toggle_open_in_pane(self) -> None:
        """Open F3 and F4 in a pane, or full-screen, and remember it."""
        from . import config as config_mod

        self.open_in_pane = not self.open_in_pane
        saved = config_mod.save_open_in(self.open_in_pane)
        self._set_message(("F3 and F4 open in a pane" if self.open_in_pane
                           else "F3 and F4 open full-screen")
                          + ("" if saved else "  (could not be saved)"))

    def _toggle_long(self) -> None:
        """Show the mode and owner columns, or not, and remember it."""
        from . import config as config_mod

        self.long_listing = not self.long_listing
        saved = config_mod.save_listing(self.long_listing)
        self._set_message(("Long listing" if self.long_listing
                           else "Short listing")
                          + ("" if saved else "  (could not be saved)"))

    def _end_search(self) -> None:
        """Turn the search mode off, leaving the cursor where it is."""
        self.search = None
        self._set_message(self._message_before_search)

    def _search_key(self, search: Search, key: int | str) -> bool:
        """Handle ``key`` in the search mode.  True when it was the mode's.

        Every character is part of the pattern, hotkey letters and the digit
        aliases of the F-keys included.  A key that is not text and not one
        of the mode's own -- Enter, Tab, a real F-key, the mouse, Ctrl and
        the other cursor keys -- ends the search and answers False, so the
        caller goes on to do with it exactly what it always does: Enter opens
        the entry the search found, F5 copies it.
        """
        if key == ord("/"):       # the toggle: no file name contains a "/"
            self._end_search()
        elif key == 27:
            # Alt+letter arrives as Esc and the letter.  Read together they
            # are a menu, as anywhere else; the letter on its own would be
            # a hotkey let out of the search.
            following = self._peek_key()
            self._end_search()
            if following != -1:
                self._menu_key(key, following)
        elif key in (curses.KEY_DOWN, 14):           # 14 = Ctrl-N
            search.step(1)
        elif key in (curses.KEY_UP, 16):             # 16 = Ctrl-P
            search.step(-1)
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            search.backspace()
        elif key == curses.KEY_IC:
            # Tag and go on to the next match, as Insert goes on to the next
            # row outside the search: one search can tag all it finds.
            search.panel.toggle_select()
            search.step(1)
        elif key == curses.KEY_RESIZE:
            pass
        else:
            char = typed_char(key)
            if char is None:
                self._end_search()
                return False
            search.type(char)
        return True

    # -- actions ----------------------------------------------------------
    def _quit(self) -> None:
        if not self._may_quit():
            return
        if self.jobs.active():
            self._quit_with_jobs()
            return
        if dialogs.confirm(self.stdscr, "Quit", "Exit Meridian Commander?",
                           default_yes=True):
            self.running = False

    def _quit_with_jobs(self) -> None:
        """Quit with background jobs still to finish: wait, or cancel them."""
        count = len(self.jobs.active())
        choice = dialogs.menu(
            self.stdscr, "Quit",
            ["Wait for them, then quit", "Cancel them and quit", "Back"],
            keys=["w", "c", "b"],
            text=f"{count} background job{'' if count == 1 else 's'} "
                 f"still running or waiting.")
        if choice == 0:
            self._quit_when_done = True
            self._set_message("Quitting when the jobs end -- J to watch them")
        elif choice == 1:
            self.jobs.cancel_all()
            self.jobs.wait(self.JOBS_GRACE)
            self.running = False

    def printwd_path(self) -> str | None:
        """Where a shell should follow us to, or ``None`` for nowhere.

        Only a local pane names a directory a shell could enter: an SFTP or
        FTP pane is a connection and an archive pane is a file inside one.
        Answering ``None`` for those leaves the shell where it was, which is
        the honest outcome -- moving someone to the last local directory they
        happened to visit would be a guess dressed up as a feature.
        """
        panel = self.active
        if not isinstance(panel.fs, LocalFileSystem):
            return None
        return panel.path

    def _reload(self) -> None:
        for panel in (self.left, self.right):
            panel.refresh()
            if panel.show_sizes:
                panel.forget_sizes()   # a reload is "measure it again", too
        self._set_message("Reloaded")

    def _toggle_hidden(self) -> None:
        panel = self.active
        panel.toggle_hidden()
        self._set_message("Hidden files "
                          + ("shown" if panel.show_hidden else "hidden"))

    def _toggle_sizes(self) -> None:
        """Show what is under each subdirectory, in place of "<DIR>".

        A pane rather than a report: the sizes appear in the listing you are
        already navigating, so the way to find what is filling a disk is to
        follow the big numbers down the tree, which is the same Enter key it
        always was.
        """
        panel = self.active
        if panel.toggle_sizes():
            self._set_message("Directory sizes on -- follow the big ones down")
        else:
            self._set_message("Directory sizes off")

    def _colour_menu(self) -> None:
        """Switch colour scheme, the way Options > Colours always did."""
        from . import config as config_mod

        names = theme.names()
        labels = [f"{name}{'   (current)' if name == theme.current else ''}"
                  for name in names]
        choice = dialogs.menu(self.stdscr, "Colours", labels + ["Cancel"])
        if choice is None or choice == len(names):
            return
        theme.init(names[choice])
        saved = config_mod.save_scheme(names[choice])
        self._set_message(f"Colour scheme: {names[choice]}"
                          + ("" if saved else "  (could not be saved)"))

    def _editor_menu(self) -> None:
        """Choose what F4 opens: the built-in editor, or one of your own."""
        from . import config as config_mod

        self._program_menu("Editor", EDITOR_CHOICES,
                           config_mod.external_editor(),
                           config_mod.save_editor, "built-in editor")

    def _viewer_menu(self) -> None:
        """Choose what F3 opens for a text file: the viewer, or your pager."""
        from . import config as config_mod

        self._program_menu("Viewer", VIEWER_CHOICES,
                           config_mod.external_viewer(),
                           config_mod.save_viewer, "built-in viewer")

    def _program_menu(self, title: str, choices: list[tuple[str, str]],
                      current: str, save, built_in: str) -> None:
        """Offer an outside program, check it can work, and remember it.

        One routine behind both menus: they ask the same question about the
        two halves of the same pair, and a second copy would be a second
        place for the "is it on your PATH" courtesy to rot.
        """
        labels = [
            f"{label}{'   (current)' if command == current else ''}"
            for command, label in choices
        ]
        other = len(choices)
        choice = dialogs.menu(self.stdscr, title,
                              labels + ["Other...", "Cancel"])
        if choice is None or choice > other:
            return
        if choice == other:
            typed = dialogs.prompt(self.stdscr, title, f"{title} command:",
                                   current)
            if typed is None:
                return
            command = typed.strip()
        else:
            command = choices[choice][0]

        # Say now if the command cannot work, rather than at the next keypress.
        note = ""
        if command:
            try:
                argv = extedit.editor_argv(command)
            except ValueError as exc:
                dialogs.message(self.stdscr, title, str(exc), error=True)
                return
            if argv is not None and shutil.which(argv[0]) is None:
                note = f"  ({argv[0]} is not on your PATH)"
        saved = save(command)
        self._set_message(f"{title}: {command or built_in}{note}"
                          + ("" if saved else "  (could not be saved)"))

    def _about(self) -> None:
        from . import __version__

        dialogs.message(self.stdscr, "About", (
            f"Meridian Commander {__version__}\n"
            "\n"
            "A two-pane terminal file manager: local, SFTP, SSH and FTP,\n"
            "with a viewer, an editor, archives and plug-ins.\n"
            "\n"
            "Copyright (C) 2026 Martin J. Gallagher\n"
            "License: GPL-3.0-or-later "
            "<https://www.gnu.org/licenses/gpl-3.0.html>"
        ))

    def _activate_entry(self) -> None:
        panel = self.active
        entry = panel.current()
        if entry is None:
            return
        if entry.is_dir:
            panel.enter()
        elif is_archive(entry.name):
            self._enter_archive()
        else:
            argv = self._run_argv_for(entry)
            if argv is not None:
                self._run_file(entry, argv)
            else:
                self._view()

    # -- running a file -----------------------------------------------------
    def _run_argv_for(self, entry) -> list[str] | None:
        """How Enter would run the cursor entry, or ``None`` to view it.

        Only local and SFTP/SSH panes have somewhere to run a program (an
        archive or FTP pane does not), so anywhere else everything views.
        """
        panel = self.active
        if not isinstance(panel.fs,
                          (LocalFileSystem, SFTPFileSystem, SSHFileSystem)):
            return None
        path = panel.fs.join(panel.path, entry.name)
        return runner.run_argv(panel.fs, path, entry.mode)

    def _run_current(self) -> None:
        """The menu's Run: the cursor file, or say why it cannot run."""
        panel = self.active
        entry = panel.current()
        if entry is None or entry.name == Panel.PARENT or entry.is_dir:
            dialogs.message(self.stdscr, "Run",
                            "Point the cursor at a file to run.")
            return
        argv = self._run_argv_for(entry)
        if argv is None:
            dialogs.message(
                self.stdscr, "Run",
                f"{entry.name} is not runnable here -- it needs a '#!' line "
                "or the executable bit, on a local or SFTP/SSH pane.")
            return
        self._run_file(entry, argv)

    def _run_file(self, entry, argv: list[str]) -> None:
        """Run the cursor file full-screen and hold its output on screen.

        The arguments dialog doubles as the confirmation: Esc cancels, Enter
        runs.  The program gets the real terminal (stdin included, so a script
        that prompts still works) and runs in its own directory; a remote
        pane's file runs on the remote host via ``ssh -t``, the same way the
        full-screen shell (``!``) reaches it.
        """
        panel = self.active
        fs = panel.fs
        args_text = dialogs.prompt(self.stdscr, "Run",
                                   f"Arguments for {entry.name}:")
        if args_text is None:
            return
        try:
            args = shlex.split(args_text)
        except ValueError as exc:
            dialogs.message(self.stdscr, "Run", f"Bad arguments: {exc}",
                            error=True)
            return

        full = argv + args
        if isinstance(fs, LocalFileSystem):
            cmd, cwd = full, panel.path
        else:   # SFTP/SSH -- _run_argv_for allowed nothing else
            remote = "cd {} && {}".format(
                shlex.quote(panel.path),
                " ".join(shlex.quote(a) for a in full))
            cmd, cwd = self._ssh_argv(fs, remote), None

        banner = (f"\n[Meridian Commander] Running {entry.name} in "
                  f"{fs.label()}:{panel.path}\n\n")
        status = self._suspend_and_run(cmd, cwd, banner, pause_after=True,
                                       fail_label=f"Could not run {entry.name}")
        panel.refresh()
        self.other.refresh()
        if status is None:
            self._set_message(f"{entry.name}: could not start")
        else:
            self._set_message(f"{entry.name} exited {status}")

    def _enter_archive(self) -> None:
        """Open the highlighted archive as though it were a directory."""
        panel = self.active
        target = panel.current_path()
        try:
            fs = open_archive(panel.fs, target)
        except Exception as exc:
            self._set_message(f"Cannot open archive: {exc}")
            return
        if not panel.set_location(fs, "/"):
            fs.close()
            self._set_message(f"Cannot read {panel.fs.basename(target)}")
            return
        # Registered so it is closed with everything else on the way out.
        self._backends.append(fs)
        self._set_message(f"{fs.label()} -- change it like a directory; "
                          "Backspace to leave")

    def _leave_archive(self) -> None:
        """Step back out of an archive to the directory that holds it.

        Only reached at the archive's root, where there is no parent inside it
        to go to; anywhere else go_parent() has already moved.
        """
        panel = self.active
        fs = panel.fs
        host = getattr(fs, "host_fs", None)
        if host is None:
            return
        name = host.basename(fs.archive_path)
        panel.set_location(host, host.dirname(fs.archive_path))
        panel.refresh(keep_name=name)
        if any(p.fs is fs for p in (self.left, self.right)):
            # The other pane is still in this archive -- "=" points both panes
            # at one filesystem object, and closing it here left that pane
            # listing an archive whose every file then failed to open.  It
            # closes when the pane that is still in it leaves, or on the way
            # out with the rest of the backends.
            return
        if fs in self._backends:
            self._backends.remove(fs)
        fs.close()

    def _swap_panes(self) -> None:
        alone = len(self._panes_on_screen()) == 1
        self.left, self.right = self.right, self.left
        if alone:
            # With one pane on screen, swapping only the sides would change
            # nothing anyone can see.  The swap brings the hidden pane out
            # instead, in the place the shown one had.
            self.active = self.other
            self._set_message("Panes swapped -- the hidden one is on screen")
            return
        # Keep the highlighted (active) pane pointing at the same panel object.
        self.active = self.left if self.active is self.left else self.right
        self._set_message("Panes swapped")

    def _open_terminal_pane(self) -> None:
        """Open a terminal *inside* the active pane.

        The shell runs in the pane's rectangle and follows the pane's
        location: a local pty for local panes, an interactive channel on the
        pane's existing SSH connection for SFTP/SSH panes.  Every key goes to
        the program; Ctrl-] opens the terminal's menu (Tab: the other pane,
        z: zoom to the full width and back, q: close, PgUp: scroll back).
        """
        from .plugin_api import PluginContext
        from .plugins.terminal import TerminalPlugin

        panel = self.active
        if not self._may_close(panel):
            return
        self._close_plugin(panel)
        ctx = PluginContext(app=self, own_panel=panel, other_panel=self.other)
        try:
            panel.plugin = TerminalPlugin(ctx)
        except Exception as exc:
            panel.plugin = None
            dialogs.message(self.stdscr, "Terminal", str(exc), error=True)
            return
        # 78 characters: the whole hint line of an 80-column screen.
        self._set_message("Terminal opened -- Ctrl-] then Tab: pane, "
                          "z: zoom, q: close, PgUp: scroll back")

    def _open_terminal(self) -> None:
        """Suspend the TUI and drop the user into a full-screen shell.

        The in-pane terminal (`t`) runs vim and htop too, in half the screen;
        this gives a program the whole real terminal instead -- its full
        width, its true colour and its own mouse handling.
        For a local pane this is a shell with the working directory set to the
        pane's path.  For an SFTP pane it opens an interactive ``ssh`` session
        into the same remote directory.  FTP has no shell, so it is declined.
        """
        panel = self.active
        fs = panel.fs
        cwd = None
        if isinstance(fs, LocalFileSystem):
            shell = os.environ.get("SHELL", "/bin/sh")
            cmd = [shell]
            cwd = panel.path
        elif isinstance(fs, (SFTPFileSystem, SSHFileSystem)):
            remote = f"cd {shlex.quote(panel.path)} && exec ${{SHELL:-/bin/sh}}"
            cmd = self._ssh_argv(fs, remote)
        else:
            dialogs.message(self.stdscr, "Terminal",
                            "A terminal is only available for local or SFTP panes.")
            return

        banner = (f"\n[Meridian Commander] Shell in {fs.label()}:{panel.path}\n"
                  "Type 'exit' to return to Meridian Commander.\n\n")
        self._suspend_and_run(cmd, cwd, banner,
                              fail_label="Could not start terminal")
        panel.refresh()
        self._set_message("Returned from shell")

    @staticmethod
    def _ssh_argv(fs, remote: str) -> list[str]:
        """An ``ssh -t`` argv running ``remote`` on the pane's host.

        Only what the user typed explicitly is passed, so ``~/.ssh/config``
        aliases keep providing their own HostName/User/Port/keys.
        """
        cmd = ["ssh", "-t"]
        if fs.port not in (None, 22):
            cmd += ["-p", str(fs.port)]
        typed_user = _split_user_host(fs.host)[0]
        target = fs.host
        if fs.typed_username and not typed_user:
            target = f"{fs.typed_username}@{fs.host}"
        return cmd + [target, remote]

    def _suspend_and_run(self, cmd: list[str], cwd: str | None, banner: str,
                         pause_after: bool = False,
                         fail_label: str = "Could not start") -> int | None:
        """Hand the real terminal to ``cmd``, then restore curses after.

        Returns the exit status (``None`` if the command could not start).
        With ``pause_after`` the screen waits for Enter before curses takes
        the terminal back, so the command's final output stays readable.
        """
        status: int | None = None
        curses.def_prog_mode()
        curses.endwin()
        try:
            os.write(1, banner.encode())
            status = subprocess.call(cmd, cwd=cwd)
            if pause_after:
                tail = "" if status == 0 else f" (exit status {status})"
                os.write(1, f"\n[Meridian Commander]{tail} ".encode())
                self._pause_for_enter()
        except Exception as exc:
            os.write(2, f"\n{fail_label}: {exc}\n".encode())
            self._pause_for_enter()
        finally:
            curses.reset_prog_mode()
            self.stdscr.clearok(True)
            curses.curs_set(0)
            self.stdscr.refresh()
        return status

    @staticmethod
    def _pause_for_enter() -> None:
        try:
            input("Press Enter to return...")
        except EOFError:
            pass

    # -- plug-ins -----------------------------------------------------------
    def _plugin_mode(self) -> None:
        """Offer the list of discovered plug-ins and open one in this pane."""
        from .plugin_api import PluginContext
        from .plugins import discover

        panel = self.active
        classes, errors = discover()
        if not classes:
            text = "No plug-ins found."
            if errors:
                text += "\nLoad errors:\n" + "\n".join(errors[:5])
            dialogs.message(self.stdscr, "Plug-ins", text)
            return

        labels = [f"{cls.name} -- {cls.description}"[:56] for cls in classes]
        # Every plug-in answers to a letter, like the presets menu -- so once
        # the list is muscle-memory, opening one is "p" then a single keystroke
        # rather than a walk down thirteen entries.  "c" is kept for Cancel.
        keys = dialogs.accelerators([cls.name for cls in classes], reserved="c")
        choice = dialogs.menu(self.stdscr, "Open plug-in",
                              labels + ["Cancel"], keys=keys + ["c"])
        if choice is None or choice == len(classes):
            return
        cls = classes[choice]

        if not self._may_close(panel):
            return
        self._close_plugin(panel)
        ctx = PluginContext(app=self, own_panel=panel, other_panel=self.other)
        try:
            panel.plugin = cls(ctx)
        except Exception as exc:
            dialogs.message(self.stdscr, "Plug-in error",
                            f"{cls.name} failed to start:\n{exc}", error=True)
            panel.plugin = None
            return
        # Single mode shows both panes while the plug-in works on the other
        # one; saying so explains why the second pane has just come back.
        both = (" -- both panes on screen while it runs"
                if self.single and len(self._panes_on_screen()) == 2 else "")
        self._set_message(
            f"Plug-in '{cls.name}' opened -- Esc closes it, Tab switches panes"
            + both)
        if errors:
            self._set_message(f"Plug-in '{cls.name}' opened "
                              f"({len(errors)} plug-in(s) failed to load)"
                              + both)

    # -- configuration ------------------------------------------------------
    def _config_menu(self) -> None:
        """Edit the app configuration or plug-in files from inside the app."""
        from . import config as config_mod
        from .plugins import plugin_dirs, user_plugin_dir

        choice = dialogs.menu(self.stdscr, "Configuration", [
            "Edit configuration (config.ini)",
            "Edit a plug-in file",
            "Open user plug-in folder in this pane",
            "Cancel",
        ])
        if choice is None or choice == 3:
            return

        local = LocalFileSystem()
        if choice == 0:
            path = config_mod.ensure_config()
            self._edit_local_file(local, path)
            self._set_message("Configuration saved -- reopen plug-ins to apply")
        elif choice == 1:
            files: list[str] = []
            for pdir in plugin_dirs():
                if not os.path.isdir(pdir):
                    continue
                for fn in sorted(os.listdir(pdir)):
                    if fn.endswith(".py") and not fn.startswith("_"):
                        files.append(os.path.join(pdir, fn))
            if not files:
                dialogs.message(self.stdscr, "Plug-ins", "No plug-in files found.")
                return
            labels = [f"{os.path.basename(f)}  ({os.path.dirname(f)})"[:56]
                      for f in files]
            pick = dialogs.menu(self.stdscr, "Edit plug-in", labels + ["Cancel"])
            if pick is None or pick == len(files):
                return
            self._edit_local_file(local, files[pick])
            self._set_message("Plug-in saved -- reopen it to apply changes")
        elif choice == 2:
            udir = user_plugin_dir()
            os.makedirs(udir, exist_ok=True)
            panel = self.active
            fs = panel.fs
            if not isinstance(fs, LocalFileSystem):
                fs = local
                self._backends.append(local)
            panel.set_location(fs, udir)
            self._set_message(
                "User plug-in folder -- drop .py files here to add plug-ins")

    def _edit_local_file(self, fs: LocalFileSystem, path: str) -> None:
        if self._edit_externally(fs, path):
            return
        try:
            editor = Editor(fs, path)
            editor.run(self.stdscr)
        except Exception as exc:
            dialogs.message(self.stdscr, "Edit error", str(exc), error=True)
        curses.curs_set(0)

    def _external_editor(self) -> list[str] | None:
        """The editor named by ``[ui] editor``, or ``None`` for the built-in.

        A setting that cannot work is said out loud before falling back:
        "F4 opened the wrong editor" is a great deal harder to work out from
        the outside than a dialog naming what is wrong with the setting.
        """
        from . import config as config_mod

        try:
            return extedit.editor_argv(config_mod.external_editor())
        except ValueError as exc:
            dialogs.message(self.stdscr, "Editor",
                            f"[ui] editor: {exc}\n\nUsing the built-in editor.",
                            error=True)
            return None

    def _external_viewer(self) -> list[str] | None:
        """The pager named by ``[ui] viewer``, or ``None`` for the built-in."""
        from . import config as config_mod

        try:
            return extedit.editor_argv(config_mod.external_viewer())
        except ValueError as exc:
            dialogs.message(self.stdscr, "Viewer",
                            f"[ui] viewer: {exc}\n\nUsing the built-in viewer.",
                            error=True)
            return None

    def _view_externally(self, fs: FileSystem, path: str) -> bool:
        """Show ``path`` in the configured pager; False if there is none.

        A file with a browser of its own keeps it, whatever the setting says:
        a pager given a spreadsheet, a deck, a PDF or an image shows the bytes
        of the container, which is nobody's idea of viewing it.  Choosing a
        pager answers the question about *text*.
        """
        if has_own_browser(fs, path):
            return False
        argv = self._external_viewer()
        if argv is None:
            return False
        if self._program_in_pane(fs, path, argv, editing=False):
            return True

        def run(cmd: list[str], cwd: str | None) -> int | None:
            return self._suspend_and_run(
                cmd, cwd, "", fail_label=f"Could not start {argv[0]}")

        self._set_message(extedit.view(fs, path, argv, run))
        return True

    def _edit_externally(self, fs: FileSystem, path: str) -> bool:
        """Edit ``path`` with the configured editor; False if there is none."""
        argv = self._external_editor()
        if argv is None:
            return False
        if self._program_in_pane(fs, path, argv, editing=True):
            return True

        def run(cmd: list[str], cwd: str | None) -> int | None:
            return self._suspend_and_run(
                cmd, cwd, "", fail_label=f"Could not start {argv[0]}")

        self._set_message(extedit.edit(fs, path, argv, run))
        return True

    # -- viewing and editing in a pane (MC-025) ------------------------------
    def _view_in_pane(self, view) -> bool:
        """Show a viewer or the editor in a pane, if the setting says so.

        False when it should run full-screen instead: the setting is off, or
        there is no pane to put it in.
        """
        if not self.open_in_pane:
            return False

        def make(ctx):
            # Short enough to sit beside the hidden pane's note on an
            # 80-column hint line.
            self._set_message("Ctrl-] then z: two panes, Tab: listing"
                              if isinstance(view, Editor) else
                              "z: two panes  Ctrl-] Tab: listing")
            return ViewPane(ctx, view)

        return self._open_in_pane(make)

    def _program_in_pane(self, fs: FileSystem, path: str, argv: list[str],
                         editing: bool) -> bool:
        """Run an outside pager or editor in a pane, if the setting says so.

        False when it should have the whole terminal instead, as it always
        did: the setting is off, or there is no pane to put it in.  Anything
        that stops it starting is said here, and counts as handled.
        """
        if not self.open_in_pane:
            return False

        def make(ctx):
            session = extedit.Session(fs, path, argv, editing)
            started = session.start()
            if isinstance(started, str):
                session.close()
                self._set_message(started)
                return None
            command, cwd = started
            try:
                pane = ProgramPane(ctx, session, command, cwd)
            except Exception:
                session.close()
                raise
            self._set_message("Ctrl-] then z: two panes, Tab: listing")
            return pane

        try:
            return self._open_in_pane(make)
        except Exception as exc:
            dialogs.message(self.stdscr, argv[0],
                            f"Could not start {argv[0]}: {exc}", error=True)
            return True

    def _pane_for_view(self) -> Panel | None:
        """The pane F3 and F4 open in, or None when both are busy.

        The other pane, as quick view does, so that the two-pane view puts the
        listing the file came from beside it, and F3 on the next file there
        shows it in the same place.  This pane, for this once, when the other
        is running a plug-in of its own.
        """
        for panel in (self.other, self.active):
            if panel.plugin is None or isinstance(panel.plugin,
                                                  (ViewPane, ProgramPane)):
                return panel
        return None

    def _open_in_pane(self, make) -> bool:
        """Install the pane ``make(ctx)`` builds where F3 and F4 open.

        Shown alone, at full width: single mode, turned on for the view and
        put back as it was when the view closes (:meth:`_view_closed`), with
        the keyboard back in the listing the file came from.  The next file
        shown in a view already open takes its place on the screen as it
        stands -- two panes stay two -- and closing it still puts back the
        layout from before the first.

        False when no pane is free; True otherwise, including when a view
        already there holds unsaved changes the user chose to keep, or
        ``make`` gives nothing.
        """
        from .plugin_api import PluginContext

        target = self._pane_for_view()
        if target is None:
            return False
        source = self.active
        screen = layout = (self.single, self._zoomed_by)
        replacing = target.plugin is not None
        if replacing:
            if not self._may_close(target):
                return True
            # Taken over, so closing the old view puts nothing back.
            layout, target.plugin.layout = target.plugin.layout, None
            self._close_plugin(target)
        other = self.right if target is self.left else self.left
        pane = make(PluginContext(app=self, own_panel=target, other_panel=other))
        if pane is None:
            self.single, self._zoomed_by = layout
            return True
        pane.source = source
        pane.layout = layout
        target.plugin = pane
        self.active = target
        self.single, self._zoomed_by = screen if replacing else (True, target)
        self._beside = None
        return True

    def _view_closed(self, pane) -> None:
        """Put back what opening a view changed: the layout and the keyboard.

        Called from the pane's ``on_exit``, however it closed -- its own keys,
        quitting, something else opened over it -- and safe to call twice.
        """
        if pane.layout is None:
            return
        self.single, self._zoomed_by = pane.layout
        pane.layout = None
        self._beside = None
        self.active = pane.source
        if pane.editing:
            # An outside editor's saves were never seen, and a new file may
            # only now exist: the listing shows what the editing left.
            self._reload_after_change(pane.source)

    def _unsaved_in(self, fs: FileSystem, paths: list[str]) -> str | None:
        """A file open with unsaved changes in a pane, that ``paths`` would
        delete, move or overwrite (one of them, or under one); its name."""
        for panel in (self.left, self.right):
            held = getattr(panel.plugin, "unsaved_file", None)
            found = held() if held is not None else None
            if found is None:
                continue
            for path in paths:
                if overlapping(fs, path, found[0], found[1]):
                    return found[0].basename(found[1])
        return None

    def _refuse_unsaved(self, fs: FileSystem, paths: list[str]) -> bool:
        """Refuse, saying why, to touch a file open with unsaved changes.

        The editor in a pane holds the file's text: deleting or moving the
        file under it, or copying another over it, would leave the next save
        writing somewhere unexpected or putting back what was removed.
        """
        name = self._unsaved_in(fs, paths)
        if name is None:
            return False
        dialogs.message(self.stdscr, "Open in the editor",
                        f"{name} is open in a pane with unsaved changes.\n"
                        f"Save it or close it first.", error=True)
        return True

    # -- mouse ------------------------------------------------------------
    def _handle_mouse(self) -> None:
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return

        # The menu bar owns the top row: a click there opens a menu, exactly
        # as clicking a caption did in the IDEs.
        if my == 0:
            for index, (start, span) in enumerate(menu_layout()):
                if start <= mx < start + span:
                    self.open_menu(index)
                    return
            return

        target = None
        for panel, y, x, h, w in self._panel_boxes:
            if x <= mx < x + w and y <= my < y + h:
                target = (panel, y, x, h, w)
                break
        if target is None:
            return
        panel, y, x, h, w = target
        body_h = h - 3
        self.active = panel  # clicking a pane focuses it

        # Mouse wheel scrolls the pane under the pointer.
        wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
        wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)

        # A pane owned by a plugin: focus it, and let the wheel scroll its
        # output; clicks otherwise stay with the plugin's own key handling.
        # A plug-in with a wheel() of its own gets the wheel itself: in the
        # terminal PgUp and PgDn belong to the program, so sending those
        # would page through less instead of scrolling the history.
        if panel.plugin is not None:
            wheel = getattr(panel.plugin, "wheel", None)
            try:
                if wheel_up and bstate & wheel_up:
                    if wheel is not None:
                        wheel(3)
                    else:
                        panel.plugin.handle_key(curses.KEY_PPAGE)
                elif wheel_down and bstate & wheel_down:
                    if wheel is not None:
                        wheel(-3)
                    else:
                        panel.plugin.handle_key(curses.KEY_NPAGE)
            except Exception:
                pass
            return
        if wheel_up and bstate & wheel_up:
            panel.move(-3)
            return
        if wheel_down and bstate & wheel_down:
            panel.move(3)
            return

        row = my - (y + 2)
        index = panel.top + row if 0 <= row < body_h else None
        on_entry = index is not None and index < len(panel.entries)

        if on_entry:
            panel.move_to(index)
            if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                self._activate_entry()
            elif bstate & (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED):
                self._context_menu()
        elif bstate & (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED):
            # Right-click on the header/empty area: still offer the menu.
            self._context_menu()

    def _context_menu(self) -> None:
        panel = self.active
        entry = panel.current()
        name = entry.name if entry and entry.name != Panel.PARENT else None
        header = name or panel.fs.basename(panel.path) or panel.path

        labels = ["View", "Edit", "Run...", "Copy to other pane",
                  "Move to other pane", "Extract archive to other pane",
                  "Rename", "Delete", "Tag / untag",
                  "New directory", "New file", "Compare with other pane",
                  "Head + tail in this pane", "Home directory",
                  "Trash folder", "Same location in other pane",
                  "Presets (go to / save)", "Find files here",
                  "Terminal in this pane", "Full-screen shell", "Cancel"]
        actions = ["view", "edit", "run", "copy", "move", "extract", "rename",
                   "delete", "tag", "mkdir", "touch", "compare", "peek",
                   "home", "open_trash", "mirror", "presets", "find",
                   "terminal", "shell", None]
        choice = dialogs.menu(self.stdscr, header[:40], labels)
        if choice is None:
            return
        action = actions[choice]
        if action == "view":
            self._view()
        elif action == "edit":
            self._edit()
        elif action == "run":
            self._run_current()
        elif action == "copy":
            self._copy()
        elif action == "move":
            self._move()
        elif action == "extract":
            self._extract()
        elif action == "rename":
            self._rename()
        elif action == "delete":
            self._delete()
        elif action == "tag":
            panel.toggle_select()
        elif action == "mkdir":
            self._mkdir()
        elif action == "touch":
            self._touch()
        elif action == "compare":
            self._compare()
        elif action == "peek":
            self._peek()
        elif action == "home":
            self._go_home()
        elif action == "open_trash":
            self._open_trash()
        elif action == "mirror":
            self._mirror_to_other_pane()
        elif action == "presets":
            self._presets_menu()
        elif action == "find":
            self._find_files()
        elif action == "terminal":
            self._open_terminal_pane()
        elif action == "shell":
            self._open_terminal()

    def _rename(self) -> None:
        panel = self.active
        entry = panel.current()
        if entry is None or entry.name == Panel.PARENT:
            return
        new_name = dialogs.prompt(self.stdscr, "Rename", "New name:",
                                  default=entry.name)
        if not new_name or new_name == entry.name:
            return
        src = panel.fs.join(panel.path, entry.name)
        dst = panel.fs.join(panel.path, new_name)
        if self._refuse_unsaved(panel.fs, [src, dst]):
            return
        if self._rename_would_replace(panel.fs, src, dst) and not dialogs.confirm(
                self.stdscr, "Rename",
                f"'{new_name}' already exists.\nReplace it?"):
            return
        try:
            panel.fs.rename(src, dst)
            self._reload_after_change(panel, keep_name=new_name)
            self._set_message(f"Renamed to {new_name}")
        except Exception as exc:
            dialogs.message(self.stdscr, "Rename error", str(exc), error=True)

    @staticmethod
    def _rename_would_replace(fs: FileSystem, src: str, dst: str) -> bool:
        """Whether renaming ``src`` to ``dst`` would replace something else.

        Every backend's rename replaces an existing file without a word, so
        that is asked first.  Not a directory in the way -- a rename onto one
        that holds anything fails and says so -- and not ``src`` itself under
        another case on a disk that ignores case, where renaming ``readme`` to
        ``README`` finds "README" already there and it is the same file.
        """
        try:
            existing = fs.stat(dst)
        except Exception:
            return False
        if existing.is_dir and not existing.is_symlink:
            return False
        if isinstance(fs, LocalFileSystem):
            try:
                return not os.path.samefile(src, dst)
            except OSError:
                return True
        return True

    def _find_files(self) -> None:
        """Search the active pane's tree; browse results, jump/view/edit."""
        from .findfiles import FindBrowser, collect_matches

        panel = self.active
        pattern = dialogs.prompt(
            self.stdscr, "Find files",
            "Name (substring or glob, e.g. conf or *.py):")
        if not pattern:
            return

        dlg = dialogs.ProgressDialog(self.stdscr, "Find files")
        dlg.set_overall(f"Searching under {panel.fs.label()}:{panel.path}")

        def progress(count: int, rel: str) -> None:
            dlg.update(0, 0, f"{count} found -- in {rel}")

        try:
            matches, truncated = collect_matches(
                panel.fs, panel.path, pattern,
                cancel=dlg.cancelled, progress=progress)
        finally:
            dlg.close()

        if not matches:
            dialogs.message(self.stdscr, "Find files",
                            f"No matches for '{pattern}'.")
            return

        browser = FindBrowser(panel.fs, panel.path, pattern, matches,
                              truncated)
        rel = browser.run(self.stdscr)
        curses.curs_set(0)
        if rel:
            parts = [p for p in rel.split("/") if p]
            parent = panel.fs.join(panel.path, *parts[:-1]) if len(parts) > 1 \
                else panel.path
            if panel.chdir(parent, keep_name=parts[-1]):
                self._set_message(f"Jumped to {parts[-1]}")
            else:
                dialogs.message(self.stdscr, "Find files",
                                f"Cannot open:\n{parent}", error=True)

    def _go_to_path(self) -> None:
        panel = self.active
        target = dialogs.prompt(self.stdscr, "Go to directory", "Path:",
                                default=panel.path)
        if not target:
            return
        if not panel.chdir(target):
            dialogs.message(self.stdscr, "Error",
                            f"Cannot open:\n{target}", error=True)

    def _go_home(self) -> None:
        """Jump the active pane to its own filesystem's home directory.

        On a remote pane that is the remote account's home, so the key means
        the same thing wherever the pane happens to be pointing.
        """
        panel = self.active
        try:
            home = panel.fs.home()
        except Exception as exc:
            dialogs.message(self.stdscr, "Error",
                            f"Cannot determine the home directory:\n{exc}",
                            error=True)
            return
        if panel.chdir(home):
            self._set_message(f"Home: {panel.fs.label()}:{panel.path}")
        else:
            dialogs.message(self.stdscr, "Error",
                            f"Cannot open home directory:\n{home}", error=True)

    def _open_trash(self) -> None:
        """Point the active pane at the trash; ``<`` comes back (MC-024).

        The trash ``F8`` would use from here: on a local pane, that of the
        disk the pane is on if the disk has one, and otherwise the home trash.
        From a server or an archive it is the home trash, through a local
        backend as a local preset would be -- the trash is this computer's,
        and nothing on the far side of a connection is ever called one.
        """
        panel = self.active
        if getattr(panel.fs, "scheme", "") == "local":
            fs, where = panel.fs, trash_location(panel.path)
        else:
            fs, where = self._local_backend(), trash_location()
        if panel.set_location(fs, where):
            self._set_message(f"Trash: {where} -- '<' goes back")
            return
        reason = str(panel.failure)
        text = f"Cannot list the trash:\n{where}\n{reason}"
        if sys.platform == "darwin" and "not permitted" in reason:
            # macOS privacy (TCC) can refuse a terminal a listing of
            # ~/.Trash until it is given Full Disk Access.
            text += ("\n\nmacOS shows the trash only to a terminal with "
                     "Full Disk Access: System Settings > Privacy & "
                     "Security > Full Disk Access, then restart it.")
        dialogs.message(self.stdscr, "Trash", text, error=True)

    def _local_backend(self) -> FileSystem:
        """An open local backend, the other pane's first, or a new one kept.

        Asked for when the active pane is on a server or in an archive.
        """
        for fs in [self.other.fs] + list(reversed(self._backends)):
            if getattr(fs, "scheme", "") == "local":
                return fs
        fs = LocalFileSystem()
        self._backends.append(fs)
        return fs

    def _back(self) -> None:
        panel = self.active
        if panel.back(self._revive):
            self._set_message(f"Back: {panel.fs.label()}:{panel.path}")
        else:
            self._set_message("Nothing further back in this pane's history")

    def _forward(self) -> None:
        panel = self.active
        if panel.forward(self._revive):
            self._set_message(f"Forward: {panel.fs.label()}:{panel.path}")
        else:
            self._set_message("Nothing further forward in this pane's history")

    def _revive(self, fs: FileSystem) -> FileSystem | None:
        """The backend a step through history uses: ``fs``, open again.

        The last pane out of an archive closes it, but the history still has
        its places, so going back into one reopens it -- from the file as it
        now is, which may have changed.  An archive that will not reopen (it
        was deleted) answers ``None``, and that step is passed over.
        """
        if isinstance(fs, ArchiveFileSystem) and fs.closed:
            try:
                fs.reopen()
            except Exception:
                return None
            # Closed with everything else on the way out, as when entered.
            self._backends.append(fs)
        return fs

    def _mirror_to_other_pane(self) -> None:
        """Show this pane's location in the other pane as well.

        The panes then share one live connection, so a remote location is not
        dialled (or authenticated) a second time and a move between the two
        panes is a cheap same-server rename.  The other pane's own view
        settings (sort order, hidden files) are left alone.
        """
        panel = self.active
        other = self.other
        if other.plugin is not None:
            self._set_message("Other pane is busy -- close its plug-in "
                              "or terminal first")
            return
        if other.fs is panel.fs and other.path == panel.path:
            self._set_message("Both panes already show this location")
            return
        if not other.set_location(panel.fs, panel.path):
            dialogs.message(self.stdscr, "Error",
                            f"Cannot open in the other pane:\n{panel.path}",
                            error=True)
            return
        which = "Hidden pane" if self._hidden(other) else "Other pane"
        self._set_message(f"{which}: {other.fs.label()}:{other.path} "
                          "-- '<' there goes back")

    def _sort_menu(self) -> None:
        options = ["Name", "Name (natural: run_2 before run_10)", "Extension",
                   "Size", "Modify time"]
        keys = ["name", "natural", "ext", "size", "mtime"]
        said = ["name", "natural name order", "extension", "size",
                "modify time"]
        choice = dialogs.menu(self.stdscr, "Sort by", options)
        if choice is not None:
            self.active.set_sort(keys[choice])
            self._set_message(f"Sorted by {said[choice]}")

    def _connect(self, info: dict) -> FileSystem:
        """Open the backend described by a connect-dialog/preset dict.

        Raises whatever the backend raises; callers report it.
        """
        if info["scheme"] == "local":
            return LocalFileSystem()
        if info["scheme"] == "sftp":
            return SFTPFileSystem(
                host=info["host"], username=info["username"],
                password=info.get("password"), port=info["port"],
                key_filename=info.get("key_filename"),
            )
        if info["scheme"] == "ssh":
            return SSHFileSystem(
                host=info["host"], username=info["username"],
                password=info.get("password"), port=info["port"],
                key_filename=info.get("key_filename"),
            )
        return FTPFileSystem(
            host=info["host"], username=info["username"] or "anonymous",
            password=info.get("password") or "", port=info["port"],
        )

    def _open_location(self) -> None:
        info = dialogs.connect_dialog(self.stdscr)
        if not info:
            return
        panel = self.active
        try:
            fs = self._connect(info)
        except FileSystemError as exc:
            dialogs.message(self.stdscr, "Connection failed", str(exc), error=True)
            return
        except Exception as exc:
            dialogs.message(self.stdscr, "Connection failed",
                            f"{type(exc).__name__}: {exc}", error=True)
            return

        self._backends.append(fs)
        # Through set_location, like every other change of place, so that
        # "<" comes back from a connection as it does from anywhere else.
        if not panel.set_location(fs, fs.home()):
            dialogs.message(self.stdscr, "Open location",
                            f"Connected to {fs.label()}, but its home "
                            "directory cannot be listed.", error=True)
            return
        self._set_message(f"Connected: {fs.label()}")

    # -- presets (saved locations) ------------------------------------------
    def _presets_menu(self) -> None:
        """List the saved locations, and offer to save or delete one.

        Every preset answers to a letter, so returning to a saved place is
        ``b`` and one keystroke rather than a walk down the list -- which is
        the point of having saved it.  The three fixed actions keep their own
        initials whatever the presets are called, so ``s`` is always Save and
        ``d`` always Delete; the presets are assigned around them.
        """
        items = presets.load()
        labels = [f"{p.name}  --  {p.label()}"[:60] for p in items]
        extras = ["Save this location as a preset...",
                  "Delete a preset...", "Cancel"]
        keys = dialogs.accelerators([p.name for p in items], reserved="sdc")
        choice = dialogs.menu(self.stdscr, "Presets", labels + extras,
                              keys=keys + ["s", "d", "c"])
        if choice is None or choice == len(labels) + 2:
            return
        if choice < len(labels):
            self._open_preset(items[choice])
        elif choice == len(labels):
            self._save_preset()
        else:
            self._delete_preset(items)

    def _open_preset(self, preset: presets.Preset) -> None:
        """Point the active pane at a saved location."""
        panel = self.active
        fs = self._live_fs_for(preset) or self._connect_preset(preset)
        if fs is None:
            return
        if panel.set_location(fs, preset.path):
            self._set_message(f"{preset.name}: {fs.label()}:{panel.path}")
            return
        # The connection is good but the directory has gone: land on the
        # location's home rather than leaving the pane where it was.
        if panel.set_location(fs, fs.home()):
            self._set_message(f"'{preset.name}': {preset.path} is gone -- "
                              f"opened {panel.path}")
        else:
            dialogs.message(self.stdscr, "Preset",
                            f"Cannot open:\n{preset.label()}", error=True)

    def _live_fs_for(self, preset: presets.Preset) -> FileSystem | None:
        """An open backend the preset can reuse, newest first, or None.

        Reusing the connection a pane already holds means a saved remote
        location opens instantly and without a second authentication.
        """
        candidates = [self.active.fs, self.other.fs] + list(
            reversed(self._backends))
        for fs in candidates:
            if preset.matches(fs):
                return fs
        return None

    def _connect_preset(self, preset: presets.Preset) -> FileSystem | None:
        """Dial a preset's location, asking for a password only if needed."""
        fs = self._dial(preset)
        if fs is not None:
            self._backends.append(fs)
        return fs

    def _dial(self, preset: presets.Preset) -> FileSystem | None:
        """A new connection to a preset's place, or ``None`` having said why."""
        info = preset.connect_info()
        try:
            fs = self._connect(info)
        except Exception as exc:
            # Presets deliberately store no password, so the first attempt
            # goes through the agent and default keys, like ssh itself.  Give
            # the user a chance to supply one before giving up.
            password = dialogs.prompt(
                self.stdscr, f"Connect to {preset.host}",
                "Password (Esc/blank = give up):", is_password=True)
            if not password:
                dialogs.message(self.stdscr, "Connection failed",
                                f"{preset.label()}\n\n{exc}", error=True)
                return None
            info["password"] = password
            try:
                fs = self._connect(info)
            except Exception as exc2:
                dialogs.message(self.stdscr, "Connection failed",
                                f"{preset.label()}\n\n{exc2}", error=True)
                return None
        return fs

    def _save_preset(self) -> None:
        """Save the active pane's location under a name."""
        panel = self.active
        default = panel.fs.basename(panel.path) or panel.fs.label()
        name = dialogs.prompt(self.stdscr, "Save preset",
                              f"Name for {panel.fs.label()}:{panel.path}",
                              default=default)
        if name is None:
            return
        name = name.strip()
        if not presets.valid_name(name):
            dialogs.message(self.stdscr, "Save preset",
                            "A preset name cannot be empty, contain [ or ],\n"
                            "or be called DEFAULT.", error=True)
            return
        items = presets.load()
        if any(p.name == name for p in items):
            if not dialogs.confirm(self.stdscr, "Save preset",
                                   f"'{name}' already exists.\nReplace it?"):
                return
        preset = presets.from_location(name, panel.fs, panel.path)
        try:
            presets.save(presets.add(preset, items))
        except OSError as exc:
            dialogs.message(self.stdscr, "Save preset",
                            f"Could not write presets:\n{exc}", error=True)
            return
        note = "" if preset.scheme == "local" else " (no password stored)"
        self._set_message(f"Preset '{name}' saved -- press b to return{note}")

    def _delete_preset(self, items: list[presets.Preset]) -> None:
        if not items:
            dialogs.message(self.stdscr, "Presets", "No presets saved yet.")
            return
        labels = [f"{p.name}  --  {p.label()}"[:60] for p in items]
        keys = dialogs.accelerators([p.name for p in items], reserved="c")
        choice = dialogs.menu(self.stdscr, "Delete preset", labels + ["Cancel"],
                              keys=keys + ["c"])
        if choice is None or choice == len(items):
            return
        victim = items[choice]
        if not dialogs.confirm(self.stdscr, "Delete preset",
                               f"Delete '{victim.name}'?\n{victim.label()}"):
            return
        try:
            presets.save(presets.remove(victim.name, items))
        except OSError as exc:
            dialogs.message(self.stdscr, "Delete preset",
                            f"Could not write presets:\n{exc}", error=True)
            return
        self._set_message(f"Preset '{victim.name}' deleted")

    def _view(self) -> None:
        panel = self.active
        target = panel.current_path()
        if not target:
            return
        entry = panel.current()
        if entry and entry.is_dir:
            panel.enter()
            return
        if self._view_externally(panel.fs, target):
            return
        try:
            view = viewer_for(panel.fs, target)
            if not self._view_in_pane(view):
                view.run(self.stdscr)
        except Exception as exc:
            dialogs.message(self.stdscr, "View error", str(exc), error=True)
        curses.curs_set(0)

    def _sole_file(self, panel) -> tuple[str | None, str]:
        """The one file a pane is offering, or ``None`` and why not.

        Tagged entries win over the cursor -- that is what
        :meth:`Panel.selected_entries` already means -- but *two* tags are an
        ambiguity rather than a choice, and are refused instead of silently
        taking the first one.
        """
        entries = panel.selected_entries()
        if not entries:
            return None, "nothing is selected"
        if len(entries) > 1:
            return None, f"{len(entries)} files are tagged; tag one"
        entry = entries[0]
        if entry.is_dir:
            return None, f"{entry.name} is a directory"
        return panel.fs.join(panel.path, entry.name), ""

    def _compare(self) -> None:
        """Each pane's file, side by side, the two scrolled as one.

        The screen's left half is the left pane's file whichever pane is
        active: a comparison that swapped sides depending on where the cursor
        happened to be would be a puzzle rather than a picture.
        """
        picked = []
        for panel, side in ((self.left, "left"), (self.right, "right")):
            path, why = self._sole_file(panel)
            if path is None:
                hidden = " (hidden)" if self._hidden(panel) else ""
                self._set_message(f"Compare: {side} pane{hidden} -- {why}")
                return
            picked.append((panel.fs, path))
        try:
            Comparison(picked[0][0], picked[0][1],
                       picked[1][0], picked[1][1]).run(self.stdscr)
        except Exception as exc:
            dialogs.message(self.stdscr, "Compare error", str(exc), error=True)
        curses.curs_set(0)

    def _peek(self) -> None:
        """Turn this pane into head+tail of the file the *other* pane is on.

        A pane, not a full screen: the listing you are choosing from keeps its
        cursor, and moving that cursor moves what this pane shows, so looking
        into ten files costs ten arrow keys rather than ten windows opened and
        closed.  Pressing it again gives the pane back to its listing.
        """
        from .plugin_api import PluginContext

        panel = self.active
        if isinstance(panel.plugin, PeekPane):
            self._close_plugin(panel)
            panel.refresh()
            self._set_message("Head + tail closed")
            return
        if not self._may_close(panel):
            return
        self._close_plugin(panel)
        ctx = PluginContext(app=self, own_panel=panel, other_panel=self.other)
        panel.plugin = PeekPane(ctx)
        self._set_message("Head + tail of the other pane's file "
                          "-- +/- lines, Esc closes")

    def _quick_view(self) -> None:
        """Show the file under the cursor in the other pane, or stop showing it.

        The other pane follows this one's cursor in the viewer F3 would open,
        and keeps its own listing underneath, so turning it off gives it back
        exactly as it was.  In single mode both panes show while it is on --
        a plug-in working on the other pane always pairs them.  A pane
        running something of its own is not taken over.
        """
        from .plugin_api import PluginContext

        for panel in (self.left, self.right):
            if isinstance(panel.plugin, QuickViewPane):
                self._close_plugin(panel)
                panel.refresh()
                self._set_message("Quick view closed")
                return
        shown_in = self.other
        if shown_in.plugin is not None:
            self._set_message("The other pane is busy -- close it first")
            return
        ctx = PluginContext(app=self, own_panel=shown_in,
                            other_panel=self.active)
        shown_in.plugin = QuickViewPane(ctx)
        self._set_message("Quick view -- Tab into it, Enter for the full "
                          "viewer, Ctrl-Q closes")

    def _edit(self) -> None:
        panel = self.active
        entry = panel.current()
        if entry and entry.is_dir:
            self._set_message("Cannot edit a directory")
            return
        target = panel.current_path()
        if not target:
            # Offer to create a new file.
            name = dialogs.prompt(self.stdscr, "Edit new file", "File name:")
            if not name:
                return
            target = panel.fs.join(panel.path, name)
        if not self._edit_externally(panel.fs, target):
            try:
                editor = Editor(panel.fs, target)
                if not self._view_in_pane(editor):
                    editor.run(self.stdscr)
            except Exception as exc:
                dialogs.message(self.stdscr, "Edit error", str(exc), error=True)
            curses.curs_set(0)
        panel.refresh()

    def _copy(self) -> None:
        self._transfer(move=False)

    def _move(self) -> None:
        self._transfer(move=True)

    def _transfer(self, move: bool) -> None:
        src_panel = self.active
        dst_panel = self.other
        sources = src_panel.selected_entries()
        if not sources:
            return
        if move and self._refuse_unsaved(
                src_panel.fs, [src_panel.fs.join(src_panel.path, s.name)
                               for s in sources]):
            return
        verb = "Move" if move else "Copy"

        # Ask for the destination directory, defaulting to the other pane --
        # named, when single mode has it out of sight, so nothing lands in a
        # directory the user has forgotten the hidden pane was showing.
        default_dest = dst_panel.path
        where = self._destination(dst_panel)
        if len(sources) == 1:
            prompt_label = f"{verb} '{sources[0].name}'{self._not_shown(src_panel)} to{where}:"
        else:
            prompt_label = (f"{verb} {len(sources)} items"
                            f"{self._not_shown(src_panel)} to{where}:")
        keep_mode = dialogs.Toggle("Keep permissions", "p")
        background = dialogs.Toggle("Background", "b", on=False)
        dest = dialogs.prompt(self.stdscr, f"{verb}", prompt_label,
                              default=default_dest,
                              toggles=[keep_mode, background])
        if dest is None:
            return
        dst_fs = dst_panel.fs
        # Against the destination *pane*, not the process's working directory.
        # The prompt is pre-filled with the pane's path, so clearing it and
        # typing "backup" is the ordinary way to ask for a new folder beside
        # what you are looking at -- and that used to write it next to
        # wherever the shell that started the application happened to be,
        # where the pane would never show it.
        dest = dst_fs.normpath(dst_panel.resolve(dest))
        if self._refuse_unsaved(dst_fs, [dst_fs.join(dest, s.name)
                                         for s in sources]):
            return
        if background.on:
            self._background(move, src_panel, dst_panel, sources, dest,
                             keep_mode.on)
            return

        # Pre-count for an overall byte total across all sources.
        try:
            grand_total = 0
            file_total = 0
            for s in sources:
                fc, bc = count_tree(src_panel.fs, src_panel.fs.join(src_panel.path, s.name))
                file_total += fc
                grand_total += bc
        except Exception:
            grand_total = 0

        dlg = dialogs.ProgressDialog(self.stdscr, verb)

        def progress(cur: int, total: int, label: str) -> None:
            dlg.update(cur, total, label)

        cancelled = False
        errors: list[str] = []
        #: What the transfer could not do exactly as asked -- targets kept,
        #: links it could not make, modes it could not set.  Named rather than
        #: left unmentioned: the alternative is a copy that looks complete and
        #: is not.
        report = TransferReport()
        conflicts = TransferConflicts(self.stdscr, verb)
        #: Moved sources, deleted only once the destination's batch has closed
        #: cleanly: an archive has not written its copies until then.
        moved: list[str] = []
        try:
            try:
                with dst_fs.batch():
                    for i, entry in enumerate(sources):
                        if dlg.cancelled():
                            cancelled = True
                            break
                        src = src_panel.fs.join(src_panel.path, entry.name)
                        target = dst_fs.join(dest, entry.name)
                        dlg.set_overall(
                            f"{verb} {i + 1}/{len(sources)}: {entry.name}")

                        try:
                            if move:
                                move_path(
                                    src_panel.fs, src, dst_fs, target,
                                    progress, dlg.cancelled, deferred=moved,
                                    on_conflict=conflicts,
                                    keep_mode=keep_mode.on, links=True,
                                    report=report)
                            else:
                                copy_path(
                                    src_panel.fs, src, dst_fs, target,
                                    progress, dlg.cancelled,
                                    on_conflict=conflicts,
                                    keep_mode=keep_mode.on, links=True,
                                    report=report)
                        except OperationCancelled:
                            cancelled = True
                            break
                        except Exception as exc:
                            errors.append(f"{entry.name}: {exc}")
            except Exception as exc:
                # Every item's own failure is caught above, so this is the
                # batch failing to write what was copied -- none of which has
                # landed, so none of the sources can go.
                errors.append(str(exc))
                moved.clear()
            self._delete_sources(src_panel.fs, moved, errors)
        finally:
            dlg.close()

        src_panel.clear_selection()
        src_panel.refresh()
        dst_panel.refresh()

        if errors:
            dialogs.message(self.stdscr, f"{verb} errors",
                            "\n".join(errors[:8]), error=True)
        elif cancelled:
            self._set_message(f"{verb} cancelled")
        else:
            self._set_message(
                f"{verb} complete: {len(sources)} item(s)"
                + self._transfer_note(report, move))
        if report.links_left:
            dialogs.message(
                self.stdscr, f"{verb}: symlinked directories",
                "These are links, not directories, and there is no way to "
                "make a link on the other side, so they were left alone:\n"
                + "\n".join(report.links_left[:8])
                + ("\n\nThe source was kept." if move else ""))
        if report.links_as_files:
            dialogs.message(
                self.stdscr, f"{verb}: links copied as files",
                "The other side cannot hold a symbolic link, so these were "
                "copied as the files they point at:\n"
                + "\n".join(report.links_as_files[:8]))

    @staticmethod
    def _delete_sources(fs: FileSystem, paths: list[str],
                        errors: list[str]) -> None:
        """Delete what a move has copied, as one change to ``fs``.

        Batched because the sources can be in an archive, which would
        otherwise be rewritten once for every item moved out of it.
        """
        try:
            with fs.batch():
                for path in paths:
                    try:
                        fs.delete_tree(path)
                    except Exception as exc:
                        errors.append(f"{fs.basename(path)}: {exc}")
        except Exception as exc:
            # The deletes could not be written: the sources are all still
            # there, beside their copies.
            errors.append(str(exc))

    @staticmethod
    def _transfer_note(report: TransferReport, move: bool) -> str:
        """The tail of the status line: what was not done exactly as asked."""
        notes = []
        if report.kept:
            notes.append(f"{len(report.kept)} existing kept")
        if report.links_left:
            count = len(report.links_left)
            notes.append(f"{count} symlinked "
                         f"director{'y' if count == 1 else 'ies'} skipped")
        if report.links_as_files:
            notes.append(f"{len(report.links_as_files)} link(s) copied as files")
        if report.modes_lost:
            notes.append(f"permissions not kept on {len(report.modes_lost)}")
        if not notes:
            return ""
        kept_source = move and (report.kept or report.links_left)
        return " -- " + ", ".join(notes) + (", source kept" if kept_source else "")

    def _extract(self) -> None:
        """Unpack the archive here, or the tagged ones, into the other pane.

        Each archive lands in a folder of its own under the directory asked
        for -- named after the archive, unless everything in it is already in
        one folder, which is then the folder that lands.  That is the
        difference between ``project.zip`` arriving as ``project/`` and as
        ``project/project/``, and between a zip of loose files arriving in a
        folder and scattering itself through a directory that already has
        things in it.  Files keep the dates the archive gives them.
        """
        src_panel = self.active
        dst_panel = self.other
        archives = [e for e in src_panel.selected_entries()
                    if not e.is_dir and is_archive(e.name)]
        if not archives:
            dialogs.message(self.stdscr, "Extract",
                            "Point the cursor at a zip or tar archive to "
                            "extract it, or tag several.")
            return
        where = self._destination(dst_panel)
        if where:
            where = f" in{where}"
        if len(archives) == 1:
            label = (f"Extract '{archives[0].name}' into its own folder"
                     f"{where or ' under'}:")
        else:
            label = (f"Extract {len(archives)} archives, each into its own "
                     f"folder,{where or ' under'}:")
        dest = dialogs.prompt(self.stdscr, "Extract", label,
                              default=dst_panel.path)
        if dest is None:
            return
        dst_fs = dst_panel.fs
        dest = dst_fs.normpath(dst_panel.resolve(dest))

        dlg = dialogs.ProgressDialog(self.stdscr, "Extract")
        failed: list[str] = []
        landed: list[str] = []
        cancelled = False
        try:
            with dst_fs.batch():
                for i, entry in enumerate(archives):
                    if dlg.cancelled():
                        cancelled = True
                        break
                    dlg.set_overall(
                        f"Extract {i + 1}/{len(archives)}: {entry.name}")
                    try:
                        target, problems = self._extract_one(
                            src_panel.fs,
                            src_panel.fs.join(src_panel.path, entry.name),
                            dst_fs, dest, dlg)
                    except OperationCancelled:
                        cancelled = True
                        break
                    except Exception as exc:
                        failed.append(f"{entry.name}: {exc}")
                        continue
                    landed.append(target)
                    failed += [f"{entry.name}: {p}" for p in problems]
        except Exception as exc:
            # The destination could not write what was unpacked into it.
            failed.append(str(exc))
            landed.clear()
        finally:
            dlg.close()

        src_panel.clear_selection()
        src_panel.refresh()
        dst_panel.refresh()
        if failed:
            dialogs.message(self.stdscr, "Extract problems",
                            "\n".join(failed[:8])
                            + (f"\n... and {len(failed) - 8} more"
                               if len(failed) > 8 else ""),
                            error=True)
        note = f" -- {len(failed)} problem(s)" if failed else ""
        if cancelled:
            self._set_message("Extract cancelled")
        elif not landed:
            self._set_message("Nothing extracted" + note)
        elif len(archives) == 1:
            self._set_message(f"Extracted to {landed[0]}" + note)
        else:
            self._set_message(f"Extracted {len(landed)} of {len(archives)} "
                              f"archives into {dest}" + note)

    @staticmethod
    def _extract_one(src_fs: FileSystem, path: str, dst_fs: FileSystem,
                     dest: str, dlg) -> tuple[str, list[str]]:
        """Unpack one archive under ``dest``.

        Returns the folder it landed in and the members that would not come
        out -- the rest still come.  An archive that will not open raises.
        """
        source = open_archive(src_fs, path)
        try:
            top = source.listdir("/")
            if len(top) == 1 and top[0].is_dir:
                # It brings its own folder, so it goes straight into dest.
                into, target = dest, dst_fs.join(dest, top[0].name)
            else:
                stem = archive_stem(src_fs.basename(path))
                into = target = dst_fs.join(dest, stem)
            problems = copy_contents(source, "/", dst_fs, into, dlg.update,
                                     dlg.cancelled, preserve_mtime=True)
        finally:
            source.close()
        return target, problems

    def _mkdir(self) -> None:
        panel = self.active
        name = dialogs.prompt(self.stdscr, "Make directory", "Directory name:")
        if not name:
            return
        target = panel.fs.join(panel.path, name)
        try:
            panel.fs.makedirs(target)
            self._reload_after_change(
                panel, keep_name=name.split(panel.fs.sep)[0].split("/")[0])
            self._set_message(f"Created {name}")
        except Exception as exc:
            dialogs.message(self.stdscr, "Mkdir error", str(exc), error=True)

    def _touch(self) -> None:
        """Create an empty file in this pane, or restamp one that is there.

        The name is taken relative to the pane's directory, exactly as
        :meth:`_mkdir` takes its own -- but no parent is created for it: a
        name with a directory in it that does not exist is an error here, the
        way ``touch a/b`` is at a shell, rather than a silent ``mkdir -p``.
        """
        panel = self.active
        name = dialogs.prompt(self.stdscr, "New file", "File name:")
        if not name:
            return
        target = panel.fs.join(panel.path, name)
        existed = panel.fs.exists(target)
        try:
            panel.fs.touch(target)
            self._reload_after_change(
                panel, keep_name=name.split(panel.fs.sep)[0].split("/")[0])
            self._set_message(("Touched " if existed else "Created ") + name)
        except Exception as exc:
            dialogs.message(self.stdscr, "New file error", str(exc), error=True)

    def _delete(self) -> None:
        panel = self.active
        targets = panel.selected_entries()
        if not targets:
            return
        if self._refuse_unsaved(panel.fs, [panel.fs.join(panel.path, t.name)
                                           for t in targets]):
            return
        if len(targets) == 1:
            text = f"Delete '{targets[0].name}'{self._not_shown(panel)}?"
        else:
            text = (f"Delete {len(targets)} selected items"
                    f"{self._not_shown(panel)}?")
        has_dir = any(t.is_dir for t in targets)
        if has_dir:
            text += "\n(directories are removed recursively)"
        # The trash is the local disk's: a server or an archive has none, so
        # there it is said plainly that this is for good.
        local = getattr(panel.fs, "scheme", "") == "local"
        # In the trash already, "to the trash" would only rename the item
        # inside it and leave its .trashinfo describing nothing, so for good
        # is the one way out offered, and the record goes with the item.
        in_trash = local and trash_root(panel.path) is not None
        to_trash = self.use_trash and local and not in_trash
        if in_trash:
            if dialogs.menu(self.stdscr, "Delete from the trash",
                            ["Delete permanently", "Cancel"], keys=["p", "c"],
                            text=text + "\nAlready in the trash: this "
                                        "cannot be undone.") != 0:
                return
        elif to_trash:
            choice = dialogs.menu(self.stdscr, "Delete",
                                  ["Move to trash", "Delete permanently",
                                   "Cancel"], keys=["t", "p", "c"], text=text)
            if choice is None or choice == 2:
                return
            to_trash = choice == 0
        elif not dialogs.confirm(self.stdscr, "Delete",
                                 text + "\nThis cannot be undone."):
            return

        # Decide where the cursor goes while the listing is still intact: the
        # nearest entry that is not being deleted.  Tagged items can be spread
        # anywhere above and below the bar, so the answer is rarely the index
        # the cursor happens to be on, and it has to be worked out before the
        # entries it is worked out from are gone.
        keep = panel.name_after_removing({t.name for t in targets})

        errors: list[str] = []
        removed: list[str] = []
        #: Items with no trash on their disk, put to the user afterwards.
        homeless: list[str] = []
        cancelled = False
        dlg = dialogs.ProgressDialog(self.stdscr, "Delete")
        try:
            # Batched, so deleting tagged files from an archive rewrites it
            # once rather than once per file.
            with panel.fs.batch():
                for i, entry in enumerate(targets):
                    if dlg.cancelled():
                        cancelled = True
                        break
                    dlg.set_overall(
                        f"Deleting {i + 1}/{len(targets)}: {entry.name}")
                    dlg.update(i, len(targets), entry.name)
                    target = panel.fs.join(panel.path, entry.name)
                    try:
                        if to_trash:
                            move_to_trash(target)
                        else:
                            panel.fs.delete_tree(target)
                    except TrashUnavailable:
                        homeless.append(target)
                        continue
                    except Exception as exc:
                        errors.append(f"{entry.name}: {exc}")
                        continue
                    removed.append(target)
                    if in_trash:
                        try:
                            forget(target)
                        except OSError as exc:
                            errors.append(f"{entry.name}: deleted, but not "
                                          f"its .trashinfo: {exc}")
        except Exception as exc:
            # The archive could not be written, so nothing went after all.
            errors.append(str(exc))
            removed.clear()
        finally:
            dlg.close()

        trashed = len(removed)
        if homeless and dialogs.menu(
                self.stdscr, "Delete", ["Delete permanently", "Skip them"],
                keys=["p", "s"],
                text=f"{len(homeless)} item(s) have no trash on their disk:\n"
                     + "\n".join(os.path.basename(h) for h in homeless[:6])) == 0:
            for target in homeless:
                try:
                    panel.fs.delete_tree(target)
                except Exception as exc:
                    errors.append(f"{panel.fs.basename(target)}: {exc}")
                    continue
                removed.append(target)

        panel.clear_selection()
        self._reload_after_change(panel, keep_name=keep, gone=tuple(removed))
        if errors:
            dialogs.message(self.stdscr, "Delete errors",
                            "\n".join(errors[:8]), error=True)
        elif cancelled:
            # Count what actually went, not what was asked for: stopping after
            # two of ten used to be reported as ten deleted.
            self._set_message(
                f"Delete cancelled -- {len(removed)} of {len(targets)} removed")
        elif to_trash:
            deleted = len(removed) - trashed
            self._set_message(f"Moved {trashed} item(s) to the trash"
                              + (f", deleted {deleted}" if deleted else ""))
        else:
            self._set_message(f"Deleted {len(removed)} item(s)"
                              + (" from the trash" if in_trash else ""))

    def _sync_scope_warning(self) -> str | None:
        """Text to show before syncing two panes, or None if they look sane.

        Both panes have already been listed to be drawn, so counting what is in
        them is free -- which is the point.  Anything more accurate would mean
        walking the trees, and walking the trees is the expensive thing the
        warning is trying to save the user from.

        Counting the pane's own listing rather than re-reading the directory
        also means the numbers in the dialog are the ones the pane holds,
        hidden files included or excluded exactly as the pane has them -- and
        a filter's hidden names counted, since sync walks past any filter.
        """
        sides = [("Left ", self.left), ("Right", self.right)]
        surveys = [(label, panel, survey_directory(panel.listed))
                   for label, panel in sides]
        if not any(survey.is_large for _l, _p, survey in surveys):
            return None

        lines: list[str] = []
        for label, panel, survey in surveys:
            lines.append(f"{label}: {panel.fs.label()}:{panel.path}"
                         + self._hidden_tag(panel))
            lines.append(f"       {survey.describe()}"
                         + ("   <-- large" if survey.is_large else ""))
        lines += [
            "",
            "Sync descends into every subdirectory on both sides and",
            "copies the newer of every pair of files, in both",
            "directions. On a directory this size that can take a long",
            "time, and it may copy far more than you meant to.",
            "",
            "Nothing is ever deleted, and you still get to see the full",
            "plan before anything is written.",
            "",
            "Scan these two directories anyway?",
        ]
        return "\n".join(lines)

    def _hidden_tag(self, panel: Panel) -> str:
        """A sync line's mark for the side single mode keeps out of sight.

        Sync writes into both panes, so the side nobody can see is the one
        its confirmation most needs to point out.
        """
        return "   (hidden pane)" if self._hidden(panel) else ""

    def _sync(self) -> None:
        left, right = self.left, self.right
        warning = self._sync_scope_warning()
        if warning and not dialogs.confirm(self.stdscr, "Synchronize panes",
                                           warning):
            self._set_message("Sync cancelled")
            return

        # The scan gets its own progress dialog. It is the slow half on a big
        # tree -- and the half with nothing to show for itself -- so it needs
        # the escape hatch more than the copying that follows does.
        scan = dialogs.ProgressDialog(self.stdscr, "Scanning for differences")
        scan.set_overall("Comparing the two directories ...")
        try:
            plan = build_sync_plan(left.fs, left.path, right.fs, right.path,
                                   progress=scan.update, cancel=scan.cancelled)
        except OperationCancelled:
            scan.close()
            self._set_message("Sync cancelled")
            return
        except Exception as exc:
            scan.close()
            dialogs.message(self.stdscr, "Sync error", str(exc), error=True)
            return
        finally:
            scan.close()

        if not plan:
            dialogs.message(self.stdscr, "Synchronize",
                            "The two directories are already in sync.")
            self._set_message("Already in sync")
            return

        # Build a preview.
        lines = [
            f"Left : {left.fs.label()}:{left.path}" + self._hidden_tag(left),
            f"Right: {right.fs.label()}:{right.path}" + self._hidden_tag(right),
            "",
            f"{len(plan.actions)} file(s), {human_size(plan.total_bytes).strip()} to copy:",
            "",
        ]
        for action in plan.actions[:10]:
            lines.append(action.render()[:60])
        if len(plan.actions) > 10:
            lines.append(f" ... and {len(plan.actions) - 10} more")
        lines.append("")
        lines.append("'->' copy left->right   '<-' copy right->left")

        if not dialogs.confirm(self.stdscr, "Synchronize panes",
                               "\n".join(lines), default_yes=True):
            self._set_message("Sync cancelled")
            return

        dlg = dialogs.ProgressDialog(self.stdscr, "Synchronize")

        def on_action(action, index, total):
            dlg.set_overall(f"{index + 1}/{total}  {action.direction} {action.rel}")

        try:
            # Batched, so a side that is an archive is rewritten once at the
            # end rather than twice for every file (once for the file and
            # once more for its timestamp).
            with left.fs.batch(), right.fs.batch():
                copied = execute_sync_plan(
                    plan, left.fs, right.fs,
                    progress=dlg.update, cancel=dlg.cancelled,
                    on_action=on_action,
                )
        except Exception as exc:
            dlg.close()
            dialogs.message(self.stdscr, "Sync error", str(exc), error=True)
            return
        finally:
            dlg.close()

        left.refresh()
        right.refresh()
        planned = len(plan.actions)
        if copied < planned:
            # execute_sync_plan stops where the cancel came, so a smaller count
            # is the only sign of it: "Synchronized" for three of fifty would
            # be the delete count's mistake in another place.
            self._set_message(
                f"Sync cancelled -- {copied} of {planned} file(s) copied")
        else:
            self._set_message(f"Synchronized: {copied} file(s) copied")

    def _help(self) -> None:
        text = (
            "Meridian Commander -- two-pane terminal file manager\n"
            "\n"
            "  Tab            switch active pane\n"
            "  Up/Down j/k    move cursor      PgUp/PgDn  page\n"
            "  Home/End       first / last\n"
            "  Enter / Right  enter dir / archive (zip, tar); run a\n"
            "                 script ('#!' line) or executable, with an\n"
            "                 arguments prompt (Esc cancels); view the rest\n"
            "  Backspace/Left parent directory\n"
            "  Insert / Space tag file    -  untag all    *  invert\n"
            "  + / \\          tag / untag the names matching a pattern\n"
            "  a              permissions, owner and group of what is tagged\n"
            "  Ctrl-Q         quick view: the other pane shows the cursor's file\n"
            "  Ctrl-T         sort: name, natural (run_2 < run_10), ext, size, time\n"
            "  l              link in the other pane; on a link, change its target\n"
            "  w              open with the system's application (a copy,\n"
            "                 if remote or in an archive)\n"
            "  F5/F6 Ctrl-B   in the prompt: run it in the background\n"
            "  J              background jobs: p pause, c cancel, Enter errors\n"
            "  F8             on a local pane: to the trash, or delete for good;\n"
            "                 in the trash itself, for good only\n"
            "  Ctrl-U         swap panes   Ctrl-R  reload panes\n"
            "  Ctrl-G         go to path   Ctrl-T  sort order\n"
            "  Esc            the menu bar (Alt+F, Alt+C, Alt+O, Alt+H\n"
            "                 open a menu by name; click one to open it)\n"
            "  ~              home directory of this pane's location\n"
            "  T              the trash, in this pane: the one F8 uses from\n"
            "                 here (from a server or an archive, this\n"
            "                 computer's); < goes back\n"
            "  < / >          back / forward through the places this pane\n"
            "                 has been: '=', F2 and archives included\n"
            "  =              other pane: same directory and connection\n"
            "  b              presets: go to / save / delete a location\n"
            "  /              search this pane: type a name and the cursor\n"
            "                 jumps to the first one containing it (all\n"
            "                 lowercase ignores case); Down/Up or Ctrl-N/P\n"
            "                 next/previous, Ins tag, Enter/F-keys act on it,\n"
            "                 / or Esc ends.  Every letter and digit is part\n"
            "                 of the name, so no hotkey runs meanwhile\n"
            "  .              show/hide hidden files (this pane)\n"
            "  u              directory sizes: each subdirectory shows what\n"
            "                 is under it, with a bar against the biggest\n"
            "                 here -- follow them down to find the space\n"
            "  z              single pane: this pane at full width, the other\n"
            "                 kept as it was (Tab flips to it, Ctrl-U brings\n"
            "                 it out); copy, move, extract and sync name it.\n"
            "                 h, Ctrl-] Tab in the terminal and plug-ins that\n"
            "                 use the other pane show both while they run.\n"
            "                 z again: two panes.  In the terminal it is\n"
            "                 Ctrl-] z, and closing the terminal ends it\n"
            "  t              terminal in this pane: every key goes to the\n"
            "                 program; Ctrl-] then Tab other pane, z zoom to\n"
            "                 the full width and back, q close, PgUp scroll\n"
            "                 back (or the mouse wheel)\n"
            "  !              full-screen shell: the whole real terminal\n"
            "  p / F11        plug-in mode: run a plug-in in this pane\n"
            "  f              find files: browsable results (view/edit/goto)\n"
            "  r              rename the file under the cursor\n"
            "  x              extract the archive under the cursor (or the\n"
            "                 tagged ones) into the other pane, each in a\n"
            "                 folder of its own; F5/F6 into a pane that is\n"
            "                 inside an archive add the files to it\n"
            "  n              new empty file here (an existing one is\n"
            "                 restamped, never emptied)\n"
            "  D              compare both panes' files side by side:\n"
            "                 n/N next/previous difference, one scrollbar\n"
            "  h              this pane becomes head + tail of the file the\n"
            "                 other pane is on, and follows its cursor;\n"
            "                 +/- more/fewer lines, Esc gives the pane back\n"
            "  F3 on .xlsx    spreadsheet grid: Tab sheet, / find, w width\n"
            "  F3 on .tsv/.csv  table: f filter, =/! this value, Bksp/0\n"
            "                 unfilter, s sort, Enter row, H header, c colour,\n"
            "                 # the comment lines above the table, i column\n"
            "                 summary, F freeze columns, -/+ hide/show columns\n"
            "  F3 on .docx    document view: headings, lists, tables, w wrap\n"
            "  F3 on .pptx    slide browser: Tab slide, t notes, / find\n"
            "  F3 on .md      markdown rendered; r shows the source\n"
            "  F3 on code     .py .sh .yml .json .toml .ini .conf and #!\n"
            "                 scripts in colour; c turns the colour off\n"
            "  F3 on an image arrows pan, +/- zoom, c colour/ASCII, n frame\n"
            "  F3 on .pdf     page text: Tab page, / find, i scanned image\n"
            "  F3 on .dc      datacenter floor plan (a layout, not a dc\n"
            "                 script): arrows move, Enter zooms in to a\n"
            "                 row and a rack, Bksp/Esc out, c colours by\n"
            "                 role/tag/model/off\n"
            "  C              configuration: edit config.ini / plug-ins\n"
            "  Options>Colours  turbo (blue), midnight (black), mono\n"
            "  Options>Editor   F4 opens vi/vim/$EDITOR instead of the\n"
            "                   built-in editor\n"
            "  Options>Viewer   F3 opens less/$PAGER instead of the built-in\n"
            "                   viewer, for plain text and code (a spreadsheet,\n"
            "                   table, deck, document, PDF, image or .dc\n"
            "                   layout keeps its own)\n"
            "  Options>View and edit in a pane\n"
            "                   F3 and F4 open in a pane, alone at full width,\n"
            "                   rather than full-screen (outside pagers and\n"
            "                   editors too, in the pane's terminal).  z in a\n"
            "                   viewer, or Ctrl-] z, shows two panes: the file\n"
            "                   beside the listing it came from, and back.\n"
            "                   Ctrl-] Tab gives the listing the keys, Tab\n"
            "                   comes back; F3 there shows the next file in\n"
            "                   the same view.  Closing puts the panes back\n"
            "                   as they were\n"
            "\n"
            "  Function keys -- each also has digit and letter aliases,\n"
            "  for terminals that swallow the F-keys:\n"
            "  F1/1/?  Help      F2/2/o  Open/Connect   F3/3/v  View\n"
            "  F4/4/e  Edit      F5/5/c  Copy           F6/6/m  Move\n"
            "  F7/7    Mkdir     F8/8/d  Delete          F9/9/s  Sync\n"
            "  F10/0/q Quit\n"
            "  F       filter: show only the names matching a pattern\n"

            "\n"
            "  F5/F6 ask before replacing a file (overwrite, skip, if\n"
            "  older; once or for all), keep permissions (Ctrl-P in the\n"
            "  prompt drops them) and copy symbolic links as links.\n"
            "\n"
            "  Mouse: click to select, double-click to open, wheel to\n"
            "  scroll, right-click for a context menu of actions.\n"
            "\n"
            "F2 connects to SFTP, SSH (shell) or FTP; copy/move/sync\n"
            "work across local and remote panes alike. F9 makes both\n"
            "panes hold the newest version of every file."
        )
        dialogs.message(self.stdscr, "Help", text)

    def _close_plugin(self, panel: Panel) -> None:
        """Let the pane's plug-in go, giving it its documented last word.

        ``PanePlugin.on_exit`` promises to be called when a plug-in closes,
        and a plug-in holding a pty or an SSH channel releases it there.  The
        promise was only kept on the way out of the application: a plug-in
        that closed itself, or was replaced by another, was simply dropped.
        Calling it is safe more than once -- the built-in terminal already
        calls it itself and guards every resource -- so this is the one place
        that has to remember, rather than each caller.
        """
        plugin = panel.plugin
        panel.plugin = None
        if plugin is None:
            return
        if self._zoomed_by is panel:
            # Single mode was this plug-in's zoom, and ends with it: the
            # pane's listing comes back in the two panes it had before.
            self._toggle_single()
        exit_hook = getattr(plugin, "on_exit", None)
        if exit_hook is not None:
            try:
                exit_hook()
            except Exception as exc:
                self._set_message(f"Plugin error: {exc}")

    def _end_plugin(self, panel: Panel) -> None:
        """Close a plug-in that has asked to close, and say so."""
        plugin = panel.plugin
        self._close_plugin(panel)
        panel.refresh()
        self._set_message(getattr(plugin, "closed_note", "") or "Plugin closed")

    def _may_close(self, panel: Panel) -> bool:
        """Whether the pane's plug-in agrees to be closed; it may ask first."""
        ask = getattr(panel.plugin, "may_close", None)
        return ask is None or bool(ask())

    def _may_quit(self) -> bool:
        """Whether every pane agrees to the application quitting."""
        return all(self._may_close(panel) for panel in (self.left, self.right))

    def _close_backends(self) -> None:
        for panel in (self.left, self.right):
            self._close_plugin(panel)
        self.opener.cleanup()
        self.jobs.cancel_all()
        self.jobs.wait(self.JOBS_GRACE)
        for fs in self._backends:
            try:
                fs.close()
            except Exception:
                pass


def _main(stdscr, args) -> str | None:
    from . import config as config_mod

    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass
    # Every colour the application uses is allocated here, once, so nothing
    # drawn later has to ask whether it may have any.
    theme.init(config_mod.colour_scheme())
    stdscr.keypad(True)
    # How long curses waits after Esc to see whether a key sequence follows.
    # Its default is a whole second, so Esc -- leaving insert mode in vim in
    # the terminal pane, or closing a dialog -- lagged that long.  A terminal
    # sends a key's sequence in one write, so 25 ms is ample; an ESCDELAY in
    # the environment is the user's own choice and is left alone.
    if "ESCDELAY" not in os.environ:
        try:
            curses.set_escdelay(ESCDELAY_MS)
        except curses.error:
            pass
    # Raw mode: without it the tty's XON/XOFF flow control eats Ctrl-S (the
    # editor's save key!) and Ctrl-Q, and Ctrl-S can freeze the display.
    # Ctrl-C consequently arrives as key 3, handled as quit in handle_key.
    curses.raw()
    # Enable mouse reporting (clicks, wheel, right-click menus). Harmless on
    # terminals that do not support it -- the mask simply stays empty.
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(200)  # ms window for double-click detection
    except curses.error:
        pass
    app = App(stdscr, left_path=args.left, right_path=args.right,
              single=config_mod.single_pane())
    app.run()
    # Returned rather than written here: curses still owns the terminal at
    # this point, so a failure to write could not be reported legibly.
    return app.printwd_path()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="meridian-commander",
        description="A two-pane terminal file manager (Midnight Commander style) "
                    "with local + SFTP/FTP browsing, copy/move/sync, viewer and editor.",
        # Raw description formatting keeps --version's line breaks intact.
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    from . import __version__

    version_text = (
        f"%(prog)s {__version__}\n"
        "Copyright (C) 2026 Martin J. Gallagher\n"
        "License: GPL-3.0-or-later "
        "<https://www.gnu.org/licenses/gpl-3.0.html>\n"
        "This is free software: you are free to change and redistribute it.\n"
        "There is no warranty, to the extent permitted by law."
    )
    parser.add_argument("left", nargs="?", default=None,
                        help="starting directory for the left pane "
                             "(default: the current directory)")
    parser.add_argument("right", nargs="?", default=None,
                        help="starting directory for the right pane "
                             "(default: your home directory)")
    parser.add_argument("-V", "--version", action="version",
                        version=version_text)
    parser.add_argument("--printwd", metavar="FILE",
                        help="on exit, write the active pane's directory to "
                             "FILE, for a shell function to cd into; nothing "
                             "is written when that pane is not local")
    parser.add_argument("--shell-init", metavar="SHELL",
                        choices=list(shellinit.SNIPPETS),
                        help="print the shell integration for SHELL (%s) and "
                             "exit: a Ctrl-O binding that leaves the shell in "
                             "the directory you browsed to"
                             % ", ".join(shellinit.SNIPPETS))
    args = parser.parse_args(argv)

    # Printing and exiting: no terminal is taken over, so this works in a
    # pipe, which is how it is meant to be used ("eval $(...)").
    if args.shell_init:
        sys.stdout.write(shellinit.snippet(args.shell_init))
        return 0

    # The frames are drawn with box-drawing characters, which curses can only
    # put on the screen once the locale has been taken from the environment.
    # Without this the terminal is told to render them in ASCII instead --
    # see meridian_commander.theme.
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass

    final_dir = None
    try:
        final_dir = curses.wrapper(_main, args)
    except KeyboardInterrupt:
        # Interrupted rather than quit: no directory was chosen, so the shell
        # stays where it is.
        pass
    if args.printwd:
        _write_printwd(args.printwd, final_dir)
    return 0


def _write_printwd(path: str, directory: str | None) -> None:
    """Write the directory the shell should follow us to.

    Nothing is written for ``None`` -- the caller's ``[ -s "$wd" ]`` then
    finds an empty file and leaves the shell alone.  No trailing newline
    either: the shell reads this with ``$(cat ...)``, which would strip one
    anyway, and leaving it off keeps a directory whose name genuinely ends in
    a newline unambiguous.

    A write that fails is reported and otherwise ignored. The browsing is
    done and the terminal is back; refusing to exit over it would be worse
    than a shell that did not move.
    """
    if not directory:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(directory)
    except OSError as exc:
        print(f"meridian: could not write {path}: {exc}", file=sys.stderr)
