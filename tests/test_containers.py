# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""The Containers plug-in: Podman or Docker images and containers in a pane.

The engines here are stand-ins: small scripts called ``podman`` and
``docker`` on a ``PATH`` each test sets for itself, which answer from a
script of canned replies and write down every argument list they are given.
A test must not need a particular machine, and CI's runners have a real
Docker installed -- so ``PATH`` must not reach it.

The canned replies are what Podman 4.9.3 and Docker 29.9.2 really printed for
the plug-in's own ``--format`` templates, recorded once with real images and
containers, published ports, an image under two names and an untagged one.
"""

from __future__ import annotations

import curses
import json
import os
import sys
import time
import types

import pytest

from meridian_commander import dialogs
from meridian_commander.dialogs import accelerators
from meridian_commander.plugin_api import PluginContext
from meridian_commander.plugins import containers as containers_mod
from meridian_commander.plugins import discover
from meridian_commander.plugins.containers import (
    IMAGES_FORMAT, PS_FORMAT, SEARCH_FORMAT, ContainersPlugin, EngineTerminal,
    age_seconds, find_engine, parse_containers, parse_images, parse_search,
    pulled_image, size_bytes, split_reference,
)
from meridian_commander.plugins.terminal import MENU_KEY

from support import (
    _FakeRemoteBackend, _ScriptedDialogs, terminal_text, with_curses_screen,
)


# -- what the real engines printed -------------------------------------------------

PODMAN_PS = (
    "7cbf49a196e4\tweb-1\tquay.io/libpod/alpine:latest\trunning\t"
    "Up 5 seconds\t\n"
    "d12be249bcf5\tstopped_one\tpublic.ecr.aws/docker/library/busybox:latest"
    "\texited\tExited (0) 4 seconds ago\t\n"
    "416316feaf00\tcreated-only\tpublic.ecr.aws/docker/library/busybox:latest"
    "\tcreated\tCreated\t\n"
    "28498488b86a\tported\tquay.io/libpod/alpine:latest\trunning\t"
    "Up Less than a second\t0.0.0.0:8080->80/tcp, 0.0.0.0:5353->53/udp, "
    "127.0.0.1:8443->443/tcp\n"
)
PODMAN_IMAGES = (
    "<none>\t<none>\t00cbeb27e6bb\tLess than a second ago\t5.85 MB\n"
    "localhost/mybox\tv2\taaef90e06523\t6 months ago\t4.69 MB\n"
    "public.ecr.aws/docker/library/busybox\tlatest\taaef90e06523\t"
    "6 months ago\t4.69 MB\n"
    "quay.io/libpod/alpine\tlatest\t961769676411\t7 years ago\t5.85 MB\n"
)
PODMAN_SEARCH = (
    "quay.io/libpod/alpine\tThis image is used for testing purposes only...\n"
    "quay.io/libpod/alpine_healthcheck\tThis is a test image used in podman "
    "e2e test...\n"
    "quay.io/libpod/alpine_nginx\tThis image is used in podman e2e tests, "
    "and...\n"
)
PODMAN_DOWN = (
    "Cannot connect to Podman. Please verify your connection to the Linux "
    "system using `podman system connection list`, or try `podman machine "
    "init` and `podman machine start` to manage a new Linux VM\n"
    "Error: unable to connect to Podman socket: Get "
    "\"http://d/v4.9.3/libpod/_ping\": dial unix /nonexistent/podman.sock: "
    "connect: no such file or directory: unix:///nonexistent/podman.sock\n"
)
PODMAN_RMI_IN_USE = (
    "Error: image used by 7cbf49a196e409c895d1b88c4b1141ec0743dfb5437174dfbd"
    "39a1ee20644b11: image is in use by a container: consider listing "
    "external containers and force-removing image\n"
)

DOCKER_PS = (
    "813e733398f3\tported\tquay.io/libpod/alpine:latest\trunning\tUp 1 second"
    "\t0.0.0.0:6353->53/udp, 0.0.0.0:9080->80/tcp, 127.0.0.1:9443->443/tcp\n"
    "0d4b755b2759\tcreated-only\tpublic.ecr.aws/docker/library/busybox:latest"
    "\tcreated\tCreated\t\n"
    "fc9b6def36a6\tstopped_one\tpublic.ecr.aws/docker/library/busybox:latest"
    "\texited\tExited (0) 2 minutes ago\t\n"
    "6b62abbde7f4\tweb-1\tquay.io/libpod/alpine:latest\trunning\t"
    "Up 4 seconds\t\n"
)
DOCKER_IMAGES = (
    "localhost/mybox\tv2\ta46257e06c82\t6 months ago\t6.81MB\n"
    "public.ecr.aws/docker/library/busybox\tlatest\ta46257e06c82\t"
    "6 months ago\t6.81MB\n"
    "quay.io/libpod/alpine\tlatest\tfa93b01658e3\t7 years ago\t9.05MB\n"
)
#: Docker 29 lists an untagged image only with -a; this is its line then.
DOCKER_UNTAGGED = "<none>\t<none>\t26836d1455a1\t3 seconds ago\t9.06MB\n"
DOCKER_SEARCH = (
    "alpine\tA minimal Docker image based on Alpine Linux…\n"
    "alpine/git\tA  simple git container running in alpine li…\n"
    "alpine/socat\tRun socat command in alpine container\n"
)
DOCKER_DOWN = (
    "failed to connect to the docker API at unix:///var/run/docker.sock; "
    "check if the path is correct and if the daemon is running: dial unix "
    "/var/run/docker.sock: connect: no such file or directory\n"
)
DOCKER_RM_RUNNING = (
    "Error response from daemon: cannot remove container \"web-1\": "
    "container is running: stop the container before removing or force "
    "remove\n"
)
DOCKER_RMI_IN_USE = (
    "Error response from daemon: conflict: unable to delete fa93b01658e3 "
    "(cannot be forced) - image is being used by running container "
    "6b62abbde7f4\n"
)

REPLIES = {
    "podman": {"ps": {"out": PODMAN_PS}, "images": {"out": PODMAN_IMAGES},
               "search": {"out": PODMAN_SEARCH}},
    "docker": {"ps": {"out": DOCKER_PS}, "images": {"out": DOCKER_IMAGES},
               "search": {"out": DOCKER_SEARCH}},
}


# -- stand-in engines --------------------------------------------------------------

#: The stand-in: records its argv, then answers for its first argument from
#: the script beside it.  ``then`` changes the script afterwards -- a pull
#: that leaves a new image for the next listing; ``sleep`` waits before the
#: answer, ``hold`` after it.
STUB = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
engine = os.path.basename(sys.argv[0])
with open(os.path.join(here, "calls.jsonl"), "a") as log:
    log.write(json.dumps(sys.argv) + "\\n")
path = os.path.join(here, "script.json")
with open(path) as handle:
    script = json.load(handle)
answer = script[engine].get(sys.argv[1] if len(sys.argv) > 1 else "", {{}})
if answer.get("then"):
    script[engine].update(answer["then"])
    with open(path, "w") as handle:
        json.dump(script, handle)
time.sleep(answer.get("sleep", 0))
sys.stdout.write(answer.get("out", ""))
sys.stdout.flush()
sys.stderr.write(answer.get("err", ""))
sys.stderr.flush()
time.sleep(answer.get("hold", 0))
sys.exit(answer.get("code", 0))
"""


