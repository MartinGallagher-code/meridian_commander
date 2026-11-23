# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""F1's help: what it says, how it is laid out, and how it scrolls (MC-032).

The help was one string passed to a message box that kept only the lines
that fitted -- on an 80x24 terminal, 17 of 143.  It is now titled sections
of (keys, description), laid out in two columns where the terminal is wide
enough, and it scrolls for whatever does not fit.
"""

from __future__ import annotations

import curses
from pathlib import Path

import pytest

from meridian_commander import dialogs, helptext
from meridian_commander.app import MENUS

from support import (
    _ScriptedDialogs,
    _ScriptedWindow,
    layout_lines,
    scripted_menu,
    with_curses_screen,
)


def _entries() -> dict[str, str]:
    return {keys: text for section in helptext.SECTIONS
            for keys, text in section.entries}


def _flat(text: str) -> str:
    return " ".join(text.split())


# -- what it says --------------------------------------------------------------

#: Every entry of the help as it stood before MC-032, as (keys, phrase): the
#: entry listed under those keys now must still say it.  Where the keys were
#: renamed by the move into sections, it is to the name under its section's
#: title: "F3 on .xlsx" is ".xlsx" under "Viewers: F3 on a file", and
#: "Options>Colours" is "Colours" under "Options menu and configuration".
WAS = [
    ("Tab", "switch active pane"),
    ("Up/Down j/k", "move cursor"),
    ("PgUp/PgDn", "page"),
    ("Home/End", "first / last"),
    ("Enter / Right", "enter dir / archive (zip, tar)"),
    ("Enter / Right", "run a script ('#!' line) or executable, with an "
                      "arguments prompt (Esc cancels); view the rest"),
    ("Backspace/Left", "parent directory"),
    ("Insert / Space", "tag file"),
    ("-", "untag all"),
    ("*", "invert"),
    ("+ / \\", "tag / untag the names matching a pattern"),
    ("a", "permissions, owner and group of what is tagged"),
    ("Ctrl-Q", "quick view: the other pane shows the cursor's file"),
    ("Ctrl-T", "sort order"),
    ("Ctrl-T", "name, natural (run_2 < run_10), ext, size, time"),
    ("l", "link in the other pane; on a link, change its target"),
    ("w", "open with the system's application (a copy, if remote or in an "
          "archive)"),
    ("F5/F6 Ctrl-B", "in the prompt: run it in the background"),
    ("J", "background jobs: p pause, c cancel, Enter errors"),
    ("J", "and the background commands: Enter shows one in this pane, k "
          "stops it, d removes an ended one"),
    ("&", "background commands: a saved one by its letter, Type a command, "
          "or Add/Edit/Delete the list (commands.ini)"),
    ("&", "It runs with your shell in its own directory, or this pane's "
          "(local panes only), and keeps running whatever the panes do"),
    ("&", "Shown in a pane, every key goes to it (Ctrl-C too); Ctrl-] then "
          "q hides it, k stops it, Tab, z and PgUp as in the terminal"),
    ("&", "Quitting stops them"),
    ("F8", "on a local pane: to the trash, or delete for good; in the trash "
           "itself, for good only"),
    ("Ctrl-U", "swap panes"),
    ("Ctrl-R", "reload panes"),
    ("Ctrl-G", "go to path"),
    ("Esc", "the menu bar (Alt+F, Alt+C, Alt+O, Alt+H open a menu by name; "
            "click one to open it)"),
    ("~", "home directory of this pane's location"),
    ("T", "the trash, in this pane: the one F8 uses from here (from a "
          "server or an archive, this computer's); < goes back"),
    ("< / >", "back / forward through the places this pane has been: '=', "
              "F2 and archives included"),
    ("=", "other pane: same directory and connection"),
    ("b", "presets: go to / save / delete a location"),
    ("/", "search this pane: type a name and the cursor jumps to the first "
          "one containing it (all lowercase ignores case)"),
    ("/", "Down/Up or Ctrl-N/P next/previous, Ins tag, Enter/F-keys act on "
          "it, / or Esc ends"),
    ("/", "Every letter and digit is part of the name, so no hotkey runs "
          "meanwhile"),
    (".", "show/hide hidden files (this pane)"),
    ("u", "directory sizes: each subdirectory shows what is under it, with "
          "a bar against the biggest here -- follow them down to find the "
          "space"),
    ("z", "single pane: this pane at full width, the other kept as it was "
          "(Tab flips to it, Ctrl-U brings it out); copy, move, extract and "
          "sync name it"),
    ("z", "h, Ctrl-] Tab in the terminal and plug-ins that use the other "
          "pane show both while they run"),
    ("z", "z again: two panes"),
    ("z", "In the terminal it is Ctrl-] z, and closing the terminal ends it"),
    ("t", "terminal in this pane: every key goes to the program; Ctrl-] "
          "then Tab other pane, z zoom to the full width and back, q close, "
          "PgUp scroll back (or the mouse wheel)"),
    ("!", "full-screen shell: the whole real terminal"),
    ("p / F11", "plug-in mode: run a plug-in in this pane"),
    ("f", "find files: browsable results (view/edit/goto)"),
    ("f", "p there, or Command>Find files to pane, lists every hit in this "
          "pane by its path below the directory searched: tag, F3/F4, a, l, "
          "w, r, F8 work on the real files, F5/F6 keep that path under the "
          "target"),
    ("f", "Ctrl-R drops hits gone since (no new search); <, .. or any move "
          "goes back to a directory, and < > history skips the list"),
    ("f", "F9, D, u and plug-ins on it are refused"),
    ("r", "rename the file under the cursor"),
    ("x", "extract the archive under the cursor (or the tagged ones) into "
          "the other pane, each in a folder of its own; F5/F6 into a pane "
          "that is inside an archive add the files to it"),
    ("n", "new empty file here (an existing one is restamped, never "
          "emptied)"),
    ("D", "compare both panes' files side by side: n/N next/previous "
          "difference, one scrollbar"),
    ("h", "this pane becomes head + tail of the file the other pane is on, "
          "and follows its cursor; +/- more/fewer lines, Esc gives the pane "
          "back"),
    (".xlsx", "spreadsheet grid: Tab sheet, / find, w width, F2/Ctrl-S save "
              "the sheet as a .tsv (or .csv)"),
    (".tsv .csv", "table: f filter, =/! this value, Bksp/0 unfilter, s "
                  "sort, Enter row, H header, c colour, # the comment lines "
                  "above the table, i column summary, F freeze columns, -/+ "
                  "hide/show columns, F2/Ctrl-S save the rows and columns "
                  "shown as a new file: .tsv .tab .psv or .csv, by its "
                  "name"),
    (".docx", "document view: headings, lists, tables, w wrap"),
    (".pptx", "slide browser: Tab slide, t notes, / find"),
    (".md", "markdown rendered; r shows the source"),
    ("code", ".py .sh .yml .json .toml .ini .conf and #! scripts in colour; "
             "c turns the colour off"),
    ("an image", "arrows pan, +/- zoom, c colour/ASCII, n frame"),
    (".pdf", "page text: Tab page, / find, i scanned image"),
    (".dc", "datacenter floor plan (a layout, not a dc script): arrows "
            "move, Enter zooms in to a row and a rack, Bksp/Esc out, c "
            "colours by role/tag/model/off"),
    ("C", "configuration: edit config.ini / plug-ins"),
    ("Colours", "turbo (blue), midnight (black), mono"),
    ("Editor", "F4 opens vi/vim/$EDITOR instead of the built-in editor"),
    ("Viewer", "F3 opens less/$PAGER instead of the built-in viewer, for "
               "plain text and code (a spreadsheet, table, deck, document, "
               "PDF, image or .dc layout keeps its own)"),
    ("View and edit in a pane",
     "F3 and F4 open in a pane, alone at full width, rather than "
     "full-screen (outside pagers and editors too, in the pane's terminal)"),
    ("View and edit in a pane",
     "z in a viewer, or Ctrl-] z, shows two panes: the file beside the "
     "listing it came from, and back"),
    ("View and edit in a pane",
     "Ctrl-] Tab gives the listing the keys, Tab comes back; F3 there shows "
     "the next file in the same view"),
    ("View and edit in a pane", "Closing puts the panes back as they were"),
    ("F1/1/?", "Help"),
    ("F2/2/o", "Open/Connect"),
    ("F3/3/v", "View"),
    ("F4/4/e", "Edit"),
    ("F5/5/c", "Copy"),
    ("F6/6/m", "Move"),
    ("F7/7", "Mkdir"),
    ("F8/8/d", "Delete"),
    ("F9/9/s", "Sync"),
    ("F10/0/q", "Quit"),
    ("F", "filter: show only the names matching a pattern"),
    ("F5/F6", "ask before replacing a file (overwrite, skip, if older; once "
              "or for all), keep permissions (Ctrl-P in the prompt drops "
              "them) and copy symbolic links as links"),
    ("click", "select"),
    ("double-click", "open"),
    ("wheel", "scroll"),
    ("right-click", "a context menu of actions"),
    ("F2/2/o", "SFTP, SSH (shell) or FTP; copy/move/sync work across local "
               "and remote panes alike"),
    ("F9/9/s", "both panes hold the newest version of every file"),
]


@pytest.mark.parametrize("keys, phrase", WAS)
def test_nothing_the_help_said_is_lost(keys, phrase):
    entries = _entries()
    assert keys in entries
    assert _flat(phrase) in _flat(entries[keys])


def test_the_help_still_says_what_it_is_and_why_the_aliases_exist():
    assert "two-pane terminal file manager" in helptext.INTRO
    notes = {section.title: section.note for section in helptext.SECTIONS}
    assert "digit and letter aliases, for terminals that swallow the " \
           "F-keys" in notes["Function keys"]


def test_every_key_is_listed_once():
    """Ctrl-T was listed twice; the two entries are one now."""
    keys = [keys for section in helptext.SECTIONS
            for keys, _ in section.entries]
    assert len(keys) == len(set(keys))
    assert keys.count("Ctrl-T") == 1


def test_the_sections_have_titles_and_the_descriptions_are_prose():
    titles = [section.title for section in helptext.SECTIONS]
    assert titles == ["Moving around", "Tagging", "Working on files",
                      "Panes and views", "Viewers: F3 on a file",
                      "Background work", "Options menu and configuration",
                      "Function keys", "Mouse"]
    for section in helptext.SECTIONS:
        assert section.entries
        for keys, text in section.entries:
            # Wrapped when drawn, to the width there is, so never by hand.
            assert keys and text and "\n" not in text
            assert "  " not in text.replace(".  ", ". ")


def test_the_help_is_ascii_so_its_columns_line_up():
    """Column arithmetic counts one cell per character."""
    text = helptext.INTRO + "".join(
        section.title + section.note + "".join(k + t for k, t in
                                               section.entries)
        for section in helptext.SECTIONS)
    assert text.isascii()


# -- how it is laid out --------------------------------------------------------

def _layout(screen_cols: int) -> list[dialogs.Column]:
    """The help's columns on a terminal that wide: the window takes two
    columns of it, and the frame and margins six more."""
    return dialogs.help_layout(helptext.SECTIONS, screen_cols - 8,
                               helptext.INTRO)


def _titles_in(rows) -> list[str]:
    titles = {section.title for section in helptext.SECTIONS}
    return [segment[1] for row in rows for segment in row
            if segment[1] in titles]


def test_on_the_owners_195_column_terminal_it_is_two_columns():
    (left_x, left_w, left), (right_x, right_w, right) = _layout(195)
    assert left_x == 0 and left_w == 92
    assert right_x == 92 + dialogs.HELP_GUTTER and right_w == 92
    # The intro heads the left column; the sections follow in order, split
    # between the columns where it evens their heights out.
    assert layout_lines(left)[0] == helptext.INTRO
    assert _titles_in(left) + _titles_in(right) == [
        section.title for section in helptext.SECTIONS]
    assert abs(len(left) - len(right)) < 20


def test_no_section_is_split_between_the_columns():
    left, right = _layout(195)
    for section in helptext.SECTIONS:
        keys = {keys for keys, _ in section.entries}
        in_left = {segment[1] for row in left[2] for segment in row} & keys
        in_right = {segment[1] for row in right[2] for segment in row} & keys
        assert not (in_left and in_right), section.title


@pytest.mark.parametrize("cols", [40, 80, 150, 151, 195, 400])
def test_no_line_runs_past_its_column(cols):
    columns = _layout(cols)
    for _, width, rows in columns:
        for row in rows:
            for offset, text, _ in row:
                assert offset + len(text) <= width, (cols, text)
    # And the right column starts past the left one's last cell.
    for (x, width, _), (next_x, _, _) in zip(columns, columns[1:]):
        assert x + width < next_x


@pytest.mark.parametrize("cols, count", [
    (80, 1), (120, 1), (150, 1),    # too narrow for two readable columns
    (151, 2), (195, 2), (400, 2),
])
def test_two_columns_only_where_each_stays_readable(cols, count):
    """Question 5's default: one column under about 150 columns wide."""
    assert len(_layout(cols)) == count


