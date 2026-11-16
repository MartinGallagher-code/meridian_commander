# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Built-in plugin: Podman or Docker images and containers, in a pane.

Two lists, one key apart:

* **containers** (``c``) -- name, image, status and ports; the running ones,
  and the stopped ones too after ``a``;
* **images** (``i``) -- what has been downloaded: repository, tag, short ID,
  age and size.

and what can be done from them:

* ``Enter`` on a running container opens a shell inside it
  (``podman exec -it ID sh``) in the pane's own terminal -- the one ``t``
  opens, with its ``Ctrl-]`` menu, zoom and scrollback.  Leaving the shell
  brings the list back.
* ``p`` downloads (pulls) an image by name, and ``f`` finds one in the
  registries first; the pull runs in the pane's terminal with the engine's
  own progress, and the list comes back with the new image under the cursor.
* ``s`` starts or stops a container, ``R`` restarts it, and ``F8`` removes a
  container or an image.  Each asks first, naming the item by name and ID.
  Nothing is ever forced and nothing deletes in bulk.

It runs this machine's ``podman`` or ``docker`` command line -- the two take
the same commands -- and nothing else: no SDK, no socket.  Podman is used
when both are installed.  Every command is an argv list, never a shell line,
and a name only ever reaches the engine after a check that it is a name, so
nothing typed can run a command or be read as an option.  Every captured
command has a time limit, and the slow ones (stopping a container waits out
its grace period) run in the background while the list stays usable.

Local panes only: on an SFTP, SSH, FTP or archive pane it is refused.

Configure it in ``config.ini``::

    [plugin:containers]
    # auto (Podman if installed, otherwise Docker), podman or docker
    engine = auto
    # seconds between automatic refreshes
    refresh = 3
    # seconds a listing or a registry search may take
    timeout = 10
