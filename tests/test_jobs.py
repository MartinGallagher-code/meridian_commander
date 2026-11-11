# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Copies and moves in the background, with a jobs list (MC-020).

Most of these run a job's work on the spot (a queue whose ``spawn`` calls it
at once), which is the same code a worker thread runs, minus the waiting.
The tests that are about threads -- pausing, the default spawn, quitting
while one runs -- use real ones, with timeouts.
"""

from __future__ import annotations

import contextlib
import curses
import os
import threading
import time
import zipfile

import pytest

from meridian_commander import jobs as jobs_mod
from meridian_commander import operations
from meridian_commander.app import MENUS, App
from meridian_commander.archive import ArchiveFileSystem
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.jobs import (
    CANCELLED,
    DONE,
    FAILED,
    RUNNING,
    WAITING,
    Job,
    JobQueue,
    RuleConflicts,
    place_key,
    summary,
)
from meridian_commander.jobspane import JobsPane
from meridian_commander.operations import OVERWRITE, SKIP, TransferReport

from support import (
    _FakeRemoteBackend,
    _ScriptedDialogs,
    _StubScreen,
    read,
    with_curses_screen,
    write,
)
from test_app_draw import _LoopScreen


def _now(queue_spawned):
    def spawn(work):
        queue_spawned.append(work)
        work()
    return spawn


def _job(tmp_path, names=("a.txt",), move=False, rule=OVERWRITE, **kwargs):
    fs = LocalFileSystem()
    src = str(tmp_path / "src")
    dst = str(tmp_path / "dst")
    os.makedirs(src, exist_ok=True)
    os.makedirs(dst, exist_ok=True)
    return Job(1, move, fs, src, list(names), fs, dst, rule, **kwargs)


class _Entry:
    def __init__(self, mtime):
        self.mtime = mtime


# -- the rule a job applies ------------------------------------------------------

def test_the_rule_is_applied_without_asking():
    assert RuleConflicts(OVERWRITE)("t", _Entry(1), _Entry(2)) == OVERWRITE
    assert RuleConflicts(SKIP)("t", _Entry(9), _Entry(2)) == SKIP
    older = RuleConflicts("older")
    assert older("t", _Entry(100), _Entry(10)) == OVERWRITE   # target older
    assert older("t", _Entry(10), _Entry(100)) == SKIP        # target newer
    assert older("t", _Entry(10), _Entry(11)) == SKIP         # within slack
    assert older("t", _Entry(None), _Entry(10)) == SKIP       # cannot tell
    with pytest.raises(ValueError):
        RuleConflicts("ask")


def test_places():
    assert place_key(LocalFileSystem()) == ("local",)
    remote = _FakeRemoteBackend(host="h", username="u", port=2222)
    assert place_key(remote) == ("sftp", "h", 2222, "u")
    assert place_key(_FakeRemoteBackend(host="h")) != place_key(remote)


def test_summary():
    report = TransferReport(kept=["a"], links_left=["l"],
                            links_as_files=["x", "y"], modes_lost=["m"])
    assert summary(report, False) == ("1 existing kept, 1 symlinked directory "
                                      "skipped, 2 link(s) copied as files, "
                                      "permissions not kept on 1")
    assert summary(report, True).endswith(", source kept")
    report.links_left.append("k")
    assert "2 symlinked directories" in summary(report, False)
    assert summary(TransferReport(), True) == ""


# -- one job ------------------------------------------------------------------------

def test_a_copy_runs_to_the_end(tmp_path):
    job = _job(tmp_path, names=("a.txt", "dir"))
    write(str(tmp_path / "src" / "a.txt"), "hello")
    write(str(tmp_path / "src" / "dir" / "b.txt"), "world!")
    assert job.title() == f"Copy 2 items to {tmp_path / 'dst'}"
    assert job.speed() == 0.0 and job.state_text() == WAITING
    job.run()
    assert job.state == DONE and job.errors == []
    assert read(str(tmp_path / "dst" / "dir" / "b.txt")) == "world!"
    assert job.bytes_total == 11 and job.bytes_done == 11
    assert job.progress_text() == "100%  11 of 11"
    assert job.speed() > 0
    assert job.current == ""
    job.run()                                  # a finished job stays finished
    assert job.state == DONE


def test_progress_is_summed_across_files(tmp_path):
    job = _job(tmp_path)
    job.progress(10, 30, "one")
    job.progress(30, 30, "one")
    job.progress(30, 30, "one")                # the closing report adds nothing
    job.progress(5, 5, "two")
    job.progress(4, 9, "two")                  # a new file of the same name
    assert job.bytes_done == 39
    assert job.progress_text() == "39"         # no total yet
    job.bytes_total = 78
    assert job.progress_text() == " 50%  39 of 78"


def test_a_move_deletes_the_sources_through_the_app(tmp_path):
    deleted = []

    def delete(fs, paths, errors):
        deleted.extend(paths)
        for path in paths:
            fs.delete_tree(path)

    job = _job(tmp_path, move=True, delete_sources=delete)
    job.dst_fs = LocalFileSystem()             # two backends: copy, then delete
    write(str(tmp_path / "src" / "a.txt"), "x")
    assert job.title().startswith("Move a.txt to")
    job.run()
    assert job.state == DONE
    assert deleted == [str(tmp_path / "src" / "a.txt")]
    assert not (tmp_path / "src" / "a.txt").exists()


def test_an_existing_target_follows_the_rule(tmp_path):
    job = _job(tmp_path, rule=SKIP)
    write(str(tmp_path / "src" / "a.txt"), "new")
    write(str(tmp_path / "dst" / "a.txt"), "old")
    job.run()
    assert read(str(tmp_path / "dst" / "a.txt")) == "old"
    assert job.note == "1 existing kept"


def test_errors_stay_with_the_job(tmp_path):
    job = _job(tmp_path, names=("missing.txt",))
    job.run()
    assert job.state == FAILED
    assert job.errors[0].startswith("missing.txt:")


def test_a_destination_that_will_not_write_fails_the_job(tmp_path):
    class Refusing(LocalFileSystem):
        @contextlib.contextmanager
        def batch(self):
            yield
            raise OSError("archive could not be written")

    deleted = []
    job = _job(tmp_path, move=True,
               delete_sources=lambda fs, paths, errors: deleted.extend(paths))
    job.dst_fs = Refusing()
    write(str(tmp_path / "src" / "a.txt"), "x")
    job.run()
    assert job.state == FAILED
    assert job.errors == ["archive could not be written"]
    assert deleted == []                       # nothing landed: nothing goes
    assert (tmp_path / "src" / "a.txt").exists()


def test_a_size_that_cannot_be_counted_is_left_out(tmp_path, monkeypatch):
    def broken(fs, path):
        raise OSError("no")

    monkeypatch.setattr(jobs_mod, "count_tree", broken)
    job = _job(tmp_path)
    write(str(tmp_path / "src" / "a.txt"), "x")
    job.run()
    assert job.state == DONE and job.bytes_total == 0


def test_the_jobs_own_connections_are_closed(tmp_path):
    closed = []

    class Conn:
        def __init__(self, fails):
            self.fails = fails

        def close(self):
            closed.append(self)
            if self.fails:
                raise OSError("already gone")

    conns = [Conn(True), Conn(False)]
    job = _job(tmp_path, closing=conns)
    write(str(tmp_path / "src" / "a.txt"), "x")
    job.run()
    assert closed == conns


def test_a_waiting_job_cancelled_never_runs(tmp_path):
    job = _job(tmp_path)
    write(str(tmp_path / "src" / "a.txt"), "x")
    job.cancel()
    assert job.state == CANCELLED and not job.active()
    job.run()
    assert not (tmp_path / "dst" / "a.txt").exists()
    job.pause()                                # nothing to pause any more
    assert job.paused is False


def test_cancelling_leaves_no_half_written_file(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "CHUNK_SIZE", 4)
    job = _job(tmp_path, names=("first.txt", "big.txt"))
    write(str(tmp_path / "src" / "first.txt"), "done")
    write(str(tmp_path / "src" / "big.txt"), "x" * 40)
    original = job.progress

    def progress(done, total, label):
        original(done, total, label)
        if label == "big.txt" and done >= 8:
            job.cancel()

    job.progress = progress
    job.run()
    assert job.state == CANCELLED
    assert read(str(tmp_path / "dst" / "first.txt")) == "done"
    assert not (tmp_path / "dst" / "big.txt").exists()


def test_cancelling_between_items(tmp_path):
    job = _job(tmp_path, names=("a.txt", "b.txt"))
    write(str(tmp_path / "src" / "a.txt"), "a")
    write(str(tmp_path / "src" / "b.txt"), "b")
    original = job.progress

    def progress(done, total, label):
        original(done, total, label)
        job.cancel()

    job.progress = progress
    job.run()
    assert job.state == CANCELLED
    assert not (tmp_path / "dst" / "b.txt").exists()


def test_a_file_cut_short_whose_removal_fails(tmp_path, monkeypatch):
    class Sticky(LocalFileSystem):
        def remove(self, path):
            raise OSError("busy")

    write(str(tmp_path / "a.txt"), "data")
    with pytest.raises(operations.OperationCancelled):
        operations.copy_file(LocalFileSystem(), str(tmp_path / "a.txt"),
                             Sticky(), str(tmp_path / "b.txt"),
                             cancel=lambda: True)
    assert (tmp_path / "b.txt").exists()       # said nothing, raised the cancel


def test_pause_and_resume_on_a_real_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(operations, "CHUNK_SIZE", 4)
    job = _job(tmp_path)
    write(str(tmp_path / "src" / "a.txt"), "y" * 64)
    job.pause()
    job.pause()                                # twice is once
    assert job.state_text() == "waiting, paused"
    worker = threading.Thread(target=job.run)
    worker.start()
    deadline = time.monotonic() + 5
    while job.state != RUNNING and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert job.state == RUNNING and job.state_text() == "paused"
    assert job.bytes_done == 0                 # held at the first chunk
    assert job.speed() == 0.0
    job.resume()
    job.resume()                               # resuming a running job: nothing
    worker.join(5)
    assert job.state == DONE and job.bytes_done == 64
    assert read(str(tmp_path / "dst" / "a.txt")) == "y" * 64


def test_cancelling_a_paused_job(tmp_path):
    job = _job(tmp_path)
    write(str(tmp_path / "src" / "a.txt"), "x")
    job.pause()
    worker = threading.Thread(target=job.run)
    worker.start()
    deadline = time.monotonic() + 5
    while job.state != RUNNING and time.monotonic() < deadline:
        time.sleep(0.01)
    job.cancel()
    worker.join(5)
    assert job.state == CANCELLED


def test_speed_leaves_out_the_pauses(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(jobs_mod.time, "monotonic", lambda: clock[0])
    job = _job(tmp_path)
    job.started = 100.0
    job.bytes_done = 1000
    clock[0] = 110.0
    job.pause()
    clock[0] = 150.0
    assert job.speed() == 100.0                # 10 s working, 40 s paused
    job.resume()
    clock[0] = 160.0
    assert job.speed() == 50.0
    assert job.speed(now=170.0) == pytest.approx(1000 / 30)
    job.started, job.finished = 100.0, 100.0
    job._paused_for = 0.0
    assert job.speed() == 0.0                  # no time at all: no speed


# -- the queue ------------------------------------------------------------------------

def test_one_job_at_a_time_between_the_same_places(tmp_path):
    spawned = []
    queue = JobQueue(spawn=spawned.append)
    first = _job(tmp_path)
    second = _job(tmp_path)
    second.number = 2
    queue.submit(first)
    queue.submit(second)
    assert spawned == [first.run]
    assert queue.started(first) and not queue.started(second)
    assert queue.status() == "Jobs: 0 running, 2 waiting"
    first.run()                                # the thread does its work
    assert queue.tick() == [first]
    assert spawned == [first.run, second.run]
    assert queue.tick() == []                  # reported once
    second.run()
    assert queue.tick() == [second]
    assert queue.status() == ""


def test_jobs_between_different_places_run_side_by_side(tmp_path):
    spawned = []
    queue = JobQueue(spawn=spawned.append)
    local = _job(tmp_path)
    remote = _job(tmp_path)
    remote.number = 2
    remote.pair = (("local",), ("sftp", "h", 22, "u"))
    queue.submit(local)
    queue.submit(remote)
    assert spawned == [local.run, remote.run]


def test_the_status_line(tmp_path, monkeypatch):
    queue = JobQueue(spawn=lambda work: None)
    running = _job(tmp_path)
    queue.submit(running)
    running.state = RUNNING
    running.bytes_done = 2048
    monkeypatch.setattr(running, "speed", lambda: 1024.0)
    paused = _job(tmp_path)
    paused.number = 2
    paused.pair = ("elsewhere",)
    queue.submit(paused)
    paused.state, paused.paused = RUNNING, True
    assert queue.status() == "Jobs: 2 running  2.0K  1.0K/s"


def test_cancel_all_and_wait(tmp_path):
    queue = JobQueue(spawn=lambda work: None)
    job = _job(tmp_path)
    queue.submit(job)
    assert queue.wait(0.05) is False           # still waiting for a thread
    queue.cancel_all()
    assert job.state == CANCELLED
    assert queue.wait(1) is True


def test_the_default_spawn_is_a_thread(tmp_path):
    queue = JobQueue()
    job = _job(tmp_path)
    write(str(tmp_path / "src" / "a.txt"), "threaded")
    queue.submit(job)
    assert queue.wait(5) is True
    assert read(str(tmp_path / "dst" / "a.txt")) == "threaded"
    assert job.state == DONE


# -- in the app -----------------------------------------------------------------------

@pytest.fixture
def ran(app):
    spawned: list = []
    app.jobs = JobQueue(spawn=_now(spawned))
    return spawned


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def _background(app, monkeypatch, tmp_path, menu, prompt=None):
    return _ScriptedDialogs(monkeypatch, prompt=prompt or [str(tmp_path / "right")],
                            toggles=[{"Background": True}], menu=menu)


def test_f5_with_background_queues_a_job(app, tmp_path, monkeypatch, ran):
    write(str(tmp_path / "left" / "new.txt"), "fresh")
    app.left.refresh()
    _point_at(app.left, "new.txt")
    scripted = _background(app, monkeypatch, tmp_path, ["Overwrite if older"])
    app.handle_key(curses.KEY_F5)
    assert scripted.menus[0] == ("Background copy", ["Overwrite", "Overwrite if older",
                                                     "Skip", "Cancel"])
    assert scripted.menu_texts[0] == "If a target is already there:"
    job = app.jobs.jobs[0]
    assert job.conflicts.rule == "older"
    assert app.message.startswith("Job 1: Copy new.txt to")
    assert app.message.endswith("-- J to watch it")
    assert read(str(tmp_path / "right" / "new.txt")) == "fresh"
    assert "new.txt" not in [e.name for e in app.right.entries]
    app._tick_plugins()                        # the main loop notices
    assert "new.txt" in [e.name for e in app.right.entries]
    assert app.message.startswith("Job 1 done: Copy new.txt")


def test_a_local_move_is_a_rename(app, tmp_path, monkeypatch, ran):
    write(str(tmp_path / "left" / "mv.txt"), "m")
    app.left.refresh()
    _point_at(app.left, "mv.txt")
    _background(app, monkeypatch, tmp_path, ["Overwrite"])
    app._move()
    job = app.jobs.jobs[0]
    assert job.src_fs is job.dst_fs            # one backend: a rename
    assert job.state == DONE
    assert read(str(tmp_path / "right" / "mv.txt")) == "m"
    assert not (tmp_path / "left" / "mv.txt").exists()


def test_a_queued_job_says_it_waits(app, tmp_path, monkeypatch):
    app.jobs = JobQueue(spawn=lambda work: None)
    _point_at(app.left, "file.txt")
    _background(app, monkeypatch, tmp_path, ["Skip"])
    app._copy()
    _point_at(app.left, "file.txt")
    _background(app, monkeypatch, tmp_path, ["Skip"])
    app._copy()
    assert "after the one before it" in app.message


def test_a_job_with_errors_points_at_the_list(app, tmp_path, monkeypatch, ran):
    _point_at(app.left, "file.txt")
    _background(app, monkeypatch, tmp_path, ["Overwrite"],
                prompt=[str(tmp_path / "left" / "file.txt" / "no")])
    app._copy()
    app._tick_plugins()
    assert app.message.endswith("1 error, J to read")
    assert app.jobs.jobs[0].state == FAILED


def test_a_job_with_a_note(app, tmp_path, monkeypatch, ran):
    _point_at(app.left, "file.txt")
    _background(app, monkeypatch, tmp_path, ["Skip"])
    app._move()
    app._tick_plugins()
    assert app.message.endswith("1 existing kept, source kept")


def test_cancel_at_the_rule_queues_nothing(app, tmp_path, monkeypatch, ran):
    _point_at(app.left, "file.txt")
    _background(app, monkeypatch, tmp_path, ["Cancel"])
    app._copy()
    assert app.jobs.jobs == []


def test_an_archive_cannot_take_part(app, tmp_path, monkeypatch, ran):
    path = str(tmp_path / "left" / "bundle.zip")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("in.txt", "x")
    app.left.set_location(ArchiveFileSystem(LocalFileSystem(), path), "/")
    _point_at(app.left, "in.txt")
    scripted = _background(app, monkeypatch, tmp_path, [])
    app._copy()
    assert app.jobs.jobs == []
    assert "cannot take part in a background job" in scripted.messages[0][1]


class _Remote(_FakeRemoteBackend):
    instances: list = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.closed = False
        _Remote.instances.append(self)

    def close(self):
        self.closed = True


def test_a_remote_side_gets_a_connection_of_its_own(app, tmp_path,
                                                    monkeypatch, ran):
    pane_fs = _Remote(host="web1", username="deploy")
    app.right.set_location(pane_fs, str(tmp_path / "right"))
    dialled = []

    def connect(info):
        dialled.append(info)
        return _Remote(host=info["host"], username=info["username"])

    monkeypatch.setattr(app, "_connect", connect)
    write(str(tmp_path / "left" / "up.txt"), "upload")
    app.left.refresh()
    _point_at(app.left, "up.txt")
    _background(app, monkeypatch, tmp_path, ["Overwrite"])
    app._copy()
    job = app.jobs.jobs[0]
    assert dialled[0]["host"] == "web1" and dialled[0]["username"] == "deploy"
    assert job.dst_fs is not pane_fs and job.dst_pane_fs is pane_fs
    assert job.dst_fs.closed is True           # the job closed its own
    assert pane_fs.closed is False             # and left the pane's alone
    assert isinstance(job.src_fs, LocalFileSystem)
    assert job.src_fs is not app.left.fs
    assert read(str(tmp_path / "right" / "up.txt")) == "upload"


def test_a_connection_that_cannot_be_made(app, tmp_path, monkeypatch, ran):
    app.left.set_location(_Remote(host="a"), str(tmp_path / "left"))
    app.right.set_location(_Remote(host="b"), str(tmp_path / "right"))
    opened = []

    def connect(info):
        if info["host"] == "b":
            raise OSError("refused")
        fs = _Remote(host="a")
        opened.append(fs)
        return fs

    monkeypatch.setattr(app, "_connect", connect)
    _point_at(app.left, "file.txt")
    scripted = _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right"), ""],
                                toggles=[{"Background": True}], menu=["Skip"])
    app._copy()
    assert app.jobs.jobs == []
    assert opened[0].closed is True            # the half-made pair is undone
    assert scripted.messages[-1][0] == "Connection failed"


def test_the_jobs_pane(app, tmp_path, monkeypatch):
    app.jobs = JobQueue(spawn=lambda work: None)
    app.handle_key(ord("J"))
    pane = app.left.plugin
    assert isinstance(pane, JobsPane)
    assert app.message.startswith("Jobs -- p pauses")
    assert pane.selected() is None
    assert pane.handle_key(ord("p")) is True   # nothing to pause
    first = _job(tmp_path)
    second = _job(tmp_path)
    second.number = 2
    app.jobs.submit(first)
    app.jobs.submit(second)
    assert pane.selected() is first
    for key in (curses.KEY_DOWN, ord("j"), curses.KEY_UP, ord("k"), ord("j")):
        pane.handle_key(key)
    assert pane.selected() is second
    pane.handle_key(ord("p"))
    assert second.paused
    pane.handle_key(ord("p"))
    assert not second.paused
    _ScriptedDialogs(monkeypatch, confirm=[False, True])
    pane.handle_key(ord("c"))
    assert second.state == WAITING             # declined
    pane.handle_key(curses.KEY_DC)
    assert second.state == CANCELLED
    pane.handle_key(ord("c"))                  # finished: nothing asked
    for key in (9, ord("J"), curses.KEY_F10):
        assert pane.handle_key(key) is None
    assert pane.handle_key(ord("x")) is True
    app.handle_key(ord("J"))
    assert app.left.plugin is None
    assert app.message == "Jobs closed"


def test_the_jobs_pane_shows_errors_and_notes(app, tmp_path, monkeypatch):
    app.jobs = JobQueue(spawn=lambda work: None)
    job = _job(tmp_path)
    app.jobs.submit(job)
    app._jobs_pane()
    pane = app.left.plugin
    scripted = _ScriptedDialogs(monkeypatch)
    pane.handle_key(10)
    assert scripted.messages[-1][1].endswith("No errors.")
    job.note = "1 existing kept"
    pane.handle_key(10)
    assert scripted.messages[-1][1].endswith("1 existing kept")
    job.errors = ["a.txt: denied"]
    pane.handle_key(13)
    assert scripted.messages[-1] == ("Job 1", f"{job.title()}\n\na.txt: denied",
                                     True)
    assert pane.handle_key(27) is False


def test_the_jobs_pane_is_one_at_a_time(app):
    app.right.plugin = JobsPane(_ctx(app, app.right))
    assert app.active is app.left
    app._jobs_pane()                            # closes the one on the right
    assert app.right.plugin is None
    app.left.plugin = object()
    app._jobs_pane()
    assert app.message == "This pane is busy -- close it first"


def _ctx(app, panel):
    from meridian_commander.plugin_api import PluginContext
    return PluginContext(app=app, own_panel=panel, other_panel=app.other)


def test_the_menu_offers_it():
    command = next(m for m in MENUS if m["name"] == "command")
    assert any(i.get("name") == "jobs" and i.get("key") == "J"
               for i in command["items"])


def test_the_lines_for_a_job(tmp_path, monkeypatch):
    job = _job(tmp_path)
    job.state, job.current, job.bytes_done = RUNNING, "a.txt", 2048
    monkeypatch.setattr(job, "speed", lambda: 1024.0)
    top, bottom = JobsPane.lines_for(job)
    assert top == f" 1. running   {job.title()}"
    assert bottom == "    2.0K  1.0K/s  a.txt"
    job.state, job.current = DONE, ""
    job.errors = ["x", "y"]
    assert JobsPane.lines_for(job)[1] == "    2.0K  2 errors: Enter"
    job.errors = ["x"]
    assert JobsPane.lines_for(job)[1].endswith("1 error: Enter")
    job.errors, job.note = [], "1 existing kept"
    assert JobsPane.lines_for(job)[1].endswith("(1 existing kept)")


def _draw(tmp_path, prepare, rows=24, cols=100):
    def run(stdscr):
        app = App(stdscr, str(tmp_path / "left"), str(tmp_path / "right"))
        app.jobs = JobQueue(spawn=lambda work: None)
        prepare(app)
        app.draw()
        return "\n".join(stdscr.instr(row, 0).decode(errors="replace")
                         for row in range(rows))

    return with_curses_screen(rows, cols, run)


def test_the_pane_and_the_status_line_draw(app, tmp_path, monkeypatch):
    def prepare(app):
        for number in range(1, 13):            # more than the pane can hold
            job = _job(tmp_path)
            job.number = number
            job.pair = (number,)
            app.jobs.submit(job)
            job.state = RUNNING
        app._jobs_pane()
        app.left.plugin.row = 11

    text = _draw(tmp_path, prepare)
    assert "12. running" in text and " 1. running" not in text
    assert "p: pause  c: cancel  Enter: errors  J: close" in text
    assert "Jobs: 12 running" in text


def test_an_empty_jobs_pane_and_a_tiny_one(app, tmp_path):
    text = _draw(tmp_path, lambda app: app._jobs_pane())
    assert "No jobs. F5 or F6 with Background on starts one." in text
    app._jobs_pane()
    app.left.plugin.draw(None, 0, 0, 3, 40)    # too small: nothing touched
    app.left.plugin.draw(None, 0, 0, 20, 11)


def test_the_status_line_gives_way_on_a_narrow_screen(tmp_path):
    def prepare(app):
        app.single = True
        job = _job(tmp_path)
        app.jobs.submit(job)
        job.state = RUNNING

    assert "Jobs: 1 running" in _draw(tmp_path, prepare, cols=160)
    assert "Jobs:" not in _draw(tmp_path, prepare, cols=60)


def test_the_loop_polls_while_jobs_run(app, tmp_path):
    app.jobs = JobQueue(spawn=lambda work: None)
    assert app._tick_plugins() is None
    app.jobs.submit(_job(tmp_path))
    assert app._tick_plugins() == app.JOBS_POLL_MS


# -- quitting with jobs ----------------------------------------------------------------

def test_quit_can_wait_for_the_jobs(app, tmp_path, monkeypatch):
    app.jobs = JobQueue(spawn=lambda work: None)
    job = _job(tmp_path)
    app.jobs.submit(job)
    scripted = _ScriptedDialogs(monkeypatch, menu=["Wait for them, then quit"])
    app._quit()
    assert scripted.menu_texts[0] == "1 background job still running or waiting."
    assert app.running is True
    assert app.message.startswith("Quitting when the jobs end")
    job.cancel()
    app._tick_plugins()
    assert app.running is False


def test_quit_can_cancel_the_jobs(app, tmp_path, monkeypatch):
    app.jobs = JobQueue(spawn=lambda work: None)
    for number in (1, 2):
        job = _job(tmp_path)
        job.number = number
        app.jobs.submit(job)
    scripted = _ScriptedDialogs(monkeypatch, menu=["Cancel them and quit"])
    app._quit()
    assert "2 background jobs" in scripted.menu_texts[0]
    assert app.running is False
    assert all(job.state == CANCELLED for job in app.jobs.jobs)


def test_quit_can_go_back(app, tmp_path, monkeypatch):
    app.jobs = JobQueue(spawn=lambda work: None)
    app.jobs.submit(_job(tmp_path))
    _ScriptedDialogs(monkeypatch, menu=["Back"])
    app._quit()
    assert app.running is True and not app._quit_when_done


def test_ctrl_c_with_jobs_running(tmp_path, monkeypatch):
    from meridian_commander import dialogs

    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda visibility: 0)
    (tmp_path / "left").mkdir()
    (tmp_path / "right").mkdir()
    answers = iter([2, 1])                     # Back, then Cancel and quit
    monkeypatch.setattr(dialogs, "menu", lambda *a, **k: next(answers))
    screen = _LoopScreen([KeyboardInterrupt(), KeyboardInterrupt()])
    app = App(screen, str(tmp_path / "left"), str(tmp_path / "right"))
    app.jobs = JobQueue(spawn=lambda work: None)
    app.jobs.submit(_job(tmp_path))
    app.run()
    assert app.running is False


def test_the_loop_ends_when_the_last_job_does(tmp_path, monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda visibility: 0)
    (tmp_path / "left").mkdir()
    (tmp_path / "right").mkdir()
    screen = _LoopScreen([-1])
    app = App(screen, str(tmp_path / "left"), str(tmp_path / "right"))
    app.jobs = JobQueue(spawn=lambda work: None)
    job = _job(tmp_path)
    app.jobs.submit(job)
    app._quit_when_done = True
    original = app._tick_plugins

    def tick():
        result = original()
        job.cancel()                           # it ends while the loop waits
        return result

    monkeypatch.setattr(app, "_tick_plugins", tick)
    app.run()                                  # one poll, then out
    assert screen.keys == []


def test_closing_the_app_cancels_the_jobs(tmp_path):
    app = App(_StubScreen(), str(tmp_path), str(tmp_path))
    app.jobs = JobQueue(spawn=lambda work: None)
    job = _job(tmp_path)
    app.jobs.submit(job)
    app._close_backends()
    assert job.state == CANCELLED