def test_a_column_stops_widening_on_a_very_wide_terminal():
    columns = _layout(1000)
    assert [width for _, width, _ in columns] == [dialogs.HELP_COLUMN_MAX] * 2
    assert len(dialogs.help_layout(helptext.SECTIONS, 1000)[0][2]) > 0


def test_the_keys_sit_beside_their_description_where_there_is_room():
    (_, _, rows), = _layout(80)
    lines = layout_lines(rows)
    assert "  Tab            switch active pane" in lines
    # Keys too long for the key column get a row of their own.
    at = lines.index("  View and edit in a pane")
    assert lines[at + 1].startswith(" " * 17 + "F3 and F4 open in a pane")


@pytest.mark.parametrize("width", range(1, 40))
def test_a_narrow_column_puts_the_keys_above_the_description(width):
    (_, col_w, rows), = dialogs.help_layout(helptext.SECTIONS, width)
    assert col_w == width
    for row in rows:
        for offset, text, role in row:
            if role != "dialoghot" and role != "dialogtitle":
                # Every description and note is wrapped into the width.
                assert offset + len(text) <= width
    entries = [row for row in rows if row and row[0][2] == "dialoghot"]
    beside = width >= (dialogs.HELP_INDENT + dialogs.HELP_KEYS
                       + dialogs.HELP_TEXT_MIN)
    assert all(len(row) == (2 if beside else 1) for row in entries
               if len(row[0][1]) < dialogs.HELP_KEYS)


