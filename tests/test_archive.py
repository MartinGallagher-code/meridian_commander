"""Browsing zip and tar archives as read-only filesystems.

Every fixture is a real archive written by the standard library, so what is
under test is the format and the backend, not a stand-in for either.
"""

from __future__ import annotations

import curses
import zipfile

import pytest

from meridian_commander import archive
from meridian_commander.archive import ArchiveFileSystem, is_archive, open_archive
from meridian_commander.filesystems import FileSystemError, LocalFileSystem
from meridian_commander.operations import copy_path

from support import (
    _NoLocalPath,
    write,
    write_tar,
    write_zip,
)


@pytest.fixture
def bundle(tmp_path):
    """A zip with nested content, an implied directory and an empty one."""
    path = write_zip(str(tmp_path / "bundle.zip"), {
        "readme.txt": "hello from the archive\n",
        "src/deep/nested/mod.py": "print('hi')\n",   # no directory entries
        "src/top.py": "x = 1\n",
        "empty/": "",
    })
    return ArchiveFileSystem(LocalFileSystem(), path)


@pytest.fixture
def tarball(tmp_path):
    path = write_tar(str(tmp_path / "bundle.tar.gz"),
                     {"docs/guide.md": "# Guide\n"},
                     mode="w:gz", symlinks={"docs/link.md": "guide.md"})
    return ArchiveFileSystem(LocalFileSystem(), path)


# -- names ---------------------------------------------------------------------
@pytest.mark.parametrize("name, expected", [
    ("bundle.zip", True), ("PACKAGE.ZIP", True), ("lib.jar", True),
    ("dist.whl", True), ("thing.egg", True),
    ("bundle.tar", True), ("bundle.tar.gz", True), ("bundle.tgz", True),
    ("bundle.tar.bz2", True), ("bundle.tbz", True), ("bundle.tar.xz", True),
    ("notes.txt", False), ("book.xlsx", False), ("archive", False),
    ("file.gz", False),          # a bare .gz is one compressed file, not a tree
])
def test_is_archive(name, expected):
    assert is_archive(name) is expected


@pytest.mark.parametrize("name, scheme", [
    ("a.zip", "zip"), ("a.jar", "zip"), ("a.tar", "tar"), ("a.tar.gz", "tar"),
])
def test_the_kind_is_taken_from_the_name(tmp_path, name, scheme):
    if scheme == "zip":
        path = write_zip(str(tmp_path / name), {"x": "y"})
    else:
        path = write_tar(str(tmp_path / name), {"x": "y"},
                         mode="w:gz" if name.endswith(".gz") else "w")
    assert ArchiveFileSystem(LocalFileSystem(), path).scheme == scheme


# -- member names --------------------------------------------------------------
@pytest.mark.parametrize("name, cleaned", [
    ("a.txt", "a.txt"),
    ("dir/a.txt", "dir/a.txt"),
    ("/leading.txt", "leading.txt"),
    ("./here.txt", "here.txt"),
    ("dir//double.txt", "dir/double.txt"),
    ("win\\style.txt", "win/style.txt"),
    ("dir/", "dir"),
    ("../escape.txt", None),
    ("dir/../../escape.txt", None),
    ("..", None),
    ("", None),
    ("/", None),
])
def test_cleaning_a_member_name(name, cleaned):
    assert archive.clean_member(name) == cleaned


def test_a_member_that_would_escape_is_refused_and_counted(tmp_path):
    path = write_zip(str(tmp_path / "evil.zip"), {
        "ok.txt": "fine",
        "../escape.txt": "not fine",
        "a/../../escape2.txt": "also not fine",
    })
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert [e.name for e in fs.listdir("/")] == ["ok.txt"]
    assert fs.skipped == 2
    assert "2 unsafe member(s) skipped" in fs.label()


def test_an_untidy_but_harmless_name_is_kept(tmp_path):
    path = write_zip(str(tmp_path / "untidy.zip"), {"/absolute.txt": "x"})
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert [e.name for e in fs.listdir("/")] == ["absolute.txt"]
    assert fs.skipped == 0


# -- the tree ------------------------------------------------------------------
def test_the_root_lists_what_the_archive_holds(bundle):
    assert sorted(e.name for e in bundle.listdir("/")) == \
        ["empty", "readme.txt", "src"]


