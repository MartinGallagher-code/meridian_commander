"""Plug-in discovery and the built-in plug-ins."""

from __future__ import annotations

import importlib

import pytest

from meridian_commander import plugins
from meridian_commander.plugins.csv_build import CsvBuild
from meridian_commander.plugins.csv_clean import CsvClean
from meridian_commander.plugins.csv_profile import CsvProfile
from meridian_commander.plugins.find_files import FindFiles

from support import read, write


# -- discovery -----------------------------------------------------------------

def test_the_builtin_plugins_are_found():
    classes, errors = plugins.discover()
    names = [c.name for c in classes]
    assert "Find in other pane" in names
    assert "Profile table" in names
    assert errors == []
    # The menu is sorted by name so it does not shuffle between runs.
    assert names == sorted(names, key=str.lower)


def test_the_search_path_starts_with_the_builtins(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    dirs = plugins.plugin_dirs()
    assert dirs[0] == plugins.builtin_plugin_dir()
    assert dirs[1] == plugins.user_plugin_dir()
    assert dirs[1].endswith("meridian-commander/plugins")


def test_a_user_plugin_is_discovered(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pdir = tmp_path / "meridian-commander" / "plugins"
    pdir.mkdir(parents=True)
    (pdir / "mine.py").write_text(
        "from meridian_commander.plugin_api import InputOutputPlugin\n"
        "class Mine(InputOutputPlugin):\n"
        "    name = 'Mine'\n"
        "    description = 'a user plugin'\n"
        "    def process(self, line):\n"
        "        return line\n"
    )
    classes, errors = plugins.discover()
    assert "Mine" in [c.name for c in classes]
    assert errors == []


def test_a_broken_user_plugin_is_reported_not_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pdir = tmp_path / "meridian-commander" / "plugins"
    pdir.mkdir(parents=True)
    (pdir / "broken.py").write_text("raise RuntimeError('bad plugin')\n")

    classes, errors = plugins.discover()
    assert any("broken.py: bad plugin" in e for e in errors)
    # The working built-ins are still offered.
    assert "Find in other pane" in [c.name for c in classes]


def test_private_and_non_python_files_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pdir = tmp_path / "meridian-commander" / "plugins"
    pdir.mkdir(parents=True)
    (pdir / "_helper.py").write_text("raise RuntimeError('never loaded')\n")
    (pdir / "notes.txt").write_text("not a plugin")

    _, errors = plugins.discover()
    assert errors == []


def test_a_missing_plugin_directory_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nowhere"))
    _, errors = plugins.discover()
    assert errors == []


def test_a_builtin_that_fails_to_import_is_reported(monkeypatch):
    real_import = importlib.import_module

    def flaky(name, *args, **kwargs):
        if name.endswith(".csv_profile"):
            raise ImportError("pretend this one is broken")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(plugins.importlib, "import_module", flaky)
    classes, errors = plugins.discover()
    assert any("csv_profile: pretend this one is broken" in e for e in errors)
    assert "Profile table" not in [c.name for c in classes]


def test_a_duplicate_plugin_name_is_only_offered_once(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pdir = tmp_path / "meridian-commander" / "plugins"
    pdir.mkdir(parents=True)
    (pdir / "clash.py").write_text(
        "from meridian_commander.plugin_api import InputOutputPlugin\n"
        "class Clash(InputOutputPlugin):\n"
        "    name = 'Find in other pane'\n"
        "    description = 'shadows a built-in'\n"
        "    def process(self, line):\n"
        "        return line\n"
    )
    classes, _ = plugins.discover()
    names = [c.name for c in classes]
    assert names.count("Find in other pane") == 1


# -- find in other pane --------------------------------------------------------

@pytest.fixture
def find_ctx(data_ctx):
    return data_ctx({
        "notes.txt": "x",
        "sub/deep.txt": "y",
        "sub/other.log": "z",
    })


def test_find_plugin_greeting_names_the_target(find_ctx):
    plugin = FindFiles(find_ctx)
    assert "Searching under" in plugin.greeting
    assert find_ctx.other_path in plugin.greeting


def test_find_plugin_needs_a_pattern(find_ctx):
    plugin = FindFiles(find_ctx)
    assert plugin.process("   ") == "Give a pattern, e.g. *.txt"


def test_find_plugin_lists_matches_recursively(find_ctx):
    plugin = FindFiles(find_ctx)
    summary = plugin.process("*.txt")
    printed = "\n".join(plugin.output)
    assert "notes.txt" in printed
    assert "sub/deep.txt" in printed
    assert "other.log" not in printed
    assert summary == "2 match(es)"


def test_find_plugin_marks_directories(find_ctx):
    plugin = FindFiles(find_ctx)
    plugin.process("sub")
    assert any(line.strip() == "sub/" for line in plugin.output)


def test_find_plugin_reports_an_unreadable_directory(data_ctx, monkeypatch):
    ctx = data_ctx({"sub/a.txt": "x"})
    real_listdir = ctx.other_fs.listdir

    def selective(path):
        if path.endswith("/sub"):
            raise PermissionError("permission denied")
        return real_listdir(path)

    monkeypatch.setattr(ctx.other_fs, "listdir", selective)
    plugin = FindFiles(ctx)
    plugin.process("*")
    assert any("permission denied" in line for line in plugin.output)


def test_find_plugin_stops_at_the_result_cap(data_ctx, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.find_files.MAX_RESULTS", 3)
    ctx = data_ctx({f"f{i}.txt": "x" for i in range(10)})
    plugin = FindFiles(ctx)
    summary = plugin.process("*.txt")
    assert "stopped at 3" in summary


def test_find_plugin_respects_the_depth_cap(data_ctx, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.find_files.MAX_DEPTH", 0)
    ctx = data_ctx({"a/b/deep.txt": "x"})
    assert FindFiles(ctx).process("deep") == "0 match(es)"


# -- grep in other pane --------------------------------------------------------

@pytest.fixture
def grep_ctx(data_ctx):
    return data_ctx({
        "notes.txt": "alpha\nBeta line\ngamma\n",
        "sub/deep.txt": "another beta here\n",
        "sub/quiet.log": "nothing to see\n",
    })


def test_grep_greeting_names_the_target(grep_ctx):
    from meridian_commander.plugins.grep_files import GrepFiles

    assert "Searching under" in GrepFiles(grep_ctx).greeting


def test_grep_needs_something_to_find(grep_ctx):
    from meridian_commander.plugins.grep_files import GrepFiles

    assert GrepFiles(grep_ctx).process("   ") == "Give some text to find, or re:<pattern>."


def test_grep_matches_content_case_insensitively_and_recursively(grep_ctx):
    from meridian_commander.plugins.grep_files import GrepFiles

    plugin = GrepFiles(grep_ctx)
    summary = plugin.process("beta")
    printed = "\n".join(plugin.output)
    assert "notes.txt:2:" in printed
    assert "sub/deep.txt:1:" in printed
    assert "quiet.log" not in printed
    assert summary == "2 match(es)"


def test_grep_supports_a_regex(grep_ctx):
    from meridian_commander.plugins.grep_files import GrepFiles

    plugin = GrepFiles(grep_ctx)
    summary = plugin.process(r"re:^gamma$")
    assert "notes.txt:3:" in "\n".join(plugin.output)
    assert summary == "1 match(es)"


def test_grep_reports_a_bad_regex(grep_ctx):
    from meridian_commander.plugins.grep_files import GrepFiles

    assert "Bad regular expression" in GrepFiles(grep_ctx).process("re:(")


def test_grep_skips_binary_files(data_ctx):
    from meridian_commander.plugins.grep_files import GrepFiles

    ctx = data_ctx({"blob.bin": "AB\x00CDbeta"})
    plugin = GrepFiles(ctx)
    assert plugin.process("beta") == "0 match(es)"


def test_grep_stops_at_the_result_cap(data_ctx, monkeypatch):
    from meridian_commander.plugins.grep_files import GrepFiles

    monkeypatch.setattr("meridian_commander.plugins.grep_files.MAX_RESULTS", 2)
    ctx = data_ctx({f"f{i}.txt": "match\n" for i in range(10)})
    assert "stopped at 2" in GrepFiles(ctx).process("match")


def test_grep_notes_a_file_it_only_partly_scanned(data_ctx, monkeypatch):
    from meridian_commander.plugins.grep_files import GrepFiles

    monkeypatch.setattr("meridian_commander.plugins.grep_files.MAX_FILE_BYTES", 8)
    monkeypatch.setattr("meridian_commander.plugins.grep_files.CHUNK", 8)
    ctx = data_ctx({"big.txt": "hit\n" + "x" * 100})
    plugin = GrepFiles(ctx)
    plugin.process("hit")
    assert any("scanned only the first" in line for line in plugin.output)


def test_grep_reports_an_unreadable_directory(data_ctx, monkeypatch):
    from meridian_commander.plugins.grep_files import GrepFiles

    ctx = data_ctx({"sub/a.txt": "x"})
    real_listdir = ctx.other_fs.listdir

    def selective(path):
        if path.endswith("/sub"):
            raise PermissionError("permission denied")
        return real_listdir(path)

    monkeypatch.setattr(ctx.other_fs, "listdir", selective)
    plugin = GrepFiles(ctx)
    plugin.process("x")
    assert any("permission denied" in line for line in plugin.output)


# -- profile -------------------------------------------------------------------

def test_profile_reports_shape_types_and_stats(data_ctx):
    ctx = data_ctx({"data.csv": "id,name,score\n1,alice,10\n2,bob,20\n3,,30\n"},
                   selected={"data.csv"})
    joined = "\n".join(CsvProfile(ctx).process(""))
    assert "3 row(s) x 3 column(s)" in joined
    assert "id [int]" in joined
    assert "name [str]" in joined
    assert "nulls=1" in joined
    assert "mean=20" in joined


def test_profile_of_a_truncated_file_says_so(data_ctx, monkeypatch):
    ctx = data_ctx({"big.csv": "a\n" + "1\n" * 5000}, selected={"big.csv"})
    plugin = CsvProfile(ctx)
    monkeypatch.setitem(plugin.config, "max_bytes", 100)
    joined = "\n".join(plugin.process(""))
    assert "truncated at the byte cap" in joined


def test_profile_of_one_column(data_ctx):
    ctx = data_ctx({"d.csv": "id,name\n1,alice\n2,bob\n3,alice\n"},
                   selected={"d.csv"})
    joined = "\n".join(CsvProfile(ctx).process("col name"))
    assert "value counts" in joined
    assert "'alice': 2" in joined


def test_profile_of_a_numeric_column_draws_a_distribution(data_ctx):
    ctx = data_ctx({"d.csv": "n\n1\n2\n3\n4\n5\n"}, selected={"d.csv"})
    joined = "\n".join(CsvProfile(ctx).process("col n"))
    assert "distribution" in joined
    assert "#" in joined


def test_profile_col_needs_a_name(data_ctx):
    ctx = data_ctx({"d.csv": "a\n1\n"}, selected={"d.csv"})
    assert CsvProfile(ctx).process("col") == "usage: col <name>"


def test_profile_col_rejects_an_unknown_name(data_ctx):
    ctx = data_ctx({"d.csv": "a\n1\n"}, selected={"d.csv"})
    with pytest.raises(ValueError, match="no such column"):
        CsvProfile(ctx).process("col nope")


def test_profile_head_and_tail(data_ctx):
    ctx = data_ctx({"d.csv": "id\n1\n2\n3\n"}, selected={"d.csv"})
    plugin = CsvProfile(ctx)
    head = "\n".join(plugin.process("head 1"))
    assert "1" in head and "3" not in head
    tail = "\n".join(plugin.process("tail 1"))
    assert "3" in tail and "1" not in tail.split("\n", 2)[-1]


def test_profile_head_with_a_nonsense_count(data_ctx):
    ctx = data_ctx({"d.csv": "id\n1\n"}, selected={"d.csv"})
    assert CsvProfile(ctx).process("head lots") == "usage: head [n]"


def test_profile_rejects_an_unknown_command(data_ctx):
    ctx = data_ctx({"d.csv": "a\n1\n"}, selected={"d.csv"})
    assert "unknown command" in CsvProfile(ctx).process("wobble")


def test_profile_needs_a_selection(data_ctx):
    ctx = data_ctx({"d.csv": "a\n1\n"})
    ctx.other_panel.move_to(0)              # the ".." entry, not a file
    with pytest.raises(RuntimeError, match="Select a CSV"):
        CsvProfile(ctx).process("")


# -- clean ---------------------------------------------------------------------

def _clean(ctx, command):
    plugin = CsvClean(ctx)
    result = plugin.process(command)
    return "\n".join(result) if isinstance(result, list) else result


def test_clean_greeting_names_the_target_or_says_there_is_none(data_ctx):
    ctx = data_ctx({"t.csv": "a\n1\n"}, selected={"t.csv"})
    assert "Target (other pane): t.csv" in CsvClean(ctx).greeting

    ctx.other_panel.selected = set()
    ctx.other_panel.move_to(0)
    assert "<nothing selected>" in CsvClean(ctx).greeting


def test_clean_with_no_command_does_nothing(data_ctx):
    ctx = data_ctx({"t.csv": "a\n1\n"}, selected={"t.csv"})
    assert CsvClean(ctx).process("") is None


def test_clean_preview_without_a_verb(data_ctx):
    ctx = data_ctx({"t.csv": "a\n1\n"}, selected={"t.csv"})
    assert CsvClean(ctx).process("preview") == "usage: preview <verb> ..."


def test_clean_trim(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "a,b\n 1 , x \n"}, selected={"t.csv"})
    _clean(ctx, "trim")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == "a,b\n1,x\n"


def test_clean_dedupe_on_all_columns_and_on_a_subset(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "a,b\n1,x\n1,x\n1,y\n"}, selected={"t.csv"})
    _clean(ctx, "dedupe")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == "a,b\n1,x\n1,y\n"
    _clean(ctx, "dedupe a")
    assert read(str(tmp_path / "data" / "t.cleaned2.csv")) == "a,b\n1,x\n"


def test_clean_dropnull(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "a,b\n1,x\n,y\n3,\n"}, selected={"t.csv"})
    _clean(ctx, "dropnull a")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == "a,b\n1,x\n3,\n"


def test_clean_dropnull_without_columns_changes_nothing(data_ctx):
    ctx = data_ctx({"t.csv": "a\n\n1\n"}, selected={"t.csv"})
    assert "no columns given" in _clean(ctx, "dropnull")


def test_clean_fillnull_pads_short_rows(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "a,b\n1,\n2\n"}, selected={"t.csv"})
    _clean(ctx, "fillnull b none")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == "a,b\n1,none\n2,none\n"


def test_clean_drop_and_keep_columns(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "a,b,c\n1,2,3\n"}, selected={"t.csv"})
    _clean(ctx, "drop b")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == "a,c\n1,3\n"
    _clean(ctx, "keep c,a")
    assert read(str(tmp_path / "data" / "t.cleaned2.csv")) == "c,a\n3,1\n"


