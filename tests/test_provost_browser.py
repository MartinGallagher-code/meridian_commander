"""The provost store browser plug-in, over a real store built on disk."""

from __future__ import annotations

import base64
import curses
import types

import pytest

from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.plugin_api import PluginContext
from meridian_commander.plugins import provost_browser as mod
from meridian_commander.plugins.provost_browser import (
    ProvostBrowser, align_rows, decode_raw, find_store, is_store,
    parse_meta, parse_tsv, provenance_lookup,
)

from support import with_curses_screen, write


RAW = base64.b64encode(b"one\ttwo\nend").decode()

DF_TSV = ("#\tfilesystem\tsize\tuse\n"
          "1\t/dev/sda1\t100G\t42%\n"
          "2\ttmpfs\t16G\t1%\n"
          "3\t/dev/sdb1\t2T\t93%\n")

META_1 = ("id\tc0001\nts\t2026-08-01T10:00:00Z\nsource\trun\n"
          "command\tdf -h\nrc\t0\nstrategy\tdf\nconfidence\t95\n"
          "dataset\tdf\nrow_ids\t1-3\n")

META_2 = ("id\tc0002\nts\t2026-08-02T11:00:00Z\nsource\tstdin\n"
          "command\t\nrc\t\nstrategy\t(none)\nconfidence\t0\n"
          "dataset\tunsorted\nrow_ids\t4-4\ncontext\tcontext.txt\n")


def make_store(root) -> str:
    """A provost store with two datasets, two captures and provenance."""
    store = str(root / "store")
    write(f"{store}/.provostrc", "provost workspace (store model test)\n")
    write(f"{store}/datasets/df.tsv", DF_TSV)
    write(f"{store}/datasets/unsorted.tsv", f"#\traw\n4\t{RAW}\n")
    write(f"{store}/captures/c0001/meta.tsv", META_1)
    write(f"{store}/captures/c0001/output.txt",
          "Filesystem Size Use\n/dev/sda1 100G 42%\n")
    write(f"{store}/captures/c0002/meta.tsv", META_2)
    write(f"{store}/captures/c0002/output.txt", "one\ttwo\nend\n")
    write(f"{store}/captures/c0002/context.txt", "#% host=box\n")
    write(f"{store}/provenance.tsv",
          "1\tdf\tc0001\n2\tdf\tc0001\n3\tdf\tc0001\n4\tunsorted\tc0002\n")
    return store


class _Panel:
    """Enough of a panel for the browser: a backend, a path, and moving."""

    def __init__(self, fs, path: str) -> None:
        self.fs = fs
        self.path = path

    def chdir(self, path: str) -> None:
        self.path = path


def _ctx(path: str):
    fs = LocalFileSystem()
    return PluginContext(app=None, own_panel=_Panel(fs, path),
                         other_panel=_Panel(fs, path))


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path)


@pytest.fixture
def plugin(store, monkeypatch):
    monkeypatch.delenv("PROVOST_WORKSPACE", raising=False)
    monkeypatch.delenv("PROVOST_HOME", raising=False)
    return ProvostBrowser(_ctx(store))


def keys(plugin, *ks):
    for k in ks:
        plugin.handle_key(k if isinstance(k, int) else ord(k))


# -- parsing helpers -------------------------------------------------------------

def test_parse_tsv_splits_rows_and_cells():
    assert parse_tsv("a\tb\n1\t2\n\n") == [["a", "b"], ["1", "2"]]


def test_parse_meta_keeps_order_and_empty_values():
    pairs = parse_meta("id\tc0001\ncommand\t\nnote\ta\tb\n")
    assert pairs[0] == ("id", "c0001")
    assert pairs[1] == ("command", "")
    assert pairs[2] == ("note", "a\tb")   # only the first tab separates


def test_decode_raw_unescapes_to_one_line():
    assert decode_raw(RAW) == "one\\ttwo\\nend"


def test_decode_raw_leaves_a_non_base64_value_alone():
    assert decode_raw("not base64!") == "not base64!"