def test_directories_nobody_wrote_down_are_created(bundle):
    # The archive holds "src/deep/nested/mod.py" and no directory entries.
    assert sorted(e.name for e in bundle.listdir("/src")) == ["deep", "top.py"]
    assert [e.name for e in bundle.listdir("/src/deep")] == ["nested"]
    assert [e.name for e in bundle.listdir("/src/deep/nested")] == ["mod.py"]


def test_an_explicit_empty_directory_survives(bundle):
    assert bundle.listdir("/empty") == []
    assert bundle.stat("/empty").is_dir is True


def test_sizes_and_kinds(bundle):
    entry = bundle.stat("/readme.txt")
    assert entry.is_dir is False
    assert entry.size == len("hello from the archive\n")
    assert bundle.stat("/src").is_dir is True


def test_the_root_stats_as_a_directory(bundle):
    assert bundle.stat("/").is_dir is True


def test_paths_are_normalised(bundle):
    assert bundle.normpath("src") == "/src"
    assert bundle.normpath("/src/") == "/src"
    assert bundle.normpath("/src/deep/..") == "/src"
    assert bundle.normpath("") == "/"


def test_home_is_the_archive_root(bundle):
    assert bundle.home() == "/"


def test_listing_something_that_is_not_there(bundle):
    with pytest.raises(FileSystemError, match="no such directory"):
        bundle.listdir("/nowhere")


def test_stating_something_that_is_not_there(bundle):
    with pytest.raises(FileSystemError, match="no such entry"):
        bundle.stat("/nowhere.txt")
    with pytest.raises(FileSystemError, match="no such entry"):
        bundle.stat("/nowhere/deep.txt")


# -- timestamps ----------------------------------------------------------------
@pytest.mark.parametrize("date_time, expected", [
    ((1980, 0, 0, 0, 0, 0), None),        # a writer that left it zero
    ((2020, 0, 5, 0, 0, 0), None),
    ((2020, 5, 0, 0, 0, 0), None),
])
def test_a_zip_entry_with_no_real_timestamp(date_time, expected):
    assert archive.zip_mtime(date_time) is expected


def test_a_zip_entry_with_a_real_timestamp():
    assert archive.zip_mtime((2023, 3, 15, 12, 30, 0)) > 0


def test_a_tar_keeps_its_timestamps(tarball):
    assert tarball.stat("/docs/guide.md").mtime == 1700000000


# -- tar specifics -------------------------------------------------------------
def test_a_compressed_tar_is_read_transparently(tarball):
    assert tarball.open_read("/docs/guide.md").read() == b"# Guide\n"


def test_a_symlink_in_a_tar_is_flagged(tarball):
    assert tarball.stat("/docs/link.md").is_symlink is True


def test_a_symlink_reads_through_to_its_target(tarball):
    # tarfile follows a link whose target is in the same archive, which is
    # what the user would expect from opening it.
    assert tarball.open_read("/docs/link.md").read() == b"# Guide\n"


def test_a_symlink_pointing_outside_the_archive(tmp_path):
    path = write_tar(str(tmp_path / "b.tar"), {"real": "x"},
                     symlinks={"dangling": "nowhere"})
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    with pytest.raises(FileSystemError, match="cannot read"):
        fs.open_read("/dangling")


def test_an_entry_with_no_contents_of_its_own(tmp_path):
    path = write_tar(str(tmp_path / "b.tar"), {"real": "x"}, fifos=["pipe"])
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    with pytest.raises(FileSystemError, match="no contents to read"):
        fs.open_read("/pipe")


def test_an_unsafe_member_in_a_tar_is_refused_too(tmp_path):
    path = write_tar(str(tmp_path / "evil.tar"),
                     {"ok.txt": "fine", "../escape.txt": "not fine"})
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert [e.name for e in fs.listdir("/")] == ["ok.txt"]
    assert fs.skipped == 1


def test_the_member_cap_applies_to_tars_as_well(tmp_path, monkeypatch):
    path = write_tar(str(tmp_path / "many.tar"),
                     {f"f{i}.txt": "x" for i in range(10)})
    monkeypatch.setattr(archive, "MAX_MEMBERS", 3)
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert fs.truncated is True and len(fs.listdir("/")) == 3


