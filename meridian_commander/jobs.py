# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Copies and moves that run in the background while the panes stay usable.

A :class:`Job` is one copy or move, started from the ``F5``/``F6`` prompt with
its **Background** switch on.  It runs on a worker thread of its own, so it
must never touch curses: it only updates its own fields, under a lock, and the
main loop reads them between keystrokes (:meth:`JobQueue.tick`) to draw the
status line and the jobs list (``J``), and to refresh the panes a finished job
has changed.

Three decisions are made before a job starts, because a job cannot stop and
ask:

*What to do with a target that is already there* -- overwrite, overwrite
only an older one, or skip -- applies to every item in the job.

*Which connection it uses.*  A job between remote places gets connections of
its own, opened by the application before the job is queued, so browsing that
host is not held up behind the copy (and a paramiko channel is not shared
between two threads).  The job closes them when it ends.

*When it runs.*  Jobs between the same two places run one at a time, in the
order they were started, as a file manager's queue does; jobs between
different places run side by side.

Pausing waits inside the cancel check the copy makes before every chunk, so a
paused job stops within one chunk and holds its files open until resumed.
Cancelling ends it at the next chunk, and the file being written is removed
(:func:`~meridian_commander.operations.copy_file` does that), so no half-written
target is left behind; the items it had already finished stay.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from .operations import (
    OVERWRITE,
    SKIP,
    OperationCancelled,
    TransferReport,
    copy_path,
    count_tree,
    move_path,
)
from .sync import MTIME_TOLERANCE
from .util import human_size

WAITING = "waiting"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLED = "cancelled"

#: States a job does not leave.
FINISHED = (DONE, FAILED, CANCELLED)

#: The rules a job can take for a target that is already there.
RULES = (OVERWRITE, "older", SKIP)


def place_key(fs) -> tuple:
    """What makes two locations "the same place" for the queue.

    The machine and the account, not the connection object: a job opens a
    connection of its own, so two jobs to one server hold two different
    objects naming one place.
    """
    scheme = getattr(fs, "scheme", "local")
    if scheme == "local":
        return ("local",)
    user = getattr(fs, "typed_username", "") or getattr(fs, "username", "")
    return (scheme, getattr(fs, "host", ""), int(getattr(fs, "port", 0) or 0),
            user or "")


class RuleConflicts:
    """The ``on_conflict`` a job hands the copy: its rule, never a question."""

    def __init__(self, rule: str) -> None:
        if rule not in RULES:
            raise ValueError(f"unknown rule: {rule}")
        self.rule = rule

    def __call__(self, target: str, source, existing) -> str:
        if self.rule != "older":
            return self.rule
        if existing.mtime is None or source.mtime is None:
            return SKIP
        if source.mtime - existing.mtime > MTIME_TOLERANCE:
            return OVERWRITE
        return SKIP


