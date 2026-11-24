# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Auto-sync's checks and the thread that runs them (MC-034).

The checks are driven one at a time, with no thread and no clock: each
``pass_once()`` is one look at both sides, as the runner makes every few
seconds.  The directories are real, and so are the trashes they use.
"""

from __future__ import annotations

import os
import shutil
import time

import pytest

from meridian_commander import autosync as autosync_mod
from meridian_commander import config, trash
from meridian_commander.autosync import (
    RUNNING,
    STOPPED,
    WAITING,
    AutoSync,
    AutoSyncRunner,
    PassReport,
    Place,
    Unreachable,
    in_step,
    move_to_remote_trash,
)
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.operations import OperationCancelled

from support import _FakeRemoteBackend, read, write


@pytest.fixture
def where(tmp_path, monkeypatch):
    """Two empty directories, ``a`` and ``b``, and a trash and home of their
    own.

    The FreeDesktop trash on every platform, as the trash's own tests have
    it: on macOS the desktop's trash is the real ``~/.Trash``.
    """
    monkeypatch.setattr(trash.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("a", "b", "home"):
        (tmp_path / name).mkdir()
    return tmp_path


def _engine(where, left_fs=None, right_fs=None, **kwargs) -> AutoSync:
    return AutoSync(Place(left_fs or LocalFileSystem(), str(where / "a"), "a"),
                    Place(right_fs or LocalFileSystem(), str(where / "b"), "b"),
                    **kwargs)


def _both(where, rel, content="same", mtime=1000.0):
    for side in ("a", "b"):
        write(str(where / side / rel), content, mtime=mtime)


def _trashed(where) -> dict[str, str]:
    """What is in the local trash: name -> contents."""
    files = where / "data" / "Trash" / "files"
    if not files.is_dir():
        return {}
    return {name: read(str(files / name)) for name in os.listdir(files)
            if (files / name).is_file()}


# -- copying -------------------------------------------------------------------

@pytest.mark.parametrize("side, other, into", [("a", "b", [0, 1]),
                                                ("b", "a", [1, 0])])
def test_a_new_file_is_copied_once_it_has_held_still(where, side, other,
                                                     into):
    sync = _engine(where)
    assert not sync.pass_once()            # the first check only looks
    write(str(where / side / "new.txt"), "hello", mtime=1000)
    report = sync.pass_once()
    assert report.held == 1 and not (where / other / "new.txt").exists()
    report = sync.pass_once()
    assert report.copied == into
    assert read(str(where / other / "new.txt")) == "hello"
    assert os.stat(where / other / "new.txt").st_mtime == 1000
    assert report.changed(into.index(1)) and not report.changed(into.index(0))
    assert "new.txt copied to " + other in report.events[0]
    # A second look finds the two the same: nothing is copied back.
    again = sync.pass_once()
    assert not again and again.held == 0


def test_a_changed_file_is_copied_across(where):
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()
    write(str(where / "b" / "f.txt"), "changed on b", mtime=2000)
    assert sync.pass_once().held == 1
    report = sync.pass_once()
    assert report.copied == [1, 0]
    assert read(str(where / "a" / "f.txt")) == "changed on b"
    assert "changed on b" in report.events[0]


def test_the_side_that_changed_wins_even_with_an_older_clock(where):
    """F9 would take the newer copy; auto-sync knows which one was edited,
    so a file put back to an older version is not overwritten by the copy
    it replaced."""
    _both(where, "f.txt", mtime=5000)
    sync = _engine(where)
    sync.pass_once()
    write(str(where / "a" / "f.txt"), "an older version", mtime=1000)
    sync.pass_once()
    report = sync.pass_once()
    assert report.copied == [0, 1]
    assert read(str(where / "b" / "f.txt")) == "an older version"


def test_a_file_still_being_written_waits_until_it_stops(where):
    sync = _engine(where)
    sync.pass_once()
    write(str(where / "a" / "big.bin"), "part", mtime=1000)
    assert sync.pass_once().held == 1
    write(str(where / "a" / "big.bin"), "part and more", mtime=1001)
    assert sync.pass_once().held == 1           # it moved: still waiting
    assert not (where / "b" / "big.bin").exists()
    assert sync.pass_once().copied == [0, 1]
    assert read(str(where / "b" / "big.bin")) == "part and more"


def test_nothing_changed_copies_nothing(where):
    _both(where, "f.txt")
    _both(where, "sub/g.txt")
    sync = _engine(where)
    for _ in range(3):
        report = sync.pass_once()
        assert not report and report.held == 0


def test_both_changed_keeps_the_newer_and_trashes_the_other(where):
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()
    write(str(where / "a" / "f.txt"), "edit on a", mtime=3000)
    write(str(where / "b" / "f.txt"), "edit on b, later", mtime=4000)
    sync.pass_once()
    report = sync.pass_once()
    assert report.conflicts == ["f.txt"]
    assert report.copied == [1, 0]
    assert read(str(where / "a" / "f.txt")) == "edit on b, later"
    # The edit that lost is not written over: it is in a's trash.
    assert _trashed(where) == {"f.txt": "edit on a"}
    assert "changed on both sides" in report.events[0]
    assert not sync.pass_once()


def test_both_changed_with_no_trash_copies_nothing(where, monkeypatch):
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()
    write(str(where / "a" / "f.txt"), "edit on a", mtime=3000)
    write(str(where / "b" / "f.txt"), "edit on b, later", mtime=4000)

    def refuse(path):
        raise trash.TrashUnavailable("no trash here")

    monkeypatch.setattr(trash, "move_to_trash", refuse)
    sync.pass_once()
    report = sync.pass_once()
    assert report.copied == [0, 0] and report.conflicts == []
    assert "could not be kept in the trash" in report.failures[0]
    assert read(str(where / "a" / "f.txt")) == "edit on a"
    assert not sync.pass_once()                 # not tried again unchanged


def test_a_backend_that_cannot_set_times_does_not_bounce(where):
    """The copy comes out newer than its source; the two are still the
    same file, and the next check must not copy it back."""

    class _NoTimes(LocalFileSystem):
        def utime(self, path, mtime):
            pass

    write(str(where / "a" / "f.txt"), "x", mtime=1000)
    sync = _engine(where, right_fs=_NoTimes())
    sync.pass_once()
    assert sync.pass_once().copied == [0, 1]
    assert os.stat(where / "b" / "f.txt").st_mtime > 1000 + 60
    for _ in range(3):
        assert not sync.pass_once()


# -- deletes and renames -------------------------------------------------------

def test_a_delete_goes_to_the_trash_on_the_other_side(where):
    _both(where, "f.txt", "keep me")
    sync = _engine(where)
    sync.pass_once()
    os.remove(where / "b" / "f.txt")
    report = sync.pass_once()
    # Gone for one check only: an editor saving by renaming looks like that.
    assert report.held == 1 and (where / "a" / "f.txt").exists()
    report = sync.pass_once()
    assert report.trashed == [1, 0]
    assert not (where / "a" / "f.txt").exists()
    assert _trashed(where) == {"f.txt": "keep me"}
    assert "deleted on b -- moved to the trash on a" in report.events[0]
    assert not sync.pass_once()


def test_never_on_the_first_check_nor_for_a_file_one_side_never_had(where):
    """A file only one side holds when auto-sync starts is new, not deleted:
    the checks have never seen the two the same."""
    write(str(where / "a" / "only.txt"), "mine", mtime=1000)
    sync = _engine(where)
    report = sync.pass_once()
    assert report.trashed == [0, 0] and report.held == 1
    assert sync.pass_once().copied == [0, 1]
    assert _trashed(where) == {}


def test_a_rename_is_made_on_the_other_side(where):
    _both(where, "old.txt", "contents")
    sync = _engine(where)
    sync.pass_once()
    os.rename(where / "a" / "old.txt", where / "a" / "new.txt")
    sync.pass_once()
    report = sync.pass_once()
    assert report.copied == [0, 1] and report.trashed == [0, 1]
    assert sorted(os.listdir(where / "b")) == ["new.txt"]
    assert _trashed(where) == {"old.txt": "contents"}


def test_a_deleted_folder_takes_its_empty_shell_with_it(where):
    _both(where, "keep.txt")
    _both(where, "d/x.txt")
    _both(where, "d/deeper/y.txt")
    sync = _engine(where)
    sync.pass_once()
    shutil.rmtree(where / "a" / "d")
    sync.pass_once()
    report = sync.pass_once()
    assert os.listdir(where / "b") == ["keep.txt"]
    # Two files to the trash, two folders left empty removed.
    assert report.trashed == [0, 4]
    assert any("d/: left empty" in event for event in report.events)


def test_a_folder_the_other_side_still_has_is_kept(where):
    _both(where, "d/x.txt")
    _both(where, "d/y.txt")
    sync = _engine(where)
    sync.pass_once()
    os.remove(where / "a" / "d" / "x.txt")
    os.remove(where / "a" / "d" / "y.txt")
    sync.pass_once()
    sync.pass_once()
    assert (where / "b" / "d").is_dir()         # a still has d, empty
    assert os.listdir(where / "b" / "d") == []


def test_a_folder_with_something_new_in_it_is_kept(where):
    _both(where, "keep.txt")
    _both(where, "d/x.txt")
    _both(where, "d/y.txt")
    sync = _engine(where)
    sync.pass_once()
    shutil.rmtree(where / "a" / "d")
    write(str(where / "b" / "d" / "fresh.txt"), "new on b", mtime=1000)
    sync.pass_once()
    sync.pass_once()
    # The deleted files went; the one nobody had seen on a was kept -- and,
    # being new on b, copied to a.
    assert os.listdir(where / "b" / "d") == ["fresh.txt"]
    assert read(str(where / "a" / "d" / "fresh.txt")) == "new on b"


def test_a_folder_that_cannot_be_removed_is_left(where):
    class _Stuck(LocalFileSystem):
        def rmdir(self, path):
            raise OSError("busy")

    _both(where, "keep.txt")
    _both(where, "d/x.txt")
    _both(where, "d/y.txt")
    sync = _engine(where, right_fs=_Stuck())
    sync.pass_once()
    shutil.rmtree(where / "a" / "d")
    sync.pass_once()
    report = sync.pass_once()
    assert report.trashed == [0, 2]
    assert (where / "b" / "d").is_dir()


def test_changed_on_one_side_and_deleted_on_the_other_keeps_the_change(where):
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()
    write(str(where / "a" / "f.txt"), "an edit worth keeping", mtime=5000)
    os.remove(where / "b" / "f.txt")
    sync.pass_once()
    report = sync.pass_once()
    assert report.copied == [0, 1]
    assert read(str(where / "b" / "f.txt")) == "an edit worth keeping"
    assert "changed, and deleted on b" in report.events[0]
    assert _trashed(where) == {}


def test_a_file_saved_by_renaming_is_not_deleted(where):
    """Gone for one check and back by the next: an editor's save."""
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()
    os.remove(where / "b" / "f.txt")
    sync.pass_once()
    write(str(where / "b" / "f.txt"), "saved", mtime=6000)
    sync.pass_once()
    assert sync.pass_once().copied == [1, 0]
    assert read(str(where / "a" / "f.txt")) == "saved"
    assert _trashed(where) == {}


