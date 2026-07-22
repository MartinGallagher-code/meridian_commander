"""Tests for the filesystem-agnostic core: copy, move and sync.

These exercise the real code paths using the local filesystem backend and
temporary directories, which is enough to validate the transfer and sync logic
that also drives remote transfers (the backend interface is identical).
"""

from __future__ import annotations

import os
import time

import pytest

from martin_commander.filesystems import FTPFileSystem, LocalFileSystem
from martin_commander.operations import copy_path, count_tree, move_path
from martin_commander.panel import Panel
from martin_commander.sync import build_sync_plan, execute_sync_plan


@pytest.fixture
def fs():
    return LocalFileSystem()


def write(path: str, content: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


def test_copy_file(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello world")
    dst = tmp_path / "sub" / "b.txt"
    copy_path(fs, str(src), fs, str(dst))
    assert dst.read_text() == "hello world"
    assert src.exists()  # copy leaves the source


def test_copy_directory_tree(fs, tmp_path):
    root = tmp_path / "src"
    write(str(root / "one.txt"), "1")
    write(str(root / "nested" / "two.txt"), "2")
    write(str(root / "nested" / "deep" / "three.txt"), "3")

    dst = tmp_path / "dst"
    copy_path(fs, str(root), fs, str(dst))

    assert read(str(dst / "one.txt")) == "1"
    assert read(str(dst / "nested" / "two.txt")) == "2"
    assert read(str(dst / "nested" / "deep" / "three.txt")) == "3"


def test_count_tree(fs, tmp_path):
    root = tmp_path / "src"
    write(str(root / "a"), "12345")      # 5 bytes
    write(str(root / "sub" / "b"), "678")  # 3 bytes
    files, total = count_tree(fs, str(root))
    assert files == 2
    assert total == 8


def test_move_same_fs_renames(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("data")
    dst = tmp_path / "moved" / "a.txt"
    move_path(fs, str(src), fs, str(dst))
    assert dst.read_text() == "data"
    assert not src.exists()


def test_move_directory(fs, tmp_path):
    root = tmp_path / "src"
    write(str(root / "x.txt"), "x")
    write(str(root / "y" / "z.txt"), "z")
    dst = tmp_path / "dst"
    move_path(fs, str(root), fs, str(dst))
    assert read(str(dst / "x.txt")) == "x"
    assert read(str(dst / "y" / "z.txt")) == "z"
    assert not root.exists()


def test_sync_new_files_both_directions(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    write(str(left / "only_left.txt"), "L")
    write(str(right / "only_right.txt"), "R")

    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)

    # After sync both files exist on both sides.
    assert read(str(left / "only_left.txt")) == "L"
    assert read(str(right / "only_left.txt")) == "L"
    assert read(str(left / "only_right.txt")) == "R"
    assert read(str(right / "only_right.txt")) == "R"


def test_sync_newer_wins(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    now = time.time()
    # Same relative file; left is newer and should overwrite right.
    write(str(left / "shared.txt"), "new", mtime=now)
    write(str(right / "shared.txt"), "old", mtime=now - 1000)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    assert len(plan.actions) == 1
    assert plan.actions[0].direction == "->"
    execute_sync_plan(plan, fs, fs)

    assert read(str(right / "shared.txt")) == "new"
    assert read(str(left / "shared.txt")) == "new"


def test_sync_older_side_updated_from_newer(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    now = time.time()
    write(str(left / "shared.txt"), "stale", mtime=now - 1000)
    write(str(right / "shared.txt"), "fresh", mtime=now)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    assert plan.actions[0].direction == "<-"
    execute_sync_plan(plan, fs, fs)
    assert read(str(left / "shared.txt")) == "fresh"


def test_sync_preserves_mtime(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    src_mtime = time.time() - 5000
    write(str(left / "only_left.txt"), "L", mtime=src_mtime)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)

    copied = right / "only_left.txt"
    assert copied.exists()
    # The copied file must carry the source's timestamp, not "now".
    assert abs(copied.stat().st_mtime - src_mtime) < 1.0


def test_sync_is_idempotent(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    old = time.time() - 3000
    write(str(left / "a.txt"), "A", mtime=old)
    write(str(right / "b.txt"), "B", mtime=old)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)

    # A second run finds nothing to do because timestamps were preserved.
    plan2 = build_sync_plan(fs, str(left), fs, str(right))
    assert not plan2, [a.render() for a in plan2.actions]


def test_copy_preserve_mtime_flag(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("data")
    old = time.time() - 8000
    os.utime(str(src), (old, old))
    dst = tmp_path / "out" / "a.txt"
    copy_path(fs, str(src), fs, str(dst), preserve_mtime=True)
    assert abs(dst.stat().st_mtime - old) < 1.0


def test_sync_in_sync_is_noop(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    now = time.time()
    write(str(left / "same.txt"), "identical", mtime=now)
    write(str(right / "same.txt"), "identical", mtime=now)
    plan = build_sync_plan(fs, str(left), fs, str(right))
    assert not plan


def test_sync_nested_directories(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    write(str(left / "a" / "b" / "c.txt"), "deep")
    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)
    assert read(str(right / "a" / "b" / "c.txt")) == "deep"


def test_panel_lists_and_navigates(fs, tmp_path):
    write(str(tmp_path / "dir" / "file.txt"), "hi")
    os.makedirs(str(tmp_path / "dir" / "child"), exist_ok=True)
    panel = Panel(fs, str(tmp_path / "dir"))
    names = [e.name for e in panel.entries]
    # Parent entry plus the directory (sorted first) and the file.
    assert ".." in names
    assert "child" in names
    assert "file.txt" in names
    # Directories sort before files.
    non_parent = [n for n in names if n != ".."]
    assert non_parent.index("child") < non_parent.index("file.txt")


def test_panel_hidden_toggle(fs, tmp_path):
    d = tmp_path / "d"
    write(str(d / "visible.txt"), "v")
    write(str(d / ".secret"), "s")
    panel = Panel(fs, str(d))
    # Shown by default.
    assert ".secret" in [e.name for e in panel.entries]
    panel.toggle_hidden()
    names = [e.name for e in panel.entries]
    assert ".secret" not in names
    assert "visible.txt" in names
    panel.toggle_hidden()
    assert ".secret" in [e.name for e in panel.entries]


def test_ftp_list_parser_unix():
    # Servers that answer MLSD with "500 Unknown command" fall back to LIST;
    # verify the ls -l parser handles the common shapes.
    parse = FTPFileSystem._parse_list_line

    d = parse("drwxr-xr-x   2 owner group     4096 Jul 20 12:00 my dir")
    assert d is not None and d.is_dir and d.name == "my dir" and d.size == 4096

    f = parse("-rw-r--r--   1 owner group      842 Jul 22  2024 file.txt")
    assert f is not None and not f.is_dir and f.name == "file.txt" and f.size == 842

    link = parse("lrwxrwxrwx   1 o g    7 Jul 22 12:00 link -> /tmp/target")
    assert link is not None and link.is_symlink and link.name == "link"

    # ACL marker, and junk lines are ignored.
    assert parse("-rw-r--r--+  1 o g  100 Jan 05 09:15 acl") is not None
    assert parse("total 48") is None
    assert parse("") is None


def test_ftp_list_parser_dos():
    parse = FTPFileSystem._parse_list_line
    d = parse("07-22-25  09:15AM       <DIR>          images")
    assert d is not None and d.is_dir and d.name == "images"

    f = parse("07-22-2025  09:15AM              1024 report.pdf")
    assert f is not None and not f.is_dir and f.name == "report.pdf" and f.size == 1024


def test_panel_selection(fs, tmp_path):
    write(str(tmp_path / "d" / "one"), "1")
    write(str(tmp_path / "d" / "two"), "2")
    panel = Panel(fs, str(tmp_path / "d"))
    panel.select_all()
    assert "one" in panel.selected and "two" in panel.selected
    panel.clear_selection()
    assert not panel.selected