def test_provenance_lookup():
    text = "1\tdf\tc0001\n4\tunsorted\tc0002\n"
    assert provenance_lookup(text, "4") == ("unsorted", "c0002")
    assert provenance_lookup(text, "#1") == ("df", "c0001")
    assert provenance_lookup(text, "9") is None


def test_align_rows_pads_to_the_widest_cell():
    lines = align_rows([["ab", "c"], ["x", "long"]])
    assert lines == ["ab  c", "x   long"]


def test_align_rows_clips_an_overlong_cell():
    lines = align_rows([["x" * 100, "y"]])
    assert lines[0].startswith("x" * (mod.MAX_COL_WIDTH - 1) + "…")


def test_align_rows_tolerates_ragged_rows():
    lines = align_rows([["a", "b", "c"], ["1"]])
    assert lines[1] == "1"


def test_parse_meta_skips_blank_lines():
    assert parse_meta("id\tc0001\n\nx\ty\n") == [("id", "c0001"), ("x", "y")]


def test_align_rows_of_nothing_is_nothing():
    assert align_rows([]) == []


def test_mtime_text_handles_missing_and_absurd_values():
    assert mod._mtime_text(None) == ""
    assert mod._mtime_text(1e30) == ""


# -- store discovery ---------------------------------------------------------------

def test_is_store_accepts_markers_and_shape(tmp_path, store):
    fs = LocalFileSystem()
    assert is_store(fs, store)
    bare = tmp_path / "bare"
    (bare / "datasets").mkdir(parents=True)
    assert not is_store(fs, str(bare))
    (bare / "captures").mkdir()
    assert is_store(fs, str(bare))
    assert not is_store(fs, str(tmp_path / "missing"))


def test_is_store_survives_a_failing_backend():
    class _Broken:
        def is_dir(self, path):
            raise OSError("gone")

        def join(self, *parts):
            return "/".join(parts)

    assert not is_store(_Broken(), "/anything")


def test_find_store_prefers_the_configured_path(store):
    found, how = find_store(LocalFileSystem(), "/", {}, configured=store)
    assert (found, how) == (store, "configured")


def test_find_store_honours_workspace_env(store, tmp_path):
    env = {"PROVOST_WORKSPACE": store}
    found, how = find_store(LocalFileSystem(), str(tmp_path), env)
    assert (found, how) == (store, "PROVOST_WORKSPACE")


def test_find_store_takes_the_pane_directory_itself(store):
    found, how = find_store(LocalFileSystem(), store, {})
    assert (found, how) == (store, "this directory")


def test_find_store_finds_the_store_from_inside_it(store):
    """Standing in the store's own captures/ still resolves to the store."""
    fs = LocalFileSystem()
    found, how = find_store(fs, fs.join(store, "captures"), {})
    assert found == store
    assert how == f"store: {store}"


def test_find_store_walks_up_to_a_controlled_directory(store, tmp_path):
    project = tmp_path / "proj"
    deep = project / "a" / "b"
    deep.mkdir(parents=True)
    write(str(project / ".provost" / "CONTROLLED"), str(project))
    found, how = find_store(LocalFileSystem(), str(deep), {})
    assert found == str(project / ".provost")
    assert how == f"controlled: {project}"


def test_find_store_falls_back_to_provost_home(store, tmp_path):
    env = {"PROVOST_HOME": store}
    found, how = find_store(LocalFileSystem(), str(tmp_path), env)
    assert (found, how) == (store, "global")


def test_find_store_reports_everywhere_it_looked(tmp_path):
    env = {"PROVOST_HOME": str(tmp_path / "nowhere"),
           "PROVOST_WORKSPACE": str(tmp_path / "ws")}
    found, tried = find_store(LocalFileSystem(), str(tmp_path), env,
                              configured=str(tmp_path / "cfg"))
    assert found is None
    assert "cfg" in tried and "ws" in tried and "nowhere" in tried
    assert ".provost" in tried


def _remote_fs(home=lambda: "/root"):
    return types.SimpleNamespace(
        scheme="sftp",
        is_dir=lambda p: False, exists=lambda p: False,
        join=lambda *p: "/".join(p), normpath=lambda p: p,
        parent=lambda p: p, home=home)


