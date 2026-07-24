"""Key dispatch, the mouse, the plug-in/config menus, find, and main()."""

from __future__ import annotations

import curses
import os
import subprocess

import pytest

from meridian_commander import app as app_mod
from meridian_commander import dialogs
from meridian_commander.app import App, main
from meridian_commander.filesystems import LocalFileSystem

from meridian_commander.filesystems import (
    FTPFileSystem,
    SFTPFileSystem,
)

from support import _FakeRemoteBackend, _ScriptedDialogs, read, write


class _RemoteLike:
    """Mixin: a remote backend that actually reads the local disk.

    The full-screen shell branches on the backend's *class*, so the pane needs
    a real SFTP/FTP type; everything it does with the files is delegated to a
    local backend so the pane still lists and refreshes.
    """

    def __init__(self, host="web1", username="deploy", port=22):
        self.host = host
        self.port = port
        self.typed_username = username
        self.username = username
        self._local = LocalFileSystem()

    def home(self):
        return self._local.home()

    def listdir(self, path):
        return self._local.listdir(path)

    def stat(self, path):
        return self._local.stat(path)

    def open_read(self, path):
        return self._local.open_read(path)

    def open_write(self, path):
        return self._local.open_write(path)

    def mkdir(self, path):
        self._local.mkdir(path)

    def remove(self, path):
        self._local.remove(path)

    def rmdir(self, path):
        self._local.rmdir(path)

    def rename(self, src, dst):
        self._local.rename(src, dst)


class _SftpLike(_RemoteLike, SFTPFileSystem):
    def label(self):
        return f"sftp://{self.username}@{self.host}"


class _FtpLike(_RemoteLike, FTPFileSystem):
    def label(self):
        return f"ftp://{self.username}@{self.host}"


@pytest.fixture(autouse=True)
def _quiet_screen(monkeypatch):
    monkeypatch.setattr(curses, "doupdate", lambda: None)
    monkeypatch.setattr(curses, "curs_set", lambda n: None)
    monkeypatch.setattr(curses, "ACS_VLINE",
                        getattr(curses, "ACS_VLINE", ord("|")), raising=False)
    monkeypatch.setattr(curses, "has_colors", lambda: False)


@pytest.fixture(autouse=True)
def _quiet_progress(monkeypatch):
    class _Progress:
        def __init__(self, stdscr, title):
            self.cancel = False

        def set_overall(self, text):
            pass

        def update(self, cur, total, label):
            pass

        def cancelled(self):
            return self.cancel

        def close(self):
            pass

    monkeypatch.setattr(dialogs, "ProgressDialog", _Progress)


def _names(panel):
    return [e.name for e in panel.entries]


def _point_at(panel, name):
    panel.move_to(_names(panel).index(name))


# -- navigation keys -----------------------------------------------------------

def test_cursor_keys(app):
    app.handle_key(curses.KEY_DOWN)
    assert app.left.cursor == 1
    app.handle_key(ord("j"))
    assert app.left.cursor == 2
    app.handle_key(curses.KEY_UP)
    app.handle_key(ord("k"))
    assert app.left.cursor == 0
    app.handle_key(curses.KEY_END)
    assert app.left.cursor == len(app.left.entries) - 1
    app.handle_key(curses.KEY_HOME)
    assert app.left.cursor == 0
    app.handle_key(curses.KEY_NPAGE)
    assert app.left.cursor > 0
    app.handle_key(curses.KEY_PPAGE)
    assert app.left.cursor == 0


def test_tab_switches_panes(app):
    assert app.active is app.left
    app.handle_key(9)
    assert app.active is app.right


@pytest.mark.parametrize("key", [curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT])
def test_enter_descends(app, tmp_path, key):
    _point_at(app.left, "sub")
    app.handle_key(key)
    assert app.left.path == str(tmp_path / "left" / "sub")


@pytest.mark.parametrize("key", [curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT])
def test_backspace_goes_up(app, tmp_path, key):
    app.left.chdir(str(tmp_path / "left" / "sub"))
    app.handle_key(key)
    assert app.left.path == str(tmp_path / "left")


@pytest.mark.parametrize("key", [curses.KEY_IC, ord(" ")])
def test_tagging_keys(app, key):
    _point_at(app.left, "file.txt")
    before = app.left.cursor
    app.handle_key(key)
    assert "file.txt" in app.left.selected
    # The cursor advances so a run of files can be tagged quickly (clamped at
    # the end of the listing).
    assert app.left.cursor >= before


