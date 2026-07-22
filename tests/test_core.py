"""Tests for the filesystem-agnostic core: copy, move and sync.

These exercise the real code paths using the local filesystem backend and
temporary directories, which is enough to validate the transfer and sync logic
that also drives remote transfers (the backend interface is identical).
"""

from __future__ import annotations

import os
import time

import pytest

from martin_commander.filesystems import (
    FileSystemError,
    FTPFileSystem,
    LocalFileSystem,
    SSHFileSystem,
    _parse_scp_header,
)
from martin_commander.operations import copy_path, count_tree, move_path
from martin_commander.panel import Panel
from martin_commander.sync import build_sync_plan, execute_sync_plan


@pytest.fixture
def fs():
    return LocalFileSystem()


def write(path: str, content: str, mtime: float | None = None) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def read(path: str) -> str:
    with open(path) as f:
        return f.read()


def test_copy_file(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello world")
    dst = tmp_path / "sub" / "b.txt"
    copy_path(fs, str(src), fs, str(dst))
    assert dst.read_text() == "hello world"
    assert src.exists()  # copy leaves the source


def test_copy_directory_tree(fs, tmp_path):
    root = tmp_path / "src"
    write(str(root / "one.txt"), "1")
    write(str(root / "nested" / "two.txt"), "2")
    write(str(root / "nested" / "deep" / "three.txt"), "3")

    dst = tmp_path / "dst"
    copy_path(fs, str(root), fs, str(dst))

    assert read(str(dst / "one.txt")) == "1"
    assert read(str(dst / "nested" / "two.txt")) == "2"
    assert read(str(dst / "nested" / "deep" / "three.txt")) == "3"


def test_count_tree(fs, tmp_path):
    root = tmp_path / "src"
    write(str(root / "a"), "12345")      # 5 bytes
    write(str(root / "sub" / "b"), "678")  # 3 bytes
    files, total = count_tree(fs, str(root))
    assert files == 2
    assert total == 8


def test_move_same_fs_renames(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("data")
    dst = tmp_path / "moved" / "a.txt"
    move_path(fs, str(src), fs, str(dst))
    assert dst.read_text() == "data"
    assert not src.exists()


def test_move_directory(fs, tmp_path):
    root = tmp_path / "src"
    write(str(root / "x.txt"), "x")
    write(str(root / "y" / "z.txt"), "z")
    dst = tmp_path / "dst"
    move_path(fs, str(root), fs, str(dst))
    assert read(str(dst / "x.txt")) == "x"
    assert read(str(dst / "y" / "z.txt")) == "z"
    assert not root.exists()


def test_sync_new_files_both_directions(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    write(str(left / "only_left.txt"), "L")
    write(str(right / "only_right.txt"), "R")

    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)

    # After sync both files exist on both sides.
    assert read(str(left / "only_left.txt")) == "L"
    assert read(str(right / "only_left.txt")) == "L"
    assert read(str(left / "only_right.txt")) == "R"
    assert read(str(right / "only_right.txt")) == "R"


def test_sync_newer_wins(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    now = time.time()
    # Same relative file; left is newer and should overwrite right.
    write(str(left / "shared.txt"), "new", mtime=now)
    write(str(right / "shared.txt"), "old", mtime=now - 1000)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    assert len(plan.actions) == 1
    assert plan.actions[0].direction == "->"
    execute_sync_plan(plan, fs, fs)

    assert read(str(right / "shared.txt")) == "new"
    assert read(str(left / "shared.txt")) == "new"


def test_sync_older_side_updated_from_newer(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    now = time.time()
    write(str(left / "shared.txt"), "stale", mtime=now - 1000)
    write(str(right / "shared.txt"), "fresh", mtime=now)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    assert plan.actions[0].direction == "<-"
    execute_sync_plan(plan, fs, fs)
    assert read(str(left / "shared.txt")) == "fresh"


def test_sync_preserves_mtime(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    src_mtime = time.time() - 5000
    write(str(left / "only_left.txt"), "L", mtime=src_mtime)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)

    copied = right / "only_left.txt"
    assert copied.exists()
    # The copied file must carry the source's timestamp, not "now".
    assert abs(copied.stat().st_mtime - src_mtime) < 1.0


def test_sync_is_idempotent(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    old = time.time() - 3000
    write(str(left / "a.txt"), "A", mtime=old)
    write(str(right / "b.txt"), "B", mtime=old)

    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)

    # A second run finds nothing to do because timestamps were preserved.
    plan2 = build_sync_plan(fs, str(left), fs, str(right))
    assert not plan2, [a.render() for a in plan2.actions]


def test_copy_preserve_mtime_flag(fs, tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("data")
    old = time.time() - 8000
    os.utime(str(src), (old, old))
    dst = tmp_path / "out" / "a.txt"
    copy_path(fs, str(src), fs, str(dst), preserve_mtime=True)
    assert abs(dst.stat().st_mtime - old) < 1.0


def test_sync_in_sync_is_noop(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    now = time.time()
    write(str(left / "same.txt"), "identical", mtime=now)
    write(str(right / "same.txt"), "identical", mtime=now)
    plan = build_sync_plan(fs, str(left), fs, str(right))
    assert not plan


def test_sync_nested_directories(fs, tmp_path):
    left = tmp_path / "left"
    right = tmp_path / "right"
    write(str(left / "a" / "b" / "c.txt"), "deep")
    plan = build_sync_plan(fs, str(left), fs, str(right))
    execute_sync_plan(plan, fs, fs)
    assert read(str(right / "a" / "b" / "c.txt")) == "deep"


def test_panel_lists_and_navigates(fs, tmp_path):
    write(str(tmp_path / "dir" / "file.txt"), "hi")
    os.makedirs(str(tmp_path / "dir" / "child"), exist_ok=True)
    panel = Panel(fs, str(tmp_path / "dir"))
    names = [e.name for e in panel.entries]
    # Parent entry plus the directory (sorted first) and the file.
    assert ".." in names
    assert "child" in names
    assert "file.txt" in names
    # Directories sort before files.
    non_parent = [n for n in names if n != ".."]
    assert non_parent.index("child") < non_parent.index("file.txt")


def test_panel_hidden_toggle(fs, tmp_path):
    d = tmp_path / "d"
    write(str(d / "visible.txt"), "v")
    write(str(d / ".secret"), "s")
    panel = Panel(fs, str(d))
    # Shown by default.
    assert ".secret" in [e.name for e in panel.entries]
    panel.toggle_hidden()
    names = [e.name for e in panel.entries]
    assert ".secret" not in names
    assert "visible.txt" in names
    panel.toggle_hidden()
    assert ".secret" in [e.name for e in panel.entries]


def test_ftp_list_parser_unix():
    # Servers that answer MLSD with "500 Unknown command" fall back to LIST;
    # verify the ls -l parser handles the common shapes.
    parse = FTPFileSystem._parse_list_line

    d = parse("drwxr-xr-x   2 owner group     4096 Jul 20 12:00 my dir")
    assert d is not None and d.is_dir and d.name == "my dir" and d.size == 4096

    f = parse("-rw-r--r--   1 owner group      842 Jul 22  2024 file.txt")
    assert f is not None and not f.is_dir and f.name == "file.txt" and f.size == 842

    link = parse("lrwxrwxrwx   1 o g    7 Jul 22 12:00 link -> /tmp/target")
    assert link is not None and link.is_symlink and link.name == "link"

    # ACL marker, and junk lines are ignored.
    assert parse("-rw-r--r--+  1 o g  100 Jan 05 09:15 acl") is not None
    assert parse("total 48") is None
    assert parse("") is None


def test_ftp_list_parser_dos():
    parse = FTPFileSystem._parse_list_line
    d = parse("07-22-25  09:15AM       <DIR>          images")
    assert d is not None and d.is_dir and d.name == "images"

    f = parse("07-22-2025  09:15AM              1024 report.pdf")
    assert f is not None and not f.is_dir and f.name == "report.pdf" and f.size == 1024


# -- SSH shell backend: read fallbacks (faked, no paramiko needed) ---------
class _FakeChannel:
    def __init__(self, status):
        self._status = status

    def recv_exit_status(self):
        return self._status

    def shutdown_write(self):
        pass


class _FakeStdout:
    def __init__(self, data: bytes, status: int):
        self._data = data
        self.channel = _FakeChannel(status)

    def read(self, n=-1):
        if n is None or n < 0:
            d, self._data = self._data, b""
            return d
        d, self._data = self._data[:n], self._data[n:]
        return d


class _FakeStderr:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data


class _FakeStdin:
    def __init__(self, fail: bool):
        self._fail = fail
        self.received = bytearray()
        self.channel = _FakeChannel(1 if fail else 0)

    def write(self, data):
        if self._fail:
            raise OSError("channel closed")
        self.received.extend(data)


class _FakeSSHClient:
    """Maps command prefixes to canned (stdout, stderr, exit) responses."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def exec_command(self, cmd, timeout=None):
        self.calls.append(cmd)
        for prefix, (out, err, status) in self.responses.items():
            if cmd.startswith(prefix):
                stdin = _FakeStdin(fail=status != 0)
                stdout = _FakeStdout(out, status)
                stdout.channel = _FakeChannel(status)
                return stdin, stdout, _FakeStderr(err)
        return (_FakeStdin(True), _FakeStdout(b"", 127),
                _FakeStderr(b"not found"))


def _fake_ssh_fs(responses) -> SSHFileSystem:
    fs = SSHFileSystem.__new__(SSHFileSystem)
    fs._client = _FakeSSHClient(responses)
    fs._read_templates = list(SSHFileSystem.READ_TEMPLATES)
    fs._write_templates = list(SSHFileSystem.WRITE_TEMPLATES)
    return fs


def test_ssh_read_uses_cat_when_available():
    fs = _fake_ssh_fs({"cat ": (b"hello world", b"", 0)})
    reader = fs.open_read("/tmp/f.txt")
    assert reader.read() == b"hello world"
    assert fs._read_templates[0].startswith("cat")


def test_ssh_read_falls_back_to_dd_when_cat_missing():
    # A restricted shell that answers cat with "Command 'cat' not supported".
    fs = _fake_ssh_fs({
        "cat ": (b"", b"-sh: Command 'cat' not supported\n", 127),
        "dd if=": (b"file body", b"0+1 records in\n", 0),
    })
    reader = fs.open_read("/flash/config.txt")
    assert reader.read() == b"file body"
    # dd is promoted; the next read skips the dead cat straight away.
    assert fs._read_templates[0].startswith("dd")
    fs._client.calls.clear()
    reader2 = fs.open_read("/flash/config.txt")
    assert reader2.read() == b"file body"
    assert fs._client.calls[0].startswith("dd")


def test_ssh_read_empty_file_is_not_a_failure():
    fs = _fake_ssh_fs({"cat ": (b"", b"", 0)})
    reader = fs.open_read("/tmp/empty")
    assert reader.read() == b""


def test_ssh_read_partial_reads_keep_probe_byte():
    fs = _fake_ssh_fs({"cat ": (b"abcdef", b"", 0)})
    reader = fs.open_read("/tmp/f")
    assert reader.read(2) == b"ab"
    assert reader.read(2) == b"cd"
    assert reader.read() == b"ef"


def test_ssh_write_falls_back_to_dd():
    fs = _fake_ssh_fs({
        "cat > ": (b"", b"not supported", 127),
        "dd of=": (b"", b"", 0),
    })
    writer = fs.open_write("/flash/new.txt")
    writer.write(b"payload")
    writer.close()  # cat> fails at close, dd replay succeeds
    dd_calls = [c for c in fs._client.calls if c.startswith("dd of=")]
    assert dd_calls, "dd fallback was not attempted"
    assert fs._write_templates[0].startswith("dd")


def test_scp_header_parsing():
    assert _parse_scp_header("C0644 1234 config.txt\n") == 1234
    assert _parse_scp_header("C0755 0 empty\n") == 0
    with pytest.raises(FileSystemError):
        _parse_scp_header("garbage\n")


# -- editor key handling ------------------------------------------------------
def test_editor_typing_and_save(fs, tmp_path):
    import curses

    from martin_commander.editor import Editor

    path = str(tmp_path / "note.txt")
    ed = Editor(fs, path)
    for ch in "hello":
        ed.handle_key(ord(ch))
    ed.handle_key(10)  # Enter
    for ch in "world":
        ed.handle_key(ord(ch))
    assert ed.dirty
    # F2 saves (VS Code-safe alias for Ctrl-S).
    assert ed.handle_key(curses.KEY_F2) is None
    assert not ed.dirty
    assert open(path).read() == "hello\nworld"


def test_editor_save_aliases(fs, tmp_path):
    import curses

    from martin_commander.editor import Editor

    path = str(tmp_path / "a.txt")
    for key in (19, 15, curses.KEY_F2):  # Ctrl-S, Ctrl-O, F2
        ed = Editor(fs, path)
        ed.handle_key(ord("x"))
        ed.handle_key(key)
        assert not ed.dirty, f"key {key} did not save"


def test_editor_quit_aliases_and_no_esc(fs, tmp_path):
    import curses

    from martin_commander.editor import Editor

    ed = Editor(fs, str(tmp_path / "b.txt"))
    assert ed.handle_key(17) == "quit"                # Ctrl-Q
    assert ed.handle_key(curses.KEY_F10) == "quit"    # F10
    # Esc is NOT a quit key (and must not insert anything either).
    before = list(ed.lines)
    assert ed.handle_key(27) is None
    assert ed.lines == before


def test_editor_delete_line_aliases(fs, tmp_path):
    from martin_commander.editor import Editor

    path = str(tmp_path / "c.txt")
    write(path, "one\ntwo\nthree")
    ed = Editor(fs, path)
    ed.handle_key(11)   # Ctrl-K deletes "one"
    assert ed.lines[0] == "two"
    ed.handle_key(25)   # Ctrl-Y (VS Code-safe alias) deletes "two"
    assert ed.lines[0] == "three"


# -- in-pane terminal ---------------------------------------------------------
def test_term_emulator_basics():
    from martin_commander.plugins.terminal import TermEmulator

    t = TermEmulator()
    t.feed(b"hello\r\nworld")
    assert t.lines == ["hello"]
    assert t.cur == "world"

    # \r + overwrite (how shells redraw a prompt line)
    t.feed(b"\rWORLD")
    assert t.cur == "WORLD"

    # backspace + erase to end of line (CSI K)
    t.feed(b"\b\b\x1b[K")
    assert t.cur == "WOR"

    # SGR colours and OSC titles are stripped
    t.feed(b"\x1b[1;32mgreen\x1b[0m")
    assert t.cur == "WORgreen"
    t.feed(b"\x1b]0;window title\x07!")
    assert t.cur == "WORgreen!"


def test_term_emulator_clear_and_tabs():
    from martin_commander.plugins.terminal import TermEmulator

    t = TermEmulator()
    t.feed(b"junk\r\nmore\x1b[2J")
    assert t.lines == [] and t.cur == ""
    t.feed(b"a\tb")
    assert t.cur == "a       b"


def test_term_emulator_visible_window():
    from martin_commander.plugins.terminal import TermEmulator

    t = TermEmulator()
    for i in range(30):
        t.feed(f"line{i}\n".encode())
    vis = t.visible(5)
    assert vis[-1] == ""          # current (empty) line
    assert vis[0] == "line26"
    back = t.visible(5, scroll=10)
    assert back[-1] == "line20"


def test_terminal_plugin_local_shell_roundtrip(fs, tmp_path):
    import time
    from types import SimpleNamespace

    from martin_commander.plugins.terminal import TerminalPlugin

    workdir = tmp_path / "termwork"
    workdir.mkdir()
    panel = Panel(fs, str(workdir))
    ctx = SimpleNamespace(own_panel=panel, other_panel=panel,
                          own_fs=fs, own_path=str(workdir))
    plug = TerminalPlugin(ctx)
    try:
        # Type a command and press Enter.
        for ch in "echo MARKER_$((6*7))":
            plug.handle_key(ord(ch))
        plug.handle_key(10)
        deadline = time.time() + 10
        joined = ""
        while time.time() < deadline:
            plug.tick()
            joined = "\n".join(plug.term.lines + [plug.term.cur])
            if "MARKER_42" in joined:
                break
            time.sleep(0.05)
        assert "MARKER_42" in joined
        # The shell started in the pane's directory.
        for ch in "pwd":
            plug.handle_key(ord(ch))
        plug.handle_key(10)
        deadline = time.time() + 10
        while time.time() < deadline:
            plug.tick()
            joined = "\n".join(plug.term.lines + [plug.term.cur])
            if str(workdir) in joined:
                break
            time.sleep(0.05)
        assert str(workdir) in joined
        # Ctrl-] closes the plugin.
        assert plug.handle_key(29) is False
    finally:
        plug.on_exit()


# -- pane plugins -----------------------------------------------------------
def test_plugin_discovery_finds_builtins():
    from martin_commander.plugins import discover

    classes, errors = discover()
    names = [c.name for c in classes]
    assert "Find in other pane" in names
    assert "JSON push" in names
    assert "Run remote script" in names
    assert not errors


def test_plugin_discovery_user_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    pdir = tmp_path / "martin-commander" / "plugins"
    pdir.mkdir(parents=True)
    (pdir / "my_plug.py").write_text(
        "from martin_commander.plugin_api import InputOutputPlugin\n"
        "class Mine(InputOutputPlugin):\n"
        "    name = 'My user plug-in'\n"
        "    description = 'test'\n"
        "    def process(self, line):\n"
        "        return [line]\n"
    )
    from martin_commander.plugins import discover

    classes, errors = discover()
    assert "My user plug-in" in [c.name for c in classes]
    assert not errors


def test_config_defaults_and_plugin_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from martin_commander import config as config_mod

    path = config_mod.ensure_config()
    assert path.endswith("config.ini")
    assert "plugin:run_remote_script" in open(path).read()

    # Values from the file override defaults; empty values keep defaults;
    # int defaults are coerced.
    (tmp_path / "martin-commander" / "config.ini").write_text(
        "[plugin:demo]\nhost = h.example.com\nport = 2222\nusername =\n")
    merged = config_mod.plugin_settings(
        "demo", {"host": "", "port": 22, "username": "fallback"})
    assert merged["host"] == "h.example.com"
    assert merged["port"] == 2222
    assert merged["username"] == "fallback"


def test_io_plugin_input_and_process():
    from martin_commander.plugin_api import InputOutputPlugin

    class Echo(InputOutputPlugin):
        name = "Echo"
        description = "test"

        def process(self, line):
            return [line.upper()]

    plug = Echo(ctx=None)
    for ch in "abc":
        assert plug.handle_key(ord(ch)) is True
    plug.handle_key(10)  # Enter
    assert "ABC" in plug.output
    assert plug.history == ["abc"]
    # Tab is passed back to the app; Esc closes the plugin.
    assert plug.handle_key(9) is None
    assert plug.handle_key(27) is False


def test_io_plugin_catches_process_errors():
    from martin_commander.plugin_api import InputOutputPlugin

    class Boom(InputOutputPlugin):
        name = "Boom"
        description = "test"

        def process(self, line):
            raise RuntimeError("kapow")

    plug = Boom(ctx=None)
    for ch in "hi":
        plug.handle_key(ord(ch))
    plug.handle_key(10)
    assert any("kapow" in l for l in plug.output)


def test_find_files_plugin_searches_other_pane(fs, tmp_path):
    from types import SimpleNamespace

    from martin_commander.plugins.find_files import FindFiles

    write(str(tmp_path / "data" / "notes.txt"), "n")
    write(str(tmp_path / "data" / "sub" / "todo.txt"), "t")
    write(str(tmp_path / "data" / "image.png"), "p")

    other = Panel(fs, str(tmp_path / "data"))
    ctx = SimpleNamespace(other_fs=fs, other_path=str(tmp_path / "data"),
                          other_panel=other)
    plug = FindFiles(ctx)
    plug.process("*.txt")
    joined = "\n".join(plug.output)
    assert "notes.txt" in joined
    assert "sub/todo.txt" in joined
    assert "image.png" not in joined


def test_panel_selection(fs, tmp_path):
    write(str(tmp_path / "d" / "one"), "1")
    write(str(tmp_path / "d" / "two"), "2")
    panel = Panel(fs, str(tmp_path / "d"))
    panel.select_all()
    assert "one" in panel.selected and "two" in panel.selected
    panel.clear_selection()
    assert not panel.selected