def test_clean_rename_a_column(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "a,b\n1,2\n"}, selected={"t.csv"})
    _clean(ctx, "rename a first")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == "first,b\n1,2\n"


def test_clean_rename_needs_a_new_name(data_ctx):
    ctx = data_ctx({"t.csv": "a,b\n1,2\n"}, selected={"t.csv"})
    with pytest.raises(ValueError, match="usage: rename"):
        CsvClean(ctx).process("rename a")


def test_clean_normalize_headers(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "First Name,lastName,  spaced  \n1,2,3\n"},
                   selected={"t.csv"})
    _clean(ctx, "normalize-headers")
    written = read(str(tmp_path / "data" / "t.cleaned.csv"))
    assert written.splitlines()[0] == "first_name,last_name,spaced"


def test_clean_rejects_an_unknown_verb(data_ctx):
    ctx = data_ctx({"t.csv": "a\n1\n"}, selected={"t.csv"})
    with pytest.raises(ValueError, match="unknown verb"):
        CsvClean(ctx).process("wobble")


@pytest.mark.parametrize("expression, kept", [
    ("n > 2", ["3", "10"]),
    ("n < 3", ["1", "2"]),
    ("n >= 3", ["3", "10"]),
    ("n <= 2", ["1", "2"]),
    ("n == 2", ["2"]),
    ("n != 2", ["1", "3", "10"]),
])
def test_clean_filter_operators(data_ctx, tmp_path, expression, kept):
    ctx = data_ctx({"t.csv": "n\n1\n2\n3\n10\n"}, selected={"t.csv"})
    _clean(ctx, f"filter {expression}")
    written = read(str(tmp_path / "data" / "t.cleaned.csv"))
    assert written.splitlines()[1:] == kept