class Engines:
    """The stand-in engines on a ``PATH`` of the test's own."""

    def __init__(self, directory, monkeypatch) -> None:
        self.bin = directory
        self.bin.mkdir()
        monkeypatch.setenv("PATH", str(self.bin))
        self.script: dict = {}

    def install(self, *names: str) -> None:
        for name in names:
            path = self.bin / name
            path.write_text(STUB.format(python=sys.executable))
            path.chmod(0o755)
            self.script[name] = json.loads(json.dumps(REPLIES[name]))
        self._save()

    def answer(self, engine: str, verb: str, **reply) -> None:
        self.script[engine][verb] = reply
        self._save()

    def _save(self) -> None:
        (self.bin / "script.json").write_text(json.dumps(self.script))

    def calls(self) -> list[list[str]]:
        log = self.bin / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def verbs(self) -> list[str]:
        return [argv[1] for argv in self.calls()]

    def path(self, name: str) -> str:
        return str(self.bin / name)


@pytest.fixture
def engines(tmp_path, monkeypatch):
    return Engines(tmp_path / "bin", monkeypatch)


@pytest.fixture
def ctx(app):
    return PluginContext(app=app, own_panel=app.left, other_panel=app.right)


@pytest.fixture
def podman(engines, ctx):
    engines.install("podman")
    plugin = ContainersPlugin(ctx)
    yield plugin
    plugin.on_exit()


@pytest.fixture
def docker(engines, ctx):
    engines.install("docker")
    plugin = ContainersPlugin(ctx)
    yield plugin
    plugin.on_exit()


def _config(tmp_path, text: str) -> None:
    folder = tmp_path / "config" / "meridian-commander"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.ini").write_text(text)


def _pump(plugin, predicate, tries: int = 1000) -> bool:
    """Tick the plug-in until ``predicate`` holds: the stand-ins are real
    processes, on pipes and on a pty."""
    for _ in range(tries):
        plugin.tick()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _settle(plugin) -> None:
    assert _pump(plugin, lambda: plugin.task is None)


def _type(plugin, text: str) -> None:
    for char in text:
        plugin.handle_key(ord(char))


def _point(plugin, name: str) -> None:
    plugin.cursor = next(i for i, row in enumerate(plugin.view)
                         if row.name == name)


def _paint(plugin, rows: int = 20, cols: int = 100) -> list[str]:
    def paint(stdscr):
        plugin.draw(stdscr, 0, 0, rows, cols)
        return [stdscr.instr(r, 0, cols).decode(errors="replace")
                for r in range(rows)]

    return with_curses_screen(rows + 2, cols + 2, paint)


# -- parsing ----------------------------------------------------------------------

@pytest.mark.parametrize("text", [PODMAN_PS, DOCKER_PS])
def test_both_engines_ps_output_parses(text):
    rows = {row.name: row for row in parse_containers(text)}
    assert set(rows) == {"web-1", "stopped_one", "created-only", "ported"}
    assert rows["web-1"].running and rows["ported"].running
    assert not rows["stopped_one"].running
    assert not rows["created-only"].running
    assert rows["stopped_one"].status.startswith("Exited (0)")
    # Several ports, with spaces after the commas, stay one field.
    assert rows["ported"].ports.count("->") == 3
    assert rows["web-1"].ports == ""
    assert all(len(row.id) == 12 for row in rows.values())


@pytest.mark.parametrize("text, untagged", [
    (PODMAN_IMAGES, 1), (DOCKER_IMAGES + DOCKER_UNTAGGED, 1),
    (DOCKER_IMAGES, 0),
])
def test_both_engines_images_output_parses(text, untagged):
    images = parse_images(text)
    assert sum(1 for image in images if not image.tagged) == untagged
    names = [image.name for image in images]
    assert "localhost/mybox:v2" in names
    assert "quay.io/libpod/alpine:latest" in names
    mybox = next(image for image in images if image.repository ==
                 "localhost/mybox")
    busybox = next(image for image in images if "busybox" in image.repository)
    assert mybox.id == busybox.id          # one image, two names
    assert mybox.tag == "v2" and mybox.created == "6 months ago"


def test_an_untagged_image_is_named_none():
    image = parse_images(DOCKER_UNTAGGED)[0]
    assert image.name == "<none>" and image.label() == "<none> (26836d1455a1)"


@pytest.mark.parametrize("text", [PODMAN_SEARCH, DOCKER_SEARCH])
def test_both_engines_search_output_parses(text):
    found = parse_search(text)
    assert len(found) == 3
    assert [f.rank for f in found] == [0, 1, 2]
    assert found[0].description


def test_garbled_lines_are_skipped():
    text = ("CONTAINER ID  NAMES  IMAGE\n"                 # a header
            "\n"
            "not-hex-id\tx\timg\trunning\tUp\t\n"         # no ID
            "7cbf49a196e4\t\timg\trunning\tUp\t\n"         # no name
            "7cbf49a196e4\tok\timg\trunning\tUp\t\t\n"     # a field too many
            "7cbf49a196e4\tgood\timg\trunning\tUp 1 second\t\r\n")
    assert [row.name for row in parse_containers(text)] == ["good"]
    assert parse_images("x\ty\tnot-an-id\t1 day ago\t1MB\nshort\tline\n") == []
    assert parse_containers("") == [] and parse_images("") == []


def test_a_long_or_prefixed_id_is_shortened():
    text = ("sha256:961769676411f082461f9ef46626dd7a2d1e2b2a38e6a44364bcbecf51"
            "e66dd4")
    image = parse_images(f"repo\tlatest\t{text}\t1 day ago\t1MB\n")[0]
    assert image.id == "961769676411"


def test_empty_repository_fields_read_as_none():
    image = parse_images("\t\t961769676411\t1 day ago\t1MB\n")[0]
    assert image.repository == image.tag == "<none>"


def test_a_search_result_that_could_not_be_pulled_is_left_out():
    text = ("-evil\tstarts like an option\n"
            "two words\tspaces\n"
            "fine/name\t\n"
            "bare-name\n")
    assert [(f.name, f.description) for f in parse_search(text)] == [
        ("fine/name", ""), ("bare-name", "")]


@pytest.mark.parametrize("text, expected", [
    ("6.81MB", 6_810_000), ("4.69 MB", 4_690_000), ("0B", 0),
    ("12kB", 12_000), ("1.5GiB", int(1.5 * 1024 ** 3)), ("2 TB", 2 * 10 ** 12),
    ("bad", 0), ("", 0), ("1.2.3MB", 0), ("5 XB", 0),
])
def test_sizes_read_for_sorting(text, expected):
    assert size_bytes(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Less than a second ago", 0), ("3 seconds ago", 3),
    ("About a minute ago", 60), ("About an hour ago", 3600),
    ("2 days ago", 2 * 86400), ("3 weeks ago", 21 * 86400),
    ("6 months ago", 180 * 86400), ("7 years ago", 7 * 365 * 86400),
    ("", 0), ("Created", 0), ("many years ago", 0), ("3 fortnights ago", 0),
])
def test_ages_read_for_sorting(text, expected):
    assert age_seconds(text) == expected


