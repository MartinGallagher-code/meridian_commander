# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""a: show and change permissions, owner and group; the long listing (MC-014)."""

from __future__ import annotations

import curses
import os
import stat as stat_mod

import pytest

from meridian_commander import config, dialogs
from meridian_commander import filesystems as fsmod
from meridian_commander.app import MENUS, App
from meridian_commander.attributes import (
    AttributeChange,
    apply_change,
    common_bits,
    octal_text,
)
from meridian_commander.filesystems import DirEntry, LocalFileSystem
from meridian_commander.operations import OperationCancelled

from support import (
    _ScriptedDialogs,
    _StubScreen,
    scripted_menu,
    with_curses_screen,
    write,
    write_zip,
)


def _mode(path) -> int:
    return stat_mod.S_IMODE(os.stat(path).st_mode)


def _entry(fs, path):
    return (str(path), fs.stat(str(path)))


# -- working out a change -------------------------------------------------------

def test_common_bits_and_their_octal():
    on, off = common_bits([0o755, 0o751])
    assert on == 0o751 and off == 0o022
    assert octal_text(on, off) == "75?"
    assert octal_text(*common_bits([0o644])) == "644"
    assert octal_text(0o700, 0o000) == "7??"


def test_a_mixed_bit_stays_each_entrys_own():
    change = AttributeChange(on=0o100, off=0o002)
    assert change.mode_for(0o100644) == 0o744
    assert change.mode_for(0o100606) == 0o704


# -- making it ------------------------------------------------------------------

def test_one_file(fs, tmp_path):
    write(str(tmp_path / "run.sh"), "x")
    os.chmod(tmp_path / "run.sh", 0o644)
    done, failures = apply_change(
        fs, [_entry(fs, tmp_path / "run.sh")],
        AttributeChange(on=0o755, off=0o022))
    assert (done, failures) == (1, [])
    assert _mode(tmp_path / "run.sh") == 0o755


def test_recursive_three_levels_and_links_left_alone(fs, tmp_path):
    for rel in ("top/a", "top/mid/b", "top/mid/low/c"):
        write(str(tmp_path / rel), "x")
    write(str(tmp_path / "elsewhere"), "x")
    os.chmod(tmp_path / "elsewhere", 0o644)
    os.symlink(tmp_path / "elsewhere", tmp_path / "top" / "link")
    done, failures = apply_change(
        fs, [_entry(fs, tmp_path / "top")],
        AttributeChange(on=0o700, off=0o077, recursive=True))
    assert failures == []
    assert done == 6          # top, a, mid, b, low, c -- not the link
    for rel in ("top", "top/a", "top/mid", "top/mid/b", "top/mid/low/c"):
        assert _mode(tmp_path / rel) == 0o700
    assert _mode(tmp_path / "elsewhere") == 0o644


def test_a_link_named_is_changed_through(fs, tmp_path):
    write(str(tmp_path / "real"), "x")
    os.chmod(tmp_path / "real", 0o600)
    os.symlink("real", tmp_path / "link")
    entry = next(e for e in fs.listdir(str(tmp_path)) if e.name == "link")
    apply_change(fs, [(str(tmp_path / "link"), entry)],
                 AttributeChange(on=0o004, off=0))
    assert _mode(tmp_path / "real") == 0o604


def test_a_refusal_in_the_middle_is_named_and_the_rest_go_on(tmp_path):
    class Refusing(LocalFileSystem):
        def chmod(self, path, mode):
            if path.endswith("b"):
                raise PermissionError("not yours")
            super().chmod(path, mode)

        def listdir(self, path):
            if path.endswith("locked"):
                raise PermissionError("cannot list")
            return super().listdir(path)

    fs = Refusing()
    for rel in ("d/a", "d/b", "d/c", "d/locked/x"):
        write(str(tmp_path / rel), "x")
    done, failures = apply_change(
        fs, [_entry(fs, tmp_path / "d")],
        AttributeChange(on=0o700, off=0o077, recursive=True))
    assert done == 4          # d, a, c, locked
    assert failures == [f"{tmp_path}/d/b: not yours",
                        f"{tmp_path}/d/locked: cannot list"]


def test_cancel_stops_the_walk(fs, tmp_path):
    write(str(tmp_path / "f"), "x")
    with pytest.raises(OperationCancelled):
        apply_change(fs, [_entry(fs, tmp_path / "f")],
                     AttributeChange(on=0o700, off=0o077), cancel=lambda: True)


def test_owner_and_group_by_name(fs, tmp_path):
    """Run as root here, so giving a file to root is allowed."""
    if os.geteuid() != 0:
        pytest.skip("only root can give a file away")
    import grp
    import pwd

    write(str(tmp_path / "f"), "x")
    user = pwd.getpwuid(0)[0]
    group = grp.getgrgid(0)[0]
    done, failures = apply_change(
        fs, [_entry(fs, tmp_path / "f")],
        AttributeChange(on=0, off=0, owner=user, group=group))
    assert (done, failures) == (1, [])
    assert fs.stat(str(tmp_path / "f")).owner == user


