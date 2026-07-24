"""The filesystem interface and the local backend."""

from __future__ import annotations

import os

import pytest

from meridian_commander.filesystems import (
    DirEntry,
    FileSystem,
    LocalFileSystem,
    local_fs,
)

from support import read, write


# -- the shared interface ------------------------------------------------------

class _Memory(FileSystem):
    """A minimal in-memory backend, to exercise the base class defaults."""

    scheme = "memory"

    def __init__(self):
        self.dirs = {"/"}
        self.files: dict[str, bytes] = {}

    def label(self):
        return "memory"

    def home(self):
        return "/"

    def listdir(self, path):
        prefix = path.rstrip("/") + "/"
        names = set()
        for known in list(self.dirs) + list(self.files):
            if known.startswith(prefix) and known != prefix:
                names.add(known[len(prefix):].split("/")[0])
        return [self.stat(prefix + n) for n in sorted(names)]

    def stat(self, path):
        path = path.rstrip("/") or "/"
        if path in self.dirs:
            return DirEntry(name=path.rsplit("/", 1)[-1], is_dir=True)
        if path in self.files:
            return DirEntry(name=path.rsplit("/", 1)[-1], is_dir=False,
                            size=len(self.files[path]))
        raise FileNotFoundError(path)

    def open_read(self, path):
        raise NotImplementedError

    def open_write(self, path):
        raise NotImplementedError

    def mkdir(self, path):
        self.dirs.add(path.rstrip("/"))

    def remove(self, path):
        del self.files[path]

    def rmdir(self, path):
        self.dirs.discard(path.rstrip("/"))

    def rename(self, src, dst):
        self.files[dst] = self.files.pop(src)


def test_path_helpers_are_posix_by_default():
    fs = _Memory()
    assert fs.sep == "/"
    assert fs.join("/a", "b", "c") == "/a/b/c"
    assert fs.dirname("/a/b/c") == "/a/b"
    assert fs.basename("/a/b/c") == "c"
    assert fs.normpath("/a/./b/../c") == "/a/c"
    assert fs.parent("/a/b") == "/a"
    # The root is its own parent, so navigation stops there.
    assert fs.parent("/") == "/"


def test_same_fs_compares_identity():
    one, two = _Memory(), _Memory()
    assert one.same_fs(one) is True
    # Two connections to the same kind of server are still two connections.
    assert one.same_fs(two) is False


def test_is_dir_and_exists_answer_false_rather_than_raising():
    fs = _Memory()
    fs.mkdir("/data")
    assert fs.is_dir("/data") is True
    assert fs.exists("/data") is True
    assert fs.is_dir("/nowhere") is False
    assert fs.exists("/nowhere") is False


def test_makedirs_creates_missing_parents():
    fs = _Memory()
    fs.makedirs("/a/b/c")
    assert fs.exists("/a") and fs.exists("/a/b") and fs.exists("/a/b/c")


def test_makedirs_of_an_existing_or_empty_path_does_nothing():
    fs = _Memory()
    fs.makedirs("/a")
    before = set(fs.dirs)
    fs.makedirs("/a")
    fs.makedirs("")
    assert fs.dirs == before


def test_delete_tree_removes_a_whole_directory():
    fs = _Memory()
    fs.makedirs("/a/b")
    fs.files["/a/b/f.txt"] = b"x"
    fs.files["/a/g.txt"] = b"y"
    fs.delete_tree("/a")
    assert not fs.exists("/a")
    assert fs.files == {}


def test_delete_tree_of_a_single_file():
    fs = _Memory()
    fs.files["/f.txt"] = b"x"
    fs.delete_tree("/f.txt")
    assert fs.files == {}


def test_delete_tree_of_something_that_is_not_there_is_quiet():
    fs = _Memory()
    fs.delete_tree("/nowhere")          # must not raise


def test_delete_tree_removes_a_symlink_without_following_it():
    fs = _Memory()
    fs.dirs.add("/link")

    def as_link(path):
        entry = DirEntry(name="link", is_dir=True, is_symlink=True)
        return entry if path.rstrip("/") == "/link" else _Memory.stat(fs, path)

    fs.stat = as_link
    removed = []
    fs.remove = removed.append
    fs.delete_tree("/link")
    # Removed as a link rather than descended into.
    assert removed == ["/link"]


def test_utime_is_a_no_op_by_default():
    assert _Memory().utime("/a", 123.0) is None


def test_close_is_a_no_op_by_default():
    assert _Memory().close() is None


def test_dir_entry_knows_it_is_a_file():
    assert DirEntry(name="a", is_dir=False).is_file is True
    assert DirEntry(name="d", is_dir=True).is_file is False


# -- the local backend ---------------------------------------------------------

