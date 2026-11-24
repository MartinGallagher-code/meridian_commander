# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Auto-sync: the two panes kept in step until it is stopped (MC-034).

``F9`` is a one-off: it walks both trees, shows a plan, copies, and is done.
Auto-sync (``Shift-F9``) starts with that same pass and then keeps going: it
looks at both directories again every few seconds and makes whatever changed
on one side on the other, until it is stopped, either pane leaves its
directory, or Meridian quits.  Nothing watches a directory -- the standard
library has no file watcher -- so "automatically" means checking.

Two halves, so the hard one can be tested without a thread or a clock:

:class:`AutoSync` is one check, :meth:`AutoSync.pass_once`, plain and
synchronous.  It remembers what the last check saw on each side, and the
last time each file was the same on both, which is what lets it tell a file
that is new on the left from one that was deleted on the right -- a
question a plain ``F9`` plan cannot answer, and why ``F9`` never deletes.

:class:`AutoSyncRunner` runs a check every interval on a thread of its own,
with connections of its own for a remote side (as a background job does:
browsing that host is not held up, and a paramiko channel is not shared
between threads), and never draws.  The main loop reads what it did between
keystrokes, reloads the panes it changed and keeps the AUTO-SYNC bar up to
date.

The rules a check follows, as the owner set them:

- A file new or changed on one side is copied to the other, keeping its
  modification time -- but only once it has held still across two checks, so
  a file still being written is not copied half-way.
- When both sides changed the same file, the newer wins, as in ``F9``; the
  copy it replaces goes to the trash first, and the status line says so.
- A file deleted (or renamed away) on one side, which the last check saw on
  both, goes to the trash on the other side once it has been gone for two
  checks -- never on the first, and never for good: the desktop's trash on a
  local pane, ``~/.meridian-trash/`` on a remote one.  A folder left empty by
  that, and gone from the other side, is removed too; it holds nothing.
- Anything that fails is counted, kept to be read, and not tried again until
  one of the two copies changes.  A side that cannot be reached at all --
  a dropped connection -- stops the check: nothing is copied or deleted
  until it is back, and the runner reconnects at every check.
- A side whose whole folder is suddenly empty is not believed: an unmounted
  disk looks exactly like that, and mirroring it would send everything on the
  other side to the trash.  Nothing is done until it has something in it
  again.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import trash
from .filesystems import DirEntry, FileSystem
from .operations import CancelCB, OperationCancelled, _noop_cancel, copy_file
from .sync import MTIME_TOLERANCE, _index_tree

#: Seconds between checks when both sides are on this machine, and when
#: either is on another one.  A remote check walks both trees over the
#: network, so it is made less often.  ``[autosync]`` in ``config.ini`` can
#: change both.
LOCAL_INTERVAL = 2.0
REMOTE_INTERVAL = 10.0

#: The trash folder auto-sync keeps in the home directory of a remote side.
#: Meridian's trash is the desktop's own, which only a local pane has, so a
#: delete mirrored onto a remote pane is moved here instead -- under the date
#: and the path it had -- and nothing is ever deleted for good.
REMOTE_TRASH = ".meridian-trash"

#: How many lines of what auto-sync did are kept to be read (``J``), and how
#: many failures.  Old ones go first.
LOG_LIMIT = 500
FAILURE_LIMIT = 1000

#: A file as one check saw it: its size and modification time.
Sig = tuple[int, "float | None"]


def signature(entry: DirEntry) -> Sig:
    return (entry.size or 0, entry.mtime)


def in_step(a: Sig, b: Sig) -> bool:
    """Whether two copies count as the same, by ``F9``'s rule.

    The same size, and times within two seconds of each other; a side that
    reports no time cannot be compared by age, so the size decides.
    """
    if a[0] != b[0]:
        return False
    if a[1] is None or b[1] is None:
        return True
    return abs(a[1] - b[1]) <= MTIME_TOLERANCE