def test_find_store_ignores_env_on_a_remote_pane(store, tmp_path):
    env = {"PROVOST_WORKSPACE": store, "PROVOST_HOME": store}
    found, _ = find_store(_remote_fs(), str(tmp_path), env)
    assert found is None


def test_find_store_survives_a_backend_with_no_home(tmp_path):
    def broken_home():
        raise OSError("no home")

    found, _ = find_store(_remote_fs(home=broken_home), str(tmp_path), {})
    assert found is None


# -- opening ------------------------------------------------------------------------

def test_no_store_anywhere_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("PROVOST_WORKSPACE", raising=False)
    monkeypatch.setenv("PROVOST_HOME", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="No provost store"):
        ProvostBrowser(_ctx(str(tmp_path)))


def test_opens_on_the_dataset_list(plugin):
    view = plugin.stack[-1]
    assert view.kind == "datasets"
    assert view.items == ["df", "unsorted"]
    assert "DATASET" in view.header and "SIZE" in view.header


def test_an_empty_store_still_opens(tmp_path, monkeypatch):
    monkeypatch.delenv("PROVOST_WORKSPACE", raising=False)
    empty = tmp_path / "empty"
    write(str(empty / ".provostrc"), "x\n")
    plugin = ProvostBrowser(_ctx(str(empty)))
    assert "no datasets yet" in plugin.stack[-1].lines[0]
    plugin.handle_key(10)                     # Enter on the placeholder
    assert len(plugin.stack) == 1


# -- the dataset table ----------------------------------------------------------------

def test_enter_opens_the_dataset_as_a_table(plugin):
    keys(plugin, 10)
    view = plugin.stack[-1]
    assert view.kind == "table"
    assert "df" in view.title and "3 rows" in view.title
    assert view.header.split()[:2] == ["#", "filesystem"]
    assert any("/dev/sda1" in line for line in view.lines)


def test_the_unsorted_dataset_is_decoded(plugin):
    keys(plugin, "t")
    view = plugin.stack[-1]
    assert view.kind == "table"
    assert any("one\\ttwo\\nend" in line for line in view.lines)


def test_triage_key_reports_an_empty_queue(plugin, store):
    import os

    os.remove(f"{store}/datasets/unsorted.tsv")
    keys(plugin, "r", "t")
    assert plugin.stack[-1].kind == "datasets"
    assert "triage queue empty" in plugin.status


def test_a_truncated_dataset_is_flagged(plugin, monkeypatch):
    monkeypatch.setattr(mod, "MAX_DATASET_BYTES", 40)
    keys(plugin, 10)
    view = plugin.stack[-1]
    assert view.truncated
    assert "[truncated]" in plugin._footer(view)


def test_filter_narrows_the_table(plugin):
    keys(plugin, 10, "/", "s", "d")           # filter "sd"
    view = plugin.stack[-1]
    assert view.filter == "sd"
    assert len(view.lines) == 2               # sda1 and sdb1
    keys(plugin, 27)                          # Esc clears, stays in the view
    assert view.filter == "" and len(view.lines) == 3
    assert len(plugin.stack) == 2


def test_filter_backspace_and_ctrl_u(plugin):
    keys(plugin, "/", "d", "f")
    view = plugin.stack[-1]
    keys(plugin, curses.KEY_BACKSPACE)
    assert view.filter == "d"
    keys(plugin, 21)                          # Ctrl-U
    assert view.filter == ""
    keys(plugin, 10)                          # Enter leaves filter mode
    assert plugin.mode == "list"


def test_filter_mode_still_lets_tab_switch_panes(plugin):
    keys(plugin, "/")
    assert plugin.handle_key(9) is None


def test_non_tsv_files_in_datasets_are_ignored(plugin, store, tmp_path):
    write(f"{store}/datasets/notes.txt", "not a dataset\n")
    (tmp_path / "store" / "datasets" / "sub").mkdir()
    keys(plugin, "r")
    assert plugin.stack[-1].items == ["df", "unsorted"]