@pytest.mark.parametrize("name, expected", [
    ("alpine", ("alpine", "latest")),
    ("quay.io/libpod/alpine:3.19", ("quay.io/libpod/alpine", "3.19")),
    ("localhost:5000/team/app", ("localhost:5000/team/app", "latest")),
    ("alpine@sha256:abc", ("alpine", None)),
])
def test_a_name_to_pull_splits_into_repository_and_tag(name, expected):
    assert split_reference(name) == expected


def test_the_pulled_image_is_found_by_its_name():
    images = parse_images(
        "docker.io/library/alpine\tlatest\t111111111111\t1 day ago\t1MB\n"
        "quay.io/libpod/alpine\tlatest\t222222222222\t1 day ago\t1MB\n"
        "alpine\tlatest\t333333333333\t1 day ago\t1MB\n"
        "alpine\t3.19\t444444444444\t1 day ago\t1MB\n")
    # An exact name wins over a longer one that ends the same way.
    assert pulled_image(images, "alpine").startswith("333333333333")
    assert pulled_image(images, "alpine:3.19").startswith("444444444444")
    assert pulled_image(images, "library/alpine").startswith("111111111111")
    assert pulled_image(images, "alpine@sha256:ab").startswith("333333333333")
    assert pulled_image(images, "nginx") is None


# -- choosing the engine ------------------------------------------------------------

def test_podman_is_preferred_when_both_are_installed(engines):
    engines.install("podman", "docker")
    assert find_engine("auto") == ("podman", engines.path("podman"))


def test_docker_is_the_fallback(engines):
    engines.install("docker")
    assert find_engine("auto") == ("docker", engines.path("docker"))


def test_the_setting_overrides_the_choice(engines, ctx, tmp_path):
    engines.install("podman", "docker")
    _config(tmp_path, "[plugin:containers]\nengine = Docker\n")
    plugin = ContainersPlugin(ctx)
    assert plugin.engine == "docker"
    assert engines.calls()[0][0] == engines.path("docker")


def test_an_engine_asked_for_but_missing_is_named(engines):
    engines.install("docker")
    with pytest.raises(RuntimeError, match="podman was not found"):
        find_engine("podman")


def test_an_engine_setting_that_is_not_one_is_refused(engines):
    engines.install("podman")
    with pytest.raises(RuntimeError, match="auto, podman or docker"):
        find_engine("containerd")


def test_with_neither_installed_it_does_not_open(engines, ctx):
    with pytest.raises(RuntimeError) as raised:
        ContainersPlugin(ctx)
    assert "podman" in str(raised.value) and "docker" in str(raised.value)


def test_with_neither_installed_the_menu_says_why(engines, app, monkeypatch):
    scripted = _ScriptedDialogs(monkeypatch, menu=["Containers"])
    app._plugin_mode()
    assert app.left.plugin is None
    assert "Containers failed to start" in scripted.last_message
    assert "Neither podman nor docker" in scripted.last_message


# -- local panes only --------------------------------------------------------------

def _pane(app, fs):
    panel = types.SimpleNamespace(fs=fs, path="/")
    return PluginContext(app=app, own_panel=panel, other_panel=app.right)


@pytest.mark.parametrize("fs", [
    _FakeRemoteBackend(),
    types.SimpleNamespace(scheme="ssh"),
    types.SimpleNamespace(scheme="ftp"),
    types.SimpleNamespace(scheme="zip"),
    types.SimpleNamespace(scheme="tar"),
    types.SimpleNamespace(),
])
def test_a_pane_that_is_not_local_is_refused(engines, app, fs):
    engines.install("podman", "docker")
    with pytest.raises(RuntimeError, match="local panes only"):
        ContainersPlugin(_pane(app, fs))
    assert engines.calls() == []          # the engine is never called


def test_the_menu_refuses_a_remote_pane_with_the_reason(engines, app,
                                                        monkeypatch):
    engines.install("podman")
    app.left.fs = _FakeRemoteBackend()
    scripted = _ScriptedDialogs(monkeypatch, menu=["Containers"])
    app._plugin_mode()
    assert app.left.plugin is None
    assert "local panes only" in scripted.last_message
    assert engines.calls() == []


# -- opening and listing --------------------------------------------------------------

def test_it_opens_from_the_menu_and_lists_running_containers(engines, app,
                                                             monkeypatch):
    engines.install("podman")
    _ScriptedDialogs(monkeypatch, menu=["Containers"])
    app._plugin_mode()
    plugin = app.left.plugin
    assert isinstance(plugin, ContainersPlugin)
    assert "opened" in app.message
    assert [row.name for row in plugin.view] == ["ported", "web-1"]
    assert engines.calls() == [
        [engines.path("podman"), "ps", "-a", "--format", PS_FORMAT],
        [engines.path("podman"), "images", "--format", IMAGES_FORMAT],
    ]
    app._tick_plugins()
    assert app.handle_key(27) is None and app.left.plugin is None


def test_the_caption_counts_what_the_engine_has(podman):
    assert podman._caption() == (" [containers] podman: 2 running, 2 stopped,"
                                 " 4 images ")


def test_a_shows_the_stopped_containers_too(podman):
    podman.handle_key(ord("a"))
    assert [row.name for row in podman.view] == [
        "created-only", "ported", "stopped_one", "web-1"]
    assert "stopped" in podman.status
    podman.handle_key(ord("a"))
    assert [row.name for row in podman.view] == ["ported", "web-1"]
    assert podman.status == "showing running containers"


def test_a_from_the_images_goes_to_the_containers(podman):
    podman.handle_key(ord("i"))
    podman.handle_key(ord("a"))
    assert podman.list == "containers" and podman.show_all


def test_one_key_switches_between_the_lists(podman):
    podman.handle_key(ord("i"))
    assert podman.list == "images"
    assert [row.name for row in podman.view][:2] == [
        "<none>", "localhost/mybox:v2"]
    podman.handle_key(ord("i"))               # already there: nothing moves
    assert podman.list == "images"
    podman.handle_key(ord("c"))
    assert podman.list == "containers"


def test_empty_lists_say_so(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "ps", out="")
    engines.answer("docker", "images", out="")
    plugin = ContainersPlugin(ctx)
    assert plugin.view == []
    assert "no containers" in _paint(plugin)[2]
    plugin.handle_key(ord("i"))
    assert "no images" in _paint(plugin)[2]


def test_with_none_running_it_points_at_a(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "ps", out="".join(
        line + "\n" for line in DOCKER_PS.splitlines() if "running" not in line))
    plugin = ContainersPlugin(ctx)
    assert "a shows the stopped ones" in _paint(plugin)[2]