def test_a_delete_with_no_trash_is_left_and_counted(where, monkeypatch):
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()

    def refuse(path):
        raise trash.TrashUnavailable("no trash on that disk")

    monkeypatch.setattr(trash, "move_to_trash", refuse)
    os.remove(where / "b" / "f.txt")
    sync.pass_once()
    report = sync.pass_once()
    assert report.failures == ["f.txt: deleted on b, but could not be moved "
                               "to the trash on a, so it was left: no trash "
                               "on that disk"]
    assert (where / "a" / "f.txt").exists()
    # Not tried again, nor counted again, until something changes.
    assert not sync.pass_once()


def test_a_delete_onto_a_remote_side_goes_to_its_own_trash(where):
    _both(where, "site/index.html", "<html>")
    sync = _engine(where, right_fs=_FakeRemoteBackend())
    sync.pass_once()
    os.remove(where / "a" / "site" / "index.html")
    sync.pass_once()
    report = sync.pass_once()
    assert report.trashed[1] >= 1
    kept = list((where / "home" / ".meridian-trash").rglob("index.html"))
    assert len(kept) == 1
    # Under the time it went, then the whole path it had.
    stamp = kept[0].relative_to(where / "home" / ".meridian-trash").parts[0]
    assert time.strptime(stamp, "%Y-%m-%d_%H%M%S")
    assert str(kept[0]).endswith(str(where / "b" / "site" / "index.html"))
    assert read(str(kept[0])) == "<html>"


