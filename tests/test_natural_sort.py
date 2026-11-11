# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Ctrl-T's natural name order: run_2 before run_10 (MC-016)."""

from __future__ import annotations

import time

import pytest

from meridian_commander.filesystems import DirEntry, LocalFileSystem
from meridian_commander.panel import Panel, natural_key

from support import _ScriptedDialogs, write, write_zip


@pytest.mark.parametrize("names, expected", [
    (["run_10", "run_2", "run_100"], ["run_2", "run_10", "run_100"]),
    (["a1b10", "a1b2"], ["a1b2", "a1b10"]),
    (["beta", "Alpha", "gamma"], ["Alpha", "beta", "gamma"]),
    (["10", "2", "x"], ["2", "10", "x"]),
    (["RUN_3", "run_10", "run_2"], ["run_2", "RUN_3", "run_10"]),
])
def test_natural_order(names, expected):
    assert sorted(names, key=natural_key) == expected


def test_ties_come_out_the_same_way_every_time():
    assert sorted(["file1", "file01"], key=natural_key) == \
        sorted(["file01", "file1"], key=natural_key) == ["file01", "file1"]


def _tree(tmp_path):
    for name in ("run_10.tsv", "run_2.tsv", "run_1.tsv"):
        write(str(tmp_path / "d" / name), "x")
    for name in ("dir_10", "dir_9"):
        (tmp_path / "d" / name).mkdir()
    return tmp_path / "d"


def test_directories_first_and_the_way_up_first_of_all(tmp_path):
    panel = Panel(LocalFileSystem(), str(_tree(tmp_path)))
    panel.set_sort("natural")
    assert [e.name for e in panel.entries] == [
        "..", "dir_9", "dir_10", "run_1.tsv", "run_2.tsv", "run_10.tsv"]
    panel.set_sort("natural")                  # again: reversed
    assert [e.name for e in panel.entries][:3] == ["..", "dir_10", "dir_9"]


def test_inside_an_archive_too(tmp_path):
    write_zip(str(tmp_path / "z.zip"), {"run_10": "x", "run_2": "y"})
    from meridian_commander.archive import open_archive

    fs = open_archive(LocalFileSystem(), str(tmp_path / "z.zip"))
    panel = Panel(fs, "/")
    panel.set_sort("natural")
    assert [e.name for e in panel.entries] == ["run_2", "run_10"]


def test_the_sort_menu_offers_it(app, tmp_path, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch, menu=["natural"])
    app._sort_menu()
    assert app.active.sort_key == "natural"
    assert app.message == "Sorted by natural name order"
    assert "Name (natural: run_2 before run_10)" in scripted.menus[0][1]


def test_it_costs_little_more_than_the_plain_order():
    """100,000 names; measured, and recorded on the ticket."""
    entries = [DirEntry(name=f"run_{i}_part{i % 7}.tsv", is_dir=False)
               for i in range(100_000, 0, -1)]

    def best(key):
        times = []
        for _ in range(3):
            start = time.perf_counter()
            sorted(entries, key=key)
            times.append(time.perf_counter() - start)
        return min(times)

    plain = best(lambda e: e.name.lower())
    natural = best(lambda e: natural_key(e.name))
    print(f"plain {plain:.3f}s natural {natural:.3f}s")
    assert natural < plain * 25            # a loose bound: CI machines vary