def test_a_refused_chown_is_reported(tmp_path):
    class NotRoot(LocalFileSystem):
        def chown(self, path, owner="", group=""):
            raise PermissionError("Operation not permitted")

    fs = NotRoot()
    write(str(tmp_path / "f"), "x")
    done, failures = apply_change(fs, [_entry(fs, tmp_path / "f")],
                                  AttributeChange(on=0, off=0, owner="nobody"))
    assert done == 0
    assert failures == [f"{tmp_path}/f: Operation not permitted"]


def test_local_listing_names_owners_and_falls_back_to_numbers(fs, tmp_path,
                                                            monkeypatch):
    write(str(tmp_path / "f"), "x")
    entry = fs.listdir(str(tmp_path))[0]
    assert entry.owner and entry.group

    def nobody(number):
        raise KeyError(number)

    table: dict = {}
    assert fsmod._id_name(table, 4242, nobody) == "4242"
    assert table == {4242: "4242"}


def test_ftp_cannot_change_owners():
    fs = fsmod.FTPFileSystem.__new__(fsmod.FTPFileSystem)
    fs.username, fs.host = "anon", "files"
    assert not fs.can_chown
    with pytest.raises(fsmod.FileSystemError,
                       match="ftp://anon@files cannot change owners"):
        fs.chown("/a", "deploy")


def test_an_ls_line_keeps_its_owner_and_group():
    entry = fsmod._parse_unix_ls_line(
        "-rwxr-x--- 1 deploy www-data 10 Jan  1 12:00 run.sh")
    assert (entry.owner, entry.group) == ("deploy", "www-data")


# -- the dialog -----------------------------------------------------------------

def _run_dialog(monkeypatch, keys, **kwargs):
    captured = scripted_menu(monkeypatch, keys)
    args = {"title": "Attributes", "subject": "run.sh", "on": 0o644,
            "off": 0o133}
    args.update(kwargs)
    choice = with_curses_screen(24, 80, lambda s: dialogs.attributes(s, **args))
    text = "\n".join(str(d) for d in captured["window"].drawn)
    return choice, text


def test_the_dialog_shows_boxes_and_octal(monkeypatch):
    choice, text = _run_dialog(monkeypatch, [10])
    assert choice == dialogs.AttributeChoice(0o644, 0o133, "", "", False)
    assert "Octal      644" in text
    assert "run.sh" in text
    assert "Owner: (various)" in text


def test_space_toggles_and_the_octal_follows(monkeypatch):
    # The cursor starts on owner-read; across to owner-exec and set it.
    keys = [curses.KEY_RIGHT, curses.KEY_RIGHT, ord(" "), 10]
    choice, text = _run_dialog(monkeypatch, keys)
    assert (choice.on, choice.off) == (0o744, 0o033)
    assert "Octal      744" in text


def test_typed_octal_sets_every_box(monkeypatch):
    choice, _ = _run_dialog(monkeypatch, [ord("7"), ord("5"), ord("5"), 10])
    assert (choice.on, choice.off) == (0o755, 0o022)


def test_a_mixed_box_turns_on_and_then_off(monkeypatch):
    keys = [curses.KEY_DOWN, curses.KEY_DOWN, curses.KEY_LEFT, ord("x"), 10]
    choice, text = _run_dialog(monkeypatch, keys, on=0o640, off=0o136)
    assert "[~]" in text and "Octal      64?" in text
    assert (choice.on, choice.off) == (0o641, 0o136)
    # Up wraps to "Other", Left wraps to exec; Tab steps right and wraps.
    keys = [curses.KEY_UP, curses.KEY_LEFT, 9, curses.KEY_LEFT,
            ord(" "), ord(" "), 10]
    choice, _ = _run_dialog(monkeypatch, keys, on=0o640, off=0o136)
    assert (choice.on, choice.off) == (0o640, 0o137)


def test_escape_changes_nothing(monkeypatch):
    choice, _ = _run_dialog(monkeypatch, [27])
    assert choice is None


def test_owner_group_and_recursion_where_allowed(monkeypatch):
    answers = iter(["deploy", "www-data"])
    monkeypatch.setattr(dialogs, "prompt",
                        lambda *a, **k: next(answers))
    choice, text = _run_dialog(
        monkeypatch, [ord("o"), ord("g"), ord("r"), 10],
        owner="root", group="root", can_chown=True, has_dirs=True)
    assert (choice.owner, choice.group, choice.recursive) == \
        ("deploy", "www-data", True)
    assert "o / g" in text and "[x] Recursive" in text