def test_tag_all_and_untag_all(app):
    app.handle_key(ord("+"))
    assert ".." not in app.left.selected
    assert app.left.selected
    app.handle_key(ord("-"))
    assert app.left.selected == set()
    app.handle_key(ord("+"))
    app.handle_key(ord("\\"))
    assert app.left.selected == set()


def test_swap_and_reload_keys(app, tmp_path):
    left_path = app.left.path
    app.handle_key(21)                      # Ctrl-U
    assert app.right.path == left_path
    write(str(tmp_path / "left" / "added.txt"), "new")
    app.handle_key(18)                      # Ctrl-R
    assert "Reloaded" in app.message
    assert "added.txt" in _names(app.right)


def test_hidden_files_toggle(app):
    app.handle_key(ord("."))
    assert app.left.show_hidden is False
    assert "Hidden files hidden" in app.message
    app.handle_key(ord("."))
    assert "Hidden files shown" in app.message


def test_the_go_to_path_key(app, tmp_path, monkeypatch):
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "left" / "sub")])
    app.handle_key(7)                       # Ctrl-G
    assert app.left.path == str(tmp_path / "left" / "sub")


def test_the_sort_key(app, monkeypatch):
    _ScriptedDialogs(monkeypatch, menu=["Extension"])
    app.handle_key(20)                      # Ctrl-T
    assert app.active.sort_key == "ext"


def test_the_resize_key_is_absorbed(app):
    app.handle_key(curses.KEY_RESIZE)       # must not raise


def test_an_unknown_key_is_ignored(app):
    before = app.left.cursor
    app.handle_key(0x1234)
    assert app.left.cursor == before


# -- function keys -------------------------------------------------------------

@pytest.mark.parametrize("key", [curses.KEY_F1, ord("1"), ord("?")])
def test_the_help_key(app, monkeypatch, key):
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(key)
    assert scripted.messages[-1][0] == "Help"


@pytest.mark.parametrize("key", [curses.KEY_F2, ord("2"), ord("o")])
def test_the_connect_key(app, monkeypatch, key):
    monkeypatch.setattr(dialogs, "connect_dialog", lambda stdscr: None)
    app.handle_key(key)


@pytest.mark.parametrize("key", [curses.KEY_F7, ord("7")])
def test_the_mkdir_key(app, tmp_path, monkeypatch, key):
    _ScriptedDialogs(monkeypatch, prompt=[f"made-{key}"])
    app.handle_key(key)
    assert (tmp_path / "left" / f"made-{key}").is_dir()


@pytest.mark.parametrize("key", [curses.KEY_F8, ord("8"), ord("d")])
def test_the_delete_key(app, tmp_path, monkeypatch, key):
    name = f"doomed-{key}"
    write(str(tmp_path / "left" / name), "x")
    app.left.refresh()
    _point_at(app.left, name)
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app.handle_key(key)
    assert not (tmp_path / "left" / name).exists()


@pytest.mark.parametrize("key", [curses.KEY_F5, ord("5"), ord("c")])
def test_the_copy_key(app, tmp_path, monkeypatch, key):
    _point_at(app.left, "file.txt")
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app.handle_key(key)
    assert (tmp_path / "right" / "file.txt").exists()


@pytest.mark.parametrize("key", [curses.KEY_F6, ord("6"), ord("m")])
def test_the_move_key(app, tmp_path, monkeypatch, key):
    name = f"movable-{key}"
    write(str(tmp_path / "left" / name), "x")
    app.left.refresh()
    _point_at(app.left, name)
    _ScriptedDialogs(monkeypatch, prompt=[str(tmp_path / "right")])
    app.handle_key(key)
    assert (tmp_path / "right" / name).exists()


@pytest.mark.parametrize("key", [curses.KEY_F9, ord("9"), ord("s")])
def test_the_sync_key(app, monkeypatch, key):
    _ScriptedDialogs(monkeypatch, confirm=[False])
    app.handle_key(key)
    assert "cancelled" in app.message or "sync" in app.message.lower()


@pytest.mark.parametrize("key", [curses.KEY_F3, ord("3"), ord("v")])
def test_the_view_key(app, monkeypatch, key):
    opened = []
    monkeypatch.setattr(app_mod, "Viewer",
                        lambda fs, path: _Recorder(opened, path))
    _point_at(app.left, "file.txt")
    app.handle_key(key)
    assert opened


@pytest.mark.parametrize("key", [curses.KEY_F4, ord("4"), ord("e")])
def test_the_edit_key(app, monkeypatch, key):
    opened = []
    monkeypatch.setattr(app_mod, "Editor",
                        lambda fs, path: _Recorder(opened, path))
    _point_at(app.left, "file.txt")
    app.handle_key(key)
    assert opened