def test_the_remote_trash_never_writes_over_what_is_in_it(where):
    fs = LocalFileSystem()
    for content in ("first", "second"):
        write(str(where / "b" / "f.txt"), content)
        move_to_remote_trash(fs, str(where / "b" / "f.txt"), now=lambda: 0)
    stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(0))
    base = where / "home" / ".meridian-trash" / stamp
    target = str(base) + str(where / "b" / "f.txt")
    assert read(target) == "first"
    assert read(target + ".2") == "second"


# -- keeping out of the trash ----------------------------------------------------

def test_a_trash_inside_the_synced_folder_is_not_synced(where, monkeypatch):
    """Syncing a home directory: the trash is inside it, and what goes into
    it must not be copied to the other side."""
    monkeypatch.setenv("XDG_DATA_HOME", str(where / "a" / ".local" / "share"))
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()
    os.remove(where / "b" / "f.txt")
    for _ in range(4):
        sync.pass_once()
    assert (where / "a" / ".local" / "share" / "Trash" / "files" /
            "f.txt").exists()
    assert not (where / "b" / ".local").exists()


def test_a_remote_trash_inside_the_synced_folder_is_not_synced(where,
                                                               monkeypatch):
    monkeypatch.setenv("HOME", str(where / "b" / "me"))
    _both(where, "f.txt")
    sync = _engine(where, right_fs=_FakeRemoteBackend())
    sync.pass_once()
    os.remove(where / "a" / "f.txt")
    for _ in range(4):
        sync.pass_once()
    assert (where / "b" / "me" / ".meridian-trash").is_dir()
    assert not (where / "a" / "me").exists()