def test_owner_keys_do_nothing_where_not_allowed(monkeypatch):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: "never asked")
    choice, text = _run_dialog(monkeypatch, [ord("o"), ord("g"), ord("r"), 10],
                               owner="root", group="wheel")
    assert (choice.owner, choice.group, choice.recursive) == \
        ("root", "wheel", False)
    assert "Recursive" not in text


def test_an_escaped_owner_prompt_keeps_the_owner(monkeypatch):
    monkeypatch.setattr(dialogs, "prompt", lambda *a, **k: None)
    choice, _ = _run_dialog(monkeypatch, [ord("o"), ord("g"), 10],
                            owner="root", group="root", can_chown=True)
    assert (choice.owner, choice.group) == ("root", "root")


# -- the application -----------------------------------------------------------

@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in ("run.sh", "data.tsv"):
        write(str(tmp_path / "left" / name), "x")
        os.chmod(tmp_path / "left" / name, 0o644)
    write(str(tmp_path / "left" / "tree" / "inner"), "x")
    (tmp_path / "right").mkdir()
    monkeypatch.setattr(curses, "doupdate", lambda: None)

    class _Progress:
        def __init__(self, stdscr, title):
            self.cancel = False

        def update(self, cur, total, label):
            pass

        def set_overall(self, text):
            pass

        def cancelled(self):
            return self.cancel

        def close(self):
            pass

    monkeypatch.setattr(dialogs, "ProgressDialog", _Progress)
    return App(_StubScreen(), str(tmp_path / "left"), str(tmp_path / "right"))


def _answer(monkeypatch, choice):
    seen = {}

    def attributes(stdscr, title, subject, on, off, owner="", group="",
                   can_chown=False, has_dirs=False):
        seen.update(subject=subject, on=on, off=off, owner=owner,
                    can_chown=can_chown, has_dirs=has_dirs)
        return choice

    monkeypatch.setattr(dialogs, "attributes", attributes)
    return seen


def _point_at(panel, name):
    panel.move_to([e.name for e in panel.entries].index(name))


def test_a_makes_a_script_executable(app, tmp_path, monkeypatch):
    _point_at(app.left, "run.sh")
    seen = _answer(monkeypatch, dialogs.AttributeChoice(0o755, 0o022, "", "",
                                                        False))
    app.handle_key(ord("a"))
    assert _mode(tmp_path / "left" / "run.sh") == 0o755
    assert seen["subject"] == "run.sh" and seen["on"] == 0o644
    assert seen["can_chown"] is True and seen["has_dirs"] is False
    assert app.message == "Attributes changed on 1 item(s)"


def test_several_with_mixed_modes(app, tmp_path, monkeypatch):
    os.chmod(tmp_path / "left" / "run.sh", 0o755)
    app.left.refresh()
    app.left.selected = {"run.sh", "data.tsv"}
    seen = _answer(monkeypatch, dialogs.AttributeChoice(0o004, 0o002, "", "",
                                                        False))
    app._attributes()
    assert seen["subject"] == "2 items"
    assert (seen["on"], seen["off"]) == (0o644, 0o022)
    assert _mode(tmp_path / "left" / "run.sh") == 0o755
    assert _mode(tmp_path / "left" / "data.tsv") == 0o644


def test_escape_changes_nothing_in_the_app(app, tmp_path, monkeypatch):
    _point_at(app.left, "run.sh")
    _answer(monkeypatch, None)
    app._attributes()
    assert _mode(tmp_path / "left" / "run.sh") == 0o644


def test_recursive_is_confirmed_first(app, tmp_path, monkeypatch):
    _point_at(app.left, "tree")
    _answer(monkeypatch, dialogs.AttributeChoice(0o700, 0o077, "", "", True))
    scripted = _ScriptedDialogs(monkeypatch, confirm=[False])
    app._attributes()
    assert "Change tree and everything under it?" in scripted.messages[0][1]
    assert _mode(tmp_path / "left" / "tree" / "inner") == 0o644
    _answer(monkeypatch, dialogs.AttributeChoice(0o700, 0o077, "", "", True))
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app._attributes()
    assert _mode(tmp_path / "left" / "tree" / "inner") == 0o700
    assert app.message == "Attributes changed on 2 item(s)"


def test_only_a_changed_owner_is_sent(app, monkeypatch):
    _point_at(app.left, "run.sh")
    owner = app.left.current().owner
    calls = []
    monkeypatch.setattr(app.left.fs, "chown",
                        lambda path, o="", g="": calls.append((o, g)))
    _answer(monkeypatch, dialogs.AttributeChoice(0o644, 0o133, owner, "staff",
                                                 False))
    app._attributes()
    assert calls == [("", "staff")]