"""

from __future__ import annotations

import curses
import os
import re
import shutil
import subprocess
import time

from .. import theme
from ..config import plugin_settings
from ..plugin_api import PanePlugin
from ..util import truncate, typed_char
from .terminal import TerminalPlugin

DEFAULTS = {
    "engine": "auto",      # auto, podman or docker
    "refresh": 3,          # seconds between automatic refreshes
    "timeout": 10,         # seconds a listing or a search may take
    "search_limit": 25,    # results a registry search asks for
}

#: The engines, in the order ``auto`` tries them.
ENGINES = ("podman", "docker")

#: How long a start, stop, restart or remove may take.  ``stop`` waits out
#: the container's grace period (ten seconds unless it was given another)
#: before it kills, so this is well beyond that.  These run in the
#: background, so waiting for one never freezes the screen.
ACTION_SECONDS = 120

#: The fields asked for, tab-separated, named only from those both engines
#: have -- checked against Podman 4.9 and Docker 29.  ``--format json`` is
#: avoided: Podman prints one array and Docker one object per line, with
#: different keys.
PS_FORMAT = "\t".join(("{{.ID}}", "{{.Names}}", "{{.Image}}", "{{.State}}",
                       "{{.Status}}", "{{.Ports}}"))
IMAGES_FORMAT = "\t".join(("{{.Repository}}", "{{.Tag}}", "{{.ID}}",
                           "{{.CreatedSince}}", "{{.Size}}"))
#: Podman calls the rest of a result's fields .Stars and .Official, Docker
#: .StarCount and .IsOfficial; the name and description are all both have.
SEARCH_FORMAT = "{{.Name}}\t{{.Description}}"

#: An ID as both engines print it: hex, twelve characters unless asked for
#: more.  Only an ID that looks like one is ever passed back to the engine.
HEX_ID = re.compile(r"[0-9a-f]{12,64}\Z")

#: An image reference: registry, path, tag and digest characters only, and a
#: letter or digit first -- so it can never be taken for an option, and
#: nothing in it means anything to a shell (none is used anyway).
REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,511}\Z")

#: What the engines' sizes are counted in: Docker and Podman both write
#: decimal units ("6.81MB", "4.69 MB"); the binary ones are here in case.
SIZE_UNITS = {
    "b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4,
    "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4,
}

#: Seconds in each unit of "3 weeks ago", for sorting by age.
AGE_UNITS = {
    "second": 1, "minute": 60, "hour": 3600, "day": 86400,
    "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400,
}

#: The columns each list sorts by, in the order ``o`` steps through them.
SORTS = {
    "containers": ("name", "image", "state"),
    "images": ("repository", "tag", "age", "size"),
    "search": ("rank", "name"),     # the registry's own order first
}

#: The keys, for ``?``.
HELP = (
    ("Enter", "containers: a shell in the running container"),
    ("", "search results: download the image"),
    ("c / i", "the containers list / the images list"),
    ("a", "show the stopped containers too, or hide them"),
    ("s", "start a stopped container, or stop a running one"),
    ("R", "restart a container"),
    ("F8, Del", "remove the container or image (asks first)"),
    ("p", "download (pull) an image by name"),
    ("f", "find an image in the registries"),
    ("/", "filter the list; Esc clears it"),
    ("o / O", "sort by the next column / reverse the order"),
    ("r", "refresh now (and retry after an error)"),
    ("Tab", "the other pane"),
    ("Esc", "close (search results: back to the images)"),
)


# -- the rows -------------------------------------------------------------------

class Container:
    """One container, as ``ps -a`` lists it."""

    __slots__ = ("id", "name", "image", "state", "status", "ports")

    def __init__(self, id: str, name: str, image: str, state: str,
                 status: str, ports: str) -> None:
        self.id = id
        self.name = name
        self.image = image
        self.state = state
        self.status = status
        self.ports = ports

    @property
    def running(self) -> bool:
        return self.state.lower() == "running"

    @property
    def key(self) -> str:
        """What the cursor holds on to across a refresh."""
        return self.id

    def label(self) -> str:
        return f"{self.name} ({self.id})"


class Image:
    """One line of ``images``: an image under one of its names."""

    __slots__ = ("repository", "tag", "id", "created", "size")

    def __init__(self, repository: str, tag: str, id: str, created: str,
                 size: str) -> None:
        self.repository = repository
        self.tag = tag
        self.id = id
        self.created = created
        self.size = size

    @property
    def tagged(self) -> bool:
        return "<none>" not in (self.repository, self.tag)

    @property
    def name(self) -> str:
        """``repository:tag``, or ``<none>`` for an image with no name."""
        return f"{self.repository}:{self.tag}" if self.tagged else "<none>"

    @property
    def key(self) -> str:
        # One image can be listed under several names: each line is its own.
        return f"{self.id} {self.name}"

    @property
    def age(self) -> int:
        return age_seconds(self.created)

    @property
    def bytes(self) -> int:
        return size_bytes(self.size)

    def label(self) -> str:
        return f"{self.name} ({self.id})"


class Found:
    """One result of a registry search."""

    __slots__ = ("rank", "name", "description")

    def __init__(self, rank: int, name: str, description: str) -> None:
        self.rank = rank               # where the registry put it
        self.name = name
        self.description = description

    @property
    def key(self) -> str:
        return self.name


# -- parsing --------------------------------------------------------------------

def _fields(text: str, count: int) -> list[list[str]]:
    """The tab-separated lines of ``text`` that have ``count`` fields."""
    rows = []
    for line in text.splitlines():
        parts = line.rstrip("\r").split("\t")
        if len(parts) == count:
            rows.append([part.strip() for part in parts])
    return rows


def _short_id(text: str) -> str | None:
    """An ID as the engine printed it, checked; None if it is not one."""
    if text.startswith("sha256:"):
        text = text[len("sha256:"):]
    return text[:12] if HEX_ID.match(text) else None


def parse_containers(text: str) -> list[Container]:
    """Parse ``ps -a --format PS_FORMAT``.  Lines that do not parse are
    skipped rather than failing the whole listing."""
    found = []
    for id, name, image, state, status, ports in _fields(text, 6):
        short = _short_id(id)
        if short is not None and name:
            found.append(Container(short, name, image, state, status, ports))
    return found


def parse_images(text: str) -> list[Image]:
    """Parse ``images --format IMAGES_FORMAT``."""
    found = []
    for repository, tag, id, created, size in _fields(text, 5):
        short = _short_id(id)
        if short is not None:
            found.append(Image(repository or "<none>", tag or "<none>", short,
                               created, size))
    return found


def parse_search(text: str) -> list[Found]:
    """Parse ``search --format SEARCH_FORMAT``.  A result whose name could
    not be pulled as it stands is left out."""
    found: list[Found] = []
    for line in text.splitlines():
        name, _, description = line.rstrip("\r").partition("\t")
        name = name.strip()
        if REFERENCE.match(name):
            found.append(Found(len(found), name, description.strip()))
    return found


def size_bytes(text: str) -> int:
    """``"6.81MB"`` or ``"4.69 MB"`` as bytes, for sorting; 0 if unreadable."""
    match = re.match(r"\s*([0-9.]+)\s*([A-Za-z]+)\s*\Z", text)
    if not match:
        return 0
    unit = SIZE_UNITS.get(match.group(2).lower())
    try:
        return int(float(match.group(1)) * unit) if unit else 0
    except ValueError:
        return 0


def age_seconds(text: str) -> int:
    """``"7 years ago"``, ``"About an hour ago"`` as seconds, for sorting."""
    words = text.lower().replace("about ", "").split()
    if len(words) < 2:
        return 0
    if words[0] == "less":                     # "Less than a second ago"
        return 0
    count = 1 if words[0] in ("a", "an") else None
    if count is None:
        try:
            count = int(words[0])
        except ValueError:
            return 0
    return count * AGE_UNITS.get(words[1].rstrip("s"), 0)


def split_reference(name: str) -> tuple[str, str | None]:
    """``(repository, tag)`` of a name to pull; the tag is ``latest`` when
    none is given, and None for a digest, which no listing shows."""
    if "@" in name:
        return name.split("@", 1)[0], None
    slash = name.rfind("/")
    colon = name.rfind(":")
    if colon > slash:
        return name[:colon], name[colon + 1:]
    return name, "latest"


def pulled_image(images: list[Image], name: str) -> str | None:
    """The key of the image ``pull name`` left, or None if it is not listed.

    A short name comes back under the registry it was found in -- Podman
    lists ``alpine`` as ``docker.io/library/alpine`` -- so an exact match is
    preferred, and a match on the last part of the path taken after it.
    """
    repository, tag = split_reference(name)
    candidates = [image for image in images
                  if tag is None or image.tag == tag]
    for image in candidates:
        if image.repository == repository:
            return image.key
    for image in candidates:
        if image.repository.endswith("/" + repository):
            return image.key
    return None


# -- the engine -----------------------------------------------------------------

def find_engine(setting: str) -> tuple[str, str]:
    """``(name, command)`` of the engine to use: Podman first, then Docker,
    unless ``setting`` names one.  Raises with the reason when there is
    none, which the application shows instead of opening the pane."""
    wanted = (setting or "auto").strip().lower()
    if wanted not in ("auto",) + ENGINES:
        raise RuntimeError(
            f"[plugin:containers] engine = {setting} is not one I know: "
            f"use auto, podman or docker.")
    for name in ENGINES if wanted == "auto" else (wanted,):
        command = shutil.which(name)
        if command:
            return name, command
    if wanted == "auto":
        raise RuntimeError(
            "Neither podman nor docker was found on this machine's PATH. "
            "Install one of them to use Containers.")
    raise RuntimeError(
        f"{wanted} was not found on this machine's PATH, and "
        f"[plugin:containers] engine asks for it.")


def _message(text: str) -> str:
    """An engine's complaint on one line: its lines, joined."""
    return " ".join(line.strip() for line in text.splitlines()
                    if line.strip())