class _Recorder:
    def __init__(self, log, path):
        log.append(path)

    def run(self, stdscr):
        return None


@pytest.mark.parametrize("key", [curses.KEY_F10, ord("0"), ord("q"), 3])
def test_the_quit_key_asks_first(app, monkeypatch, key):
    _ScriptedDialogs(monkeypatch, confirm=[False])
    app.handle_key(key)
    assert app.running is True
    _ScriptedDialogs(monkeypatch, confirm=[True])
    app.handle_key(key)
    assert app.running is False


# -- plug-ins in a pane --------------------------------------------------------

class _Plugin:
    name = "Demo"
    description = "a demo plug-in"

    def __init__(self, ctx):
        self.ctx = ctx
        self.keys: list[int] = []
        self.answer = True
        self.exited = False

    def draw(self, stdscr, y, x, h, w):
        pass

    def handle_key(self, key):
        self.keys.append(key)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def on_exit(self):
        self.exited = True


def test_a_plugin_sees_keys_first(app):
    plugin = _Plugin(None)
    app.left.plugin = plugin
    app.handle_key(ord("x"))
    assert plugin.keys == [ord("x")]
    # It consumed the key, so the pane did not move.
    assert app.left.cursor == 0


def test_a_plugin_returning_false_closes_itself(app):
    plugin = _Plugin(None)
    plugin.answer = False
    app.left.plugin = plugin
    app.handle_key(ord("x"))
    assert app.left.plugin is None
    assert "Plugin closed" in app.message


def test_a_plugin_returning_none_hands_the_key_back(app):
    plugin = _Plugin(None)
    plugin.answer = None
    app.left.plugin = plugin
    app.handle_key(9)                       # Tab still switches panes
    assert app.active is app.right


def test_a_plugin_that_raises_is_reported_and_keeps_the_pane(app):
    plugin = _Plugin(None)
    plugin.answer = RuntimeError("plugin blew up")
    app.left.plugin = plugin
    app.handle_key(ord("x"))
    assert "Plugin error: plugin blew up" in app.message
    assert app.left.plugin is plugin


def test_a_resize_bypasses_the_plugin(app):
    plugin = _Plugin(None)
    app.left.plugin = plugin
    app.handle_key(curses.KEY_RESIZE)
    assert plugin.keys == []


@pytest.mark.parametrize("key", [ord("p"), curses.KEY_F11])
def test_the_plugin_menu_opens_one(app, monkeypatch, key):
    monkeypatch.setattr(app_mod, "dialogs", dialogs)
    _ScriptedDialogs(monkeypatch, menu=["Find in other pane"])
    app.handle_key(key)
    assert app.left.plugin is not None
    assert "opened" in app.message
    app.left.plugin = None


def test_the_plugin_menu_can_be_cancelled(app, monkeypatch):
    for answer in ["Cancel", None]:
        _ScriptedDialogs(monkeypatch, menu=[answer])
        app._plugin_mode()
        assert app.left.plugin is None


def test_the_plugin_menu_with_nothing_to_offer(app, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.discover",
                        lambda: ([], ["broken.py: bad"]))
    scripted = _ScriptedDialogs(monkeypatch)
    app._plugin_mode()
    assert "No plug-ins found" in scripted.last_message
    assert "broken.py: bad" in scripted.last_message


def test_a_plugin_that_fails_to_start_is_reported(app, monkeypatch):
    class _Broken:
        name = "Broken"
        description = "fails on open"

        def __init__(self, ctx):
            raise RuntimeError("cannot start")

    monkeypatch.setattr("meridian_commander.plugins.discover",
                        lambda: ([_Broken], []))
    scripted = _ScriptedDialogs(monkeypatch, menu=[0])
    app._plugin_mode()
    assert "failed to start" in scripted.last_message
    assert app.left.plugin is None


def test_load_errors_are_mentioned_when_a_plugin_opens(app, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.discover",
                        lambda: ([_Plugin], ["broken.py: bad"]))
    _ScriptedDialogs(monkeypatch, menu=[0])
    app._plugin_mode()
    assert "1 plug-in(s) failed to load" in app.message
    app.left.plugin = None


# -- terminals -----------------------------------------------------------------

def test_the_in_pane_terminal_key(app, monkeypatch):
    opened = []

    class _Terminal:
        def __init__(self, ctx):
            opened.append(ctx)

        def on_exit(self):
            pass

    monkeypatch.setattr("meridian_commander.plugins.terminal.TerminalPlugin",
                        _Terminal)
    app.handle_key(ord("t"))
    assert opened
    assert "Terminal opened" in app.message
    app.left.plugin = None