def move_to_remote_trash(fs: FileSystem, path: str,
                         now: Callable[[], float] = time.time) -> str:
    """Move ``path`` into ``~/.meridian-trash/`` on its host; returns where.

    Kept under the date and time it went, then the whole path it had, so it
    can be found and put back by hand: ``/srv/www/index.html`` trashed at
    half past three goes to
    ``~/.meridian-trash/2026-11-24_153000/srv/www/index.html``.  A rename,
    so it stays on the same machine and nothing is copied or deleted.
    """
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(now()))
    parts = [part for part in path.split(fs.sep) if part]
    target = fs.join(fs.home(), REMOTE_TRASH, stamp, *parts)
    candidate, number = target, 1
    while fs.exists(candidate):
        number += 1
        candidate = f"{target}.{number}"
    fs.makedirs(fs.dirname(candidate))
    fs.rename(path, candidate)
    return candidate


class Place:
    """One of the two directories auto-sync keeps in step.

    ``fs`` is the connection the checks use -- its own, for a remote side --
    and is replaced when the runner reconnects.  ``name`` is what messages
    call it.
    """

    def __init__(self, fs: FileSystem, root: str, name: str) -> None:
        self.fs = fs
        self.root = root
        self.name = name

    @property
    def local(self) -> bool:
        return getattr(self.fs, "scheme", "local") == "local"

    def path(self, rel: str) -> str:
        return self.fs.join(self.root, *[p for p in rel.split("/") if p])

    def trash_dir(self) -> str | None:
        """Where this side's trash is, so a check can keep out of it.

        A trash inside the folder being synced -- syncing a home directory,
        or the top of a disk -- would otherwise be found by the next check,
        and every file auto-sync put there copied across to the other side.
        """
        if self.local:
            try:
                where = trash.trash_location(self.root)
            except OSError:
                return None
            # The FreeDesktop trash lists its files/, beside info/; the whole
            # trash is the directory to keep out of.
            if os.path.basename(where) == "files":
                return os.path.dirname(where)
            return where
        return self.fs.join(self.fs.home(), REMOTE_TRASH)

    def to_trash(self, path: str) -> str:
        if self.local:
            return trash.move_to_trash(path)
        return move_to_remote_trash(self.fs, path)


class Unreachable(Exception):
    """A side could not be reached at all: the check stops and tries later."""

    def __init__(self, place: Place, error: Exception) -> None:
        super().__init__(f"{place.name}: {error}")
        self.place = place


@dataclass
class PassReport:
    """What one check did.  Index 0 is the left place, 1 the right."""

    #: Files copied into each side.
    copied: list[int] = field(default_factory=lambda: [0, 0])
    #: Files (and empty folders) moved to the trash, or removed, on each side.
    trashed: list[int] = field(default_factory=lambda: [0, 0])
    #: Files left for a later check: still changing, or just gone.
    held: int = 0
    #: Files changed on both sides since they were last the same.
    conflicts: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    #: Why the whole check was held back, or "" when it was not.
    waiting: str = ""

    def changed(self, side: int) -> bool:
        return bool(self.copied[side] or self.trashed[side])

    def __bool__(self) -> bool:
        return bool(any(self.copied) or any(self.trashed) or self.conflicts
                    or self.failures)


def _under(rel: str, folders: list[str]) -> bool:
    return any(rel.startswith(folder + "/") for folder in folders)