class EngineError(RuntimeError):
    """The engine ran and refused, or could not be reached."""


# -- running a command in the pane's terminal -----------------------------------

#: How long a command that has closed its terminal is given to exit, so its
#: exit status can be read.  Bounded, as for a pager in a pane.
REAP_SECONDS = 1.0


class EngineTerminal(TerminalPlugin):
    """``pull`` or ``exec -it`` running in the pane's terminal.

    The terminal ``t`` opens, started on the engine's command rather than a
    shell, as an outside pager in a pane is.  It needs a real terminal, not
    just a nicer one: ``exec -it`` will not start without one, and Podman
    asks *which registry* a short name is to come from only when it can.  A
    clean exit hands the pane back to the list on the next poll
    (``wants_close``); a failure stays on the screen until a key is pressed,
    so the engine's reason can be read.
    """

    # Not a plug-in of its own: discover() lists only classes with a name,
    # and this one would otherwise inherit "Terminal".
    name = ""
    description = ""

    def __init__(self, ctx, argv: list[str], title: str) -> None:
        self.argv = argv
        self.title = title
        #: Set when the command has exited cleanly: the list comes back.
        self.wants_close = False
        super().__init__(ctx)

    def _launch(self) -> None:
        self._start_local(self.ctx.own_path, self.argv)

    def _caption(self) -> str:
        return f" [{self.title}] "

    def _finish(self) -> None:
        if self.done:
            return
        status = self._reap()
        super()._finish()
        if status in (0, None):
            # A status that could not be read is taken as a clean one: the
            # list, refreshed, shows what came of it either way.
            self.wants_close = True
        else:
            self.status = (f"[exit status {status} -- any key returns to "
                           f"the list]")

    def _reap(self) -> int | None:
        """The command's exit status, waiting a moment for it; None if not."""
        if self._pid is None:
            return self.exit_status
        deadline = time.monotonic() + REAP_SECONDS
        while True:
            try:
                pid, status = os.waitpid(self._pid, os.WNOHANG)
            except ChildProcessError:
                return None
            if pid == self._pid:
                self._pid = None
                self.exit_status = os.waitstatus_to_exitcode(status)
                return self.exit_status
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.01)