def test_a_terminal_that_will_not_start_is_reported(app, monkeypatch):
    class _Broken:
        def __init__(self, ctx):
            raise RuntimeError("no shell here")

    monkeypatch.setattr("meridian_commander.plugins.terminal.TerminalPlugin",
                        _Broken)
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(ord("t"))
    assert "no shell here" in scripted.last_message
    assert app.left.plugin is None


def test_the_full_screen_shell_on_a_local_pane(app, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "call",
                        lambda cmd, cwd=None: calls.append((cmd, cwd)))
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None, raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)
    monkeypatch.setenv("SHELL", "/bin/zsh")

    app.handle_key(ord("!"))
    assert calls[0][0] == ["/bin/zsh"]
    assert calls[0][1] == str(tmp_path / "left")
    assert "Returned from shell" in app.message


def test_the_full_screen_shell_on_a_remote_pane(app, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "call",
                        lambda cmd, cwd=None: calls.append((cmd, cwd)))
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None, raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)

    backend = _SftpLike(host="web1", username="deploy", port=2222)
    app.left.set_location(backend, str(tmp_path / "left"))
    app.handle_key(ord("!"))
    cmd = calls[0][0]
    assert cmd[:2] == ["ssh", "-t"]
    assert "-p" in cmd and "2222" in cmd
    assert "deploy@web1" in cmd


def test_a_remote_shell_keeps_a_user_at_host_as_typed(app, tmp_path,
                                                      monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "call",
                        lambda cmd, cwd=None: calls.append(cmd))
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None, raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)

    backend = _SftpLike(host="deploy@web1", username="deploy", port=22)
    app.left.set_location(backend, str(tmp_path / "left"))
    app.handle_key(ord("!"))
    cmd = calls[0]
    assert "deploy@web1" in cmd
    assert "-p" not in cmd            # the default port is left to ssh


def test_the_full_screen_shell_is_declined_for_ftp(app, monkeypatch):
    app.left.fs = _FtpLike()
    scripted = _ScriptedDialogs(monkeypatch)
    app.handle_key(ord("!"))
    assert "only available for local or SFTP" in scripted.last_message


def test_a_shell_that_will_not_start_is_reported(app, monkeypatch):
    def explode(cmd, cwd=None):
        raise OSError("no such shell")

    monkeypatch.setattr(subprocess, "call", explode)
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    written = []
    monkeypatch.setattr(os, "write",
                        lambda fd, data: written.append(data) or len(data))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None, raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)

    app.handle_key(ord("!"))
    assert any(b"Could not start terminal" in w for w in written)


def test_a_shell_failure_with_no_terminal_to_prompt_on(app, monkeypatch):
    def explode(cmd, cwd=None):
        raise OSError("no such shell")

    def no_input(prompt=""):
        raise EOFError()

    monkeypatch.setattr(subprocess, "call", explode)
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))
    monkeypatch.setattr("builtins.input", no_input)
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None, raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)

    app.handle_key(ord("!"))              # must not raise


# -- the configuration menu ----------------------------------------------------

def test_the_config_menu_edits_the_config_file(app, tmp_path, monkeypatch):
    edited = []
    monkeypatch.setattr(app_mod, "Editor",
                        lambda fs, path: _Recorder(edited, path))
    _ScriptedDialogs(monkeypatch, menu=["Edit configuration"])
    app.handle_key(ord("C"))
    assert edited and edited[0].endswith("config.ini")
    assert "Configuration saved" in app.message


def test_the_config_menu_can_be_cancelled(app, monkeypatch):
    for answer in ["Cancel", None]:
        _ScriptedDialogs(monkeypatch, menu=[answer])
        app._config_menu()


def test_the_config_menu_edits_a_plugin_file(app, tmp_path, monkeypatch):
    edited = []
    monkeypatch.setattr(app_mod, "Editor",
                        lambda fs, path: _Recorder(edited, path))
    _ScriptedDialogs(monkeypatch, menu=["Edit a plug-in file", "find_files.py"])
    app._config_menu()
    assert edited and edited[0].endswith("find_files.py")
    assert "Plug-in saved" in app.message


def test_choosing_no_plugin_file_to_edit(app, monkeypatch):
    _ScriptedDialogs(monkeypatch, menu=["Edit a plug-in file", "Cancel"])
    app._config_menu()