def test_one_section_is_one_column_however_wide():
    one = helptext.SECTIONS[:1]
    assert len(dialogs.help_layout(one, 500)) == 1
    assert len(dialogs.help_layout(one, 500, "intro")) == 1


def test_a_help_with_no_intro_starts_with_its_first_section():
    left, _ = dialogs.help_layout(helptext.SECTIONS, 187)
    assert layout_lines(left[2])[0] == helptext.SECTIONS[0].title


def test_the_intro_is_never_a_column_on_its_own():
    """Heights alone would put a long intro by itself on the left here:
    beside a tall first section and a short last one, that is the most even
    split.  The intro introduces the first section, so they stay together."""
    intro = "word " * 400                     # some twenty rows at 92 wide
    tall = dialogs.HelpSection("Tall", (("k", "text"),) * 20)
    short = dialogs.HelpSection("Short", (("k", "text"),))
    left, right = dialogs.help_layout([tall, short], 187, intro)
    assert layout_lines(left[2])[0].startswith("word word")
    assert "Tall" in layout_lines(left[2])
    assert layout_lines(right[2]) == ["Short", "  k              text"]


# -- on a real screen ----------------------------------------------------------

def _show(monkeypatch, rows, cols, keys):
    """Open the help on a ``rows`` x ``cols`` screen and type ``keys``."""
    captured = scripted_menu(monkeypatch, keys)
    with_curses_screen(rows, cols, lambda stdscr: dialogs.help_screen(
        stdscr, "Help", helptext.SECTIONS, helptext.INTRO))
    return captured["window"]