# -- background commands ---------------------------------------------------------

class Task:
    """An engine command running in the background: an action or a search.

    The list goes on being drawn and used while it runs; :meth:`poll` says
    when it has finished, and the plug-in asks it every tick.
    """

    def __init__(self, argv: list[str], seconds: float, what: str,
                 done) -> None:
        self.argv = argv
        self.what = what               # "stopping web-1", for the footer
        self.done = done               # called with (returncode, out, err)
        self.deadline = time.monotonic() + seconds
        self.seconds = seconds
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, encoding="utf-8", errors="replace")

    def poll(self) -> bool:
        """Whether it has finished; if so, ``done`` has been called."""
        if self.proc.poll() is None:
            if time.monotonic() < self.deadline:
                return False
            self.proc.kill()
            self.proc.communicate()
            self.done(None, "", f"no answer after {self.seconds:g} seconds")
            return True
        out, err = self.proc.communicate()
        self.done(self.proc.returncode, out, err)
        return True


# -- the plug-in ------------------------------------------------------------------

class ContainersPlugin(PanePlugin):
    name = "Containers"
    description = "Podman or Docker images and containers"
    uses_other_pane = False  # this machine's engine; one pane will do
    wants_timer = True       # auto-refresh, background actions, the terminal

    def on_start(self) -> None:
        self.cfg = plugin_settings("containers", DEFAULTS)
        fs = self.ctx.own_fs
        if getattr(fs, "scheme", "") != "local":
            raise RuntimeError(
                "Containers works on local panes only: it runs this "
                "machine's podman or docker. Open it from a local pane.")
        self.engine, self.command = find_engine(str(self.cfg["engine"]))
        self.containers: list[Container] = []
        self.images: list[Image] = []
        self.found: list[Found] = []
        self.searched = ""             # the term the results are for
        self.list = "containers"       # "containers", "images" or "search"
        self.view: list = []
        self.cursor = 0
        self.scroll = 0
        self.show_all = False          # the stopped containers too
        self.sorts = {name: [columns[0], False]
                      for name, columns in SORTS.items()}
        self.filter = ""
        #: "list", "filter", "pull", "find", "confirm" or "help".
        self.mode = "list"
        self.entry = ""                # what is being typed at a prompt
        self.pending: tuple | None = None    # (verb, item) to confirm
        self.status = ""
        self.error = ""
        self.task: Task | None = None
        self.term: EngineTerminal | None = None
        self.after_term: tuple | None = None  # what the terminal was for
        self._last_refresh = 0.0
        self.refresh()

    # -- running the engine ---------------------------------------------------
    def _capture(self, args: list[str]) -> str:
        """Run ``engine args`` and return what it printed.

        Raises :class:`EngineError` with the engine's own message when it
        fails, and when it does not answer within ``timeout`` seconds -- a
        stuck daemon must not freeze the file manager.
        """
        seconds = float(self.cfg["timeout"])
        try:
            done = subprocess.run(
                [self.command] + args, stdin=subprocess.DEVNULL,
                capture_output=True, encoding="utf-8", errors="replace",
                timeout=seconds)
        except subprocess.TimeoutExpired:
            raise EngineError(f"{self.engine} did not answer in "
                              f"{seconds:g} seconds") from None
        except OSError as exc:
            raise EngineError(f"{self.engine} could not be run: "
                              f"{exc.strerror or exc}") from None
        if done.returncode != 0:
            raise EngineError(_message(done.stderr) or _message(done.stdout)
                              or f"{self.engine} failed ({done.returncode})")
        return done.stdout

    def refresh(self) -> None:
        """List the containers and images again, keeping the cursor on the
        same item where it is still there.

        After a failure the old lists stay, the engine's message is shown,
        and nothing refreshes by itself again until ``r`` -- otherwise a
        daemon that hangs would freeze the screen for ``timeout`` seconds
        every few seconds.
        """
        self._last_refresh = time.monotonic()
        try:
            containers = parse_containers(
                self._capture(["ps", "-a", "--format", PS_FORMAT]))
            images = parse_images(
                self._capture(["images", "--format", IMAGES_FORMAT]))
        except EngineError as exc:
            self.error = str(exc)
            return
        self.error = ""
        self.containers, self.images = containers, images
        self._rebuild_view()

    def tick(self) -> None:
        """Pump the terminal, finish background commands, and refresh on
        the configured cadence (paused while a prompt is open)."""
        if self.task is not None and self.task.poll():
            self.task = None
        if self.term is not None:
            self.term.tick()
            if self.term.wants_close:
                self._close_term()
            return
        if self.mode != "list" or self.error:
            return
        if time.monotonic() - self._last_refresh >= float(self.cfg["refresh"]):
            self.refresh()

    @property
    def poll_ms(self) -> int | None:
        """The terminal's quicker polls while it is busy."""
        return self.term.poll_ms if self.term is not None else None

    def wheel(self, lines: int) -> None:
        """The mouse wheel: the terminal's history, or the list."""
        if self.term is not None:
            self.term.wheel(lines)
        else:
            self._move(-lines)

    # -- the visible list -----------------------------------------------------
    def _rows(self) -> list:
        if self.list == "images":
            return self.images
        if self.list == "search":
            return self.found
        return [c for c in self.containers if self.show_all or c.running]

    def _matches(self, row, needle: str) -> bool:
        if isinstance(row, Container):
            fields = (row.name, row.image, row.id, row.status)
        elif isinstance(row, Image):
            fields = (row.repository, row.tag, row.id)
        else:
            fields = (row.name, row.description)
        return any(needle in field.lower() for field in fields)

    def _sort_value(self, row, column: str):
        if column == "state":
            # Running first, then by what the status says.
            return (not row.running, row.status.lower())
        if column == "age":
            return row.age
        if column == "size":
            return row.bytes
        if column == "rank":
            return row.rank
        return getattr(row, column).lower()

    def _rebuild_view(self, anchor: str | None = None) -> None:
        """Filter and sort, putting the cursor on ``anchor`` -- by default
        the item it is on now -- if it is still listed."""
        if anchor is None and self.cursor < len(self.view):
            anchor = self.view[self.cursor].key
        rows = self._rows()
        if self.filter:
            needle = self.filter.lower()
            rows = [row for row in rows if self._matches(row, needle)]
        column, descending = self.sorts[self.list]
        self.view = sorted(rows, key=lambda row: self._sort_value(row, column),
                           reverse=descending)
        self.cursor = min(self.cursor, max(0, len(self.view) - 1))
        if anchor is not None:
            for index, row in enumerate(self.view):
                if row.key == anchor:
                    self.cursor = index
                    break

    def _show(self, name: str, anchor: str | None = None) -> None:
        """Switch to another list, with no filter and the cursor at the top
        (or on ``anchor``)."""
        if name != self.list:
            self.list = name
            self.filter = ""
            self.view = []
            self.cursor = self.scroll = 0
        self._rebuild_view(anchor)

    def _current(self):
        return self.view[self.cursor] if self.cursor < len(self.view) else None

    def _move(self, step: int) -> None:
        self.cursor = max(0, min(len(self.view) - 1, self.cursor + step))

    # -- keys -----------------------------------------------------------------
    def handle_key(self, key: int | str):
        if self.term is not None:
            result = self.term.handle_key(key)
            if result is False:          # closed: Ctrl-] q, or a key after
                self._close_term()       # a failure was read
                return True
            return result
        if key == curses.KEY_MOUSE:
            # The application's: it turns the wheel into wheel() and a click
            # on the other pane into focus, as it does for the terminal.
            return None
        if self.mode == "confirm":
            return self._confirm_key(key)
        if self.mode in ("filter", "pull", "find"):
            return self._entry_key(key)
        if self.mode == "help":
            self.mode = "list"           # any key: back to the list
            return None if key == 9 else True
        return self._list_key(key)

    def _list_key(self, key: int | str):
        self.status = ""
        if key == 9:                     # Tab: let the app switch panes
            return None
        if key in (27, curses.KEY_F10):
            if self.list == "search":
                self._show("images")
                return True
            self.on_exit()
            return False
        if key == curses.KEY_UP:
            self._move(-1)
        elif key == curses.KEY_DOWN:
            self._move(1)
        elif key == curses.KEY_PPAGE:
            self._move(-10)
        elif key == curses.KEY_NPAGE:
            self._move(10)
        elif key == curses.KEY_HOME:
            self.cursor = 0
        elif key == curses.KEY_END:
            self._move(len(self.view))
        elif key in (10, 13, curses.KEY_ENTER):
            self._enter()
        elif key == ord("c"):
            self._show("containers")
        elif key == ord("i"):
            self._show("images")
        elif key == ord("a"):
            self._toggle_all()
        elif key == ord("s"):
            self._ask_start_stop()
        elif key == ord("R"):
            self._ask("restart")
        elif key in (curses.KEY_F8, curses.KEY_DC):
            self._ask_remove()
        elif key in (ord("p"), ord("f")):
            self.mode = "pull" if key == ord("p") else "find"
            self.entry = ""
        elif key == ord("/"):
            self.mode = "filter"
        elif key == ord("o"):
            columns = SORTS[self.list]
            sort = self.sorts[self.list]
            sort[0] = columns[(columns.index(sort[0]) + 1) % len(columns)]
            self._rebuild_view()
        elif key == ord("O"):
            self.sorts[self.list][1] = not self.sorts[self.list][1]
            self._rebuild_view()
        elif key == ord("r"):
            self.refresh()
        elif key == ord("?"):
            self.mode = "help"
        return True

    def _entry_key(self, key: int | str):
        """A key at the filter, pull or find prompt in the footer."""
        mode = self.mode
        if key == 9:
            return None
        if key == 27:                    # Esc: nothing is run
            self.mode = "list"
            if mode == "filter":
                self.filter = ""
                self._rebuild_view()
            return True
        if key in (10, 13, curses.KEY_ENTER):
            self.mode = "list"
            if mode == "pull":
                self._pull(self.entry.strip())
            elif mode == "find":
                self._search(self.entry.strip())
            return True
        text = self.filter if mode == "filter" else self.entry
        if key in (curses.KEY_BACKSPACE, 127, 8):
            text = text[:-1]
        elif key == 21:                  # Ctrl-U clears
            text = ""
        else:
            char = typed_char(key)
            if char is None:
                return True
            text += char
        if mode == "filter":
            self.filter = text
            self._rebuild_view()
        else:
            self.entry = text
        return True

    def _toggle_all(self) -> None:
        if self.list != "containers":
            self._show("containers")
        self.show_all = not self.show_all
        self._rebuild_view()
        self.status = ("showing stopped containers too" if self.show_all
                       else "showing running containers")

    # -- Enter ------------------------------------------------------------------
    def _enter(self) -> None:
        row = self._current()
        if isinstance(row, Found):
            self._pull(row.name)
        elif isinstance(row, Container):
            if row.running:
                self._shell(row)
            else:
                self.status = f"{row.name} is not running: s starts it"
        elif isinstance(row, Image):
            self.status = "p pulls a newer copy; c lists the containers"

    # -- asking first -------------------------------------------------------------
    def _container(self) -> Container | None:
        row = self._current()
        if isinstance(row, Container):
            return row
        self.status = "that works on a container: c lists them"
        return None

    def _ask_start_stop(self) -> None:
        row = self._container()
        if row is not None:
            self._ask("stop" if row.running else "start", row)

    def _ask(self, verb: str, row=None) -> None:
        if row is None:
            row = self._container()
            if row is None:
                return
        if self.task is not None:
            self.status = f"still {self.task.what}: wait for it to finish"
            return
        self.pending = (verb, row)
        self.mode = "confirm"

    def _ask_remove(self) -> None:
        row = self._current()
        if isinstance(row, Container):
            if row.running:
                # The engine would want --force, which is never passed.
                self.status = f"{row.name} is running: stop it first (s)"
                return
            self._ask("rm", row)
        elif isinstance(row, Image):
            self._ask("rmi", row)
        elif row is None:
            self.status = "nothing to remove"
        else:
            self.status = "a search result is not on this machine"

    def _shared(self, image: Image) -> bool:
        """Whether ``image``'s ID is listed under other names too."""
        return sum(1 for other in self.images if other.id == image.id) > 1

    def _question(self) -> str:
        assert self.pending is not None
        verb, row = self.pending
        if verb == "rmi":
            if row.tagged and self._shared(row):
                return (f"Untag {row.name} (image {row.id} keeps its other"
                        f" names)?")
            return f"Remove image {row.label()}?"
        word = {"rm": "Remove", "start": "Start", "stop": "Stop",
                "restart": "Restart"}[verb]
        return f"{word} {row.label()}?"

    def _confirm_key(self, key: int | str):
        pending, self.pending = self.pending, None
        self.mode = "list"
        if key == 9:
            self.status = "cancelled"
            return None
        if pending is None:
            return True
        if key in (10, 13, curses.KEY_ENTER, ord("y"), ord("Y")):
            self._act(*pending)
        else:                            # Esc, n, anything else
            self.status = "cancelled"
        return True

    # -- acting -------------------------------------------------------------------
    def _act(self, verb: str, row) -> None:
        """Run ``verb`` on ``row`` in the background, by its ID.

        An image listed under several names is untagged by the name under
        the cursor instead: by its ID the engine would refuse without
        ``--force``, which is never passed.
        """
        target = row.id                  # hex, checked when it was listed
        if verb == "rmi" and row.tagged and self._shared(row):
            if not REFERENCE.match(row.name):
                self.status = f"not a name {self.engine} would take: " \
                              f"{row.name}"
                return
            target = row.name
        doing = {"start": "starting", "stop": "stopping",
                 "restart": "restarting", "rm": "removing",
                 "rmi": "removing"}[verb]
        what = f"{doing} {row.name}"

        def done(code, out, err):
            if code == 0:
                past = {"start": "started", "stop": "stopped",
                        "restart": "restarted", "rm": "removed",
                        "rmi": "removed"}[verb]
                self._say(f"{past} {row.label()}")
                self.refresh()
            else:
                self._say(f"{verb} {row.name} failed: "
                          f"{_message(err) or _message(out) or code}")

        self._start_task([self.command, verb, target], ACTION_SECONDS, what,
                         done)

    def _start_task(self, argv: list[str], seconds: float, what: str,
                    done) -> None:
        try:
            self.task = Task(argv, seconds, what, done)
        except OSError as exc:
            self._say(f"{self.engine} could not be run: "
                      f"{exc.strerror or exc}")
            return
        self.status = f"{what}..."

    def _say(self, text: str) -> None:
        """A result: in the footer, and in full on the status line."""
        self.status = text
        self.ctx.set_status(f"Containers: {text}")

    # -- searching ----------------------------------------------------------------
    def _search(self, term: str) -> None:
        if not term:
            self.status = "nothing to search for"
            return
        if self.task is not None:
            self.status = f"still {self.task.what}: wait for it to finish"
            return

        def done(code, out, err):
            if code != 0:
                self._say(f"search failed: "
                          f"{_message(err) or _message(out) or code}")
                return
            self.found = parse_search(out)
            self.searched = term
            self._show("search")
            self._rebuild_view()
            if self.found:
                self.status = (f"{len(self.found)} found: Enter pulls one, "
                               f"Esc goes back")
            elif self.engine == "podman":
                self._say(f"nothing found for {term} -- Podman searches the "
                          f"registries registries.conf names; name one, as "
                          f"in quay.io/{term}")
            else:
                self._say(f"nothing found for {term}")

        # "--" ends the options: a term that starts with "-" is a term.
        argv = [self.command, "search", "--limit",
                str(int(self.cfg["search_limit"])), "--format",
                SEARCH_FORMAT, "--", term]
        self._start_task(argv, float(self.cfg["timeout"]),
                         f"searching for {term}", done)

    # -- the terminal: pull and shell ---------------------------------------------
    def _pull(self, name: str) -> None:
        if not name:
            self.status = "nothing to pull"
            return
        if not REFERENCE.match(name):
            self.status = f"not an image name: {name}"
            return
        self._open_term([self.command, "pull", name],
                        f"{self.engine} pull {name}", ("pull", name))

    def _shell(self, row: Container) -> None:
        # The ID, not the name: hex, checked when it was listed.
        self._open_term([self.command, "exec", "-it", row.id, "sh"],
                        f"{self.engine} exec {row.name}", ("shell", row))

    def _open_term(self, argv: list[str], title: str, purpose: tuple) -> None:
        try:
            self.term = EngineTerminal(self.ctx, argv, title)
        except OSError as exc:
            self._say(f"could not start {self.engine}: "
                      f"{exc.strerror or exc}")
            return
        self.after_term = purpose

    def _close_term(self) -> None:
        """The terminal has finished, or been closed: the list comes back,
        refreshed, with the cursor on what the terminal was for."""
        term, purpose = self.term, self.after_term
        self.term = self.after_term = None
        assert term is not None and purpose is not None
        term.on_exit()
        self.refresh()
        kind, subject = purpose
        if kind == "pull":
            anchor = pulled_image(self.images, subject)
            self._show("images", anchor)
            self._say(f"pulled {subject}" if anchor is not None
                      else f"pull of {subject} ended")
        else:
            self.status = f"left {subject.name}"

    # -- closing ------------------------------------------------------------------
    def on_exit(self) -> None:
        # A background action is left to finish: stopping half-way through
        # is worse than letting it end.
        if self.term is not None:
            self.term.on_exit()
            self.term = None

    # -- drawing ------------------------------------------------------------------
    def _caption(self) -> str:
        running = sum(1 for c in self.containers if c.running)
        stopped = len(self.containers) - running
        text = (f" [containers] {self.engine}: {running} running,"
                f" {stopped} stopped, {len(self.images)} images")
        if self.list == "search":
            text += f" -- found for {self.searched!r}"
        return text + " "

    def _header(self, width: int) -> str:
        return self._line(self._columns(None), width)

    def _columns(self, row) -> list[tuple[str, int]]:
        """The cells of a row (or of the header, for None), each with the
        width it wants; 0 takes what is left.  What matters most comes
        first, so a pane half the width of the screen still shows it: a
        container's name and status, an image's name and tag."""
        if self.list == "images":
            if row is None:
                return [("REPOSITORY", 0), ("TAG", 12), ("ID", 12),
                        ("CREATED", 14), ("SIZE", 9)]
            return [(row.repository, 0), (row.tag, 12), (row.id, 12),
                    (row.created, 14), (row.size, 9)]
        if self.list == "search":
            if row is None:
                return [("NAME", 32), ("DESCRIPTION", 0)]
            return [(row.name, 32), (row.description, 0)]
        if row is None:
            return [("NAME", 18), ("STATUS", 20), ("IMAGE", 28),
                    ("PORTS", 0)]
        return [(row.name, 18), (row.status, 20), (row.image, 28),
                (row.ports, 0)]

    @staticmethod
    def _line(cells: list[tuple[str, int]], width: int) -> str:
        # The column that takes what is left keeps a useful width even when
        # nothing is left: the line is cut at the pane's edge instead.
        fixed = sum(size + 1 for _, size in cells if size)
        rest = max(24, width - fixed)
        return " ".join(truncate(text, size or rest).ljust(size or rest)
                        for text, size in cells)[:width]

    def _footer(self) -> str:
        if self.mode == "confirm" and self.pending is not None:
            return f" {self._question()} Enter/y=yes  Esc=no "
        if self.mode == "filter":
            return f" filter> {self.filter} "
        if self.mode == "pull":
            return f" pull> {self.entry} "
        if self.mode == "find":
            return f" find> {self.entry} "
        if self.status:
            return f" {self.status} "
        if self.task is not None:
            return f" {self.task.what}... "
        if self.list == "search":
            return " ? keys  Enter pull  / filter  Esc back "
        if self.list == "images":
            return (" ? keys  F8 remove  p pull  f find  c containers"
                    "  / filter  o sort  Esc close ")
        return (" ? keys  Enter shell  s start/stop  R restart  F8 remove"
                "  a all  i images  p pull  / filter  Esc close ")

    def _empty(self) -> tuple[str, int | None]:
        """What the list area says when there is nothing in it."""
        if self.error:
            return f"  ! {self.error} -- r retries", theme.attr("panelerror")
        if self.filter:
            return "  (nothing matches the filter)", None
        if self.list == "images":
            return "  no images -- p pulls one", None
        if self.list == "search":
            return "  nothing found", None
        if self.containers and not self.show_all:
            return "  no running containers -- a shows the stopped ones", None
        return "  no containers", None

    def draw(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if self.term is not None:
            self.term.draw(stdscr, y, x, h, w)
            return
        if h < 3:
            return
        self.put(stdscr, y, x, w, self._caption(), theme.attr("keybar"))
        rows = h - 3                     # caption, header, footer
        if self.mode == "help":
            self.put(stdscr, y + 1, x, w, " Keys", theme.attr("panelhead"))
            for row in range(rows):
                text = ""
                if row < len(HELP):
                    keys, what = HELP[row]
                    text = f"  {keys:<9} {what}"
                self.put(stdscr, y + 2 + row, x, w, text)
            self.put(stdscr, y + h - 1, x, w, " any key: back to the list ",
                     theme.attr("keybar"))
            return
        self.put(stdscr, y + 1, x, w, self._header(w), theme.attr("panelhead"))
        if rows > 0:
            if self.cursor < self.scroll:
                self.scroll = self.cursor
            if self.cursor >= self.scroll + rows:
                self.scroll = self.cursor - rows + 1
            visible = self.view[self.scroll:self.scroll + rows]
            for row in range(rows):
                if row < len(visible):
                    attr = theme.attr("panelcursor") \
                        if self.scroll + row == self.cursor else None
                    self.put(stdscr, y + 2 + row, x, w,
                             self._line(self._columns(visible[row]), w), attr)
                elif row == 0:
                    text, attr = self._empty()
                    self.put(stdscr, y + 2, x, w, text, attr)
                else:
                    self.put(stdscr, y + 2 + row, x, w, "")
            if self.error and visible:
                # The lists are the last good ones; say why they may be old.
                self.put(stdscr, y + 1 + rows, x, w,
                         f"  ! {self.error} -- r retries",
                         theme.attr("panelerror"))
        self.put(stdscr, y + h - 1, x, w, self._footer(), theme.attr("keybar"))