def test_an_explicit_directory_entry_in_a_tar(tmp_path):
    path = write_tar(str(tmp_path / "d.tar"), {"top/file": "x"}, dirs=["top/"])
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert fs.stat("/top").is_dir is True
    assert [e.name for e in fs.listdir("/top")] == ["file"]


@pytest.mark.parametrize("mode, suffix", [
    ("w", ".tar"), ("w:gz", ".tar.gz"), ("w:bz2", ".tar.bz2"),
    ("w:xz", ".tar.xz"),
])
def test_every_tar_compression_opens(tmp_path, mode, suffix):
    path = write_tar(str(tmp_path / f"a{suffix}"), {"x.txt": "y"}, mode=mode)
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert fs.open_read("/x.txt").read() == b"y"


# -- reading -------------------------------------------------------------------
def test_reading_a_file_out_of_a_zip(bundle):
    assert bundle.open_read("/readme.txt").read() == b"hello from the archive\n"
    assert bundle.open_read("/src/deep/nested/mod.py").read() == b"print('hi')\n"


def test_reading_something_that_is_not_a_file(bundle):
    with pytest.raises(FileSystemError, match="no such file"):
        bundle.open_read("/src")
    with pytest.raises(FileSystemError, match="no such file"):
        bundle.open_read("/nowhere.txt")


def test_a_member_that_cannot_be_read(bundle, monkeypatch):
    # The directory still lists the member, but its bytes are damaged.
    def boom(name):
        raise zipfile.BadZipFile("Bad CRC-32")
    monkeypatch.setattr(bundle._zip, "open", boom)
    with pytest.raises(FileSystemError, match="cannot read"):
        bundle.open_read("/readme.txt")


# -- read-only -----------------------------------------------------------------
@pytest.mark.parametrize("call", [
    lambda fs: fs.open_write("/x"),
    lambda fs: fs.mkdir("/x"),
    lambda fs: fs.remove("/readme.txt"),
    lambda fs: fs.rmdir("/empty"),
    lambda fs: fs.rename("/readme.txt", "/other.txt"),
])
def test_every_way_of_changing_an_archive_is_refused(bundle, call):
    with pytest.raises(FileSystemError, match="read-only"):
        call(bundle)


# -- opening -------------------------------------------------------------------
def test_a_local_archive_is_opened_in_place(tmp_path, monkeypatch):
    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y"})

    def refuse(self, p):
        raise AssertionError("a local archive should not be read into memory")

    monkeypatch.setattr(LocalFileSystem, "open_read", refuse)
    assert ArchiveFileSystem(LocalFileSystem(), path).open_read("/x.txt").read() \
        == b"y"


def test_an_archive_with_no_local_path_is_streamed(tmp_path):
    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y"})
    fs = ArchiveFileSystem(_NoLocalPath(), path)
    assert fs.open_read("/x.txt").read() == b"y"


def test_a_streamed_archive_too_large_to_hold(tmp_path, monkeypatch):
    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y" * 500})
    monkeypatch.setattr(archive, "MAX_BYTES", 10)
    with pytest.raises(FileSystemError, match="larger than"):
        ArchiveFileSystem(_NoLocalPath(), path)


def test_a_file_that_is_not_an_archive(tmp_path):
    write(str(tmp_path / "fake.zip"), "definitely not a zip")
    with pytest.raises(FileSystemError, match="cannot open"):
        ArchiveFileSystem(LocalFileSystem(), str(tmp_path / "fake.zip"))


def test_a_tar_that_is_not_a_tar(tmp_path):
    write(str(tmp_path / "fake.tar"), "definitely not a tar")
    with pytest.raises(FileSystemError, match="cannot open"):
        ArchiveFileSystem(LocalFileSystem(), str(tmp_path / "fake.tar"))


def test_an_archive_that_is_not_there(tmp_path):
    with pytest.raises(Exception):
        ArchiveFileSystem(LocalFileSystem(), str(tmp_path / "missing.zip"))