def test_where_the_local_trash_is(where, monkeypatch):
    place = Place(LocalFileSystem(), str(where / "a"), "a")
    assert place.trash_dir() == str(where / "data" / "Trash")
    # macOS's trash is listed as it is, with no files/ inside it.
    monkeypatch.setattr(trash, "trash_location", lambda path: "/u/.Trash")
    assert place.trash_dir() == "/u/.Trash"

    def broken(path):
        raise OSError("no home")

    monkeypatch.setattr(trash, "trash_location", broken)
    assert place.trash_dir() is None
    assert _engine(where)._skip() == set()


# -- what cannot be read ---------------------------------------------------------

class _Blind(LocalFileSystem):
    """A local disk that cannot read the paths in ``hidden``."""

    def __init__(self):
        super().__init__()
        self.hidden: set[str] = set()

    def listdir(self, path):
        if path in self.hidden:
            raise PermissionError(f"cannot read {path}")
        return super().listdir(path)


def test_a_folder_that_cannot_be_read_is_not_taken_for_deleted(where):
    _both(where, "d/x.txt")
    blind = _Blind()
    sync = _engine(where, left_fs=blind)
    sync.pass_once()
    blind.hidden.add(str(where / "a" / "d"))
    for _ in range(3):
        report = sync.pass_once()
        assert not report and report.held == 0
    assert (where / "b" / "d" / "x.txt").exists()
    blind.hidden.clear()
    assert not sync.pass_once()