def test_clean_filter_contains_and_lexical_comparison(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "tag\napple\nbanana\ncherry\n"}, selected={"t.csv"})
    _clean(ctx, "filter tag contains an")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == "tag\nbanana\n"
    # Non-numeric values fall back to comparing as text.
    _clean(ctx, "filter tag > b")
    written = read(str(tmp_path / "data" / "t.cleaned2.csv"))
    assert written.splitlines()[1:] == ["banana", "cherry"]


def test_clean_filter_needs_a_recognised_operator(data_ctx):
    ctx = data_ctx({"t.csv": "n\n1\n"}, selected={"t.csv"})
    with pytest.raises(ValueError, match="usage: filter"):
        CsvClean(ctx).process("filter n ~ 1")


def test_clean_retype(data_ctx, tmp_path):
    # Nulls and short rows are left alone rather than failing the conversion.
    ctx = data_ctx({"t.csv": "n,m\n1.0,a\n2.50,b\nNA,c\n5\n"},
                   selected={"t.csv"})
    _clean(ctx, "retype n float")
    assert read(str(tmp_path / "data" / "t.cleaned.csv")) == \
        "n,m\n1,a\n2.5,b\nNA,c\n5\n"


def test_clean_retype_refuses_a_column_it_cannot_convert(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "n\n1\noops\n"}, selected={"t.csv"})
    with pytest.raises(ValueError, match="1 value\\(s\\)"):
        CsvClean(ctx).process("retype n int")
    assert not (tmp_path / "data" / "t.cleaned.csv").exists()


