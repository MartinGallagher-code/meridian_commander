# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""``w``: open a file with the system's own application (MC-019)."""

from __future__ import annotations

import io
import os
import stat
import subprocess
import zipfile

import pytest

from meridian_commander import opener
from meridian_commander.app import MENUS
from meridian_commander.archive import ArchiveFileSystem
from meridian_commander.filesystems import LocalFileSystem
from meridian_commander.opener import NoOpener, SystemOpener, opener_argv

from support import _FakeRemoteBackend


def _which(found):
    return lambda name: f"/usr/bin/{name}" if name in found else None


# -- which opener ------------------------------------------------------------------

def test_macos_uses_open():
    assert opener_argv("darwin", {}, _which({"open"})) == ["/usr/bin/open"]


def test_macos_without_open():
    with pytest.raises(NoOpener, match="open is not on the PATH"):
        opener_argv("darwin", {}, _which(set()))


def test_linux_uses_xdg_open_with_a_display(monkeypatch):
    monkeypatch.setattr(opener.platform, "release", lambda: "6.1.0")
    for env in ({"DISPLAY": ":0"}, {"WAYLAND_DISPLAY": "wayland-0"}):
        assert opener_argv("linux", env, _which({"xdg-open"})) == \
            ["/usr/bin/xdg-open"]


def test_linux_without_a_display(monkeypatch):
    monkeypatch.setattr(opener.platform, "release", lambda: "6.1.0")
    with pytest.raises(NoOpener, match="no desktop here"):
        opener_argv("linux", {}, _which({"xdg-open"}))


def test_linux_without_xdg_open(monkeypatch):
    monkeypatch.setattr(opener.platform, "release", lambda: "6.1.0")
    with pytest.raises(NoOpener, match="xdg-open is not on the PATH"):
        opener_argv("linux", {"DISPLAY": ":0"}, _which(set()))


def test_wsl_prefers_wslview(monkeypatch):
    monkeypatch.setattr(opener.platform, "release",
                        lambda: "5.15.90.1-microsoft-standard-WSL2")
    # No DISPLAY needed: wslview hands the file to Windows.
    assert opener_argv("linux", {}, _which({"wslview", "xdg-open"})) == \
        ["/usr/bin/wslview"]
    assert opener_argv("linux", {"WSL_DISTRO_NAME": "Ubuntu"},
                       _which({"wslview"})) == ["/usr/bin/wslview"]


def test_wsl_without_wslview_falls_back_to_xdg_open(monkeypatch):
    monkeypatch.setattr(opener.platform, "release", lambda: "6.1.0")
    env = {"WSL_DISTRO_NAME": "Ubuntu", "DISPLAY": ":0"}
    assert opener_argv("linux", env, _which({"xdg-open"})) == \
        ["/usr/bin/xdg-open"]
    with pytest.raises(NoOpener):
        opener_argv("linux", {"WSL_DISTRO_NAME": "Ubuntu"},
                    _which({"xdg-open"}))


def test_the_defaults_read_this_machine(monkeypatch):
    monkeypatch.setattr(opener.sys, "platform", "darwin")
    monkeypatch.setattr(opener.shutil, "which", lambda name: "/x/" + name)
    assert opener_argv(which=opener.shutil.which) == ["/x/open"]


def test_start_is_detached_and_silent(monkeypatch):
    seen = {}

    def popen(argv, **kwargs):
        seen.update(kwargs, argv=argv)

    monkeypatch.setattr(subprocess, "Popen", popen)
    opener.start(["/usr/bin/open", "/tmp/a.pdf"])
    assert seen["argv"] == ["/usr/bin/open", "/tmp/a.pdf"]
    assert seen["start_new_session"] is True
    for stream in ("stdin", "stdout", "stderr"):
        assert seen[stream] == subprocess.DEVNULL


# -- what is opened ------------------------------------------------------------------

class _Remote:
    """A remote backend: bytes behind a connection, no path on this machine."""

    def __init__(self, files):
        self.files = files
        self.closed = []

    def basename(self, path):
        return path.rsplit("/", 1)[-1]

    def open_read(self, path):
        data = self.files[path]
        if isinstance(data, Exception):
            raise data
        reader = io.BytesIO(data)
        original = reader.close

        def close():
            self.closed.append(path)
            original()
            raise OSError("already gone")       # closing is best effort

        reader.close = close
        return reader


@pytest.fixture
def launched():
    return []


@pytest.fixture
def sysopen(launched):
    return SystemOpener(argv=lambda: ["/usr/bin/open"], launch=launched.append)


def test_a_local_file_is_opened_where_it_is(sysopen, launched, tmp_path):
    (tmp_path / "plot.png").write_bytes(b"png")
    fs = LocalFileSystem()
    message = sysopen.open(fs, str(tmp_path / "plot.png"), False)
    assert message == "Opened plot.png"
    assert launched == [["/usr/bin/open", str(tmp_path / "plot.png")]]
    assert sysopen.workdir is None                # nothing copied


def test_a_local_directory_opens_in_the_file_browser(sysopen, launched,
                                                     tmp_path):
    fs = LocalFileSystem()
    assert sysopen.open(fs, str(tmp_path), True) == \
        f"Opened {os.path.basename(str(tmp_path))}"
    assert launched == [["/usr/bin/open", str(tmp_path)]]