def test_local_identity(fs):
    assert fs.label() == "local"
    assert fs.scheme == "local"
    assert local_fs().scheme == "local"


def test_local_path_helpers_use_the_host_conventions(fs, tmp_path):
    assert fs.sep == os.sep
    assert fs.join(str(tmp_path), "a", "b") == os.path.join(str(tmp_path), "a", "b")
    assert fs.dirname("/a/b/c") == "/a/b"
    assert fs.basename("/a/b/c") == "c"
    assert fs.normpath("/a/./b/../c") == "/a/c"
    assert fs.parent("/a/b") == "/a"


def test_local_home_follows_the_environment(fs, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert fs.home() == str(tmp_path)


def test_local_listing_reports_sizes_types_and_times(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "hello", mtime=1000)
    (tmp_path / "sub").mkdir()
    by_name = {e.name: e for e in fs.listdir(str(tmp_path))}
    assert by_name["a.txt"].size == 5
    assert by_name["a.txt"].mtime == 1000
    assert by_name["a.txt"].is_dir is False
    assert by_name["sub"].is_dir is True
    assert by_name["a.txt"].mode is not None


def test_local_listing_follows_a_directory_symlink(fs, tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    by_name = {e.name: e for e in fs.listdir(str(tmp_path))}
    # Shown as a directory (so entering works) but flagged as a link.
    assert by_name["link"].is_dir is True
    assert by_name["link"].is_symlink is True


def test_local_listing_survives_a_broken_symlink(fs, tmp_path):
    (tmp_path / "dangling").symlink_to(tmp_path / "never-existed")
    by_name = {e.name: e for e in fs.listdir(str(tmp_path))}
    assert by_name["dangling"].is_symlink is True
    assert by_name["dangling"].is_dir is False


def test_local_stat(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "hello")
    entry = fs.stat(str(tmp_path / "a.txt"))
    assert entry.name == "a.txt"
    assert entry.size == 5
    assert entry.is_dir is False
    assert fs.stat(str(tmp_path)).is_dir is True


def test_local_stat_of_a_missing_path_raises(fs, tmp_path):
    with pytest.raises(OSError):
        fs.stat(str(tmp_path / "absent"))


def test_local_read_and_write(fs, tmp_path):
    path = str(tmp_path / "a.bin")
    writer = fs.open_write(path)
    writer.write(b"payload")
    writer.close()
    reader = fs.open_read(path)
    assert reader.read(100) == b"payload"
    reader.close()


def test_local_utime(fs, tmp_path):
    path = str(tmp_path / "a.txt")
    write(path, "x")
    fs.utime(path, 123456.0)
    assert fs.stat(path).mtime == pytest.approx(123456.0)


def test_local_mkdir_and_makedirs(fs, tmp_path):
    fs.mkdir(str(tmp_path / "one"))
    assert (tmp_path / "one").is_dir()
    fs.makedirs(str(tmp_path / "a" / "b" / "c"))
    assert (tmp_path / "a" / "b" / "c").is_dir()
    # makedirs over an existing tree is not an error.
    fs.makedirs(str(tmp_path / "a" / "b" / "c"))


def test_local_remove_and_rmdir(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "x")
    fs.remove(str(tmp_path / "a.txt"))
    assert not (tmp_path / "a.txt").exists()
    (tmp_path / "empty").mkdir()
    fs.rmdir(str(tmp_path / "empty"))
    assert not (tmp_path / "empty").exists()


def test_local_delete_tree(fs, tmp_path):
    write(str(tmp_path / "tree" / "deep" / "f.txt"), "x")
    fs.delete_tree(str(tmp_path / "tree"))
    assert not (tmp_path / "tree").exists()


def test_local_delete_tree_of_a_file(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "x")
    fs.delete_tree(str(tmp_path / "a.txt"))
    assert not (tmp_path / "a.txt").exists()


def test_local_delete_tree_removes_a_link_not_its_target(fs, tmp_path):
    (tmp_path / "real").mkdir()
    write(str(tmp_path / "real" / "keep.txt"), "important")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    fs.delete_tree(str(tmp_path / "link"))
    assert not (tmp_path / "link").exists()
    # The target survives: deleting a link must not delete what it points at.
    assert read(str(tmp_path / "real" / "keep.txt")) == "important"


def test_local_rename(fs, tmp_path):
    write(str(tmp_path / "a.txt"), "payload")
    fs.rename(str(tmp_path / "a.txt"), str(tmp_path / "b.txt"))
    assert read(str(tmp_path / "b.txt")) == "payload"


def test_two_local_backends_are_not_the_same_connection():
    assert LocalFileSystem().same_fs(LocalFileSystem()) is False