def test_clean_retype_only_knows_int_and_float(data_ctx):
    ctx = data_ctx({"t.csv": "n\n1\n"}, selected={"t.csv"})
    with pytest.raises(ValueError, match="int or float"):
        CsvClean(ctx).process("retype n date")


def test_clean_preview_writes_nothing(data_ctx, tmp_path):
    ctx = data_ctx({"t.csv": "a\n 1 \n"}, selected={"t.csv"})
    out = _clean(ctx, "preview trim")
    assert "nothing written" in out
    assert not (tmp_path / "data" / "t.cleaned.csv").exists()


def test_clean_warns_when_the_source_was_truncated(data_ctx, monkeypatch):
    ctx = data_ctx({"big.csv": "a\n" + "1\n" * 5000}, selected={"big.csv"})
    plugin = CsvClean(ctx)
    monkeypatch.setitem(plugin.config, "max_bytes", 100)
    out = "\n".join(plugin.process("trim"))
    assert "truncated at the byte cap" in out


def test_clean_survives_a_pane_that_cannot_refresh(data_ctx, monkeypatch):
    ctx = data_ctx({"t.csv": "a\n1\n"}, selected={"t.csv"})

    def broken():
        raise RuntimeError("pane went away")

    monkeypatch.setattr(ctx.other_panel, "refresh", broken)
    assert "wrote" in _clean(ctx, "trim")