# -- an engine that cannot be reached ---------------------------------------------------

@pytest.mark.parametrize("engine, text", [("docker", DOCKER_DOWN),
                                          ("podman", PODMAN_DOWN)])
def test_an_unreachable_engine_shows_its_own_message(engines, ctx, engine,
                                                     text):
    engines.install(engine)
    engines.answer(engine, "ps", err=text, code=125)
    plugin = ContainersPlugin(ctx)
    assert plugin.view == [] and plugin.containers == []
    first = text.splitlines()[0]
    assert first in plugin.error
    lines = _paint(plugin, cols=400)
    assert first in lines[2] and "r retries" in lines[2]


def test_the_message_is_the_one_on_stdout_if_stderr_is_empty(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "images", out="something went wrong\n", code=1)
    assert ContainersPlugin(ctx).error == "something went wrong"


def test_a_failure_with_no_message_gives_the_status(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "ps", code=3)
    assert ContainersPlugin(ctx).error == "docker failed (3)"


def test_r_retries_and_auto_refresh_waits_for_it(engines, ctx, monkeypatch):
    engines.install("docker")
    engines.answer("docker", "ps", err=DOCKER_DOWN, code=1)
    plugin = ContainersPlugin(ctx)
    engines.answer("docker", "ps", out=DOCKER_PS)
    monkeypatch.setattr(containers_mod.time, "monotonic",
                        lambda: plugin._last_refresh + 999)
    plugin.tick()                         # an error: no refresh by itself
    assert plugin.error and plugin.view == []
    plugin.handle_key(ord("r"))
    assert plugin.error == ""
    assert [row.name for row in plugin.view] == ["ported", "web-1"]


def test_a_failure_keeps_the_last_good_lists(podman, engines):
    engines.answer("podman", "images", err="Error: storage is locked\n",
                   code=125)
    podman.refresh()
    assert len(podman.images) == 4 and len(podman.view) == 2
    lines = _paint(podman)
    assert "storage is locked" in lines[18] and "r retries" in lines[18]


def test_an_engine_that_hangs_is_cut_off(podman, engines):
    engines.answer("podman", "ps", out=PODMAN_PS, sleep=5)
    podman.cfg["timeout"] = 0.5
    started = time.monotonic()
    podman.refresh()
    assert time.monotonic() - started < 4
    assert podman.error == "podman did not answer in 0.5 seconds"


def test_an_engine_that_has_gone_is_reported(podman, engines):
    os.remove(engines.path("podman"))
    podman.refresh()
    assert podman.error.startswith("podman could not be run:")


# -- refreshing --------------------------------------------------------------------

def test_a_refresh_keeps_the_cursor_on_the_same_id(podman, engines):
    _point(podman, "web-1")
    reordered = "\n".join(reversed(PODMAN_PS.splitlines())) + "\n"
    engines.answer("podman", "ps", out=reordered.replace("web-1", "renamed"))
    podman.refresh()
    assert podman.view[podman.cursor].id == "7cbf49a196e4"
    assert podman.view[podman.cursor].name == "renamed"


def test_a_refresh_after_the_item_has_gone_stays_in_the_list(podman, engines):
    podman.handle_key(curses.KEY_END)
    engines.answer("podman", "ps", out=PODMAN_PS.splitlines()[0] + "\n")
    podman.refresh()
    assert podman.cursor == 0 and len(podman.view) == 1


def test_tick_refreshes_on_the_configured_cadence(podman, tmp_path,
                                                  monkeypatch):
    times = iter([100.0, 101.0, 104.0, 104.0])
    monkeypatch.setattr(containers_mod.time, "monotonic", lambda: next(times))
    podman.refresh()                        # stamps 100.0
    podman.tick()                           # a second later: too soon
    assert podman._last_refresh == 100.0
    podman.tick()                           # four seconds: refreshes
    assert podman._last_refresh == 104.0


def test_the_refresh_setting_is_read(engines, ctx, tmp_path):
    engines.install("podman")
    _config(tmp_path, "[plugin:containers]\nrefresh = 30\ntimeout = 4\n")
    plugin = ContainersPlugin(ctx)
    assert plugin.cfg["refresh"] == 30 and plugin.cfg["timeout"] == 4


def test_tick_is_paused_while_a_prompt_is_open(podman, monkeypatch):
    monkeypatch.setattr(containers_mod.time, "monotonic",
                        lambda: podman._last_refresh + 999)
    podman.handle_key(ord("/"))
    before = podman._last_refresh
    podman.tick()
    assert podman._last_refresh == before


# -- keys ---------------------------------------------------------------------------

def test_navigation_keys_move_and_clamp(podman):
    podman.handle_key(ord("a"))
    # The cursor stays on the container it was on as the list grows.
    assert podman.view[podman.cursor].name == "ported"
    podman.handle_key(curses.KEY_HOME)
    podman.handle_key(curses.KEY_DOWN)
    assert podman.cursor == 1
    podman.handle_key(curses.KEY_NPAGE)
    assert podman.cursor == 3
    podman.handle_key(curses.KEY_HOME)
    assert podman.cursor == 0
    podman.handle_key(curses.KEY_UP)
    assert podman.cursor == 0
    podman.handle_key(curses.KEY_END)
    assert podman.cursor == 3
    podman.handle_key(curses.KEY_PPAGE)
    assert podman.cursor == 0


def test_sorting_steps_through_the_columns_and_reverses(podman):
    podman.handle_key(ord("a"))
    _point(podman, "web-1")
    podman.handle_key(ord("o"))             # by image
    assert podman.sorts["containers"] == ["image", False]
    assert podman.view[0].image.startswith("public.ecr.aws")
    assert podman.view[podman.cursor].name == "web-1"   # the cursor follows
    podman.handle_key(ord("o"))             # by state: running first
    assert [row.running for row in podman.view] == [True, True, False, False]
    podman.handle_key(ord("O"))
    assert [row.running for row in podman.view] == [False, False, True, True]
    podman.handle_key(ord("o"))             # round to the name again
    assert podman.sorts["containers"] == ["name", True]
    assert podman.view[0].name == "web-1"


def test_images_sort_by_age_and_size(podman):
    podman.handle_key(ord("i"))
    for _ in range(2):
        podman.handle_key(ord("o"))         # tag, then age
    assert podman.view[0].created == "Less than a second ago"
    podman.handle_key(ord("O"))
    assert podman.view[0].created == "7 years ago"
    podman.handle_key(ord("o"))             # size, biggest first now
    assert podman.view[0].size == "5.85 MB"


def test_filtering_narrows_and_esc_clears(podman):
    podman.handle_key(ord("a"))
    podman.handle_key(ord("/"))
    _type(podman, "BUSY")                  # the image, any case
    assert [row.name for row in podman.view] == ["created-only",
                                                 "stopped_one"]
    assert podman._footer() == " filter> BUSY "
    podman.handle_key(curses.KEY_BACKSPACE)
    assert podman.filter == "BUS"
    podman.handle_key(21)                   # Ctrl-U
    assert podman.filter == ""
    _type(podman, "zzz")
    assert "nothing matches" in _paint(podman)[2]
    podman.handle_key(27)
    assert podman.filter == "" and len(podman.view) == 4
    assert podman.mode == "list"