def test_a_side_that_cannot_be_listed_stops_the_check(where):
    blind = _Blind()
    blind.hidden.add(str(where / "b"))
    sync = _engine(where, right_fs=blind)
    with pytest.raises(Unreachable) as caught:
        sync.pass_once()
    assert caught.value.place.name == "b"
    assert str(caught.value).startswith("b: cannot read")


def test_a_failure_on_a_side_that_has_gone_stops_the_check(where):
    """A dropped connection fails every copy after it.  Those are not the
    files' failures, which would never be tried again: the check stops."""

    class _Dropping(LocalFileSystem):
        gone = False

        def open_write(self, path):
            self.gone = True
            raise OSError("connection reset")

        def stat(self, path):
            if self.gone:
                raise OSError("not connected")
            return super().stat(path)

    sync = _engine(where, right_fs=_Dropping())
    sync.pass_once()
    write(str(where / "a" / "f.txt"), "x", mtime=1000)
    sync.pass_once()
    with pytest.raises(Unreachable):
        sync.pass_once()
    assert sync._failed == {}


def test_a_failed_copy_is_counted_once_and_tried_again_after_a_change(where):
    class _ReadOnly(LocalFileSystem):
        def open_write(self, path):
            raise PermissionError("read-only")

    sync = _engine(where, right_fs=_ReadOnly())
    sync.pass_once()
    write(str(where / "a" / "f.txt"), "x", mtime=1000)
    sync.pass_once()
    report = sync.pass_once()
    assert report.failures == ["f.txt: could not be copied to b: read-only"]
    assert report.events == ["FAILED f.txt: could not be copied to b: "
                             "read-only"]
    assert not sync.pass_once()
    write(str(where / "a" / "f.txt"), "xy", mtime=2000)
    sync.pass_once()
    assert len(sync.pass_once().failures) == 1


def test_a_side_suddenly_empty_is_not_believed(where):
    """An unmounted disk looks exactly like that, and mirroring it would
    send everything on the other side to the trash."""
    _both(where, "one.txt")
    _both(where, "two.txt")
    sync = _engine(where)
    sync.pass_once()
    for name in ("one.txt", "two.txt"):
        os.remove(where / "a" / name)
    for _ in range(3):
        report = sync.pass_once()
        assert report.waiting == ("a is suddenly empty -- nothing is mirrored "
                                  "until it has files again")
    assert sorted(os.listdir(where / "b")) == ["one.txt", "two.txt"]
    write(str(where / "a" / "one.txt"), "same", mtime=1000)
    sync.pass_once()
    assert sync.pass_once().trashed == [0, 1]       # two.txt really went


def test_one_file_deleted_from_a_folder_of_one_is_mirrored(where):
    _both(where, "only.txt")
    sync = _engine(where)
    sync.pass_once()
    os.remove(where / "a" / "only.txt")
    sync.pass_once()
    assert sync.pass_once().trashed == [0, 1]


# -- stopping --------------------------------------------------------------------

def test_a_stop_ends_the_check_between_files(where):
    write(str(where / "a" / "f.txt"), "x")
    sync = _engine(where, stopping=lambda: True)
    with pytest.raises(OperationCancelled):
        sync.pass_once()


def test_a_cancel_during_the_walk_ends_the_check(where):
    write(str(where / "a" / "f.txt"), "x")
    with pytest.raises(OperationCancelled):
        _engine(where, cancel=lambda: True).pass_once()