def test_clean_reads_a_headerless_file_when_configured(data_ctx, monkeypatch):
    ctx = data_ctx({"t.csv": "1,x\n2,y\n"}, selected={"t.csv"})
    plugin = CsvClean(ctx)
    monkeypatch.setitem(plugin.config, "has_header", "no")
    out = "\n".join(plugin.process("preview trim"))
    assert "col1" in out


# -- build ---------------------------------------------------------------------

def test_build_concat_takes_the_union_of_columns(data_ctx, tmp_path):
    ctx = data_ctx({"a.csv": "x,y\n1,2\n", "b.csv": "y,z\n3,4\n"},
                   selected={"a.csv", "b.csv"})
    CsvBuild(ctx).process("concat")
    assert read(str(tmp_path / "data" / "dataset.csv")) == "x,y,z\n1,2,\n,3,4\n"


def test_build_join_keeps_unmatched_left_rows(data_ctx, tmp_path):
    ctx = data_ctx({"a.csv": "id,name\n1,alice\n2,bob\n", "b.csv": "id,age\n1,30\n"},
                   selected={"a.csv", "b.csv"})
    CsvBuild(ctx).process("join id")
    assert read(str(tmp_path / "data" / "joined.csv")) == \
        "id,name,age\n1,alice,30\n2,bob,\n"


