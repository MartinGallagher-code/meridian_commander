"""Plugin discovery.

Plugins are collected from three places:

* the built-in package directory (``meridian_commander/plugins/*.py``) --
  dropping a file here adds a plug-in to the framework itself;
* the user directory ``~/.config/meridian-commander/plugins/*.py``
  (``$XDG_CONFIG_HOME`` is honoured); and
* any extra directories listed under ``[plugins] dirs`` in the
  configuration file.

Any class in those modules that subclasses
:class:`~meridian_commander.plugin_api.PanePlugin` and defines a ``name`` is
offered in the plugin menu.  A module that fails to import is skipped (and
reported alongside the working plugins) rather than breaking the menu.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil

from ..config import extra_plugin_dirs
from ..plugin_api import InputOutputPlugin, PanePlugin


def user_plugin_dir() -> str:
    from ..config import config_dir

    return os.path.join(config_dir(), "plugins")


def builtin_plugin_dir() -> str:
    return os.path.dirname(__file__)


def plugin_dirs() -> list[str]:
    """All directories searched for plug-ins, in load order."""
    dirs = [builtin_plugin_dir(), user_plugin_dir()]
    dirs.extend(extra_plugin_dirs())
    return dirs


def discover() -> tuple[list[type], list[str]]:
    """Return ``(plugin_classes, load_errors)`` sorted by plugin name."""
    classes: list[type] = []
    errors: list[str] = []
    seen: set[str] = set()

    # Built-in plugins shipped inside the package.
    pkg_dir = builtin_plugin_dir()
    for mod_info in pkgutil.iter_modules([pkg_dir]):
        try:
            mod = importlib.import_module(f"{__name__}.{mod_info.name}")
        except Exception as exc:
            errors.append(f"{mod_info.name}: {exc}")
            continue
        _collect(mod, classes, seen)

    # User + configured plugin directories.
    for pdir in plugin_dirs()[1:]:
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(pdir, fn)
            try:
                spec = importlib.util.spec_from_file_location(
                    f"mc_user_plugin_{fn[:-3]}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)  # type: ignore[union-attr]
            except Exception as exc:
                errors.append(f"{fn}: {exc}")
                continue
            _collect(mod, classes, seen)

    classes.sort(key=lambda c: c.name.lower())
    return classes, errors


def _collect(mod, classes: list[type], seen: set[str]) -> None:
    for obj in vars(mod).values():
        if (isinstance(obj, type)
                and issubclass(obj, PanePlugin)
                and obj not in (PanePlugin, InputOutputPlugin)
                and getattr(obj, "name", "")):
            if obj.name not in seen:
                seen.add(obj.name)
                classes.append(obj)
