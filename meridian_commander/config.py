"""Meridian Commander's configuration file.

Everything lives in one INI file, created with commented defaults on first
use::

    ~/.config/meridian-commander/config.ini     ($XDG_CONFIG_HOME honoured)

The ``[plugins]`` section configures plug-in discovery, and each plug-in reads
its own ``[plugin:<name>]`` section (e.g. ``[plugin:json_push]``).  The file
can be edited from inside the application (``C`` -> "Edit configuration");
changes take effect the next time a plug-in is opened.
"""

from __future__ import annotations

import configparser
import os

DEFAULT_CONFIG = """\
; Meridian Commander configuration.
; Edit from within the app: press C and choose "Edit configuration".

[ui]
; Colour scheme: turbo (Borland blue), midnight (black ground), mono.
; Also switchable while running, from Options > Colours.
scheme = turbo

[plugins]
; Extra directories to search for plug-ins, colon-separated.
; Built-in plug-ins and ~/.config/meridian-commander/plugins/ are always used.
dirs =

[plugin:json_push]
; SSH server that hosts the JSON listener.
host =
port = 22
username =
; Leave password empty to use your SSH agent / default keys.
password =
key_filename =
; Listener address as seen FROM the server (loopback is typical).
listener_host = 127.0.0.1
listener_port = 9000
timeout = 15

[plugin:run_remote_script]
; SSH server to run the script on.
host =
port = 22
username =
password =
key_filename =
; Local script to upload, and where to put/run it on the server.
script =
remote_dir = /tmp
; Interpreter used to run it ("sh", "bash", "python3", ...).
interpreter = sh
timeout = 30

; -- Data plugins (Profile table / Clean table / Build dataset) --------------
; These act on the CSV/TSV/JSON-lines file selected in the *other* pane.
; "delimiter" blank = auto-detect (comma/tab/semicolon/pipe); "has_header"
; yes/no; "max_bytes" caps how much of a large file is read.

[plugin:csv_profile]
delimiter =
encoding = utf-8
has_header = yes
; Most-common values shown per categorical column.
top_n = 5
; Rows shown by the head/tail commands.
preview_rows = 20
max_bytes = 67108864

[plugin:csv_clean]
delimiter =
encoding = utf-8
has_header = yes
max_bytes = 67108864

[plugin:csv_build]
delimiter =
encoding = utf-8
has_header = yes
max_bytes = 67108864
"""


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "meridian-commander")


def config_path() -> str:
    return os.path.join(config_dir(), "config.ini")


def _private_write(path: str):
    """Open ``path`` for writing, readable only by its owner.

    The file holds each plug-in's ``[plugin:<name>]`` section, and some of
    those sections have a ``password`` in them, so it must not be created at
    whatever the umask happens to allow.  The mode applies only when the file
    is created; an existing one keeps the permissions it has, which is the
    right answer for a file the user may have deliberately locked down
    further.
    """
    return os.fdopen(
        os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w")


def ensure_config() -> str:
    """Create the config file with commented defaults if missing; return path."""
    path = config_path()
    if not os.path.exists(path):
        os.makedirs(config_dir(), mode=0o700, exist_ok=True)
        with _private_write(path) as f:
            f.write(DEFAULT_CONFIG)
    return path


def load() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    try:
        parser.read(config_path())
    except (OSError, configparser.Error):
        pass
    return parser


def plugin_settings(name: str, defaults: dict) -> dict:
    """Merge a plug-in's ``[plugin:<name>]`` config section over ``defaults``.

    Empty values in the file are ignored so the defaults survive; values whose
    default is an int are coerced back to int.
    """
    merged = dict(defaults)
    parser = load()
    section = f"plugin:{name}"
    if parser.has_section(section):
        for key, value in parser.items(section):
            value = value.strip()
            if value == "":
                continue
            if isinstance(defaults.get(key), int):
                try:
                    merged[key] = int(value)
                    continue
                except ValueError:
                    pass
            merged[key] = value
    return merged


def colour_scheme() -> str:
    """The colour scheme named in ``[ui] scheme``, or the default.

    An unknown name is not an error worth stopping a file manager for: the
    theme falls back to ``turbo`` and the user sees the wrong colours rather
    than no application.
    """
    parser = load()
    return (parser.get("ui", "scheme", fallback="") or "").strip() or "turbo"


def save_scheme(name: str) -> bool:
    """Remember a colour scheme chosen from the Options menu.

    Rewritten with :mod:`configparser` rather than by hand, which loses the
    comments in the rest of the file -- so the file is only touched when the
    setting actually changes, and a failure to write is reported to the caller
    instead of raising: the scheme is already applied on screen either way.
    """
    if colour_scheme() == name:
        return True
    parser = load()
    if not parser.has_section("ui"):
        parser.add_section("ui")
    parser.set("ui", "scheme", name)
    try:
        os.makedirs(config_dir(), mode=0o700, exist_ok=True)
        with _private_write(config_path()) as f:
            parser.write(f)
    except OSError:
        return False
    return True


def extra_plugin_dirs() -> list[str]:
    """Directories listed in ``[plugins] dirs`` (colon-separated)."""
    parser = load()
    raw = parser.get("plugins", "dirs", fallback="") or ""
    return [os.path.expanduser(d) for d in raw.split(":") if d.strip()]