class AutoSync:
    """The checks themselves, with what they remember between them.

    ``cancel`` is polled while a file is copied, and stops it part way (the
    file is removed, as any cancelled copy's is); ``stopping`` is polled
    between files, so a stop that is not in a hurry lets the file being
    copied finish.
    """

    def __init__(self, left: Place, right: Place,
                 cancel: CancelCB = _noop_cancel,
                 stopping: CancelCB = _noop_cancel) -> None:
        self.places = (left, right)
        self.cancel = cancel
        self.stopping = stopping
        #: How many checks have finished.
        self.passes = 0
        #: Each file as the last check saw it on each side, after anything
        #: it did.  A file is copied only if it is the same now.
        self._last: tuple[dict[str, Sig], dict[str, Sig]] = ({}, {})
        #: Each file as it was the last time the two sides held the same
        #: copy -- what says which side changed, and that a file missing on
        #: one side was deleted there rather than never copied.
        self._agreed: dict[str, tuple[Sig, Sig]] = {}
        #: The two copies of a file whose copy or delete failed, as they were
        #: then: it is not tried again until one of them changes.
        self._failed: dict[str, tuple] = {}
        #: The folders each side held at this check, for tidying up after a
        #: delete.
        self._dirs: tuple[set[str], set[str]] = (set(), set())

    # -- one check ---------------------------------------------------------
    def pass_once(self) -> PassReport:
        """Look at both sides once and bring them into step.

        Raises :class:`Unreachable` when a side cannot be listed at all, or
        cannot be reached when something on it fails, and
        :class:`~meridian_commander.operations.OperationCancelled` when it is
        stopped or cancelled part way.
        """
        report = PassReport()
        skip = self._skip()
        indexes: list[dict[str, DirEntry]] = []
        unreadable: list[str] = []
        dirs: tuple[set[str], set[str]] = (set(), set())
        for side, place in enumerate(self.places):
            try:
                indexes.append(_index_tree(
                    place.fs, place.root, cancel=self.cancel,
                    dirs=dirs[side], failed=unreadable, skip=skip))
            except OperationCancelled:
                raise
            except Exception as exc:
                raise Unreachable(place, exc) from exc
        self._dirs = dirs

        for side, place in enumerate(self.places):
            had = sum(1 for pair in self._agreed.values()
                      if pair[side] is not None)
            if had > 1 and not indexes[side] and not dirs[side] \
                    and not unreadable:
                report.waiting = (f"{place.name} is suddenly empty -- "
                                  "nothing is mirrored until it has files "
                                  "again")
                return report

        left, right = indexes
        last: tuple[dict[str, Sig], dict[str, Sig]] = ({}, {})
        for rel in sorted(set(left) | set(right) | set(self._agreed)):
            if self.stopping():
                raise OperationCancelled()
            if _under(rel, unreadable):
                # A folder that could not be read this time says nothing
                # about what is in it: keep what the last check knew.
                for side in (0, 1):
                    if rel in self._last[side]:
                        last[side][rel] = self._last[side][rel]
                continue
            sigs = (signature(left[rel]) if rel in left else None,
                    signature(right[rel]) if rel in right else None)
            after = self._settle(rel, sigs, report)
            for side in (0, 1):
                if after[side] is not None:
                    last[side][rel] = after[side]
        self._last = last
        self.passes += 1
        return report

    def _skip(self) -> set[str]:
        """The folders neither side walks: a trash inside either one."""
        skip = set()
        for place in self.places:
            where = place.trash_dir()
            if where is None:
                continue
            sep = place.fs.sep
            root = place.fs.normpath(place.root).rstrip(sep)
            where = place.fs.normpath(where)
            if where.startswith(root + sep):
                skip.add(where[len(root) + 1:].replace(sep, "/"))
        return skip

    def _settle(self, rel: str, sigs: tuple, report: PassReport) -> tuple:
        """Do what one file needs; return how it stands on each side after."""
        left, right = sigs
        agreed = self._agreed.get(rel)
        if left is None and right is None:
            # Gone from both: nothing to do, and nothing to remember.
            self._agreed.pop(rel, None)
            self._failed.pop(rel, None)
            return sigs
        if self._failed.get(rel) == sigs:
            return sigs                 # failed before; nothing has changed
        self._failed.pop(rel, None)
        conflict = False
        if left is not None and right is not None:
            if agreed == sigs or in_step(left, right):
                self._agreed[rel] = sigs
                return sigs
            source, reason, conflict = self._direction(left, right, agreed)
        else:
            source = 0 if left is not None else 1
            target = 1 - source
            if agreed is not None and sigs[source] == agreed[source]:
                # The two were the same, this side has not changed since, and
                # the other no longer has it: deleted (or renamed) there.
                if self._last[target].get(rel) is not None:
                    # Gone only since the last check.  An editor saving by
                    # renaming leaves a gap just like that, so wait one more.
                    report.held += 1
                    return sigs
                return self._trash(rel, source, sigs, report)
            # New here -- or changed here and deleted there, where keeping
            # the change is the answer that loses nothing.
            reason = ("new" if agreed is None
                      else f"changed, and deleted on {self.places[target].name}")
        if sigs[source] != self._last[source].get(rel):
            report.held += 1            # just seen, or still being written
            return sigs
        return self._copy(rel, source, sigs, reason, conflict, report)

    def _direction(self, left: Sig, right: Sig,
                   agreed: tuple | None) -> tuple[int, str, bool]:
        """Which side's copy wins, why, and whether both had changed.

        The side that changed since the two were last the same wins.  When
        both did, or they never were the same, it is ``F9``'s rule: the
        newer, and the left when the clock cannot say.
        """
        names = (self.places[0].name, self.places[1].name)
        both = False
        if agreed is not None:
            changed = (left != agreed[0], right != agreed[1])
            if changed == (True, False):
                return 0, f"changed on {names[0]}", False
            if changed == (False, True):
                return 1, f"changed on {names[1]}", False
            both = True
        lt, rt = left[1], right[1]
        if lt is None or rt is None:
            return 0, "differs (no time)", both
        if rt - lt > MTIME_TOLERANCE:
            return 1, f"newer on {names[1]}", both
        if lt - rt > MTIME_TOLERANCE:
            return 0, f"newer on {names[0]}", both
        return 0, "differs (same age)", both

    def _copy(self, rel: str, source: int, sigs: tuple, reason: str,
              conflict: bool, report: PassReport) -> tuple:
        target = 1 - source
        src, dst = self.places[source], self.places[target]
        to = dst.path(rel)
        if conflict:
            # Both sides changed: the newer wins, as in F9, but the copy it
            # replaces is an edit too, so it goes to the trash rather than
            # being written over.
            try:
                dst.to_trash(to)
            except Exception as exc:
                self._fail(rel, sigs, f"{rel}: changed on both sides, and "
                           f"the copy on {dst.name} could not be kept in the "
                           f"trash, so nothing was copied: {exc}", report,
                           (dst,))
                return sigs
            report.conflicts.append(rel)
            report.events.append(f"{rel}: changed on both sides -- the newer, "
                                 f"from {src.name}, was kept; the other is in "
                                 f"the trash on {dst.name}")
        try:
            copy_file(src.fs, src.path(rel), dst.fs, to, cancel=self.cancel,
                      preserve_mtime=True)
            written = signature(dst.fs.stat(to))
        except OperationCancelled:
            raise
        except Exception as exc:
            self._fail(rel, sigs, f"{rel}: could not be copied to {dst.name}: "
                       f"{exc}", report, (src, dst))
            return sigs
        after = list(sigs)
        after[target] = written
        # Recorded as they are, even when a backend that cannot set times
        # left the copy newer: these two are now the same file, and the next
        # check must not copy it back.
        self._agreed[rel] = (after[0], after[1])
        report.copied[target] += 1
        report.events.append(f"{rel} copied to {dst.name} ({reason})")
        return tuple(after)

    def _trash(self, rel: str, side: int, sigs: tuple,
               report: PassReport) -> tuple:
        place, other = self.places[side], self.places[1 - side]
        try:
            place.to_trash(place.path(rel))
        except Exception as exc:
            self._fail(rel, sigs, f"{rel}: deleted on {other.name}, but could "
                       f"not be moved to the trash on {place.name}, so it was "
                       f"left: {exc}", report, (place,))
            return sigs
        self._agreed.pop(rel, None)
        report.trashed[side] += 1
        report.events.append(f"{rel}: deleted on {other.name} -- moved to the "
                             f"trash on {place.name}")
        self._prune(side, rel, report)
        after = list(sigs)
        after[side] = None
        return tuple(after)

    def _prune(self, side: int, rel: str, report: PassReport) -> None:
        """Remove the folders a delete left empty, that the other side lost.

        A folder deleted or renamed on one side takes its files with it,
        each to the trash on the other side one by one; without this, its
        empty shell would stay behind there.  Only a folder that is empty and
        that the other side no longer has, and never the top.
        """
        place, others = self.places[side], self._dirs[1 - side]
        parts = rel.split("/")[:-1]
        while parts:
            folder = "/".join(parts)
            if folder in others:
                return
            path = place.path(folder)
            try:
                if place.fs.listdir(path):
                    return
                place.fs.rmdir(path)
            except Exception:
                return
            report.trashed[side] += 1
            report.events.append(f"{folder}/: left empty and gone from "
                                 f"{self.places[1 - side].name} -- removed")
            parts.pop()

    def _fail(self, rel: str, sigs: tuple, text: str, report: PassReport,
              involved: tuple) -> None:
        """Count a failure -- unless the trouble is a side gone altogether.

        A dropped connection fails every file after it, and those are not
        failures of the files: they would never be tried again.  So the side
        is asked whether it is still there, and if it is not, the check stops
        and the runner reconnects.
        """
        for place in involved:
            try:
                place.fs.stat(place.root)
            except Exception as exc:
                raise Unreachable(place, exc) from exc
        self._failed[rel] = sigs
        report.failures.append(text)
        report.events.append(f"FAILED {text}")