def test_a_file_deleted_from_both_sides_is_forgotten(where):
    """Nothing to do, and nothing to remember: made again on one side, it
    is new there, not a delete to mirror onto the other."""
    _both(where, "keep.txt")
    _both(where, "f.txt")
    sync = _engine(where)
    sync.pass_once()
    for side in ("a", "b"):
        os.remove(where / side / "f.txt")
    assert not sync.pass_once()
    assert "f.txt" not in sync._agreed
    write(str(where / "a" / "f.txt"), "again", mtime=2000)
    sync.pass_once()
    assert sync.pass_once().copied == [0, 1]
    assert _trashed(where) == {}


def test_a_cancel_part_way_through_a_copy_leaves_no_half_file(where):
    hurry = []

    class _Noting(LocalFileSystem):
        def open_write(self, path):
            hurry.append(path)
            return super().open_write(path)

    sync = _engine(where, right_fs=_Noting(), cancel=lambda: bool(hurry))
    sync.pass_once()
    write(str(where / "a" / "f.txt"), "x" * 100_000, mtime=1000)
    sync.pass_once()
    with pytest.raises(OperationCancelled):
        sync.pass_once()
    assert hurry and not (where / "b" / "f.txt").exists()


# -- the rules on their own ------------------------------------------------------

def test_two_copies_are_the_same_by_f9s_rule():
    assert in_step((5, 1000.0), (5, 1001.5))
    assert not in_step((5, 1000.0), (5, 1003.0))
    assert not in_step((5, 1000.0), (6, 1000.0))
    assert in_step((5, None), (5, 1000.0))          # no time: the size says


def test_which_side_wins(where):
    sync = _engine(where)
    assert sync._direction((1, 1000.0), (2, 1000.0), None) == \
        (0, "differs (same age)", False)
    assert sync._direction((1, None), (2, 1000.0), None) == \
        (0, "differs (no time)", False)
    assert sync._direction((1, 1000.0), (2, 2000.0), None) == \
        (1, "newer on b", False)
    assert sync._direction((1, 3000.0), (2, 2000.0), None) == \
        (0, "newer on a", False)
    agreed = ((1, 1000.0), (1, 1000.0))
    assert sync._direction((2, 900.0), (1, 1000.0), agreed) == \
        (0, "changed on a", False)
    assert sync._direction((1, 1000.0), (3, 900.0), agreed) == \
        (1, "changed on b", False)
    assert sync._direction((2, 900.0), (3, 901.0), agreed) == \
        (0, "differs (same age)", True)


def test_a_report_is_true_when_something_happened():
    assert not PassReport(held=3)
    assert PassReport(copied=[0, 1])
    assert PassReport(trashed=[1, 0])
    assert PassReport(conflicts=["x"])
    assert PassReport(failures=["x"])


# -- the runner ------------------------------------------------------------------

class _Pane:
    """What the runner asks of a pane: where it is."""

    def __init__(self, path, fs=None):
        self.fs = fs or LocalFileSystem()
        self.path = path
        self.results = None


def _panes(where):
    return (_Pane(str(where / "a")), _Pane(str(where / "b")))


def _runner(where, left_fs=None, right_fs=None, reopen=(None, None),
            closing=None, interval=2.0, panels=None):
    places = (Place(left_fs or LocalFileSystem(), str(where / "a"), "a"),
              Place(right_fs or LocalFileSystem(), str(where / "b"), "b"))
    started = []
    runner = AutoSyncRunner(panels or _panes(where), places, reopen, interval,
                            closing=closing, spawn=started.append,
                            clock=lambda: 0.0)
    runner.started = started
    return runner


def test_the_runner_counts_what_the_checks_did(where):
    runner = _runner(where)
    assert runner.status() == "watching"
    runner.step()
    write(str(where / "a" / "f.txt"), "x", mtime=1000)
    runner.step()
    assert runner.tick() == ([], [])            # only held: nothing to say
    runner.step()
    reports, notes = runner.tick()
    assert len(reports) == 1 and notes == []
    assert reports[0].copied == [0, 1]
    assert runner.status() == "1 copied"
    assert runner.tick() == ([], [])
    os.remove(where / "a" / "f.txt")
    runner.step()
    runner.step()
    assert runner.status() == "1 copied, 1 to the trash"
    text = runner.report_text()
    assert "f.txt copied to b (new)" in text
    assert "f.txt: deleted on a -- moved to the trash on b" in text
    assert "Checked every 2 seconds, last at" in text
    assert runner.state_text() == RUNNING