def test_the_member_cap_is_reported(tmp_path, monkeypatch):
    path = write_zip(str(tmp_path / "many.zip"),
                     {f"f{i}.txt": "x" for i in range(10)})
    monkeypatch.setattr(archive, "MAX_MEMBERS", 4)
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert fs.truncated is True
    assert "(truncated)" in fs.label()
    assert len(fs.listdir("/")) == 4


def test_the_label_names_the_archive(bundle, tarball):
    assert bundle.label() == "zip:bundle.zip"
    assert tarball.label() == "tar:bundle.tar.gz"


def test_closing_is_safe_to_repeat(bundle):
    bundle.close()
    bundle.close()


def test_closing_survives_a_handle_that_will_not_close(bundle, monkeypatch):
    def boom():
        raise OSError("no")
    monkeypatch.setattr(bundle._zip, "close", boom)
    bundle.close()


def test_open_archive_returns_a_browsable_filesystem(tmp_path):
    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y"})
    fs = open_archive(LocalFileSystem(), path)
    assert isinstance(fs, ArchiveFileSystem)
    assert [e.name for e in fs.listdir(fs.home())] == ["x.txt"]


# -- through the application ---------------------------------------------------
def _point_at(panel, name):
    panel.cursor = next(i for i, e in enumerate(panel.entries) if e.name == name)


def test_entering_an_archive_from_a_pane(app, tmp_path):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"inside.txt": "content"})
    app.left.refresh()
    _point_at(app.left, "bundle.zip")
    app._activate_entry()
    assert isinstance(app.left.fs, ArchiveFileSystem)
    assert app.left.path == "/"
    assert [e.name for e in app.left.entries] == ["inside.txt"]
    assert "read-only" in app.message


def test_leaving_an_archive_returns_to_the_directory_holding_it(app, tmp_path):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"a/inside.txt": "x"})
    app.left.refresh()
    _point_at(app.left, "bundle.zip")
    app._activate_entry()
    app.left.enter()                       # into "a/"
    assert app.left.path == "/a"
    app.handle_key(curses.KEY_BACKSPACE)   # back to the archive root
    assert app.left.path == "/"
    app.handle_key(curses.KEY_BACKSPACE)   # and out of the archive entirely
    assert isinstance(app.left.fs, LocalFileSystem)
    assert app.left.path == str(tmp_path / "left")
    # The cursor lands back on the archive it just left.
    assert app.left.current().name == "bundle.zip"


def test_an_archive_that_will_not_open_is_reported(app, tmp_path):
    write(str(tmp_path / "left" / "broken.zip"), "not a zip at all")
    app.left.refresh()
    _point_at(app.left, "broken.zip")
    app._activate_entry()
    assert isinstance(app.left.fs, LocalFileSystem)
    assert "Cannot open archive" in app.message


def test_an_archive_that_opens_but_cannot_be_listed(app, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"x.txt": "y"})
    app.left.refresh()
    _point_at(app.left, "bundle.zip")

    def refuse(self, path):
        raise FileSystemError("listing refused")

    monkeypatch.setattr(ArchiveFileSystem, "listdir", refuse)
    app._activate_entry()
    assert isinstance(app.left.fs, LocalFileSystem)
    assert "Cannot read" in app.message


def test_going_up_from_an_ordinary_root_does_nothing(app, monkeypatch):
    monkeypatch.setattr(app.left, "path", "/")
    app.left.refresh()
    app.handle_key(curses.KEY_BACKSPACE)
    assert app.left.path == "/"


def test_copying_a_tree_out_of_an_archive(tmp_path):
    path = write_zip(str(tmp_path / "bundle.zip"), {
        "top/inside.txt": "copied out of the archive",
        "top/deep/also.txt": "and this one",
    })
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    out = tmp_path / "out"
    # The copy engine is untouched by this change: it sees one filesystem
    # interface either side and never learns that one of them is a zip.
    copy_path(fs, "/top", LocalFileSystem(), str(out))
    assert (out / "inside.txt").read_text() == "copied out of the archive"
    assert (out / "deep" / "also.txt").read_text() == "and this one"


def test_copying_into_an_archive_is_refused(bundle, tmp_path):
    write(str(tmp_path / "src.txt"), "payload")
    with pytest.raises(FileSystemError, match="read-only"):
        copy_path(LocalFileSystem(), str(tmp_path / "src.txt"),
                  bundle, "/src.txt")