def test_enter_keeps_the_filter(podman):
    podman.handle_key(ord("i"))
    podman.handle_key(ord("/"))
    _type(podman, "961769")                 # an ID
    podman.handle_key(10)
    assert podman.mode == "list" and podman.filter == "961769"
    assert [row.name for row in podman.view] == [
        "quay.io/libpod/alpine:latest"]


def test_a_key_code_is_not_typed_at_a_prompt(podman):
    podman.handle_key(ord("/"))
    for key in (curses.KEY_RESIZE, curses.KEY_F5, curses.KEY_UP):
        assert podman.handle_key(key) is True
    assert podman.filter == ""


def test_tab_switches_panes_from_every_mode(podman):
    assert podman.handle_key(9) is None
    for key in (ord("/"), ord("p"), ord("f")):
        podman.handle_key(key)
        assert podman.handle_key(9) is None
        podman.handle_key(27)


def test_a_mouse_report_is_left_to_the_application(podman):
    assert podman.handle_key(curses.KEY_MOUSE) is None


def test_the_wheel_moves_the_cursor(podman):
    podman.handle_key(ord("a"))
    podman.wheel(-3)
    assert podman.cursor == 3
    podman.wheel(3)
    assert podman.cursor == 0


@pytest.mark.parametrize("key", [27, curses.KEY_F10])
def test_escape_and_f10_close(podman, key):
    assert podman.handle_key(key) is False


def test_help_lists_the_keys_and_any_key_goes_back(podman):
    podman.handle_key(ord("?"))
    lines = _paint(podman)
    assert "Keys" in lines[1]
    text = "\n".join(lines)
    for key in ("Enter", "F8, Del", "o / O", "Esc"):
        assert key in text
    assert "any key" in lines[-1]
    assert podman.handle_key(27) is True    # back, not closed
    assert podman.mode == "list"
    podman.handle_key(ord("?"))
    assert podman.handle_key(9) is None and podman.mode == "list"


def test_the_footer_names_the_keys_for_each_list(podman):
    assert "Enter shell" in podman._footer()
    assert "s start/stop" in podman._footer()
    assert "F8 remove" in podman._footer()
    podman.handle_key(ord("i"))
    assert "p pull" in podman._footer() and "f find" in podman._footer()
    for footer in (podman._footer(),):
        assert footer.startswith(" ? keys")


# -- actions ----------------------------------------------------------------------

@pytest.mark.parametrize("key, row, verb, done", [
    (ord("s"), "stopped_one", "start", "started"),
    (ord("s"), "web-1", "stop", "stopped"),
    (ord("R"), "web-1", "restart", "restarted"),
    (curses.KEY_F8, "stopped_one", "rm", "removed"),
    (curses.KEY_DC, "created-only", "rm", "removed"),
])
def test_a_container_action_asks_then_runs_by_id(podman, engines, app, key,
                                                 row, verb, done):
    podman.handle_key(ord("a"))
    _point(podman, row)
    target = podman.view[podman.cursor]
    podman.handle_key(key)
    assert podman.mode == "confirm"
    question = podman._footer()
    assert target.name in question and target.id in question
    assert verb not in engines.verbs()
    podman.handle_key(10)
    assert "..." in podman._footer()
    _settle(podman)
    assert [engines.path("podman"), verb, target.id] in engines.calls()
    assert podman.status == f"{done} {target.name} ({target.id})"
    assert f"Containers: {done} {target.name}" in app.message
    assert engines.verbs()[-2:] == ["ps", "images"]     # refreshed after


@pytest.mark.parametrize("key", [27, ord("n"), ord("q")])
def test_anything_but_yes_cancels(podman, engines, key):
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    podman.handle_key(key)
    assert podman.mode == "list" and podman.status == "cancelled"
    assert podman.task is None
    assert "stop" not in engines.verbs()


def test_tab_at_the_question_cancels_and_switches(podman, engines):
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    assert podman.handle_key(9) is None
    assert podman.status == "cancelled" and "stop" not in engines.verbs()


def test_y_says_yes_too(podman, engines):
    _point(podman, "web-1")
    podman.handle_key(ord("R"))
    podman.handle_key(ord("y"))
    _settle(podman)
    assert "restart" in engines.verbs()


def test_a_question_with_nothing_pending_does_nothing(podman):
    podman.mode = "confirm"
    podman.pending = None
    assert podman.handle_key(10) is True
    assert podman.mode == "list"


def test_removing_a_running_container_is_refused_first(podman, engines):
    _point(podman, "web-1")
    podman.handle_key(curses.KEY_F8)
    assert podman.mode == "list"
    assert podman.status == "web-1 is running: stop it first (s)"
    assert "rm" not in engines.verbs()


def test_an_image_is_removed_by_its_id(podman, engines):
    podman.handle_key(ord("i"))
    _point(podman, "quay.io/libpod/alpine:latest")
    podman.handle_key(curses.KEY_F8)
    assert podman._footer().startswith(
        " Remove image quay.io/libpod/alpine:latest (961769676411)?")
    podman.handle_key(10)
    _settle(podman)
    assert [engines.path("podman"), "rmi", "961769676411"] in engines.calls()


def test_an_untagged_image_is_removed_by_its_id(podman, engines):
    podman.handle_key(ord("i"))
    _point(podman, "<none>")
    podman.handle_key(curses.KEY_DC)
    assert "Remove image <none> (00cbeb27e6bb)?" in podman._footer()
    podman.handle_key(10)
    _settle(podman)
    assert [engines.path("podman"), "rmi", "00cbeb27e6bb"] in engines.calls()


def test_an_image_with_two_names_loses_only_the_one_chosen(podman, engines):
    # By its ID both engines refuse an image with more than one name unless
    # forced, and nothing is ever forced: the name under the cursor goes.
    podman.handle_key(ord("i"))
    _point(podman, "localhost/mybox:v2")
    podman.handle_key(curses.KEY_F8)
    assert "Untag localhost/mybox:v2 (image aaef90e06523" in podman._footer()
    podman.handle_key(10)
    _settle(podman)
    assert [engines.path("podman"), "rmi", "localhost/mybox:v2"] in \
        engines.calls()


def test_a_name_that_could_be_an_option_is_never_passed(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "images", out=(
        "-evil\tlatest\ta46257e06c82\t1 day ago\t1MB\n"
        "good\tlatest\ta46257e06c82\t1 day ago\t1MB\n"))
    plugin = ContainersPlugin(ctx)
    plugin.handle_key(ord("i"))
    _point(plugin, "-evil:latest")
    plugin.handle_key(curses.KEY_F8)
    plugin.handle_key(10)
    assert plugin.task is None
    assert plugin.status == "not a name docker would take: -evil:latest"
    assert "rmi" not in engines.verbs()