def test_a_remote_directory_is_refused(sysopen, launched):
    message = sysopen.open(_Remote({}), "/srv/results", True)
    assert message == "Cannot open results: not on a remote pane"
    assert launched == []


def test_a_remote_file_is_opened_from_a_private_copy(sysopen, launched,
                                                     monkeypatch):
    monkeypatch.setattr(opener, "CHUNK", 3)       # several reads
    remote = _Remote({"/srv/report.pdf": b"%PDF-1.7 body",
                      "/other/report.pdf": b"second"})
    message = sysopen.open(remote, "/srv/report.pdf", False)
    assert message.startswith("Opened a copy of report.pdf")
    assert "not sent back" in message
    copy = launched[0][1]
    assert os.path.basename(copy) == "report.pdf"
    with open(copy, "rb") as handle:
        assert handle.read() == b"%PDF-1.7 body"
    assert stat.S_IMODE(os.stat(sysopen.workdir).st_mode) == 0o700
    assert copy.startswith(sysopen.workdir + os.sep)
    assert remote.closed == ["/srv/report.pdf"]
    # A second file of the same name gets its own copy, not the first's.
    sysopen.open(remote, "/other/report.pdf", False)
    assert launched[1][1] != copy
    with open(copy, "rb") as handle:
        assert handle.read() == b"%PDF-1.7 body"
    workdir = sysopen.workdir
    sysopen.cleanup()
    assert not os.path.exists(workdir)
    assert sysopen.workdir is None
    sysopen.cleanup()                             # twice is fine


def test_a_remote_file_that_will_not_read(sysopen, launched):
    remote = _Remote({"/srv/a.pdf": OSError("connection lost")})
    assert sysopen.open(remote, "/srv/a.pdf", False) == \
        "Cannot fetch a.pdf: connection lost"
    assert launched == []


def test_a_file_in_an_archive_is_copied(sysopen, launched, tmp_path):
    path = str(tmp_path / "bundle.zip")
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("docs/guide.pdf", b"%PDF guide")
    fs = ArchiveFileSystem(LocalFileSystem(), path)
    sysopen.open(fs, "/docs/guide.pdf", False)
    with open(launched[0][1], "rb") as handle:
        assert handle.read() == b"%PDF guide"


def test_no_desktop_runs_nothing_and_says_why(launched, tmp_path):
    def none():
        raise NoOpener("there is no desktop here")

    sysopen = SystemOpener(argv=none, launch=launched.append)
    assert sysopen.open(LocalFileSystem(), str(tmp_path), True) == \
        f"Cannot open {os.path.basename(str(tmp_path))}: there is no desktop here"
    assert launched == []


def test_an_opener_that_will_not_start(tmp_path):
    def fail(argv):
        raise FileNotFoundError("gone")

    sysopen = SystemOpener(argv=lambda: ["/usr/bin/xdg-open"], launch=fail)
    assert sysopen.open(LocalFileSystem(), str(tmp_path), True) == \
        "Could not start xdg-open: gone"


# -- in the app ----------------------------------------------------------------------

def test_w_opens_the_cursor_entry(app, launched):
    app.opener = SystemOpener(argv=lambda: ["open"], launch=launched.append)
    names = [e.name for e in app.left.entries]
    app.left.move_to(names.index("file.txt"))
    app.handle_key(ord("w"))
    assert launched == [["open", os.path.join(app.left.path, "file.txt")]]
    assert app.message == "Opened file.txt"


def test_w_on_a_remote_directory(app, launched):
    app.opener = SystemOpener(argv=lambda: ["open"], launch=launched.append)

    class Remote(_FakeRemoteBackend):
        local_path = None

    app.left.set_location(Remote(), app.left.path)
    names = [e.name for e in app.left.entries]
    app.left.move_to(names.index("sub"))
    app.handle_key(ord("w"))
    assert launched == []
    assert app.message == "Cannot open sub: not on a remote pane"


def test_w_on_nothing_and_on_a_plugin_pane(app, launched):
    app.opener = SystemOpener(argv=lambda: ["open"], launch=launched.append)
    app.left.move_to(0)                           # ".."
    app.handle_key(ord("w"))
    assert app.message == "Nothing to open"
    app.left.plugin = object()
    app._open_with()
    assert app.message == "Open with works on a directory listing"
    app.left.plugin = None
    assert launched == []


def test_the_file_menu_offers_it(app, launched):
    app.opener = SystemOpener(argv=lambda: ["open"], launch=launched.append)
    file_menu = next(m for m in MENUS if m["name"] == "file")
    assert any(i.get("name") == "open_with" and i.get("key") == "w"
               for i in file_menu["items"])
    names = [e.name for e in app.left.entries]
    app.left.move_to(names.index("file.txt"))
    app._dispatch("open_with")
    assert len(launched) == 1


def test_copies_go_when_the_app_closes(app, tmp_path):
    app.opener = SystemOpener(argv=lambda: ["open"], launch=lambda argv: None)
    app.opener.open(_Remote({"/a.txt": b"x"}), "/a.txt", False)
    workdir = app.opener.workdir
    assert os.path.isdir(workdir)
    app._close_backends()
    assert not os.path.exists(workdir)