def test_a_row_without_a_number_has_no_source(plugin, store):
    write(f"{store}/datasets/aliases.tsv", "name\ttarget\nll\tls -l\n")
    keys(plugin, "r", 10)                     # "aliases" sorts first
    keys(plugin, 10)
    assert plugin.stack[-1].kind == "table"
    assert "no #N index" in plugin.status


# -- provenance: row -> source ----------------------------------------------------------

def test_source_of_the_row_under_the_cursor(plugin):
    keys(plugin, 10, curses.KEY_DOWN, "s")    # df row #2
    view = plugin.stack[-1]
    assert view.kind == "capture"
    assert "row #2" in view.title and "c0001" in view.title
    assert any("original source (capture c0001)" in line for line in view.lines)
    assert any("/dev/sda1 100G" in line for line in view.lines)


def test_a_row_with_no_provenance_reports_it(plugin, store):
    write(f"{store}/provenance.tsv", "9\tdf\tc0009\n")
    keys(plugin, 10, 10)
    assert len(plugin.stack) == 2             # stayed on the table
    assert "no provenance for row #1" in plugin.status


def test_a_missing_provenance_file_reports_it(plugin, store):
    import os

    os.remove(f"{store}/provenance.tsv")
    keys(plugin, 10, "s")
    assert len(plugin.stack) == 2
    assert "no provenance:" in plugin.status


def test_provenance_to_a_vanished_capture_still_shows_a_view(plugin, store):
    write(f"{store}/provenance.tsv", "1\tdf\tc0009\n")
    keys(plugin, 10, 10)
    view = plugin.stack[-1]
    assert view.kind == "capture"
    assert any("no metadata" in line for line in view.lines)
    assert any("no stored output" in line for line in view.lines)


# -- the captures log ---------------------------------------------------------------------

def test_log_lists_captures_newest_first(plugin):
    keys(plugin, "l")
    view = plugin.stack[-1]
    assert view.kind == "log"
    assert view.items == ["c0002", "c0001"]
    assert "STRATEGY" in view.header
    assert "df -h" in view.lines[1]


def test_enter_on_a_capture_shows_meta_context_and_output(plugin):
    keys(plugin, "l", curses.KEY_DOWN, 10)    # c0001... wait, newest first
    view = plugin.stack[-1]
    assert view.kind == "capture"
    assert "capture c0001" in view.title
    assert "id: c0001" in view.lines[0]
    assert any("df -h" in line for line in view.lines)


def test_a_capture_with_context_shows_it(plugin):
    keys(plugin, "l", 10)                     # c0002, the newest
    view = plugin.stack[-1]
    assert any("----- context -----" in line for line in view.lines)
    assert any("#% host=box" in line for line in view.lines)


def test_a_capture_missing_its_output_says_so(plugin, store):
    import os

    os.remove(f"{store}/captures/c0002/output.txt")
    keys(plugin, "l", 10)
    assert any("no stored output" in line for line in plugin.stack[-1].lines)


def test_an_unreadable_context_is_skipped(plugin, store, tmp_path):
    import os

    os.remove(f"{store}/captures/c0002/context.txt")
    (tmp_path / "store" / "captures" / "c0002" / "context.txt").mkdir()
    keys(plugin, "l", 10)
    view = plugin.stack[-1]
    assert not any("----- context -----" in line for line in view.lines)
    assert any("original source" in line for line in view.lines)


def test_log_skips_strays_and_reloads(plugin, store, tmp_path):
    write(f"{store}/captures/README", "not a capture\n")
    (tmp_path / "store" / "captures" / "c0003").mkdir()  # no meta.tsv
    keys(plugin, "l", "r")
    assert plugin.stack[-1].items == ["c0002", "c0001"]


def test_a_store_without_captures_shows_an_empty_log(tmp_path, monkeypatch):
    monkeypatch.delenv("PROVOST_WORKSPACE", raising=False)
    empty = tmp_path / "empty"
    write(str(empty / ".provostrc"), "x\n")
    plugin = ProvostBrowser(_ctx(str(empty)))
    keys(plugin, "l")
    view = plugin.stack[-1]
    assert "no captures yet" in view.lines[0]
    plugin.handle_key(10)                     # Enter on the placeholder
    assert plugin.stack[-1] is view


