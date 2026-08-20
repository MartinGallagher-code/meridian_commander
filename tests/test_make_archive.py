"""The Make-archive built-in plug-in."""

from __future__ import annotations

import tarfile
import zipfile

import pytest

from meridian_commander.plugins.make_archive import MakeArchive


def _run(ctx, command):
    result = MakeArchive(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


def _zip_names(path):
    with zipfile.ZipFile(path) as zf:
        return sorted(zf.namelist())


def test_greeting_lists_the_tagged_files(data_ctx):
    ctx = data_ctx({"a.txt": "1", "b.txt": "2"}, selected={"a.txt", "b.txt"})
    greeting = MakeArchive(ctx).greeting
    assert "a.txt" in greeting and "b.txt" in greeting

    ctx.other_panel.selected = set()
    ctx.other_panel.move_to(0)
    assert "<nothing tagged>" in MakeArchive(ctx).greeting


def test_empty_input_does_nothing(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert MakeArchive(ctx).process("") is None


def test_rejects_an_unknown_format(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert "unknown format" in MakeArchive(ctx).process("rar")


def test_needs_a_tagged_file(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    ctx.other_panel.move_to(0)                  # the ".." entry
    with pytest.raises(RuntimeError, match="Tag at least 1"):
        MakeArchive(ctx).process("zip")


def test_zip_packs_the_selection(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "alpha", "b.txt": "beta"},
                   selected={"a.txt", "b.txt"})
    out = _run(ctx, "zip bundle")
    assert "Wrote bundle.zip" in out
    archive = tmp_path / "data" / "bundle.zip"
    assert _zip_names(archive) == ["a.txt", "b.txt"]
    with zipfile.ZipFile(archive) as zf:
        assert zf.read("a.txt") == b"alpha"


def test_directories_are_added_recursively(data_ctx, tmp_path):
    ctx = data_ctx({"top.txt": "t", "d/inner.txt": "i", "d/sub/deep.txt": "x"},
                   selected={"top.txt", "d"})
    _run(ctx, "zip pack")
    assert _zip_names(tmp_path / "data" / "pack.zip") == [
        "d/inner.txt", "d/sub/deep.txt", "top.txt"]


def test_tar_and_tgz(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "alpha"}, selected={"a.txt"})
    _run(ctx, "tar plain")
    with tarfile.open(tmp_path / "data" / "plain.tar") as tf:
        assert tf.getnames() == ["a.txt"]
        assert tf.extractfile("a.txt").read() == b"alpha"

    _run(ctx, "tgz zipped")
    zipped = tmp_path / "data" / "zipped.tar.gz"
    assert zipped.exists()
    with tarfile.open(zipped, "r:gz") as tf:
        assert tf.getnames() == ["a.txt"]


def test_default_name_comes_from_the_directory(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    _run(ctx, "zip")
    assert (tmp_path / "data" / "data.zip").exists()


def test_a_supplied_name_keeps_its_extension_only_once(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    _run(ctx, "zip already.zip")
    assert (tmp_path / "data" / "already.zip").exists()
    assert not (tmp_path / "data" / "already.zip.zip").exists()


def test_an_existing_archive_is_not_clobbered(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1", "pack.zip": "OLD"}, selected={"a.txt"})
    out = _run(ctx, "zip pack")
    assert "Wrote pack2.zip" in out
    assert (tmp_path / "data" / "pack.zip").read_text() == "OLD"


def test_numbering_steps_past_several_existing_archives(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1", "pack.zip": "x", "pack2.zip": "y"},
                   selected={"a.txt"})
    assert "Wrote pack3.zip" in _run(ctx, "zip pack")


def test_an_unreadable_directory_is_reported_and_skipped(data_ctx, monkeypatch):
    ctx = data_ctx({"d/inner.txt": "x"}, selected={"d"})
    real_listdir = ctx.other_fs.listdir

    def selective(path):
        if path.endswith("/d"):
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(ctx.other_fs, "listdir", selective)
    plugin = MakeArchive(ctx)
    plugin.process("zip pack")
    assert any("! d: denied" in line for line in plugin.output)


def test_a_read_error_is_reported_and_skips_the_file(data_ctx, tmp_path, monkeypatch):
    ctx = data_ctx({"a.txt": "1", "b.txt": "2"}, selected={"a.txt", "b.txt"})
    real_open = ctx.other_fs.open_read

    def flaky(path):
        if path.endswith("/a.txt"):
            raise OSError("unreadable")
        return real_open(path)

    monkeypatch.setattr(ctx.other_fs, "open_read", flaky)
    plugin = MakeArchive(ctx)
    plugin.process("zip pack")
    assert any("! a.txt: unreadable" in line for line in plugin.output)
    assert _zip_names(tmp_path / "data" / "pack.zip") == ["b.txt"]


def test_survives_a_pane_that_cannot_refresh(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})

    def broken():
        raise RuntimeError("pane went away")

    monkeypatch.setattr(ctx.other_panel, "refresh", broken)
    assert "Wrote" in _run(ctx, "zip pack")
