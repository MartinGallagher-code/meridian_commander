# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Saving a browser's view as a file (MC-029): the parts every browser shares.

The table browser's and the spreadsheet grid's own tests drive the whole
save through their keys; these pin down the helper underneath.
"""

from __future__ import annotations

import curses

from meridian_commander import saveview, theme
from meridian_commander.archive import ArchiveFileSystem
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.plugins import _io, _tabular

from support import _FakeRemoteBackend, write_zip


def test_the_plug_ins_write_and_name_through_the_same_helpers():
    # Moved to the core so a browser need not import a plug-in module; the
    # plug-ins still call them by their old names.
    assert _io.write_bytes is saveview.write_bytes
    assert _tabular.derive_output_path is saveview.derive_output_path


def test_the_save_keys_are_the_editors():
    assert saveview.SAVE_KEYS == (curses.KEY_F2, 19)


def test_the_shape_of_a_save_counts_rows_and_columns():
    times = theme.glyph("times")
    assert saveview.shape(1204, 7) == f"1,204 rows {times} 7 columns"
    assert saveview.shape(1, 1) == f"1 row {times} 1 column"


def test_the_times_sign_has_a_plain_spelling_too():
    assert theme.UNICODE_GLYPHS["times"] == "×"
    assert theme.ASCII_GLYPHS["times"] == "x"


def test_a_local_directory_is_asked_whether_it_may_be_written(tmp_path,
                                                              monkeypatch):
    fs = LocalFileSystem()
    assert saveview._may_write(str(tmp_path)) is True
    assert saveview.unwritable(fs, str(tmp_path)) is None
    monkeypatch.setattr(saveview, "_may_write", lambda directory: False)
    assert saveview.unwritable(fs, str(tmp_path)) == "cannot be written to"


def test_a_remote_directory_is_tried_rather_than_asked(tmp_path, monkeypatch):
    # Only a write would tell, so the write is tried and its failure shown.
    monkeypatch.setattr(saveview, "_may_write", lambda directory: False)
    assert saveview.unwritable(_FakeRemoteBackend(), str(tmp_path)) is None


def test_an_archive_is_not_rewritten_for_a_save(tmp_path):
    archive = write_zip(str(tmp_path / "bundle.zip"), {"data/a.tsv": "a\n"})
    fs = ArchiveFileSystem(LocalFileSystem(), archive)
    assert saveview.unwritable(fs, "/data") == \
        "is inside the archive bundle.zip"


def test_a_typed_name_is_made_a_path_in_the_directory_offered():
    fs = LocalFileSystem()
    assert saveview.resolve(fs, "/data", "out.tsv") == "/data/out.tsv"
    assert saveview.resolve(fs, "/data", "../out.tsv") == "/out.tsv"
    assert saveview.resolve(fs, "/data", "/tmp//out.tsv") == "/tmp/out.tsv"


def test_the_same_file_by_another_name(tmp_path):
    fs = LocalFileSystem()
    path = str(tmp_path / "a.tsv")
    (tmp_path / "a.tsv").write_text("a\n")
    (tmp_path / "b.tsv").write_text("b\n")
    (tmp_path / "link.tsv").symlink_to(path)
    assert saveview._same_file(fs, path, fs, str(tmp_path / "a.tsv"))
    assert saveview._same_file(fs, path, LocalFileSystem(),
                               str(tmp_path / "link.tsv"))
    assert not saveview._same_file(fs, path, fs, str(tmp_path / "b.tsv"))
    assert not saveview._same_file(fs, path, fs, str(tmp_path / "new.tsv"))
    # A remote pane is only the same place as itself.
    remote = _FakeRemoteBackend()
    assert saveview._same_file(remote, path, remote, path)
    assert not saveview._same_file(remote, path, _FakeRemoteBackend(), path)