@pytest.mark.parametrize("engine, verb, text", [
    ("docker", "rm", DOCKER_RM_RUNNING),
    ("docker", "rmi", DOCKER_RMI_IN_USE),
    ("podman", "rmi", PODMAN_RMI_IN_USE),
])
def test_the_engines_refusal_is_shown(engines, ctx, app, engine, verb, text):
    engines.install(engine)
    engines.answer(engine, verb, err=text, code=125)
    plugin = ContainersPlugin(ctx)
    if verb == "rm":
        # Listed as stopped, running by the time the engine is asked.
        plugin.handle_key(ord("a"))
        _point(plugin, "stopped_one")
    else:
        plugin.handle_key(ord("i"))
        _point(plugin, "quay.io/libpod/alpine:latest")
    plugin.handle_key(curses.KEY_F8)
    plugin.handle_key(10)
    _settle(plugin)
    assert text.strip() in plugin.status
    assert text.strip() in app.message


def test_a_refusal_with_only_a_status_says_so(podman, engines):
    engines.answer("podman", "stop", code=7)
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    podman.handle_key(10)
    _settle(podman)
    assert podman.status == "stop web-1 failed: 7"


def test_nothing_is_ever_forced(podman, engines):
    podman.handle_key(ord("a"))
    for name, key in (("stopped_one", ord("s")), ("web-1", ord("R")),
                      ("created-only", curses.KEY_F8)):
        _point(podman, name)
        podman.handle_key(key)
        podman.handle_key(10)
        _settle(podman)
    podman.handle_key(ord("i"))
    for name in ("localhost/mybox:v2", "<none>"):
        _point(podman, name)
        podman.handle_key(curses.KEY_F8)
        podman.handle_key(10)
        _settle(podman)
    assert {"start", "restart", "rm", "rmi"} <= set(engines.verbs())
    assert not any(arg.startswith("-f") or arg == "--force"
                   for argv in engines.calls() for arg in argv)


def test_one_action_at_a_time(podman, engines):
    engines.answer("podman", "stop", sleep=0.5)
    podman.handle_key(ord("a"))
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    podman.handle_key(10)
    assert podman.task is not None
    podman.handle_key(curses.KEY_DOWN)       # the list is still usable
    assert podman._footer() == " stopping web-1... "
    _point(podman, "ported")
    podman.handle_key(ord("R"))
    assert podman.mode == "list"
    assert podman.status == "still stopping web-1: wait for it to finish"
    _settle(podman)
    assert engines.verbs().count("stop") == 1 and "restart" not in \
        engines.verbs()


def test_an_action_that_never_ends_is_cut_off(podman, engines, monkeypatch):
    monkeypatch.setattr(containers_mod, "ACTION_SECONDS", 0.3)
    engines.answer("podman", "stop", sleep=10)
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    podman.handle_key(10)
    _settle(podman)
    assert podman.status == "stop web-1 failed: no answer after 0.3 seconds"


def test_an_action_whose_engine_has_gone_says_so(podman, engines):
    os.remove(engines.path("podman"))
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    podman.handle_key(10)
    assert podman.task is None
    assert podman.status.startswith("podman could not be run:")


@pytest.mark.parametrize("key", [ord("s"), ord("R")])
def test_a_container_action_on_an_image_says_where_to_go(podman, engines,
                                                         key):
    podman.handle_key(ord("i"))
    podman.handle_key(key)
    assert podman.mode == "list"
    assert podman.status == "that works on a container: c lists them"


def test_remove_with_nothing_listed(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "ps", out="")
    plugin = ContainersPlugin(ctx)
    plugin.handle_key(curses.KEY_F8)
    assert plugin.status == "nothing to remove"


# -- the shell -----------------------------------------------------------------------

def test_enter_opens_a_shell_in_the_running_container(podman, engines, app):
    engines.answer("podman", "exec", out="/ # exit\r\n")
    _point(podman, "web-1")
    podman.handle_key(10)
    term = podman.term
    assert isinstance(term, EngineTerminal)
    assert term.argv == [engines.path("podman"), "exec", "-it",
                         "7cbf49a196e4", "sh"]
    assert term._caption() == " [podman exec web-1] "
    assert _pump(podman, lambda: podman.term is None)
    assert podman.status == "left web-1"
    assert podman.list == "containers"
    assert podman.view[podman.cursor].name == "web-1"
    assert engines.calls()[-3][1] == "exec"
    assert engines.verbs()[-2:] == ["ps", "images"]


def test_a_stopped_container_is_not_entered(podman, engines):
    podman.handle_key(ord("a"))
    _point(podman, "stopped_one")
    podman.handle_key(10)
    assert podman.term is None
    assert podman.status == "stopped_one is not running: s starts it"
    assert "exec" not in engines.verbs()


def test_a_container_with_no_shell_leaves_the_engines_error(podman, engines):
    # What Podman says of a distroless image: no sh in it.
    text = ("Error: runc: exec failed: unable to start container process: "
            "exec: \"sh\": executable file not found in $PATH")
    engines.answer("podman", "exec", err=text + "\n", code=127)
    _point(podman, "web-1")
    podman.handle_key(10)
    term = podman.term
    assert _pump(podman, lambda: term.done)
    assert podman.term is term              # stays on the screen
    assert "executable file not found" in terminal_text(term.screen)
    assert term._footer() == " [exit status 127 -- any key returns to the " \
        "list] "
    assert podman.handle_key(ord("x")) is True
    assert podman.term is None and podman.status == "left web-1"


def test_the_terminals_keys_reach_it(podman, engines, app):
    engines.answer("podman", "exec", sleep=30)
    _point(podman, "web-1")
    podman.handle_key(10)
    term = podman.term
    assert podman.handle_key(MENU_KEY) is True and term.menu
    podman.handle_key(9)                    # Ctrl-] Tab: the other pane
    assert app.active is app.right
    assert podman.handle_key(curses.KEY_MOUSE) is None
    assert podman.poll_ms == term.poll_ms
    podman.wheel(3)                         # its history, not the list
    podman.handle_key(MENU_KEY)
    assert podman.handle_key(ord("q")) is True     # Ctrl-] q closes it
    assert podman.term is None and podman.status == "left web-1"


def test_the_terminal_is_drawn_in_the_pane(podman, engines):
    engines.answer("podman", "exec", out="inside-marker\r\n", hold=30)
    _point(podman, "web-1")
    podman.handle_key(10)
    assert _pump(podman, lambda: "inside-marker" in
                 terminal_text(podman.term.screen))
    lines = _paint(podman)
    assert "[podman exec web-1]" in lines[0]
    assert "inside-marker" in lines[1]


def test_closing_the_plugin_closes_its_terminal(podman, engines):
    engines.answer("podman", "exec", sleep=30)
    _point(podman, "web-1")
    podman.handle_key(10)
    term = podman.term
    podman.on_exit()
    assert podman.term is None and term._fd is None


def test_a_terminal_that_cannot_start_says_so(podman, app, monkeypatch):
    import pty

    def refuse():
        raise OSError("out of ptys")

    monkeypatch.setattr(pty, "fork", refuse)
    _point(podman, "web-1")
    podman.handle_key(10)
    assert podman.term is None
    assert podman.status == "could not start podman: out of ptys"


