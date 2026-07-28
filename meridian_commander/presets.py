"""Saved locations ("presets") you can return to in one keystroke.

A preset remembers *where* a pane was pointing -- the connection details and
the directory -- so a frequently visited place is two keys away instead of a
trip through the connect dialog.  They live in their own INI file, one section
per preset::

    ~/.config/meridian-commander/presets.ini     ($XDG_CONFIG_HOME honoured)

    [www]
    scheme = sftp
    host = web1
    username = deploy
    port = 22
    path = /srv/www

The file is separate from ``config.ini`` because the application rewrites it
(and rewriting the main config would throw away its explanatory comments).  It
is plain text and safe to edit by hand.

**Passwords are never stored.**  A remote preset reconnects the way the
``ssh`` command would -- your agent, ``~/.ssh/config`` and default keys -- and
the application asks for a password only if that fails.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass

from .config import config_dir

HEADER = """\
; Meridian Commander saved locations, one section per preset.
; Written by the app (press b), and safe to edit by hand.
; Passwords are never stored here.

"""

#: Characters that would break the INI round-trip if used in a preset name.
_BAD_NAME_CHARS = "[]\n\r"


def presets_path() -> str:
    return os.path.join(config_dir(), "presets.ini")


def valid_name(name: str) -> bool:
    """Whether ``name`` can be used as a preset (INI section) name."""
    name = name.strip()
    if not name or name.upper() == "DEFAULT":
        return False
    return not any(c in name for c in _BAD_NAME_CHARS)


def _fs_username(fs) -> str:
    """The username a live backend was opened with, whatever it calls it."""
    return getattr(fs, "typed_username", "") or getattr(fs, "username", "") or ""


@dataclass
class Preset:
    """One saved location: a connection plus a directory on it."""

    name: str
    scheme: str = "local"
    path: str = "/"
    host: str = ""
    username: str = ""
    port: int = 0
    key_filename: str = ""

    def label(self) -> str:
        """Human readable location, in the style of the panel header."""
        if self.scheme == "local":
            return f"local:{self.path}"
        who = f"{self.username}@" if self.username else ""
        default_port = 21 if self.scheme == "ftp" else 22
        port = f":{self.port}" if self.port and self.port != default_port else ""
        return f"{self.scheme}://{who}{self.host}{port}:{self.path}"

    def connect_info(self) -> dict:
        """Connection details in the shape the connect dialog produces."""
        return {
            "scheme": self.scheme,
            "host": self.host,
            "username": self.username or None,
            "port": self.port or (21 if self.scheme == "ftp" else 22),
            "key_filename": self.key_filename or None,
        }

    def matches(self, fs) -> bool:
        """Whether ``fs`` is already a live connection to this preset's server.

        Lets a preset reuse a connection a pane already holds instead of
        dialling (and authenticating) the same server a second time.
        """
        if getattr(fs, "scheme", None) != self.scheme:
            return False
        if self.scheme == "local":
            return True
        if getattr(fs, "host", None) != self.host:
            return False
        if int(getattr(fs, "port", 0) or 0) != int(self.port or 0):
            return False
        return _fs_username(fs) == (self.username or "")


def from_location(name: str, fs, path: str) -> Preset:
    """Build a preset describing where ``fs``/``path`` currently point."""
    scheme = getattr(fs, "scheme", "local")
    if scheme == "local":
        return Preset(name=name, scheme="local", path=path)
    return Preset(
        name=name,
        scheme=scheme,
        path=path,
        host=getattr(fs, "host", "") or "",
        username=_fs_username(fs),
        port=int(getattr(fs, "port", 0) or 0),
        key_filename=getattr(fs, "key_filename", "") or "",
    )


def load(path: str | None = None) -> list[Preset]:
    """Read the presets file.  A missing or broken file yields no presets."""
    parser = configparser.ConfigParser()
    try:
        parser.read(path or presets_path())
    except (OSError, configparser.Error):
        return []
    result: list[Preset] = []
    for name in parser.sections():
        section = parser[name]
        try:
            port = int(section.get("port", "") or 0)
        except ValueError:
            port = 0
        result.append(Preset(
            name=name,
            scheme=section.get("scheme", "local") or "local",
            path=section.get("path", "/") or "/",
            host=section.get("host", "") or "",
            username=section.get("username", "") or "",
            port=port,
            key_filename=section.get("key_filename", "") or "",
        ))
    return result


def save(presets: list[Preset], path: str | None = None) -> str:
    """Write ``presets`` out, replacing the file.  Returns the path written."""
    target = path or presets_path()
    parser = configparser.ConfigParser()
    for preset in presets:
        parser[preset.name] = {
            "scheme": preset.scheme,
            "path": preset.path,
            "host": preset.host,
            "username": preset.username,
            "port": str(preset.port or ""),
            "key_filename": preset.key_filename,
        }
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(target, "w") as f:
        f.write(HEADER)
        parser.write(f)
    return target


def add(preset: Preset, presets: list[Preset]) -> list[Preset]:
    """``presets`` with ``preset`` stored, replacing any entry of that name."""
    result = [p for p in presets if p.name != preset.name]
    # Replacing keeps the original position, so a rewritten preset does not
    # jump to the bottom of the list.
    for i, p in enumerate(presets):
        if p.name == preset.name:
            result.insert(i, preset)
            return result
    result.append(preset)
    return result


def remove(name: str, presets: list[Preset]) -> list[Preset]:
    """``presets`` without the entry called ``name``."""
    return [p for p in presets if p.name != name]