# -- navigation ------------------------------------------------------------------------------

def test_backspace_pops_and_esc_at_the_root_closes(plugin):
    keys(plugin, 10)
    assert len(plugin.stack) == 2
    keys(plugin, curses.KEY_BACKSPACE)
    assert len(plugin.stack) == 1
    assert plugin.handle_key(27) is False


def test_esc_below_the_root_goes_back_instead_of_closing(plugin):
    keys(plugin, 10)
    assert plugin.handle_key(27) is True
    assert len(plugin.stack) == 1


def test_f10_closes_from_anywhere(plugin):
    keys(plugin, 10)
    assert plugin.handle_key(curses.KEY_F10) is False


def test_tab_is_left_to_the_application(plugin):
    assert plugin.handle_key(9) is None


def test_cursor_motion_and_bounds(plugin):
    view = plugin.stack[-1]
    keys(plugin, curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_DOWN)
    assert view.cursor == 1                   # clamped to the last item
    keys(plugin, curses.KEY_UP, curses.KEY_UP)
    assert view.cursor == 0
    keys(plugin, curses.KEY_END)
    assert view.cursor == 1
    keys(plugin, curses.KEY_HOME)
    assert view.cursor == 0
    keys(plugin, curses.KEY_NPAGE)
    assert view.cursor == 1
    keys(plugin, curses.KEY_PPAGE)
    assert view.cursor == 0


def test_a_text_view_scrolls_instead_of_selecting(plugin):
    keys(plugin, "l", 10)
    view = plugin.stack[-1]
    assert not view.selectable
    keys(plugin, curses.KEY_DOWN, curses.KEY_DOWN)
    assert view.scroll == 2
    keys(plugin, curses.KEY_UP)
    assert view.scroll == 1
    keys(plugin, 10)                          # Enter selects nothing here
    assert plugin.stack[-1] is view


def test_horizontal_panning(plugin):
    keys(plugin, 10)
    view = plugin.stack[-1]
    keys(plugin, curses.KEY_RIGHT, curses.KEY_RIGHT)
    assert view.hscroll == 16
    keys(plugin, curses.KEY_LEFT, curses.KEY_LEFT, curses.KEY_LEFT)
    assert view.hscroll == 0


def test_reload_picks_up_a_new_dataset(plugin, store):
    write(f"{store}/datasets/new.tsv", "#\tx\n5\ty\n")
    keys(plugin, "r")
    assert "new" in plugin.stack[-1].items


# -- following the other pane ------------------------------------------------------

def _second_store(tmp_path) -> str:
    """A controlled directory with a store of its own, unlike the fixture's."""
    project = tmp_path / "proj"
    (project / "src").mkdir(parents=True)
    ws = project / ".provost"
    write(str(ws / "CONTROLLED"), str(project))
    write(str(ws / "datasets" / "ps.tsv"), "#\tpid\tcmd\n7\t1\tinit\n")
    write(str(ws / "provenance.tsv"), "7\tps\tc0001\n")
    return str(project)


def test_the_store_follows_the_pane_to_a_controlled_directory(plugin, tmp_path):
    project = _second_store(tmp_path)
    plugin.ctx.other_panel.chdir(project)
    plugin.tick()
    assert plugin.store == str(tmp_path / "proj" / ".provost")
    assert plugin.stack[-1].items == ["ps"]          # rebuilt on the new store
    assert "followed pane" in plugin.status


def test_it_follows_from_a_subdirectory_of_a_controlled_directory(plugin,
                                                                  tmp_path):
    _second_store(tmp_path)
    plugin.ctx.other_panel.chdir(str(tmp_path / "proj" / "src"))
    plugin.tick()
    assert plugin.store == str(tmp_path / "proj" / ".provost")


def test_tick_does_nothing_while_the_pane_stays_put(plugin, monkeypatch):
    calls = []
    monkeypatch.setattr(mod, "find_store",
                        lambda *a, **k: calls.append(a) or (None, ""))
    view = plugin.stack[-1]
    plugin.tick()
    plugin.tick()
    assert calls == []                                # no filesystem work
    assert plugin.stack[-1] is view