def _painted(window) -> set:
    return {draw[2] for draw in window.drawn if len(draw) > 2}


def _footers(window) -> list[str]:
    bottom = window.getmaxyx()[0] - 1
    return [draw[2] for draw in window.drawn
            if len(draw) > 2 and draw[0] == bottom and draw[1] == 2]


def _every_row_was_painted(window) -> None:
    columns = dialogs.help_layout(helptext.SECTIONS,
                                  window.getmaxyx()[1] - 6, helptext.INTRO)
    painted = _painted(window)
    for _, _, rows in columns:
        for row in rows:
            for _, text, _ in row:
                assert text in painted, text


def test_every_line_can_be_reached_on_an_80x24_terminal(monkeypatch):
    """17 of 143 lines were shown here, and nothing said there were more."""
    window = _show(monkeypatch, 24, 80, [curses.KEY_NPAGE] * 20 + [27])
    assert window.getmaxyx() == (22, 78)
    _every_row_was_painted(window)
    painted = _painted(window)
    assert "Tab" in painted and "switch active pane" in painted
    assert "right-click" in painted and "a context menu of actions" in painted
    # The first picture said there was more, and how to get to it.
    total = len(_layout(80)[0][2])
    assert _footers(window)[0] == (
        f"╡ 17/{total}  Esc/Enter close  Up/Down PgUp/PgDn Home/End "
        "╞")
    assert _footers(window)[-1].startswith(f"╡ {total}/{total}")