def test_the_config_menu_when_there_are_no_plugin_files(app, monkeypatch):
    monkeypatch.setattr("meridian_commander.plugins.plugin_dirs",
                        lambda: ["/nowhere-at-all"])
    scripted = _ScriptedDialogs(monkeypatch, menu=["Edit a plug-in file"])
    app._config_menu()
    assert "No plug-in files found" in scripted.last_message


def test_the_config_menu_opens_the_user_plugin_folder(app, tmp_path,
                                                      monkeypatch):
    _ScriptedDialogs(monkeypatch, menu=["Open user plug-in folder"])
    app._config_menu()
    assert app.left.path.endswith("meridian-commander/plugins")


def test_opening_the_plugin_folder_from_a_remote_pane_switches_backend(
        app, tmp_path, monkeypatch):
    app.left.set_location(_SftpLike(), str(tmp_path / "left"))
    _ScriptedDialogs(monkeypatch, menu=["Open user plug-in folder"])
    app._config_menu()
    assert isinstance(app.left.fs, LocalFileSystem)
    assert app.left.path.endswith("plugins")


def test_an_editor_that_fails_from_the_config_menu(app, monkeypatch):
    class _Broken:
        def __init__(self, fs, path):
            raise OSError("read-only")

    monkeypatch.setattr(app_mod, "Editor", _Broken)
    scripted = _ScriptedDialogs(monkeypatch, menu=["Edit configuration"])
    app._config_menu()
    assert "read-only" in scripted.last_message


# -- find files ----------------------------------------------------------------

def test_find_files_jumps_to_a_result(app, tmp_path, monkeypatch):
    write(str(tmp_path / "left" / "sub" / "needle.txt"), "x")
    app.left.refresh()
    monkeypatch.setattr("meridian_commander.findfiles.FindBrowser",
                        lambda fs, root, pattern, matches, truncated:
                        _FakeBrowser("sub/needle.txt"))
    _ScriptedDialogs(monkeypatch, prompt=["needle"])
    app.handle_key(ord("f"))
    assert app.left.path == str(tmp_path / "left" / "sub")
    assert "Jumped to needle.txt" in app.message


class _FakeBrowser:
    def __init__(self, result):
        self.result = result

    def run(self, stdscr):
        return self.result


