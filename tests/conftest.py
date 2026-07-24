"""Fixtures shared by the whole suite."""

from __future__ import annotations

import pytest

from meridian_commander.app import App
from meridian_commander.filesystems import LocalFileSystem

from support import _StubScreen, write


@pytest.fixture
def fs():
    return LocalFileSystem()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """An App with two local panes and a config directory of its own."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for side in ("left", "right"):
        (tmp_path / side / "sub").mkdir(parents=True)
        write(str(tmp_path / side / "file.txt"), side)
    return App(_StubScreen(), str(tmp_path / "left"), str(tmp_path / "right"))


@pytest.fixture
def data_ctx(app, tmp_path):
    """Factory for a plugin context whose *other* pane holds data files.

    Returns a callable taking ``{filename: contents}`` and the names to tag.
    The panel and context are real, so the plug-ins read and write through
    the same path the application uses.
    """
    from meridian_commander.plugin_api import PluginContext

    def make(files: dict, selected=()):
        directory = tmp_path / "data"
        directory.mkdir(exist_ok=True)
        for name, content in files.items():
            write(str(directory / name), content)
        app.right.chdir(str(directory))
        app.right.selected = set(selected)
        return PluginContext(app=app, own_panel=app.left, other_panel=app.right)

    return make