def test_on_195x56_it_is_two_columns_that_scroll(monkeypatch):
    window = _show(monkeypatch, 56, 195, [curses.KEY_END, 27])
    height, width = window.getmaxyx()
    assert (height, width) == (54, 193)
    body = height - 5
    (_, col_w, left), (right_x, _, right) = _layout(195)
    total = max(len(left), len(right))
    assert total > body            # even here it does not all fit
    first = [d for d in window.drawn if len(d) > 2][:400]
    # Both columns side by side on the first row.
    assert (1, 3, helptext.INTRO) in [d[:3] for d in first]
    assert (1, 3 + right_x, helptext.SECTIONS[4].title) in [
        d[:3] for d in first]
    # Nothing of the left column runs into the right one.
    for y, x, text, *_ in (d for d in window.drawn if len(d) > 2):
        if 1 <= y <= body and 3 <= x < 3 + right_x:
            assert x + len(text) <= 3 + col_w, text
    # End: the left column's last row is on the view's last row.
    last = left[-1][-1]
    assert (body, 3 + last[0], last[1]) in [d[:3] for d in window.drawn
                                            if len(d) > 2]
    _every_row_was_painted(window)
    assert _footers(window)[-1].startswith(f"╡ {total}/{total}")


@pytest.mark.parametrize("key", [27, 10, 13, curses.KEY_ENTER])
def test_esc_and_enter_close_the_help(monkeypatch, key):
    window = _show(monkeypatch, 56, 195, [key, ord("x")])
    assert window._keys == [ord("x")]


@pytest.mark.parametrize("key", [
    curses.KEY_DOWN, curses.KEY_NPAGE, curses.KEY_HOME, curses.KEY_END,
    ord("x"), ord(" "), curses.KEY_F1, curses.KEY_MOUSE,
])
def test_every_other_key_leaves_the_help_open(monkeypatch, key):
    window = _show(monkeypatch, 56, 195, [key, 27])
    assert window._keys == []
    assert len(_footers(window)) == 2        # drawn again after the key


@pytest.mark.parametrize("key, top", [
    (curses.KEY_DOWN, 1), (ord("j"), 1), (curses.KEY_NPAGE, 49),
])
def test_the_scroll_keys_move_the_help(monkeypatch, key, top):
    window = _show(monkeypatch, 56, 195, [key, 27])
    total = max(len(rows) for _, _, rows in _layout(195))
    assert _footers(window)[-1].startswith(
        f"╡ {min(top + 49, total)}/{total}")


def test_a_help_that_fits_still_closes_only_on_esc_or_enter(monkeypatch):
    """The owner's answer: Esc or Enter closes the help, whatever its size."""
    window = _show(monkeypatch, 120, 250, [ord("x"), curses.KEY_DOWN, 10])
    assert window._keys == []
    assert _footers(window) == ["╡ Esc/Enter close ╞"] * 3
    _every_row_was_painted(window)


