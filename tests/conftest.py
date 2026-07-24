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