def test_the_runner_keeps_failures_to_be_read(where):
    class _ReadOnly(LocalFileSystem):
        def open_write(self, path):
            raise PermissionError("read-only")

    runner = _runner(where, right_fs=_ReadOnly())
    runner.step()
    write(str(where / "a" / "f.txt"), "x", mtime=1000)
    runner.step()
    runner.step()
    assert runner.status() == "1 failed: J"
    text = runner.report_text()
    assert "Failures:\n" in text
    assert "f.txt: could not be copied to b: read-only" in text


def test_the_runner_keeps_only_so_much(where, monkeypatch):
    monkeypatch.setattr(autosync_mod, "LOG_LIMIT", 2)
    monkeypatch.setattr(autosync_mod, "FAILURE_LIMIT", 1)

    class _ReadOnly(LocalFileSystem):
        def open_write(self, path):
            raise PermissionError("read-only")

    runner = _runner(where, right_fs=_ReadOnly())
    runner.step()
    for name in ("x", "y", "z"):
        write(str(where / "a" / name), name, mtime=1000)
    runner.step()
    runner.step()
    assert len(runner.failures) == 1 and len(runner.log) == 2
    assert "z: could not" in runner.failures[0]


def test_a_report_with_nothing_done_says_so(where):
    runner = _runner(where)
    assert runner.report_text().endswith("What it did, newest last:\n"
                                         "nothing yet")
    assert "last at" not in runner.report_text()


def test_the_runner_waits_while_a_side_is_suddenly_empty(where):
    _both(where, "one.txt")
    _both(where, "two.txt")
    runner = _runner(where)
    runner.step()
    for name in ("one.txt", "two.txt"):
        os.remove(where / "a" / name)
    runner.step()
    runner.step()
    _, notes = runner.tick()
    assert notes == ["a is suddenly empty -- nothing is mirrored until it "
                     "has files again"]              # said once, not twice
    assert runner.state == WAITING
    assert runner.status().startswith("a is suddenly empty")
    assert "a is suddenly empty" in runner.report_text()
    write(str(where / "a" / "one.txt"), "same", mtime=1000)
    runner.step()
    assert runner.tick()[1] == ["checking again"]
    assert runner.state == RUNNING


def test_a_local_side_that_cannot_be_read_is_read_again(where):
    blind = _Blind()
    blind.hidden.add(str(where / "a"))
    runner = _runner(where, left_fs=blind)
    runner.step()
    runner.step()
    _, notes = runner.tick()
    assert notes == [f"cannot read a, trying again: a: cannot read "
                     f"{where / 'a'}"]
    assert runner.status() == "cannot read a, trying again"
    assert runner._broken == set()               # nothing to dial
    blind.hidden.clear()
    runner.step()
    assert runner.tick()[1] == ["checking again"]


class _Remote(_FakeRemoteBackend):
    def __init__(self, broken=False, fail_close=False):
        super().__init__()
        self.broken = broken
        self.closed = False
        self.fail_close = fail_close

    def listdir(self, path):
        if self.broken:
            raise OSError("connection lost")
        return super().listdir(path)

    def close(self):
        self.closed = True
        if self.fail_close:
            raise OSError("already gone")