def test_find_files_jumps_to_a_result_in_the_same_directory(app, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr("meridian_commander.findfiles.FindBrowser",
                        lambda *a: _FakeBrowser("file.txt"))
    _ScriptedDialogs(monkeypatch, prompt=["file"])
    app._find_files()
    assert app.left.path == str(tmp_path / "left")
    assert "Jumped to file.txt" in app.message


def test_find_files_reports_a_directory_it_cannot_open(app, tmp_path,
                                                       monkeypatch):
    write(str(tmp_path / "left" / "sub" / "needle.txt"), "x")
    app.left.refresh()
    monkeypatch.setattr("meridian_commander.findfiles.FindBrowser",
                        lambda *a: _FakeBrowser("sub/needle.txt"))
    monkeypatch.setattr(app.left, "chdir", lambda path, keep_name=None: False)
    scripted = _ScriptedDialogs(monkeypatch, prompt=["needle"])
    app._find_files()
    assert "Cannot open" in scripted.last_message


def test_find_files_closing_without_choosing(app, monkeypatch):
    monkeypatch.setattr("meridian_commander.findfiles.FindBrowser",
                        lambda *a: _FakeBrowser(None))
    before = app.left.path
    _ScriptedDialogs(monkeypatch, prompt=["file"])
    app._find_files()
    assert app.left.path == before


def test_find_files_with_no_matches(app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch, prompt=["no-such-name"])
    app._find_files()
    assert "No matches for 'no-such-name'" in scripted.last_message


def test_find_files_can_be_cancelled_at_the_prompt(app, monkeypatch):
    for answer in (None, ""):
        _ScriptedDialogs(monkeypatch, prompt=[answer])
        app._find_files()


def test_find_files_reports_progress(app, tmp_path, monkeypatch):
    write(str(tmp_path / "left" / "sub" / "needle.txt"), "x")
    app.left.refresh()
    seen = []

    class _Progress:
        def __init__(self, stdscr, title):
            pass

        def set_overall(self, text):
            seen.append(text)

        def update(self, cur, total, label):
            seen.append(label)

        def cancelled(self):
            return False

        def close(self):
            pass

    monkeypatch.setattr(dialogs, "ProgressDialog", _Progress)
    monkeypatch.setattr("meridian_commander.findfiles.FindBrowser",
                        lambda *a: _FakeBrowser(None))
    _ScriptedDialogs(monkeypatch, prompt=["needle"])
    app._find_files()
    assert any("Searching under" in s for s in seen)
    assert any("found -- in" in s for s in seen)


# -- the mouse -----------------------------------------------------------------

def _mouse(monkeypatch, mx, my, bstate):
    monkeypatch.setattr(curses, "getmouse", lambda: (0, mx, my, 0, bstate))


@pytest.fixture
def boxed(app):
    """Give the app the pane rectangles draw() would have recorded."""
    app._panel_boxes = [(app.left, 0, 0, 22, 39), (app.right, 0, 40, 22, 40)]
    return app


def test_a_click_focuses_a_pane_and_moves_the_cursor(boxed, monkeypatch):
    _mouse(monkeypatch, 41, 3, curses.BUTTON1_CLICKED)
    boxed.handle_key(curses.KEY_MOUSE)
    assert boxed.active is boxed.right
    assert boxed.right.cursor == 1


def test_a_click_outside_any_pane_is_ignored(boxed, monkeypatch):
    _mouse(monkeypatch, 5, 23, curses.BUTTON1_CLICKED)
    boxed.handle_key(curses.KEY_MOUSE)
    assert boxed.active is boxed.left


def test_a_mouse_report_that_cannot_be_read_is_ignored(boxed, monkeypatch):
    def explode():
        raise curses.error("no mouse event")

    monkeypatch.setattr(curses, "getmouse", explode)
    boxed.handle_key(curses.KEY_MOUSE)


def test_a_double_click_opens_the_entry(boxed, tmp_path, monkeypatch):
    row = _names(boxed.left).index("sub")
    _mouse(monkeypatch, 5, 2 + row, curses.BUTTON1_DOUBLE_CLICKED)
    boxed.handle_key(curses.KEY_MOUSE)
    assert boxed.left.path == str(tmp_path / "left" / "sub")


def test_a_right_click_on_an_entry_opens_the_menu(boxed, monkeypatch):
    row = _names(boxed.left).index("file.txt")
    _mouse(monkeypatch, 5, 2 + row, curses.BUTTON3_CLICKED)
    scripted = _ScriptedDialogs(monkeypatch, menu=[None])
    boxed.handle_key(curses.KEY_MOUSE)
    assert scripted.menus


def test_a_right_click_on_the_header_still_opens_the_menu(boxed, monkeypatch):
    _mouse(monkeypatch, 5, 0, curses.BUTTON3_CLICKED)
    scripted = _ScriptedDialogs(monkeypatch, menu=[None])
    boxed.handle_key(curses.KEY_MOUSE)
    assert scripted.menus


def test_the_wheel_scrolls_a_pane(boxed, monkeypatch):
    boxed.left.move_to(5)
    _mouse(monkeypatch, 5, 5, curses.BUTTON5_PRESSED)
    boxed.handle_key(curses.KEY_MOUSE)
    assert boxed.left.cursor == min(8, len(boxed.left.entries) - 1)
    _mouse(monkeypatch, 5, 5, curses.BUTTON4_PRESSED)
    boxed.handle_key(curses.KEY_MOUSE)
    assert boxed.left.cursor < 8


def test_the_wheel_scrolls_a_plugins_output(boxed, monkeypatch):
    plugin = _Plugin(None)
    boxed.left.plugin = plugin
    # A plug-in owning the active pane consumes KEY_MOUSE itself, so the
    # mouse handler is what turns a wheel event into a scroll.
    _mouse(monkeypatch, 5, 5, curses.BUTTON4_PRESSED)
    boxed._handle_mouse()
    _mouse(monkeypatch, 5, 5, curses.BUTTON5_PRESSED)
    boxed._handle_mouse()
    assert plugin.keys == [curses.KEY_PPAGE, curses.KEY_NPAGE]


def test_a_click_on_a_plugin_pane_only_focuses_it(boxed, monkeypatch):
    plugin = _Plugin(None)
    boxed.right.plugin = plugin
    _mouse(monkeypatch, 41, 5, curses.BUTTON1_CLICKED)
    boxed.handle_key(curses.KEY_MOUSE)
    assert boxed.active is boxed.right
    assert plugin.keys == []


def test_a_plugin_that_fails_to_scroll_is_ignored(boxed, monkeypatch):
    plugin = _Plugin(None)
    plugin.answer = RuntimeError("scroll failed")
    boxed.left.plugin = plugin
    _mouse(monkeypatch, 5, 5, curses.BUTTON4_PRESSED)
    boxed._handle_mouse()                   # must not raise


def test_a_click_below_the_last_entry_selects_nothing(boxed, monkeypatch):
    _mouse(monkeypatch, 5, 18, curses.BUTTON1_CLICKED)
    before = boxed.left.cursor
    boxed.handle_key(curses.KEY_MOUSE)
    assert boxed.left.cursor == before


# -- the entry point -----------------------------------------------------------

def test_main_wires_up_curses_and_runs_the_app(monkeypatch, tmp_path):
    started = {}

    def fake_wrapper(fn, args):
        screen = _FakeScreen()
        fn(screen, args)
        return None

    class _FakeScreen:
        def keypad(self, flag):
            started["keypad"] = flag

        def getmaxyx(self):
            return (24, 80)

    class _FakeApp:
        def __init__(self, stdscr, left_path=None, right_path=None):
            started["paths"] = (left_path, right_path)

        def run(self):
            started["ran"] = True

    monkeypatch.setattr(curses, "wrapper", fake_wrapper)
    monkeypatch.setattr(app_mod, "App", _FakeApp)
    for name in ("use_default_colors", "raw"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)
    monkeypatch.setattr(curses, "init_pair", lambda *a: None)
    monkeypatch.setattr(curses, "mousemask", lambda mask: None)
    monkeypatch.setattr(curses, "mouseinterval", lambda ms: None)

    assert main([str(tmp_path), str(tmp_path)]) == 0
    assert started["ran"] is True
    assert started["keypad"] is True
    assert started["paths"] == (str(tmp_path), str(tmp_path))


def test_main_copes_with_a_terminal_that_refuses_colour_and_mouse(monkeypatch):
    def fake_wrapper(fn, args):
        class _Screen:
            def keypad(self, flag):
                pass

        fn(_Screen(), args)

    class _FakeApp:
        def __init__(self, stdscr, left_path=None, right_path=None):
            pass

        def run(self):
            pass

    monkeypatch.setattr(curses, "wrapper", fake_wrapper)
    monkeypatch.setattr(app_mod, "App", _FakeApp)
    for name in ("use_default_colors", "raw"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "start_color", lambda: None)

    def refuse(*args):
        raise curses.error("not supported")

    monkeypatch.setattr(curses, "init_pair", refuse)
    monkeypatch.setattr(curses, "mousemask", refuse)
    assert main([]) == 0


def test_main_on_a_monochrome_terminal(monkeypatch):
    def fake_wrapper(fn, args):
        class _Screen:
            def keypad(self, flag):
                pass

        fn(_Screen(), args)

    class _FakeApp:
        def __init__(self, stdscr, left_path=None, right_path=None):
            pass

        def run(self):
            pass

    monkeypatch.setattr(curses, "wrapper", fake_wrapper)
    monkeypatch.setattr(app_mod, "App", _FakeApp)
    for name in ("use_default_colors", "raw"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    monkeypatch.setattr(curses, "mousemask", lambda mask: None)
    monkeypatch.setattr(curses, "mouseinterval", lambda ms: None)
    assert main([]) == 0


def test_main_swallows_a_keyboard_interrupt(monkeypatch):
    def interrupted(fn, args):
        raise KeyboardInterrupt()

    monkeypatch.setattr(curses, "wrapper", interrupted)
    assert main([]) == 0


def test_main_reports_its_version(capsys):
    with pytest.raises(SystemExit) as info:
        main(["--version"])
    assert info.value.code == 0
    printed = capsys.readouterr().out
    assert "meridian-commander" in printed
    assert "GPLv3+" in printed


# -- the context menu ----------------------------------------------------------

def test_the_context_menu_view_and_edit(app, monkeypatch):
    opened = []
    monkeypatch.setattr(app_mod, "Viewer",
                        lambda fs, path: _Recorder(opened, ("view", path)))
    monkeypatch.setattr(app_mod, "Editor",
                        lambda fs, path: _Recorder(opened, ("edit", path)))
    _point_at(app.left, "file.txt")

    _ScriptedDialogs(monkeypatch, menu=["View"])
    app._context_menu()
    _ScriptedDialogs(monkeypatch, menu=["Edit"])
    app._context_menu()
    assert [kind for kind, _ in opened] == ["view", "edit"]


def test_the_context_menu_copies_and_moves(app, tmp_path, monkeypatch):
    write(str(tmp_path / "left" / "movable.txt"), "payload")
    app.left.refresh()
    _point_at(app.left, "movable.txt")

    _ScriptedDialogs(monkeypatch, menu=["Copy to other pane"],
                     prompt=[str(tmp_path / "right")])
    app._context_menu()
    assert (tmp_path / "right" / "movable.txt").exists()

    _ScriptedDialogs(monkeypatch, menu=["Move to other pane"],
                     prompt=[str(tmp_path / "right" / "moved")])
    app._context_menu()
    assert not (tmp_path / "left" / "movable.txt").exists()


def test_the_context_menu_renames(app, tmp_path, monkeypatch):
    _point_at(app.left, "file.txt")
    _ScriptedDialogs(monkeypatch, menu=["Rename"], prompt=["renamed.txt"])
    app._context_menu()
    assert (tmp_path / "left" / "renamed.txt").exists()
    assert "Renamed" in app.message


def test_the_context_menu_deletes(app, tmp_path, monkeypatch):
    write(str(tmp_path / "left" / "doomed.txt"), "x")
    app.left.refresh()
    _point_at(app.left, "doomed.txt")
    _ScriptedDialogs(monkeypatch, menu=["Delete"], confirm=[True])
    app._context_menu()
    assert not (tmp_path / "left" / "doomed.txt").exists()


def test_the_context_menu_tags(app, monkeypatch):
    _point_at(app.left, "file.txt")
    _ScriptedDialogs(monkeypatch, menu=["Tag / untag"])
    app._context_menu()
    assert "file.txt" in app.left.selected


def test_the_context_menu_makes_a_directory(app, tmp_path, monkeypatch):
    _ScriptedDialogs(monkeypatch, menu=["New directory"], prompt=["ctxdir"])
    app._context_menu()
    assert (tmp_path / "left" / "ctxdir").is_dir()


def test_the_context_menu_finds_files(app, tmp_path, monkeypatch):
    monkeypatch.setattr("meridian_commander.findfiles.FindBrowser",
                        lambda *a: _FakeBrowser(None))
    _ScriptedDialogs(monkeypatch, menu=["Find files here"], prompt=["file"])
    app._context_menu()


def test_the_context_menu_opens_a_terminal(app, monkeypatch):
    opened = []

    class _Terminal:
        def __init__(self, ctx):
            opened.append(ctx)

        def on_exit(self):
            pass

    monkeypatch.setattr("meridian_commander.plugins.terminal.TerminalPlugin",
                        _Terminal)
    _ScriptedDialogs(monkeypatch, menu=["Terminal in this pane"])
    app._context_menu()
    assert opened
    app.left.plugin = None


def test_the_context_menu_opens_a_full_screen_shell(app, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "call",
                        lambda cmd, cwd=None: calls.append(cmd))
    for name in ("def_prog_mode", "endwin", "reset_prog_mode"):
        monkeypatch.setattr(curses, name, lambda: None)
    monkeypatch.setattr(os, "write", lambda fd, data: len(data))
    monkeypatch.setattr(app.stdscr, "clearok", lambda flag: None, raising=False)
    monkeypatch.setattr(app.stdscr, "refresh", lambda: None, raising=False)
    _ScriptedDialogs(monkeypatch, menu=["Full-screen shell"])
    app._context_menu()
    assert calls


def test_the_context_menu_titles_itself_after_the_entry(app, monkeypatch):
    _point_at(app.left, "file.txt")
    scripted = _ScriptedDialogs(monkeypatch, menu=["Cancel"])
    app._context_menu()
    assert scripted.menus[0][0] == "file.txt"


def test_the_context_menu_on_the_parent_entry_uses_the_directory(app,
                                                                 monkeypatch):
    app.left.move_to(0)
    scripted = _ScriptedDialogs(monkeypatch, menu=["Cancel"])
    app._context_menu()
    assert scripted.menus[0][0] == "left"


# -- rename --------------------------------------------------------------------

def test_rename_can_be_cancelled(app, tmp_path, monkeypatch):
    _point_at(app.left, "file.txt")
    for answer in (None, "", "file.txt"):
        _ScriptedDialogs(monkeypatch, prompt=[answer])
        app._rename()
    assert (tmp_path / "left" / "file.txt").exists()


def test_rename_refuses_the_parent_entry(app, monkeypatch):
    app.left.move_to(0)
    _ScriptedDialogs(monkeypatch)
    app._rename()


def test_rename_with_nothing_selected(app, monkeypatch):
    app.left.entries = []
    _ScriptedDialogs(monkeypatch)
    app._rename()


def test_a_failing_rename_is_reported(app, tmp_path, monkeypatch):
    _point_at(app.left, "file.txt")

    def explode(src, dst):
        raise OSError("permission denied")

    monkeypatch.setattr(app.left.fs, "rename", explode)
    scripted = _ScriptedDialogs(monkeypatch, prompt=["other.txt"])
    app._rename()
    assert "permission denied" in scripted.last_message