def test_build_groupby_without_pandas(data_ctx, tmp_path, monkeypatch):
    from meridian_commander.plugins import csv_build

    monkeypatch.setattr(csv_build, "has_pandas", lambda: False)
    ctx = data_ctx({"s.csv": "cat,val\nx,1\nx,3\ny,10\n"}, selected={"s.csv"})
    csv_build.CsvBuild(ctx).process("groupby cat mean:val")
    assert read(str(tmp_path / "data" / "s.groupby.csv")) == \
        "cat,mean_val\nx,2\ny,10\n"


def test_build_to_jsonl_and_back(data_ctx, tmp_path):
    ctx = data_ctx({"s.csv": "cat,val\nx,1\ny,2\n"}, selected={"s.csv"})
    CsvBuild(ctx).process("to-jsonl")
    assert read(str(tmp_path / "data" / "s.jsonl")) == \
        '{"cat": "x", "val": "1"}\n{"cat": "y", "val": "2"}\n'


def test_build_greeting_lists_the_tagged_files(data_ctx):
    ctx = data_ctx({"a.csv": "x\n1\n", "b.csv": "x\n2\n"},
                   selected={"a.csv", "b.csv"})
    greeting = CsvBuild(ctx).greeting
    assert "a.csv" in greeting and "b.csv" in greeting

    ctx.other_panel.selected = set()
    ctx.other_panel.move_to(0)
    assert "<nothing selected>" in CsvBuild(ctx).greeting


def test_build_with_no_command_does_nothing(data_ctx):
    ctx = data_ctx({"a.csv": "x\n1\n"}, selected={"a.csv"})
    assert CsvBuild(ctx).process("") is None


def test_build_rejects_an_unknown_verb(data_ctx):
    ctx = data_ctx({"a.csv": "x\n1\n"}, selected={"a.csv"})
    assert "unknown verb" in CsvBuild(ctx).process("wobble")


def test_build_needs_files_tagged(data_ctx):
    ctx = data_ctx({"a.csv": "x\n1\n"})
    ctx.other_panel.move_to(0)                  # the ".." entry
    with pytest.raises(RuntimeError, match="Tag at least 1"):
        CsvBuild(ctx).process("concat")


def test_build_join_needs_two_files(data_ctx):
    ctx = data_ctx({"a.csv": "id\n1\n"}, selected={"a.csv"})
    with pytest.raises(RuntimeError, match="Tag at least 2"):
        CsvBuild(ctx).process("join id")


def test_build_join_needs_a_key(data_ctx):
    ctx = data_ctx({"a.csv": "id\n1\n"}, selected={"a.csv"})
    assert CsvBuild(ctx).process("join") == "usage: join <key-column>"


def test_build_join_reports_how_many_rows_matched(data_ctx):
    ctx = data_ctx({"a.csv": "id,name\n1,alice\n2,bob\n",
                    "b.csv": "id,age\n1,30\n1,31\n"},
                   selected={"a.csv", "b.csv"})
    summary = CsvBuild(ctx).process("join id")
    # The later duplicate key wins, and only one left row matches.
    assert "1/2 rows matched" in summary


def test_build_writes_beside_an_existing_output(data_ctx, tmp_path):
    ctx = data_ctx({"a.csv": "x\n1\n", "dataset.csv": "already here\n"},
                   selected={"a.csv"})
    CsvBuild(ctx).process("concat")
    assert (tmp_path / "data" / "dataset.new.csv").exists()
    assert read(str(tmp_path / "data" / "dataset.csv")) == "already here\n"


