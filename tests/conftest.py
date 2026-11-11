# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
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
    """An App with two local panes and a config directory of its own.

    F8 deletes for good here, as it did before the trash (MC-018): the delete
    tests are about deleting, and a trash would be the machine's own.  The
    trash's tests turn it on, with a data directory of their own.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    for side in ("left", "right"):
        (tmp_path / side / "sub").mkdir(parents=True)
        write(str(tmp_path / side / "file.txt"), side)
    app = App(_StubScreen(), str(tmp_path / "left"), str(tmp_path / "right"))
    app.use_trash = False
    return app


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
