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
"""


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    new = os.path.join(base, "meridian-commander")
    # The project was previously named martin_commander; keep honouring an
    # existing old config directory until a new one is created.
    old = os.path.join(base, "martin-commander")
    if not os.path.isdir(new) and os.path.isdir(old):
        return old
    return new


def config_path() -> str:
    return os.path.join(config_dir(), "config.ini")


def ensure_config() -> str:
    """Create the config file with commented defaults if missing; return path."""
    path = config_path()
    if not os.path.exists(path):
        os.makedirs(config_dir(), exist_ok=True)
        with open(path, "w") as f:
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


def extra_plugin_dirs() -> list[str]:
    """Directories listed in ``[plugins] dirs`` (colon-separated)."""
    parser = load()
    raw = parser.get("plugins", "dirs", fallback="") or ""
    return [os.path.expanduser(d) for d in raw.split(":") if d.strip()]