def test_build_survives_a_pane_that_cannot_refresh(data_ctx, monkeypatch):
    ctx = data_ctx({"a.csv": "x\n1\n"}, selected={"a.csv"})

    def broken():
        raise RuntimeError("pane went away")

    monkeypatch.setattr(ctx.other_panel, "refresh", broken)
    assert "concat" in CsvBuild(ctx).process("concat")


def test_build_sample_by_count_and_percentage(data_ctx, tmp_path):
    rows = "n\n" + "".join(f"{i}\n" for i in range(10))
    ctx = data_ctx({"s.csv": rows}, selected={"s.csv"})
    plugin = CsvBuild(ctx)
    plugin.process("sample 3")
    assert read(str(tmp_path / "data" / "s.sample.csv")).count("\n") == 4
    plugin.process("sample 50%")
    assert read(str(tmp_path / "data" / "s.sample.new.csv")).count("\n") == 6


def test_build_sample_with_a_negative_count_writes_no_rows(data_ctx, tmp_path):
    ctx = data_ctx({"s.csv": "n\n1\n2\n"}, selected={"s.csv"})
    CsvBuild(ctx).process("sample -5")
    assert read(str(tmp_path / "data" / "s.sample.csv")) == "n\n"


@pytest.mark.parametrize("spec", ["sample lots", "sample many%"])
def test_build_sample_rejects_a_nonsense_size(data_ctx, spec):
    ctx = data_ctx({"s.csv": "n\n1\n"}, selected={"s.csv"})
    assert CsvBuild(ctx).process(spec) == "usage: sample <n|n%>"


def test_build_split_writes_one_file_per_value(data_ctx, tmp_path):
    ctx = data_ctx({"s.csv": "cat,v\nx,1\ny,2\nx,3\n,4\n"}, selected={"s.csv"})
    out = CsvBuild(ctx).process("split cat")
    assert "3 file(s)" in out[0]
    assert read(str(tmp_path / "data" / "s.cat-x.csv")) == "cat,v\nx,1\nx,3\n"
    # A blank value still gets a usable filename.
    assert (tmp_path / "data" / "s.cat-blank.csv").exists()


def test_build_split_sanitises_awkward_values(data_ctx, tmp_path):
    ctx = data_ctx({"s.csv": "cat,v\na/b c,1\n"}, selected={"s.csv"})
    CsvBuild(ctx).process("split cat")
    assert (tmp_path / "data" / "s.cat-a_b_c.csv").exists()


def test_build_split_needs_a_column(data_ctx):
    ctx = data_ctx({"s.csv": "a\n1\n"}, selected={"s.csv"})
    assert CsvBuild(ctx).process("split") == "usage: split <column>"


def test_build_from_jsonl(data_ctx, tmp_path):
    ctx = data_ctx({"s.jsonl": '{"a": 1, "b": "x"}\n{"a": 2}\n'},
                   selected={"s.jsonl"})
    CsvBuild(ctx).process("from-jsonl")
    assert read(str(tmp_path / "data" / "s.csv")) == "a,b\n1,x\n2,\n"


def test_build_from_jsonl_notes_a_truncated_source(data_ctx, monkeypatch):
    ctx = data_ctx({"s.jsonl": '{"a": 1}\n' * 500}, selected={"s.jsonl"})
    plugin = CsvBuild(ctx)
    monkeypatch.setitem(plugin.config, "max_bytes", 50)
    assert "source truncated" in plugin.process("from-jsonl")


def test_build_to_jsonl_writes_beside_an_existing_file(data_ctx, tmp_path):
    ctx = data_ctx({"s.csv": "a\n1\n", "s.jsonl": "already here\n"},
                   selected={"s.csv"})
    CsvBuild(ctx).process("to-jsonl")
    assert (tmp_path / "data" / "s.new.jsonl").exists()