class Job:
    """One background copy or move, and everything the screen shows of it.

    ``src_fs``/``dst_fs`` are the connections the job uses (its own, for a
    remote side); ``src_pane_fs``/``dst_pane_fs`` are the panes' own, which
    the application compares when the job ends to know which listings to
    reload.  ``closing`` lists the connections the job opened, which it
    closes when it ends.
    """

    def __init__(self, number: int, move: bool, src_fs, src_dir: str,
                 names: list[str], dst_fs, dest: str, rule: str,
                 keep_mode: bool = False, src_pane_fs=None, dst_pane_fs=None,
                 closing: list | None = None, delete_sources=None) -> None:
        self.number = number
        self.move = move
        self.verb = "Move" if move else "Copy"
        self.src_fs, self.src_dir, self.names = src_fs, src_dir, list(names)
        self.dst_fs, self.dest = dst_fs, dest
        self.conflicts = RuleConflicts(rule)
        self.keep_mode = keep_mode
        self.src_pane_fs = src_fs if src_pane_fs is None else src_pane_fs
        self.dst_pane_fs = dst_fs if dst_pane_fs is None else dst_pane_fs
        self.closing = list(closing or [])
        self._delete_sources = delete_sources
        self.pair = (place_key(src_fs), place_key(dst_fs))

        self._lock = threading.Condition()
        self.state = WAITING
        self.paused = False
        self._cancel = False
        self.bytes_done = 0
        self.bytes_total = 0
        self.current = ""
        self.errors: list[str] = []
        self.note = ""
        self.started: float | None = None
        self.finished: float | None = None
        self._paused_for = 0.0
        self._paused_at: float | None = None
        self._label = ""
        self._last = 0

    # -- what the screen asks ---------------------------------------------------
    def title(self) -> str:
        what = self.names[0] if len(self.names) == 1 else \
            f"{len(self.names)} items"
        return f"{self.verb} {what} to {self.dest}"

    def active(self) -> bool:
        return self.state in (WAITING, RUNNING)

    def speed(self, now: float | None = None) -> float:
        """Bytes a second since the job started, not counting pauses."""
        if self.started is None:
            return 0.0
        end = self.finished or (time.monotonic() if now is None else now)
        paused = self._paused_for
        if self._paused_at is not None:
            paused += end - self._paused_at
        seconds = end - self.started - paused
        return self.bytes_done / seconds if seconds > 0 else 0.0

    def progress_text(self) -> str:
        done = human_size(self.bytes_done).strip()
        if self.bytes_total:
            percent = min(100, self.bytes_done * 100 // self.bytes_total)
            return (f"{percent:3d}%  {done} of "
                    f"{human_size(self.bytes_total).strip()}")
        return done

    def state_text(self) -> str:
        if self.state == RUNNING and self.paused:
            return "paused"
        if self.state == WAITING and self.paused:
            return "waiting, paused"
        return self.state

    # -- control, from the main thread --------------------------------------------
    def pause(self) -> None:
        with self._lock:
            if self.paused or self.state in FINISHED:
                return
            self.paused = True
            self._paused_at = time.monotonic()

    def resume(self) -> None:
        with self._lock:
            if not self.paused:
                return
            self.paused = False
            if self._paused_at is not None:
                self._paused_for += time.monotonic() - self._paused_at
                self._paused_at = None
            self._lock.notify_all()

    def cancel(self) -> None:
        with self._lock:
            self._cancel = True
            if self.state == WAITING:
                self.state = CANCELLED
                self.finished = time.monotonic()
            self._lock.notify_all()

    # -- the work, on the worker thread ---------------------------------------------
    def cancelled(self) -> bool:
        """The copy's cancel check: where a paused job waits."""
        with self._lock:
            while self.paused and not self._cancel:
                self._lock.wait()
            return self._cancel

    def progress(self, done: int, total: int, label: str) -> None:
        """The copy's progress callback: per file, summed here."""
        with self._lock:
            if label != self._label or done < self._last:
                self._label, self._last = label, 0
            self.bytes_done += max(0, done - self._last)
            self._last = done
            self.current = label

    def run(self) -> None:
        """Do the copy or move; every outcome ends up in the job's fields."""
        with self._lock:
            if self.state != WAITING:
                return
            self.state = RUNNING
            self.started = time.monotonic()
        report = TransferReport()
        moved: list[str] = []
        cancelled = False
        try:
            total = 0
            for name in self.names:
                try:
                    total += count_tree(self.src_fs,
                                        self.src_fs.join(self.src_dir, name))[1]
                except Exception:
                    pass
            with self._lock:
                self.bytes_total = total
            try:
                with self.dst_fs.batch():
                    for name in self.names:
                        if self.cancelled():
                            cancelled = True
                            break
                        src = self.src_fs.join(self.src_dir, name)
                        target = self.dst_fs.join(self.dest, name)
                        try:
                            if self.move:
                                move_path(self.src_fs, src, self.dst_fs, target,
                                          self.progress, self.cancelled,
                                          deferred=moved,
                                          on_conflict=self.conflicts,
                                          keep_mode=self.keep_mode, links=True,
                                          report=report)
                            else:
                                copy_path(self.src_fs, src, self.dst_fs, target,
                                          self.progress, self.cancelled,
                                          on_conflict=self.conflicts,
                                          keep_mode=self.keep_mode, links=True,
                                          report=report)
                        except OperationCancelled:
                            cancelled = True
                            break
                        except Exception as exc:
                            self._error(f"{name}: {exc}")
            except Exception as exc:
                self._error(str(exc))
                moved.clear()
            if moved and self._delete_sources is not None:
                self._delete_sources(self.src_fs, moved, self.errors)
            self.note = summary(report, self.move)
        finally:
            for fs in self.closing:
                try:
                    fs.close()
                except Exception:
                    pass
            with self._lock:
                if cancelled:
                    self.state = CANCELLED
                elif self.errors:
                    self.state = FAILED
                else:
                    self.state = DONE
                self.finished = time.monotonic()
                self.current = ""

    def _error(self, text: str) -> None:
        with self._lock:
            self.errors.append(text)


def summary(report: TransferReport, move: bool) -> str:
    """What a job did not do exactly as asked, for the jobs list."""
    notes = []
    if report.kept:
        notes.append(f"{len(report.kept)} existing kept")
    if report.links_left:
        notes.append(f"{len(report.links_left)} symlinked director"
                     f"{'y' if len(report.links_left) == 1 else 'ies'} skipped")
    if report.links_as_files:
        notes.append(f"{len(report.links_as_files)} link(s) copied as files")
    if report.modes_lost:
        notes.append(f"permissions not kept on {len(report.modes_lost)}")
    if notes and move and (report.kept or report.links_left):
        notes.append("source kept")
    return ", ".join(notes)


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, name="meridian-job", daemon=True).start()