def test_poll_ms_is_none_with_no_terminal(podman):
    assert podman.poll_ms is None


def test_the_exit_status_is_read_however_the_end_is_noticed(podman, engines,
                                                             monkeypatch):
    engines.answer("podman", "exec", sleep=30)
    _point(podman, "web-1")
    podman.handle_key(10)
    term = podman.term
    pid = term._pid
    term._pid, term.exit_status = None, 4    # already reaped by tick
    assert term._reap() == 4
    term._pid = pid

    def gone(pid, flags):
        raise ChildProcessError()

    with monkeypatch.context() as patched:
        patched.setattr(os, "waitpid", gone)
        assert term._reap() is None
        patched.setattr(os, "waitpid", lambda pid, flags: (0, 0))
        patched.setattr(containers_mod, "REAP_SECONDS", 0.03)
        assert term._reap() is None         # still running: no freeze
        patched.setattr(term, "_reap", lambda: None)
        term._finish()
        term._finish()                      # once only
    assert term.wants_close                 # unreadable counts as clean
    term.on_exit()


# -- pulling ------------------------------------------------------------------------

NEW_IMAGE = "quay.io/podman/hello\tlatest\t5dd467fce50b\t2 years ago\t787 kB\n"


def test_p_pulls_by_name_in_the_terminal(podman, engines, app):
    engines.answer("podman", "pull", out="Copying blob 1ff\r\nWriting "
                   "manifest to image destination\r\n",
                   then={"images": {"out": PODMAN_IMAGES + NEW_IMAGE}})
    podman.handle_key(ord("p"))
    assert podman._footer() == " pull>  "
    _type(podman, "quay.io/podman/hello")
    assert podman._footer() == " pull> quay.io/podman/hello "
    podman.handle_key(10)
    assert podman.term.argv == [engines.path("podman"), "pull",
                                "quay.io/podman/hello"]
    assert podman.term._caption() == " [podman pull quay.io/podman/hello] "
    assert _pump(podman, lambda: podman.term is None)
    assert podman.list == "images"
    assert podman.view[podman.cursor].name == "quay.io/podman/hello:latest"
    assert podman.status == "pulled quay.io/podman/hello"
    assert "Containers: pulled quay.io/podman/hello" in app.message


def test_a_pull_whose_image_is_not_listed_says_it_ended(podman, engines):
    podman.handle_key(ord("p"))
    _type(podman, "quay.io/podman/hello:1.0")
    podman.handle_key(10)
    assert _pump(podman, lambda: podman.term is None)
    assert podman.list == "images"
    assert podman.status == "pull of quay.io/podman/hello:1.0 ended"


def test_a_failed_pull_keeps_its_error_until_a_key(podman, engines):
    text = ('Error: short-name "nosuchthing-zzz/qqq" did not resolve to an '
            'alias and no unqualified-search registries are defined in '
            '"/etc/containers/registries.conf"')
    engines.answer("podman", "pull", err=text + "\n", code=125)
    podman.handle_key(ord("p"))
    _type(podman, "nosuchthing-zzz/qqq")
    podman.handle_key(10)
    term = podman.term
    assert _pump(podman, lambda: term.done)
    for _ in range(5):
        podman.tick()                       # it stays
    assert podman.term is term
    assert "did not resolve" in terminal_text(term.screen)
    assert "exit status 125" in _paint(podman)[-1]
    podman.handle_key(27)
    assert podman.term is None and podman.list == "images"


@pytest.mark.parametrize("text", ["", "   "])
def test_an_empty_name_pulls_nothing(podman, engines, text):
    podman.handle_key(ord("p"))
    _type(podman, text)
    podman.handle_key(10)
    assert podman.term is None and podman.status == "nothing to pull"
    assert "pull" not in engines.verbs()


def test_esc_at_the_pull_prompt_pulls_nothing(podman, engines):
    podman.handle_key(ord("p"))
    _type(podman, "alpine")
    podman.handle_key(curses.KEY_BACKSPACE)
    assert podman.entry == "alpin"
    podman.handle_key(21)
    assert podman.entry == ""
    _type(podman, "alpine")
    podman.handle_key(27)
    assert podman.mode == "list" and podman.term is None
    assert "pull" not in engines.verbs()


@pytest.mark.parametrize("name", [
    "-v", "--all-tags", "alpine; rm -rf /", "$(id)", "`id`", "a b",
    "'alpine'", '"alpine"', "alpine|sh", "alpine&", "x\\y",
])
def test_a_hostile_name_is_never_pulled(podman, engines, name):
    podman.handle_key(ord("p"))
    _type(podman, name)
    podman.handle_key(10)
    assert podman.term is None
    assert podman.status == f"not an image name: {name.strip()}"
    assert "pull" not in engines.verbs()


def test_enter_on_an_image_says_what_works_there(podman):
    podman.handle_key(ord("i"))
    podman.handle_key(10)
    assert podman.status == "p pulls a newer copy; c lists the containers"


def test_enter_on_nothing_does_nothing(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "ps", out="")
    plugin = ContainersPlugin(ctx)
    plugin.handle_key(10)
    assert plugin.term is None and plugin.status == ""


# -- searching ----------------------------------------------------------------------

def _search(plugin, term: str) -> None:
    plugin.handle_key(ord("f"))
    _type(plugin, term)
    assert plugin._footer() == f" find> {term} "
    plugin.handle_key(10)
    _settle(plugin)


def test_f_searches_the_registries(docker, engines):
    _search(docker, "alpine")
    assert engines.calls()[-1] == [
        engines.path("docker"), "search", "--limit", "25", "--format",
        SEARCH_FORMAT, "--", "alpine"]
    assert docker.list == "search"
    assert [row.name for row in docker.view] == [
        "alpine", "alpine/git", "alpine/socat"]     # the registry's order
    assert docker.status.startswith("3 found")
    assert "found for 'alpine'" in docker._caption()
    lines = _paint(docker)
    assert "NAME" in lines[1] and "DESCRIPTION" in lines[1]
    assert "minimal Docker image" in lines[2]


def test_enter_on_a_result_pulls_it(docker, engines):
    _search(docker, "alpine")
    docker.handle_key(curses.KEY_DOWN)
    docker.handle_key(10)
    assert docker.term.argv == [engines.path("docker"), "pull", "alpine/git"]
    assert _pump(docker, lambda: docker.term is None)
    assert docker.list == "images"


def test_esc_goes_back_from_the_results(docker):
    _search(docker, "alpine")
    assert "Esc goes back" in docker._footer()
    docker.status = ""
    assert "Esc back" in docker._footer()
    assert docker.handle_key(27) is True
    assert docker.list == "images"


def test_results_filter_by_name_and_description(podman):
    _search(podman, "quay.io/libpod/alpine")
    podman.handle_key(ord("/"))
    _type(podman, "NGINX")
    assert [row.name for row in podman.view] == [
        "quay.io/libpod/alpine_nginx"]
    podman.handle_key(21)
    _type(podman, "e2e")
    assert len(podman.view) == 2