def test_a_remote_side_that_drops_is_dialled_again(where):
    dropped = _Remote(broken=True, fail_close=True)
    dials = []

    def again():
        if not dials:
            dials.append("refused")
            raise OSError("host unreachable")
        fresh = _Remote()
        dials.append(fresh)
        return fresh

    runner = _runner(where, right_fs=dropped, reopen=(None, again),
                     closing=[dropped])
    runner.step()
    assert runner.status() == "cannot reach b, reconnecting"
    assert runner._broken == {1}
    runner.step()                                  # the dial is refused
    assert runner.places[1].fs is dropped
    _, notes = runner.tick()
    assert notes == ["cannot reach b, reconnecting: b: connection lost"]
    runner.step()                                  # it answers
    assert runner.places[1].fs is dials[1]
    assert dropped.closed and runner.closing == [dials[1]]
    assert runner.state == RUNNING and runner._broken == set()
    assert runner.tick()[1] == ["checking again"]


def test_a_dropped_connection_the_runner_did_not_open_is_left_open(where):
    pane_fs = _Remote(broken=True)
    runner = _runner(where, right_fs=pane_fs,
                     reopen=(None, lambda: _Remote()))
    runner.step()
    runner.step()
    assert not pane_fs.closed
    assert runner.places[1].fs is not pane_fs


def test_a_stopped_check_ends_quietly(where):
    write(str(where / "a" / "f.txt"), "x")
    runner = _runner(where)
    runner.stop()
    runner.step()
    assert runner.tick() == ([], [])


def test_the_runner_runs_on_a_thread_until_stopped(where):
    closing = [_Remote(fail_close=True), _Remote()]
    places = (Place(LocalFileSystem(), str(where / "a"), "a"),
              Place(LocalFileSystem(), str(where / "b"), "b"))
    runner = AutoSyncRunner(_panes(where), places, (None, None), 0.01,
                            closing=closing)
    write(str(where / "a" / "f.txt"), "x", mtime=1000)
    runner.start()
    deadline = time.monotonic() + 10
    while not (where / "b" / "f.txt").exists():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    runner.stop()
    assert runner.wait(5)
    assert runner.state == STOPPED
    assert all(fs.closed for fs in closing)


def test_start_hands_the_loop_to_spawn(where):
    runner = _runner(where)
    runner.start()
    assert runner.started == [runner._run]


def test_abort_stops_now(where):
    runner = _runner(where)
    runner.abort()
    assert runner._aborted() and runner._stop.is_set()
    assert not runner.wait(0)


def test_the_runner_says_when_a_pane_moves(where):
    left, right = _Pane(str(where / "a")), _Pane(str(where / "b"))
    runner = _runner(where, panels=(left, right))
    assert runner.moved() is None
    right.path = str(where / "b" / ".")              # the same, written oddly
    assert runner.moved() is None
    left.path = str(where)
    assert runner.moved() == (left, "directory")
    left.path = str(where / "a")
    right.fs = LocalFileSystem()
    assert runner.moved() == (right, "connection")
    right.fs = runner.where[1][0]
    right.results = []
    assert runner.moved() == (right, "directory")


# -- configuration -----------------------------------------------------------------

@pytest.mark.parametrize("written, expected", [
    ("", (2.0, 10.0)),
    ("[autosync]\ninterval = 5\nremote_interval = 30\n", (5.0, 30.0)),
    ("[autosync]\ninterval = 0.1\nremote_interval = 0\n", (0.5, 0.5)),
    ("[autosync]\ninterval = soon\nremote_interval = nan\n", (2.0, 10.0)),
    ("[autosync]\ninterval = inf\nremote_interval = -inf\n", (2.0, 10.0)),
    ("[autosync]\ninterval =\n", (2.0, 10.0)),
])
def test_the_intervals_come_from_the_config(tmp_path, monkeypatch, written,
                                            expected):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    if written:
        write(str(tmp_path / "meridian-commander" / "config.ini"), written)
    assert config.autosync_intervals() == expected


def test_a_new_config_file_says_how_to_set_them(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.ensure_config()
    assert config.autosync_intervals() == (2.0, 10.0)
    text = read(str(tmp_path / "meridian-commander" / "config.ini"))
    assert "[autosync]" in text and "remote_interval = 10" in text