# ---------------------------------------------------------------------------
# The runner: a check every interval, on a thread of its own
# ---------------------------------------------------------------------------

RUNNING = "running"
WAITING = "waiting"        # a side cannot be reached, or is suddenly empty
STOPPED = "stopped"


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="meridian-autosync",
                     daemon=True).start()


class AutoSyncRunner:
    """Auto-sync between two panes, from start to stop.

    ``panels`` are the two panes, each with the place it showed at the start
    (:meth:`moved` says when that changes).  ``places`` hold the connections
    the checks use; ``reopen`` gives, for each side, a way to dial it again
    after the connection drops (``None`` for a local side), and ``closing``
    lists the connections the runner opened, which it closes when it ends.
    ``spawn`` runs the loop somewhere else: a daemon thread, unless a test
    wants it run on the spot.
    """

    def __init__(self, panels: tuple, places: tuple[Place, Place],
                 reopen: tuple, interval: float,
                 closing: list | None = None,
                 spawn: Callable[[Callable[[], None]], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.panels = panels
        self.where = tuple((panel.fs, panel.fs.normpath(panel.path))
                           for panel in panels)
        self.places = places
        self.reopen = reopen
        self.interval = interval
        self.closing = list(closing or [])
        self.spawn = spawn
        self.clock = clock
        self._stop = threading.Event()
        self._abort = False
        self.done = threading.Event()
        self._lock = threading.Lock()
        self.engine = AutoSync(places[0], places[1], cancel=self._aborted,
                               stopping=self._stop.is_set)
        self.state = RUNNING
        self.state_note = ""
        #: The sides to dial again before the next check.
        self._broken: set[int] = set()
        self.copied = 0
        self.trashed = 0
        self.conflicts = 0
        self.failures: list[str] = []
        self.log: list[str] = []
        self.last_check: float | None = None
        self._reports: list[PassReport] = []
        self._notes: list[str] = []

    # -- control, from the main thread ---------------------------------------
    def start(self) -> None:
        (self.spawn or _thread)(self._run)

    def stop(self) -> None:
        """Stop after the file being copied, if any; no more checks."""
        self._stop.set()

    def abort(self) -> None:
        """Stop now, in the middle of a file: it is removed, not left half."""
        self._abort = True
        self._stop.set()

    def wait(self, timeout: float) -> bool:
        return self.done.wait(timeout)

    def moved(self):
        """The first pane no longer where it was at the start, or None."""
        for panel, (fs, path) in zip(self.panels, self.where):
            if panel.fs is not fs:
                return panel, "connection"
            if panel.results is not None or panel.fs.normpath(panel.path) != path:
                return panel, "directory"
        return None

    def tick(self) -> tuple[list[PassReport], list[str]]:
        """The checks that did something, and the news, since last asked."""
        with self._lock:
            reports, self._reports = self._reports, []
            notes, self._notes = self._notes, []
        return reports, notes

    def status(self) -> str:
        """What the AUTO-SYNC bar says about how it is going."""
        with self._lock:
            parts = []
            if self.state == WAITING:
                parts.append(self.state_note)
            if self.copied:
                parts.append(f"{self.copied} copied")
            if self.trashed:
                parts.append(f"{self.trashed} to the trash")
            if self.failures:
                parts.append(f"{len(self.failures)} failed: J")
            if not parts:
                parts.append("watching")
            return ", ".join(parts)

    def state_text(self) -> str:
        return self.state

    def report_text(self) -> str:
        """Everything kept about this run, failures first, for ``J``."""
        with self._lock:
            checked = ""
            if self.last_check is not None:
                checked = ", last at " + time.strftime(
                    "%H:%M:%S", time.localtime(self.last_check))
            lines = [f"{self.places[0].name}  and  {self.places[1].name}",
                     f"Checked every {self.interval:g} seconds{checked}",
                     f"{self.copied} copied, {self.trashed} to the trash, "
                     f"{len(self.failures)} failed"]
            if self.state == WAITING:
                lines.append(self.state_note)
            if self.failures:
                lines += ["", "Failures:"] + self.failures
            lines += ["", "What it did, newest last:"]
            lines += self.log or ["nothing yet"]
            return "\n".join(lines)

    # -- the work, on the worker thread ----------------------------------------
    def _aborted(self) -> bool:
        return self._abort

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self.step()
                self._stop.wait(self.interval)
        finally:
            for fs in self.closing:
                try:
                    fs.close()
                except Exception:
                    pass
            with self._lock:
                self.state = STOPPED
            self.done.set()

    def step(self) -> None:
        """One check, reconnecting first if the last one lost a side."""
        if not self._reconnect():
            return
        try:
            report = self.engine.pass_once()
        except OperationCancelled:
            return
        except Unreachable as exc:
            self._lost(exc.place, exc)
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(self.clock()))
        with self._lock:
            self.last_check = self.clock()
            if report.waiting:
                if self.state != WAITING or self.state_note != report.waiting:
                    self._notes.append(report.waiting)
                self.state, self.state_note = WAITING, report.waiting
                return
            if self.state == WAITING:
                self._notes.append("checking again")
            self.state, self.state_note = RUNNING, ""
            if not report:
                return
            self.copied += sum(report.copied)
            self.trashed += sum(report.trashed)
            self.conflicts += len(report.conflicts)
            self.failures += [f"{stamp}  {text}" for text in report.failures]
            del self.failures[:-FAILURE_LIMIT]
            self.log += [f"{stamp}  {text}" for text in report.events]
            del self.log[:-LOG_LIMIT]
            self._reports.append(report)

    def _lost(self, place: Place, error: Exception) -> None:
        """A side cannot be reached: wait, and try it again every check.

        A remote side is dialled again before the next check.  A local one
        has nothing to dial: its folder is read again, and that is all.
        """
        side = 0 if place is self.places[0] else 1
        remote = self.reopen[side] is not None
        note = (f"cannot reach {place.name}, reconnecting" if remote
                else f"cannot read {place.name}, trying again")
        with self._lock:
            if self.state != WAITING or self.state_note != note:
                self._notes.append(f"{note}: {error}")
            self.state, self.state_note = WAITING, note
            if remote:
                self._broken.add(side)
            self.last_check = self.clock()

    def _reconnect(self) -> bool:
        """Dial again each side the last check lost; whether all answered."""
        for side in sorted(self._broken):
            place = self.places[side]
            again = self.reopen[side]
            old = place.fs
            try:
                fresh = again()
            except Exception as exc:
                self._lost(place, exc)
                return False
            place.fs = fresh
            self._broken.discard(side)
            if old in self.closing:
                self.closing.remove(old)
                try:
                    old.close()
                except Exception:
                    pass
            self.closing.append(fresh)
        return True
