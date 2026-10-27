# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Browsing zip and tar archives as filesystems, and writing into them.

Every fixture is a real archive written by the standard library, so what is
under test is the format and the backend, not a stand-in for either -- and
what a write is checked against is the archive file on disk, opened afresh.
"""

from __future__ import annotations

import curses
import io
import os
import tarfile
import zipfile

import pytest

from meridian_commander import archive
from meridian_commander.archive import (
    ArchiveFileSystem,
    archive_stem,
    is_archive,
    open_archive,
)
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


@pytest.mark.parametrize("stored", ["./dot.txt", "/absolute.txt", ".//odd.txt"])
def test_an_untidy_name_reads_back_by_the_name_it_is_stored_under(tmp_path,
                                                                  stored):
    # The tidied path is what the member is *shown* at; the archive will only
    # open it by the name it was written with.  "tar czf out.tgz ." writes
    # every member as "./name", so a whole archive listed and nothing in it
    # could be read.
    zip_path = write_zip(str(tmp_path / "untidy.zip"), {stored: "body"})
    fs = ArchiveFileSystem(LocalFileSystem(), zip_path)
    shown = "/" + stored.replace("//", "/").lstrip("./").lstrip("/")
    assert fs.open_read(shown).read() == b"body"

    tar_path = write_tar(str(tmp_path / "untidy.tar"), {stored: "body"})
    tfs = ArchiveFileSystem(LocalFileSystem(), tar_path)
    assert tfs.open_read(shown).read() == b"body"


def test_a_tar_of_a_whole_directory_can_be_read(tmp_path):
    path = write_tar(str(tmp_path / "dot.tar.gz"),
                     {"./": "", "./a.txt": "one", "./sub/b.txt": "two"},
                     mode="w:gz")
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    assert sorted(e.name for e in fs.listdir("/")) == ["a.txt", "sub"]
    assert fs.open_read("/a.txt").read() == b"one"
    assert fs.open_read("/sub/b.txt").read() == b"two"


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


# -- what is still refused ----------------------------------------------------
@pytest.mark.parametrize("call", [
    lambda fs: fs.remove("/readme.txt"),
    lambda fs: fs.rmdir("/empty"),
    lambda fs: fs.rename("/readme.txt", "/other.txt"),
])
def test_deleting_or_renaming_inside_an_archive_is_refused(bundle, call):
    with pytest.raises(FileSystemError, match="can be added to it, but not"):
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
    with pytest.raises(FileSystemError, match="cannot open"):
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
    assert "F5/F6 from the other pane add to it" in app.message


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


def test_leaving_an_archive_the_other_pane_is_still_in_keeps_it_open(
        app, tmp_path):
    """'=' points both panes at one filesystem object.

    Closing it when the first pane left took the archive out from under the
    other one, which went on listing it while every file in it failed to open.
    """
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"a.txt": "inside"})
    app.left.refresh()
    _point_at(app.left, "bundle.zip")
    app._activate_entry()
    shared = app.left.fs
    app._mirror_to_other_pane()
    assert app.right.fs is shared

    app._leave_archive()

    assert isinstance(app.left.fs, LocalFileSystem)
    assert app.right.fs is shared
    assert app.right.fs.open_read("/a.txt").read() == b"inside"
    # And it is still registered, so it closes with everything else.
    assert shared in app._backends


def test_the_last_pane_out_closes_the_archive(app, tmp_path):
    write_zip(str(tmp_path / "left" / "bundle.zip"), {"a.txt": "inside"})
    app.left.refresh()
    _point_at(app.left, "bundle.zip")
    app._activate_entry()
    shared = app.left.fs
    app._mirror_to_other_pane()

    app._leave_archive()                    # the left pane goes
    app.active = app.right
    app._leave_archive()                    # and now the right one

    assert shared not in app._backends
    with pytest.raises(FileSystemError, match="has been closed"):
        shared.open_read("/a.txt")


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


# -- writing into an archive --------------------------------------------------
def _zip_members(path):
    with zipfile.ZipFile(path) as zf:
        return {i.filename: zf.read(i) for i in zf.infolist()}


def _tar_members(path):
    with tarfile.open(path) as tf:
        return {m.name: m for m in tf.getmembers()}


@pytest.fixture
def rewrites(monkeypatch):
    """Count how many times an archive is written out."""
    calls = []
    real = ArchiveFileSystem._rewrite

    def counting(self, built):
        calls.append(self.name)
        return real(self, built)

    monkeypatch.setattr(ArchiveFileSystem, "_rewrite", counting)
    return calls


def test_a_file_copied_into_a_zip_is_in_the_file_on_disk(bundle, tmp_path):
    write(str(tmp_path / "src.txt"), "payload")
    copy_path(LocalFileSystem(), str(tmp_path / "src.txt"), bundle,
              "/src/new.txt")
    members = _zip_members(bundle.archive_path)
    assert members["src/new.txt"] == b"payload"
    # Everything that was there before is still there.
    assert members["readme.txt"] == b"hello from the archive\n"
    assert members["src/deep/nested/mod.py"] == b"print('hi')\n"
    assert "empty/" in members
    # And the pane's own view is the archive as reread, not a guess at it.
    assert bundle.open_read("/src/new.txt").read() == b"payload"
    fresh = ArchiveFileSystem(LocalFileSystem(), bundle.archive_path)
    assert fresh.stat("/src/new.txt").size == len("payload")


@pytest.mark.parametrize("suffix, mode", [
    (".tar", "r:"), (".tar.gz", "r:gz"), (".tgz", "r:gz"),
    (".tar.bz2", "r:bz2"), (".tbz2", "r:bz2"), (".tar.xz", "r:xz"),
])
def test_a_tar_is_written_back_compressed_as_it_was(tmp_path, suffix, mode):
    path = write_tar(str(tmp_path / f"a{suffix}"), {"old.txt": "old"},
                     mode="w" + mode[1:])
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    write(str(tmp_path / "new.txt"), "new")
    copy_path(LocalFileSystem(), str(tmp_path / "new.txt"), fs, "/new.txt")
    with tarfile.open(path, mode) as tf:
        assert sorted(tf.getnames()) == ["new.txt", "old.txt"]
        assert tf.extractfile("new.txt").read() == b"new"
        assert tf.extractfile("old.txt").read() == b"old"


def test_a_tar_compression_python_cannot_write_is_reported(tmp_path,
                                                           monkeypatch):
    path = write_tar(str(tmp_path / "a.tar.gz"), {"old.txt": "old"},
                     mode="w:gz")
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    monkeypatch.setattr(archive, "TAR_WRITE_MODES",
                        (((".tar.gz",), "w:nonesuch"),))
    with pytest.raises(FileSystemError, match="cannot write a.tar.gz"):
        fs.touch("/new.txt")
    assert sorted(_tar_members(path)) == ["old.txt"]


def test_a_copy_in_a_batch_rewrites_the_archive_once(bundle, tmp_path,
                                                    rewrites):
    for name in ("a", "b", "c"):
        write(str(tmp_path / "tree" / "sub" / name), name)
    with bundle.batch():
        copy_path(LocalFileSystem(), str(tmp_path / "tree"), bundle, "/tree")
        # Listed already -- the copy has to see the directories it made --
        # but nothing has been written to the archive yet.
        assert sorted(e.name for e in bundle.listdir("/tree/sub")) == \
            ["a", "b", "c"]
        assert "tree/sub/a" not in _zip_members(bundle.archive_path)
        assert rewrites == []
    assert rewrites == ["bundle.zip"]
    members = _zip_members(bundle.archive_path)
    assert [members[f"tree/sub/{n}"] for n in "abc"] == [b"a", b"b", b"c"]


def test_outside_a_batch_each_change_is_written_at_once(bundle, tmp_path,
                                                       rewrites):
    bundle.mkdir("/made")
    assert "made/" in _zip_members(bundle.archive_path)
    bundle.touch("/made/new.txt")
    assert _zip_members(bundle.archive_path)["made/new.txt"] == b""
    assert rewrites == ["bundle.zip", "bundle.zip"]


def test_batches_nest_and_the_outermost_writes(bundle, rewrites):
    with bundle.batch():
        with bundle.batch():
            bundle.mkdir("/inner")
        assert rewrites == []
    assert rewrites == ["bundle.zip"]


def test_a_batch_with_nothing_in_it_writes_nothing(bundle, rewrites):
    with bundle.batch():
        pass
    assert rewrites == []


def test_a_batch_left_by_an_exception_still_writes_what_it_had(bundle):
    with pytest.raises(RuntimeError):
        with bundle.batch():
            bundle.mkdir("/kept")
            raise RuntimeError("the copy failed half way")
    assert "kept/" in _zip_members(bundle.archive_path)


def test_a_writer_closed_twice_writes_once(bundle, rewrites):
    writer = bundle.open_write("/twice.txt")
    writer.write(b"once")
    writer.close()
    writer.close()
    assert rewrites == ["bundle.zip"]


def test_a_member_written_over_is_replaced_not_duplicated(bundle, tmp_path):
    write(str(tmp_path / "readme.txt"), "a new readme")
    copy_path(LocalFileSystem(), str(tmp_path / "readme.txt"), bundle,
              "/readme.txt")
    with zipfile.ZipFile(bundle.archive_path) as zf:
        assert [i.filename for i in zf.infolist()].count("readme.txt") == 1
        assert zf.read("readme.txt") == b"a new readme"


def test_a_member_stored_untidily_is_replaced_too(tmp_path):
    # "tar czf out.tgz ." stores "./name"; writing "/name" is the same file.
    path = write_tar(str(tmp_path / "dot.tar"), {"./a.txt": "old"})
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    writer = fs.open_write("/a.txt")
    writer.write(b"new")
    writer.close()
    assert list(_tar_members(path)) == ["a.txt"]
    assert fs.open_read("/a.txt").read() == b"new"


def test_written_twice_in_one_batch_the_last_one_wins(bundle):
    with bundle.batch():
        for body in (b"first", b"second"):
            writer = bundle.open_write("/again.txt")
            writer.write(body)
            writer.close()
        # The first copy is thrown away as soon as the second replaces it.
        assert len(os.listdir(bundle._stage_dir)) == 1
    assert _zip_members(bundle.archive_path)["again.txt"] == b"second"


def test_an_empty_directory_copied_in_survives(tmp_path):
    (tmp_path / "src" / "hollow").mkdir(parents=True)
    zip_path = write_zip(str(tmp_path / "a.zip"), {"x": "y"})
    tar_path = write_tar(str(tmp_path / "a.tar"), {"x": "y"})
    for path in (zip_path, tar_path):
        fs = ArchiveFileSystem(LocalFileSystem(), path)
        copy_path(LocalFileSystem(), str(tmp_path / "src"), fs, "/src")
        fresh = ArchiveFileSystem(LocalFileSystem(), path)
        assert fresh.listdir("/src/hollow") == []
    assert _tar_members(tar_path)["src/hollow"].isdir()
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.getinfo("src/hollow/").is_dir()


@pytest.mark.parametrize("call, problem", [
    (lambda fs: fs.mkdir("/readme.txt"), "already exists"),
    (lambda fs: fs.mkdir("/src"), "already exists"),
    (lambda fs: fs.mkdir("/nowhere/new"), "no such directory"),
    (lambda fs: fs.mkdir("/readme.txt/new"), "no such directory"),
    (lambda fs: fs.open_write("/src"), "is a directory"),
    (lambda fs: fs.open_write("/"), "is a directory"),
    (lambda fs: fs.open_write("/nowhere/new.txt"), "no such directory"),
])
def test_a_write_that_has_nowhere_to_go_is_refused(bundle, call, problem):
    with pytest.raises(FileSystemError, match=problem):
        call(bundle)


def test_members_it_does_not_show_are_carried_across(tmp_path):
    """A rewrite must not be a way of losing part of an archive."""
    zip_path = write_zip(str(tmp_path / "evil.zip"),
                         {"ok.txt": "fine", "../escape.txt": "not shown"})
    with zipfile.ZipFile(zip_path, "a") as zf:
        zf.comment = b"kept"
    fs = ArchiveFileSystem(LocalFileSystem(), zip_path)
    fs.touch("/new.txt")
    assert _zip_members(zip_path)["../escape.txt"] == b"not shown"
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.comment == b"kept"

    tar_path = write_tar(str(tmp_path / "odd.tar"),
                         {"../escape.txt": "hidden"},
                         symlinks={"link": "/etc/hostname"}, fifos=["pipe"],
                         dirs=["dir/"])
    fs = ArchiveFileSystem(LocalFileSystem(), tar_path)
    fs.touch("/new.txt")
    members = _tar_members(tar_path)
    assert members["link"].issym()
    assert members["link"].linkname == "/etc/hostname"
    assert members["pipe"].isfifo()
    assert members["dir"].isdir()
    with tarfile.open(tar_path) as tf:
        assert tf.extractfile("../escape.txt").read() == b"hidden"


def test_a_mode_and_a_date_inside_a_tar_are_kept(tmp_path):
    path = str(tmp_path / "m.tar")
    with tarfile.open(path, "w") as tf:
        info = tarfile.TarInfo("run.sh")
        info.size, info.mode, info.mtime = 2, 0o755, 1600000000
        tf.addfile(info, io.BytesIO(b"#!"))
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    fs.touch("/other")
    kept = _tar_members(path)["run.sh"]
    assert (kept.mode, kept.mtime) == (0o755, 1600000000)


def test_the_archive_keeps_its_permissions(bundle):
    os.chmod(bundle.archive_path, 0o640)
    bundle.touch("/new.txt")
    assert os.stat(bundle.archive_path).st_mode & 0o777 == 0o640


def test_an_archive_reached_through_a_symlink_stays_a_symlink(tmp_path):
    real = write_zip(str(tmp_path / "real" / "a.zip"), {"x": "y"})
    link = tmp_path / "link.zip"
    link.symlink_to(real)
    fs = ArchiveFileSystem(LocalFileSystem(), str(link))
    fs.touch("/new.txt")
    assert link.is_symlink()
    assert "new.txt" in _zip_members(real)


def test_nothing_is_left_behind_beside_the_archive(bundle):
    bundle.touch("/new.txt")
    assert os.listdir(os.path.dirname(bundle.archive_path)) == ["bundle.zip"]


def test_an_archive_marked_read_only_is_not_replaced(bundle, monkeypatch):
    before = _zip_members(bundle.archive_path)
    monkeypatch.setattr(archive.os, "access", lambda path, how: False)
    with pytest.raises(FileSystemError,
                       match="cannot write bundle.zip: permission denied"):
        bundle.touch("/new.txt")
    assert _zip_members(bundle.archive_path) == before
    # The tree is what the archive holds again, and the stage is gone.
    assert "new.txt" not in [e.name for e in bundle.listdir("/")]
    assert bundle._stage_dir is None


def test_a_rewrite_that_fails_leaves_the_original_alone(bundle, tmp_path,
                                                       monkeypatch):
    before = _zip_members(bundle.archive_path)

    def broken(self, source, built):
        with open(built, "wb") as half:
            half.write(b"PK half an archive")
        raise OSError("disk full")

    monkeypatch.setattr(ArchiveFileSystem, "_rewrite_zip", broken)
    with pytest.raises(FileSystemError,
                       match="cannot write bundle.zip: disk full"):
        with bundle.batch():
            copy_path(LocalFileSystem(), __file__, bundle, "/copied.py")
    assert _zip_members(bundle.archive_path) == before
    assert os.listdir(os.path.dirname(bundle.archive_path)) == ["bundle.zip"]
    assert "copied.py" not in [e.name for e in bundle.listdir("/")]
    # And it still works: the next write goes in as usual.
    monkeypatch.undo()
    bundle.touch("/after.txt")
    assert "after.txt" in _zip_members(bundle.archive_path)


def test_a_failure_to_even_start_the_rewrite_is_reported(bundle, monkeypatch):
    def refuse(**kwargs):
        raise PermissionError("directory is read-only")

    with pytest.raises(FileSystemError,
                       match="cannot write bundle.zip: directory is read-only"):
        with bundle.batch():
            bundle.touch("/new.txt")
            # Only the rewrite is refused its file; staging already had one.
            monkeypatch.setattr(archive.tempfile, "mkstemp", refuse)
    assert "new.txt" not in [e.name for e in bundle.listdir("/")]


def test_touching_a_file_restamps_its_member(tmp_path):
    zip_path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "body"})
    tar_path = write_tar(str(tmp_path / "a.tar"), {"x.txt": "body"})
    stamp = 1_500_000_000.0
    for path in (zip_path, tar_path):
        fs = ArchiveFileSystem(LocalFileSystem(), path)
        fs.utime("/x.txt", stamp)
        fresh = ArchiveFileSystem(LocalFileSystem(), path)
        # A zip date has two-second resolution and no time zone.
        assert abs(fresh.stat("/x.txt").mtime - stamp) <= 2
        assert fresh.open_read("/x.txt").read() == b"body"


def test_a_copy_that_keeps_dates_keeps_them_inside_a_batch(tmp_path):
    path = write_tar(str(tmp_path / "a.tar"), {"x": "y"})
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    write(str(tmp_path / "dated.txt"), "old news", mtime=1_400_000_000)
    with fs.batch():
        copy_path(LocalFileSystem(), str(tmp_path / "dated.txt"), fs,
                  "/dated.txt", preserve_mtime=True)
        assert fs.stat("/dated.txt").mtime == 1_400_000_000
    assert _tar_members(path)["dated.txt"].mtime == 1_400_000_000


def test_restamping_a_directory_writes_nothing(bundle, rewrites):
    bundle.utime("/src", 1_500_000_000.0)
    bundle.utime("/nowhere", 1_500_000_000.0)
    assert rewrites == []


def test_a_closed_archive_takes_no_writes(bundle):
    bundle.close()
    for call in (lambda: bundle.open_write("/x"), lambda: bundle.mkdir("/x"),
                 lambda: bundle.utime("/readme.txt", 0.0)):
        with pytest.raises(FileSystemError, match="has been closed"):
            call()


def test_closing_throws_away_what_was_never_written(bundle):
    with pytest.raises(RuntimeError):
        with bundle.batch():
            bundle.touch("/new.txt")
            stage = bundle._stage_dir
            bundle.close()
            raise RuntimeError("the application is going away")
    assert not os.path.exists(stage)


# -- writing into a remote archive ---------------------------------------------
def test_a_remote_archive_is_built_here_and_uploaded(tmp_path):
    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y"})
    fs = ArchiveFileSystem(_NoLocalPath(), path)
    fs.touch("/new.txt")
    assert sorted(_zip_members(path)) == ["new.txt", "x.txt"]
    assert fs.open_read("/new.txt").read() == b""


class _FailingUpload(_NoLocalPath):
    """A remote host whose upload dies part way, as a dropped link does."""

    def open_write(self, path):
        stream = open(path, "wb")
        real_write = stream.write

        def write(data):
            real_write(data[:10])
            raise OSError("connection reset")

        stream.write = write
        return stream


def test_a_failed_upload_keeps_the_finished_archive_here(tmp_path):
    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y"})
    fs = ArchiveFileSystem(_FailingUpload(), path)
    with pytest.raises(FileSystemError, match="kept at") as caught:
        fs.touch("/new.txt")
    kept = str(caught.value).rsplit("kept at ", 1)[1]
    try:
        assert sorted(_zip_members(kept)) == ["new.txt", "x.txt"]
    finally:
        os.remove(kept)
    # The upload truncated the archive it was replacing, so there is nothing
    # left to read, and the pane says so rather than listing a ghost.
    assert fs.listdir("/") == []
    with pytest.raises(FileSystemError, match="has been closed"):
        fs.touch("/again.txt")


def test_a_remote_archive_is_not_grown_past_what_can_be_opened(tmp_path,
                                                               monkeypatch):
    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y"})
    fs = ArchiveFileSystem(_NoLocalPath(), path)
    before = _zip_members(path)
    monkeypatch.setattr(archive, "MAX_BYTES", os.path.getsize(path) + 10)
    with pytest.raises(FileSystemError, match="would grow past"):
        with fs.batch():
            writer = fs.open_write("/big.bin")
            writer.write(os.urandom(4096))
            writer.close()
    assert _zip_members(path) == before
    assert [e.name for e in fs.listdir("/")] == ["x.txt"]


def test_an_upload_refused_outright_leaves_the_remote_archive_as_it_was(
        tmp_path):
    class _Refusing(_NoLocalPath):
        def open_write(self, path):
            raise OSError("permission denied")

    path = write_zip(str(tmp_path / "a.zip"), {"x.txt": "y"})
    fs = ArchiveFileSystem(_Refusing(), path)
    with pytest.raises(FileSystemError, match="permission denied") as caught:
        fs.touch("/new.txt")
    os.remove(str(caught.value).rsplit("kept at ", 1)[1])
    assert [e.name for e in fs.listdir("/")] == ["x.txt"]
    assert fs.open_read("/x.txt").read() == b"y"


def test_an_archive_inside_an_archive_writes_through_both(tmp_path):
    inner = tmp_path / "inner.zip"
    write_zip(str(inner), {"deep.txt": "deep"})
    outer = write_zip(str(tmp_path / "outer.zip"), {})
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, "inner.zip")
    outer_fs = ArchiveFileSystem(LocalFileSystem(), outer)
    inner_fs = ArchiveFileSystem(outer_fs, "/inner.zip")
    inner_fs.touch("/added.txt")
    with zipfile.ZipFile(outer) as zf:
        nested = zipfile.ZipFile(io.BytesIO(zf.read("inner.zip")))
    assert sorted(nested.namelist()) == ["added.txt", "deep.txt"]


# -- names for what is unpacked ------------------------------------------------
@pytest.mark.parametrize("name, stem", [
    ("project.zip", "project"), ("project.tar.gz", "project"),
    ("Project.TGZ", "Project"), ("v1.2.tar.xz", "v1.2"),
    (".zip", ".zip"), ("notes.txt", "notes.txt"),
])
def test_the_folder_an_archive_unpacks_into(name, stem):
    assert archive_stem(name) == stem

