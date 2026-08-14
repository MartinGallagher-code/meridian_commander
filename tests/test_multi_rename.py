"""The Multi-rename built-in plug-in."""

from __future__ import annotations

import pytest

from meridian_commander.plugins.multi_rename import MultiRename, rename_one


def _run(ctx, command):
    result = MultiRename(ctx).process(command)
    return "\n".join(result) if isinstance(result, list) else result


# -- the rule engine, in isolation --------------------------------------------

@pytest.mark.parametrize("name, verb, args, index, expected", [
    ("report.txt", "replace", ["report", "summary"], 1, "summary.txt"),
    ("a.txt", "prefix", ["2024-"], 1, "2024-a.txt"),
    ("a.txt", "suffix", ["-final"], 1, "a-final.txt"),
    ("noext", "suffix", ["-final"], 1, "noext-final"),
    ("Photo.JPG", "case", ["lower"], 1, "photo.jpg"),
    ("photo.jpg", "case", ["upper"], 1, "PHOTO.jpg"),
    ("my file.txt", "case", ["title"], 1, "My File.txt"),
    ("orig.png", "number", ["{n:03}-{name}.{ext}"], 7, "007-orig.png"),
    (".bashrc", "suffix", ["-x"], 1, ".bashrc-x"),
])
def test_rename_one_rules(name, verb, args, index, expected):
    assert rename_one(name, verb, args, index) == expected


@pytest.mark.parametrize("verb, args, message", [
    ("replace", ["only"], "usage: replace"),
    ("prefix", [], "usage: prefix"),
    ("case", ["sideways"], "usage: case"),
    ("number", ["a", "b"], "usage: number"),
    ("wobble", [], "unknown verb"),
])
def test_rename_one_rejects_bad_rules(verb, args, message):
    with pytest.raises(ValueError, match=message):
        rename_one("x.txt", verb, args, 1)


def test_number_reports_a_bad_template():
    with pytest.raises(ValueError, match="bad template"):
        rename_one("x.txt", "number", ["{nope}"], 1)


# -- the plug-in, end to end ---------------------------------------------------

def test_greeting_lists_the_tagged_files(data_ctx):
    ctx = data_ctx({"a.txt": "1", "b.txt": "2"}, selected={"a.txt", "b.txt"})
    greeting = MultiRename(ctx).greeting
    assert "a.txt" in greeting and "b.txt" in greeting

    ctx.other_panel.selected = set()
    ctx.other_panel.move_to(0)
    assert "<nothing tagged>" in MultiRename(ctx).greeting


def test_empty_input_does_nothing(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert MultiRename(ctx).process("") is None


def test_needs_a_tagged_file(data_ctx):
    ctx = data_ctx({"a.txt": "1"})
    ctx.other_panel.move_to(0)                  # the ".." entry, not a file
    with pytest.raises(RuntimeError, match="Tag at least 1"):
        MultiRename(ctx).process("prefix x-")


def test_prefix_renames_on_disk(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1", "b.txt": "2"}, selected={"a.txt", "b.txt"})
    out = _run(ctx, "prefix 2024-")
    assert "Renamed 2 file(s)" in out
    data = tmp_path / "data"
    assert (data / "2024-a.txt").exists() and (data / "2024-b.txt").exists()
    assert not (data / "a.txt").exists()


def test_number_uses_listing_order(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1", "b.txt": "2", "c.txt": "3"},
                   selected={"a.txt", "b.txt", "c.txt"})
    _run(ctx, "number {n:02}.{ext}")
    data = tmp_path / "data"
    assert {p.name for p in data.iterdir()} == {"01.txt", "02.txt", "03.txt"}


def test_preview_writes_nothing(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    out = _run(ctx, "preview prefix new-")
    assert "a.txt -> new-a.txt" in out
    assert "nothing written" in out
    assert (tmp_path / "data" / "a.txt").exists()


def test_preview_needs_a_rule(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert MultiRename(ctx).process("preview") == "usage: preview <rule> ..."


def test_a_rule_that_changes_nothing_is_reported(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert "left every name unchanged" in _run(ctx, "replace zzz qqq")


def test_two_names_colliding_is_refused(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1", "b.txt": "2"}, selected={"a.txt", "b.txt"})
    out = _run(ctx, "number same.txt")
    assert "two files would both become 'same.txt'" in out
    # Nothing was renamed.
    assert (tmp_path / "data" / "a.txt").exists()


def test_landing_on_an_existing_file_is_refused(data_ctx, tmp_path):
    ctx = data_ctx({"a.txt": "1", "taken.txt": "keep"}, selected={"a.txt"})
    out = _run(ctx, "replace a taken")
    assert "'taken.txt' already exists" in out
    assert (tmp_path / "data" / "taken.txt").read_text() == "keep"


def test_swapping_names_among_the_set_is_allowed(data_ctx, tmp_path):
    # b.txt already exists but is itself part of the rename, so it is not a clash.
    ctx = data_ctx({"a.txt": "1", "b.txt": "2"}, selected={"a.txt", "b.txt"})
    out = _run(ctx, "suffix -x")
    assert "Renamed 2 file(s)" in out


def test_an_invalid_target_name_is_refused(data_ctx):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})
    assert "not a valid name" in _run(ctx, "number sub/{name}.{ext}")


def test_a_failed_rename_is_reported_and_others_continue(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1", "b.txt": "2"}, selected={"a.txt", "b.txt"})
    real_rename = ctx.other_fs.rename

    def flaky(src, dst):
        if src.endswith("/a.txt"):
            raise OSError("locked")
        return real_rename(src, dst)

    monkeypatch.setattr(ctx.other_fs, "rename", flaky)
    plugin = MultiRename(ctx)
    summary = plugin.process("prefix z-")
    assert any("! a.txt: locked" in line for line in plugin.output)
    assert summary == "Renamed 1 file(s)."


def test_survives_a_pane_that_cannot_refresh(data_ctx, monkeypatch):
    ctx = data_ctx({"a.txt": "1"}, selected={"a.txt"})

    def broken():
        raise RuntimeError("pane went away")

    monkeypatch.setattr(ctx.other_panel, "refresh", broken)
    assert "Renamed 1 file(s)" in _run(ctx, "prefix z-")