def test_failures_are_listed(app, monkeypatch):
    app.left.selected = {"run.sh", "data.tsv", "tree"}

    def refuse(path, mode):
        raise PermissionError("not yours")

    monkeypatch.setattr(app.left.fs, "chmod", refuse)
    _answer(monkeypatch, dialogs.AttributeChoice(0o700, 0o077, "", "", True))
    scripted = _ScriptedDialogs(monkeypatch, confirm=[True])
    app._attributes()
    title, text, error = scripted.messages[-1]
    assert title == "Attributes: not changed" and error
    assert text.count("not yours") == 4


def test_more_than_eight_failures_are_counted(app, tmp_path, monkeypatch):
    for i in range(10):
        write(str(tmp_path / "left" / f"f{i}"), "x")
    app.left.refresh()
    app.left.selected = {f"f{i}" for i in range(10)}

    def refuse(path, mode):
        raise PermissionError("no")

    monkeypatch.setattr(app.left.fs, "chmod", refuse)
    _answer(monkeypatch, dialogs.AttributeChoice(0o700, 0o077, "", "", False))
    scripted = _ScriptedDialogs(monkeypatch)
    app._attributes()
    assert scripted.last_message.endswith("... and 2 more")


def test_a_cancelled_change_says_so(app, monkeypatch):
    _point_at(app.left, "run.sh")
    _answer(monkeypatch, dialogs.AttributeChoice(0o700, 0o077, "", "", False))

    class _Cancelled:
        def __init__(self, stdscr, title):
            pass

        def update(self, cur, total, label):
            pass

        def cancelled(self):
            return True

        def close(self):
            pass

    monkeypatch.setattr(dialogs, "ProgressDialog", _Cancelled)
    app._attributes()
    assert app.message == "Attributes cancelled"


def test_an_archive_says_it_cannot(app, tmp_path, monkeypatch):
    write_zip(str(tmp_path / "left" / "z.zip"), {"a": "1"})
    app.left.refresh()
    _point_at(app.left, "z.zip")
    app._activate_entry()
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(ord("a"))
    assert "cannot change permissions" in scripted.last_message


def test_nothing_to_change_and_a_plug_in_pane(app, monkeypatch):
    app.left.move_to(0)                      # ".."
    seen = _answer(monkeypatch, None)
    app._attributes()
    assert seen == {}
    app.left.plugin = object()
    app._attributes()
    assert app.message == "Attributes work on a directory listing"


def test_the_menus_offer_it(app):
    file_menu = next(m for m in MENUS if m["name"] == "file")
    options = next(m for m in MENUS if m["name"] == "options")
    assert any(i.get("name") == "attributes" and i.get("key") == "a"
               for i in file_menu["items"])
    assert any(i.get("name") == "long" for i in options["items"])
    assert {"attributes", "long"} <= set(app._actions())


# -- the long listing -------------------------------------------------------------

def test_the_long_listing_is_remembered(app, tmp_path):
    assert app.long_listing is False
    app._toggle_long()
    assert app.long_listing is True and app.message == "Long listing"
    assert config.long_listing() is True
    app._toggle_long()
    assert config.long_listing() is False and app.message == "Short listing"
    assert config.save_listing(False) is True


def test_a_listing_setting_that_cannot_be_saved_says_so(app, monkeypatch):
    monkeypatch.setattr(config, "_save_ui", lambda key, value: False)
    app._toggle_long()
    assert app.message == "Long listing  (could not be saved)"


def _render(tmp_path, cols, single=False):
    write(str(tmp_path / "left" / "a-rather-long-results-file-name.tsv"), "x")
    os.chmod(tmp_path / "left" / "a-rather-long-results-file-name.tsv", 0o640)

    def run(stdscr):
        app = App(stdscr, str(tmp_path / "left"), str(tmp_path / "right"),
                  single=single)
        app.long_listing = True
        app.draw()
        return "\n".join(stdscr.instr(row, 0).decode(errors="replace")
                         for row in range(24))

    return with_curses_screen(24, cols, run)


def test_the_long_listing_fits_two_panes_at_80_columns(app, tmp_path):
    text = _render(tmp_path, 80)
    assert "-rw-r-----" in text
    assert "Mode" in text and "Owner" in text
    assert "Modify time" not in text           # the time gives way first
    assert all(len(line) <= 80 for line in text.splitlines())


def test_a_wide_pane_keeps_the_time_too(app, tmp_path):
    text = _render(tmp_path, 120, single=True)
    assert "-rw-r-----" in text and "Modify time" in text


def test_the_mode_column_is_blank_where_nothing_is_known():
    assert App._mode_text(DirEntry(name="..", is_dir=True)) == " " * 10
    assert App._mode_text(DirEntry(name="f", is_dir=False)) == " " * 10
    assert App._mode_text(DirEntry(name="f", is_dir=False,
                                   mode=stat_mod.S_IFREG | 0o755)) == "-rwxr-xr-x"