def test_build_to_jsonl_survives_a_pane_that_cannot_refresh(data_ctx, monkeypatch):
    ctx = data_ctx({"s.csv": "a\n1\n"}, selected={"s.csv"})

    def broken():
        raise RuntimeError("pane went away")

    monkeypatch.setattr(ctx.other_panel, "refresh", broken)
    assert "to-jsonl" in CsvBuild(ctx).process("to-jsonl")


# -- group-by, on both engines -------------------------------------------------

GROUPBY_SOURCE = "cat,val\nx,1\nx,3\ny,10\nz,\n"


@pytest.fixture(params=["stdlib", "pandas"])
def build_engine(request, monkeypatch):
    """Run a group-by test on each engine, so both produce the same answer."""
    if request.param == "pandas":
        pytest.importorskip("pandas")
        monkeypatch.setattr("meridian_commander.plugins.csv_build.has_pandas",
                            lambda: True)
    else:
        monkeypatch.setattr("meridian_commander.plugins.csv_build.has_pandas",
                            lambda: False)
    return request.param


@pytest.mark.parametrize("agg, expected", [
    ("mean:val", "cat,mean_val\nx,2\ny,10\nz,\n"),
    ("sum:val", "cat,sum_val\nx,4\ny,10\nz,\n"),
    ("min:val", "cat,min_val\nx,1\ny,10\nz,\n"),
    ("max:val", "cat,max_val\nx,3\ny,10\nz,\n"),
])
def test_build_groupby_aggregations(data_ctx, tmp_path, build_engine, agg,
                                    expected):
    ctx = data_ctx({"s.csv": GROUPBY_SOURCE}, selected={"s.csv"})
    summary = CsvBuild(ctx).process(f"groupby cat {agg}")
    assert f"[{build_engine}]" in summary
    assert read(str(tmp_path / "data" / "s.groupby.csv")) == expected


def test_build_groupby_count_needs_no_value_column(data_ctx, tmp_path,
                                                   build_engine):
    ctx = data_ctx({"s.csv": GROUPBY_SOURCE}, selected={"s.csv"})
    CsvBuild(ctx).process("groupby cat count")
    assert read(str(tmp_path / "data" / "s.groupby.csv")) == \
        "cat,count\nx,2\ny,1\nz,1\n"


def test_build_groupby_usage_errors(data_ctx):
    ctx = data_ctx({"s.csv": GROUPBY_SOURCE}, selected={"s.csv"})
    plugin = CsvBuild(ctx)
    assert "usage: groupby" in plugin.process("groupby cat")
    assert "unknown agg" in plugin.process("groupby cat median:val")
    assert "needs a value column" in plugin.process("groupby cat mean")


def test_build_groupby_pads_short_rows(data_ctx, tmp_path, build_engine):
    ctx = data_ctx({"s.csv": "cat,val\nx,1\ny\n"}, selected={"s.csv"})
    CsvBuild(ctx).process("groupby cat count")
    assert read(str(tmp_path / "data" / "s.groupby.csv")) == \
        "cat,count\nx,1\ny,1\n"


def test_has_pandas_reports_what_is_installed(monkeypatch):
    from meridian_commander.plugins import csv_build

    assert csv_build.has_pandas() is True

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def no_pandas(name, *args, **kwargs):
        if name == "pandas":
            raise ImportError("no pandas here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", no_pandas)
    assert csv_build.has_pandas() is False


def test_clean_needs_a_file_selected(data_ctx):
    ctx = data_ctx({"t.csv": "a\n1\n"})
    ctx.other_panel.move_to(0)                  # the ".." entry, not a file
    with pytest.raises(RuntimeError, match="Select a CSV"):
        CsvClean(ctx).process("trim")


def test_clean_match_helper_ignores_an_operator_it_does_not_know():
    from meridian_commander.plugins.csv_clean import _match

    # The filter verb never produces this, but the helper stays defensive.
    assert _match("1", "~=", "1") is False


def test_apply_agg_helper_ignores_an_aggregate_it_does_not_know():
    from meridian_commander.plugins.csv_build import _apply_agg

    # The groupby verb validates the name first, but the helper stays defensive.
    assert _apply_agg("median", ["1", "2"]) == ""