@pytest.mark.parametrize("rows, cols", [
    (1, 1), (2, 2), (5, 10), (6, 40), (24, 10), (3, 200), (10, 45),
    (24, 152), (30, 160),
])
def test_the_help_on_a_tiny_or_odd_screen_does_not_crash(monkeypatch, rows,
                                                         cols):
    keys = [curses.KEY_DOWN, curses.KEY_NPAGE, curses.KEY_END,
            curses.KEY_PPAGE, curses.KEY_UP, curses.KEY_HOME, 27]
    window = _show(monkeypatch, rows, cols, keys)
    assert window._keys == []


def test_a_resize_while_the_help_is_open_draws_it_for_the_new_size(
        monkeypatch):
    keys = [curses.KEY_NPAGE, curses.KEY_RESIZE, curses.KEY_END, 27]
    windows = []
    real_center = dialogs._center

    class Resizing(_ScriptedWindow):
        def getch(self):
            key = super().getch()
            if key == curses.KEY_RESIZE:
                # What the terminal does before curses reports the key.
                curses.resizeterm(56, 195)
            return key

    def center(stdscr, height, width):
        window = Resizing(real_center(stdscr, height, width), [])
        window._keys = keys        # one script, whichever window reads it
        windows.append(window)
        return window

    monkeypatch.setattr(dialogs, "_center", center)
    sizes = with_curses_screen(24, 80, lambda stdscr: (
        dialogs.help_screen(stdscr, "Help", helptext.SECTIONS,
                            helptext.INTRO),
        [window.getmaxyx() for window in windows])[1])
    assert sizes == [(22, 78), (54, 193)]
    assert keys == []
    # The new window has the wide terminal's two columns: section titles
    # drawn where the right-hand one starts.
    (_, _, _), (right_x, _, right) = _layout(195)
    titles = set(_titles_in(right))
    assert any(d[1] == 3 + right_x and d[2] in titles
               for d in windows[1].drawn if len(d) > 2)


# -- how it is opened ----------------------------------------------------------

@pytest.mark.parametrize("key", [curses.KEY_F1, ord("1"), ord("?")])
def test_f1_1_and_question_mark_open_the_help(app, monkeypatch, key):
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(key)
    assert scripted.helps == [list(helptext.SECTIONS)]
    assert scripted.messages[-1][0] == "Help"


def test_help_contents_in_the_menu_opens_the_help(app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch)
    help_menu = next(menu for menu in MENUS if menu["name"] == "help")
    contents = next(item for item in help_menu["items"]
                    if item.get("key") == "F1")
    app._dispatch(contents["name"])
    assert scripted.helps == [list(helptext.SECTIONS)]


@pytest.mark.parametrize("cols, window_width", [
    # One column is at most HELP_COLUMN_MAX wide, so the window is too.
    (150, dialogs.HELP_COLUMN_MAX + 6),
    # From 151 the two columns take the whole width the screen allows.
    (151, 149),
])
def test_two_columns_from_151_columns_on_a_real_screen(monkeypatch, cols,
                                                       window_width):
    window = _show(monkeypatch, 30, cols, [27])
    assert window.getmaxyx()[1] == window_width


# -- what the README and the manual say ----------------------------------------

def test_the_readme_and_the_manual_say_how_to_move_through_the_help():
    root = Path(__file__).resolve().parents[1]
    readme = _flat((root / "README.md").read_text(encoding="utf-8"))
    assert "two columns on a terminal 151 columns wide or more" in readme
    assert "`Esc` or `Enter` closes it" in readme
    manual = root / "docs" / "usage.md"
    if not manual.exists():
        pytest.skip("the manual is not shipped in the source distribution")
    text = _flat(manual.read_text(encoding="utf-8"))
    for phrase in ("**The help** (`F1`, `1`, `?`, or **Help ▸ Contents**)",
                   "On a terminal 151 columns wide or more",
                   "`PgUp`/`PgDn` a page, `Home`/`End` to either end",
                   "`Esc` or `Enter` closes it; any other key leaves it open",
                   "A message that fits closes with any key"):
        assert phrase in text