class JobQueue:
    """Every job of this run, and which of them may start.

    ``spawn`` runs a job's work somewhere else -- a new daemon thread, unless
    a test wants it run on the spot.
    """

    def __init__(self, spawn: Callable[[Callable[[], None]], None] = _thread
                 ) -> None:
        self.spawn = spawn
        self.jobs: list[Job] = []
        self._reported: set[int] = set()
        self._started: set[int] = set()
        self._numbers = 0

    def number(self) -> int:
        self._numbers += 1
        return self._numbers

    def submit(self, job: Job) -> None:
        self.jobs.append(job)
        self._start_ready()

    def active(self) -> list[Job]:
        return [job for job in self.jobs if job.active()]

    def started(self, job: Job) -> bool:
        """Whether ``job`` has been handed a thread, or still waits its turn."""
        return job.number in self._started

    def _start_ready(self) -> None:
        """Start each waiting job whose pair of places has nothing running."""
        busy = {job.pair for job in self.jobs if job.state == RUNNING
                or (job.number in self._started and job.state == WAITING)}
        for job in self.jobs:
            if job.state != WAITING or job.number in self._started:
                continue
            if job.pair in busy:
                continue
            busy.add(job.pair)
            self._started.add(job.number)
            self.spawn(job.run)

    def tick(self) -> list[Job]:
        """Start what may start; return the jobs that ended since last time."""
        ended = [job for job in self.jobs
                 if job.state in FINISHED and job.number not in self._reported]
        for job in ended:
            self._reported.add(job.number)
        self._start_ready()
        return ended

    def status(self) -> str:
        """The status line's indicator, or nothing with no job active."""
        active = self.active()
        if not active:
            return ""
        running = [job for job in active if job.state == RUNNING]
        done = sum(job.bytes_done for job in running)
        speed = sum(job.speed() for job in running if not job.paused)
        waiting = len(active) - len(running)
        text = f"Jobs: {len(running)} running"
        if waiting:
            text += f", {waiting} waiting"
        if running:
            text += (f"  {human_size(done).strip()}"
                     f"  {human_size(int(speed)).strip()}/s")
        return text

    def cancel_all(self) -> None:
        for job in self.active():
            job.cancel()

    def wait(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds for every job to end."""
        end = time.monotonic() + timeout
        while self.active():
            if time.monotonic() >= end:
                return False
            time.sleep(0.02)
        return True