def test_moving_outside_any_store_keeps_the_current_one(plugin, tmp_path,
                                                        monkeypatch):
    monkeypatch.delenv("PROVOST_HOME", raising=False)
    monkeypatch.setenv("PROVOST_HOME", str(tmp_path / "nowhere"))
    was = plugin.store
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    plugin.ctx.other_panel.chdir(str(elsewhere))
    plugin.tick()
    assert plugin.store == was                        # view survives the move
    assert "no store under" in plugin.status


def test_moving_within_the_same_store_does_not_rebuild(plugin, store):
    import os

    os.mkdir(f"{store}/captures/sub")
    view = plugin.stack[-1]
    plugin.ctx.other_panel.chdir(f"{store}/captures")
    plugin.tick()
    assert plugin.stack[-1] is view                   # same store, same view
    assert plugin.status == ""


def test_reload_catches_up_without_waiting_for_the_timer(plugin, tmp_path):
    project = _second_store(tmp_path)
    plugin.ctx.other_panel.chdir(project)
    keys(plugin, "r")
    assert plugin.store == str(tmp_path / "proj" / ".provost")
    assert plugin.stack[-1].items == ["ps"]


# -- drawing ------------------------------------------------------------------------------------

def _paint(plugin, h=20, w=60):
    def paint(stdscr):
        plugin.draw(stdscr, 0, 0, h, w)
        return [stdscr.instr(r, 0, w).decode(errors="replace")
                for r in range(h)]

    return with_curses_screen(24, 62, paint)


def test_draw_renders_title_header_rows_and_footer(plugin):
    lines = _paint(plugin)
    assert "[provost]" in lines[0]
    assert "DATASET" in lines[1]
    assert any("df" in line for line in lines[2:])
    assert "Enter open" in lines[19]


def test_draw_shows_the_filter_prompt_and_crumb(plugin):
    keys(plugin, 10, "/", "z")
    lines = _paint(plugin)
    assert "filter> z" in lines[19]
    assert "(nothing matches)" in "".join(lines)
    assert ">" in lines[0]                    # breadcrumb of the view stack


def test_draw_pans_horizontally(plugin):
    keys(plugin, 10)
    before = _paint(plugin)[1]
    keys(plugin, curses.KEY_RIGHT)
    after = _paint(plugin)[1]
    assert before != after


def test_draw_keeps_the_cursor_visible(plugin, store):
    rows = "\n".join(f"{i}\tfs{i}\t1G\t1%" for i in range(1, 40))
    write(f"{store}/datasets/big.tsv", "#\tfilesystem\tsize\tuse\n"
          + rows + "\n")
    keys(plugin, "r", 10)                     # "big" sorts first; open it
    keys(plugin, curses.KEY_NPAGE, curses.KEY_NPAGE)
    view = plugin.stack[-1]
    lines = _paint(plugin, h=10)
    assert view.scroll == view.cursor - 7 + 1  # title+header+footer take 3
    keys(plugin, curses.KEY_END)
    lines = _paint(plugin, h=10)
    assert any(view.lines[view.cursor].strip()[:10] in line
               for line in lines)
    keys(plugin, curses.KEY_PPAGE)            # cursor now above the window
    lines = _paint(plugin, h=10)
    assert any(view.lines[view.cursor].strip()[:10] in line
               for line in lines)
    assert view.scroll == view.cursor


def test_draw_clamps_a_text_views_scroll(plugin):
    keys(plugin, "l", 10)                     # capture c0002's detail
    view = plugin.stack[-1]
    keys(plugin, curses.KEY_END)              # scroll pinned past the end
    _paint(plugin, h=10)
    assert view.scroll == max(0, len(view.lines) - 8)   # title + footer


def test_draw_survives_a_tiny_pane(plugin):
    with_curses_screen(24, 62, lambda s: plugin.draw(s, 0, 0, 2, 10))


def test_status_line_replaces_the_key_hints(plugin):
    plugin.status = "boom"
    assert plugin._footer(plugin.stack[-1]).strip() == "boom"