def test_results_sort_by_name_on_request(podman):
    _search(podman, "quay.io/libpod/alpine")
    podman.handle_key(ord("O"))
    assert podman.view[0].name == "quay.io/libpod/alpine_nginx"
    podman.handle_key(ord("o"))
    assert podman.sorts["search"] == ["name", True]


def test_a_hostile_term_reaches_the_engine_as_one_argument(docker, engines):
    term = "-v; $(id) 'x' \"y\" `z`"
    _search(docker, term)
    argv = engines.calls()[-1]
    assert argv[-2:] == ["--", term]        # "--": it is never an option


@pytest.mark.parametrize("text", ["", "  "])
def test_an_empty_term_searches_nothing(docker, engines, text):
    _search(docker, text)
    assert docker.status == "nothing to search for"
    assert "search" not in engines.verbs()


def test_esc_at_the_find_prompt_searches_nothing(docker, engines):
    docker.handle_key(ord("f"))
    _type(docker, "alpine")
    docker.handle_key(27)
    assert "search" not in engines.verbs() and docker.list == "containers"


def test_a_failed_search_shows_the_engines_message(docker, engines, app):
    text = ('Error response from daemon: Get "https://index.docker.io/v1/'
            'search?q=alpine&n=25": dial tcp: lookup index.docker.io: no '
            'such host')
    engines.answer("docker", "search", err=text + "\n", code=1)
    _search(docker, "alpine")
    assert docker.status == f"search failed: {text}"
    assert docker.list == "containers"
    assert text in app.message


def test_a_failed_search_with_no_message_gives_the_status(docker, engines):
    engines.answer("docker", "search", code=2)
    _search(docker, "alpine")
    assert docker.status == "search failed: 2"


def test_nothing_found_on_docker(docker, engines):
    engines.answer("docker", "search", out="")
    _search(docker, "zzz")
    assert docker.status == "nothing found for zzz"
    assert "nothing found" in _paint(docker)[2]


def test_nothing_found_on_podman_says_how_to_name_a_registry(podman, engines):
    # What Podman answers when registries.conf names no registries to
    # search: nothing, and success.
    engines.answer("podman", "search", out="")
    _search(podman, "alpine")
    assert "registries.conf" in podman.status
    assert "quay.io/alpine" in podman.status


def test_a_search_waits_for_an_action(podman, engines):
    engines.answer("podman", "stop", sleep=0.5)
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    podman.handle_key(10)
    podman.handle_key(ord("f"))
    _type(podman, "alpine")
    podman.handle_key(10)
    assert podman.status == "still stopping web-1: wait for it to finish"
    _settle(podman)
    assert "search" not in engines.verbs()


def test_remove_on_a_result_is_refused(docker, engines):
    _search(docker, "alpine")
    docker.handle_key(curses.KEY_F8)
    assert docker.status == "a search result is not on this machine"


# -- drawing ------------------------------------------------------------------------

def test_the_containers_list_is_drawn(podman):
    lines = _paint(podman)
    assert "[containers] podman: 2 running" in lines[0]
    for heading in ("NAME", "IMAGE", "STATUS", "PORTS"):
        assert heading in lines[1]
    assert "ported" in lines[2] and "0.0.0.0:8080->80/tcp" in lines[2]
    assert "web-1" in lines[3]
    assert "Enter shell" in lines[-1]


def test_the_images_list_is_drawn(podman):
    podman.handle_key(ord("i"))
    lines = _paint(podman)
    for heading in ("REPOSITORY", "TAG", "ID", "CREATED", "SIZE"):
        assert heading in lines[1]
    row = next(line for line in lines if "quay.io/libpod/alpine" in line)
    assert "latest" in row and "961769676411" in row and "7 years ago" in row


def test_the_question_is_drawn_in_the_footer(podman):
    _point(podman, "web-1")
    podman.handle_key(ord("s"))
    assert "Stop web-1 (7cbf49a196e4)?" in _paint(podman, cols=120)[-1]


def test_a_long_list_scrolls_with_the_cursor(engines, ctx):
    engines.install("docker")
    engines.answer("docker", "ps", out="".join(
        f"{n:012x}\tbox-{n}\timg\trunning\tUp\t\n" for n in range(40)))
    plugin = ContainersPlugin(ctx)
    plugin.handle_key(curses.KEY_END)
    assert any("box-39" in line for line in _paint(plugin, rows=10))
    plugin.handle_key(curses.KEY_HOME)
    assert any("box-0 " in line for line in _paint(plugin, rows=10))
    assert plugin.scroll == 0


def test_half_a_screen_still_shows_name_and_status(podman):
    lines = _paint(podman, rows=8, cols=40)
    assert "[containers]" in lines[0]
    row = next(line for line in lines if "web-1" in line)
    assert "Up 5 seconds" in row
    podman.handle_key(ord("i"))
    lines = _paint(podman, rows=8, cols=40)
    row = next(line for line in lines if "quay.io/libpod/alpine" in line)
    assert "latest" in row


def test_a_tiny_pane_draws_nothing(podman):
    with_curses_screen(24, 62, lambda s: podman.draw(s, 0, 0, 2, 10))


# -- the plug-in menu ---------------------------------------------------------------

def test_discovery_lists_it_once_and_not_its_terminal():
    classes, errors = discover()
    names = [cls.name for cls in classes]
    assert names.count("Containers") == 1
    assert names.count("Terminal") == 1
    assert EngineTerminal not in classes


def test_the_plugin_menu_letters_are_the_documented_ones():
    """The letters the changelog lists for 1.23.0, where Containers took
    "l" and seven plug-ins moved along.  A new plug-in that moves them again
    fails here, so its changelog can say so too."""
    names = [cls.name for cls in discover()[0]]
    letters = dict(zip(names, accelerators(names, reserved="c")))
    assert letters == {
        "Build dataset": "b", "Checksum / verify": "a", "Clean table": "e",
        "Compare panes": "h", "Containers": "l", "Disk usage": "d",
        "Find duplicates": "f", "Find in other pane": "o", "Git": "g",
        "Grep in other pane": "q", "Inspect file": "i", "JSON push": "u",
        "Make archive": "m", "Multi-rename": "v", "Normalise text": "n",
        "Processes": "p", "Profile table": "w", "Provost data": "x",
        "Run remote script": "r", "SSH doctor": "s", "Tail file": "t",
        "Terminal": "y",
    }


def test_the_menu_offers_it_on_its_letter(engines, app, monkeypatch):
    engines.install("docker")
    monkeypatch.setattr(dialogs, "menu", dialogs.menu)
    scripted = _ScriptedDialogs(monkeypatch, menu=["Containers"])
    app._plugin_mode()
    title, options = scripted.menus[0]
    row = next(i for i, o in enumerate(options) if o.startswith("Containers"))
    assert scripted.menu_keys[0][row] == "l"
    assert options[row] == ("Containers -- Podman or Docker images and "
                            "containers")
    app._close_plugin(app.left)
