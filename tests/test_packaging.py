"""The setup.py guard against a setuptools too old to read pyproject.toml.

The failure it exists to prevent is a quiet one -- an old setuptools ignores
the PEP 621 ``[project]`` table instead of rejecting it, and installs a
working import with no ``meridian`` command attached -- so the guard is worth
testing at the level it actually runs: a subprocess, against a setuptools that
reports whatever version the test wants it to.
"""

from __future__ import annotations

import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


SETUP_PY = Path(__file__).resolve().parents[1] / "setup.py"

# A stand-in for setuptools that records the call instead of building
# anything, so the success path can be observed without a real build.
STUB = """\
__version__ = {version!r}

def setup(**kwargs):
    print("setup() called with %d keyword arguments" % len(kwargs))
"""


@pytest.fixture(scope="module")
def guard():
    """setup.py imported as a module -- importing it builds nothing."""
    spec = importlib.util.spec_from_file_location("_setup_guard", SETUP_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_setup(tmp_path, source):
    """Run setup.py against a stub setuptools written from *source*.

    The stub goes on ``PYTHONPATH``, which is searched before site-packages,
    so the real setuptools never gets a look in.
    """
    (tmp_path / "setuptools.py").write_text(source)
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    return subprocess.run(
        [sys.executable, str(SETUP_PY)], cwd=str(tmp_path), env=env,
        capture_output=True, text=True,
    )


# -- the version test ----------------------------------------------------------

@pytest.mark.parametrize("version, expected", [
    ("60.10.0", True),          # the version Debian bookworm ships
    ("44.1.1", True),           # the last one supporting Python 2
    ("9.3", True),
    ("61.0.0", False),          # the first with [project] support
    ("61.0", False),
    ("62.0b1", False),          # pre-releases sort with their major version
    ("83.0.0", False),
    ("161.0.0", False),         # not "61" by string comparison
])
def test_too_old(guard, version, expected):
    assert guard.too_old(version) is expected


@pytest.mark.parametrize("version", ["", "unknown", None, "v61"])
def test_unreadable_version_is_allowed_through(guard, version):
    """A version this cannot parse must never block the build.

    Refusing to build against some future versioning scheme would be a worse
    bug than the silent one being guarded against, so the check fails open.
    """
    assert guard.too_old(version) is False


# -- the guard as a build actually meets it ------------------------------------

def test_old_setuptools_is_refused(tmp_path):
    result = run_setup(tmp_path, STUB.format(version="60.10.0"))
    assert result.returncode == 1
    assert "setup() called" not in result.stdout
    # The message has to be actionable on its own: pip shows it and nothing else.
    assert "60.10.0" in result.stderr
    assert "setuptools>=61" in result.stderr
    assert "--no-build-isolation" in result.stderr


def test_current_setuptools_builds(tmp_path):
    result = run_setup(tmp_path, STUB.format(version="83.0.0"))
    assert result.returncode == 0
    assert result.stderr == ""
    # No arguments: every field lives in pyproject.toml, so there is nothing
    # here to drift out of step with it.
    assert "setup() called with 0 keyword arguments" in result.stdout


def test_setuptools_without_a_version_builds(tmp_path):
    """Some vendored builds strip __version__; that must not stop the build."""
    result = run_setup(
        tmp_path, "def setup(**kwargs):\n    print('setup() called')\n")
    assert result.returncode == 0
    assert "setup() called" in result.stdout


# -- what the guard is protecting ----------------------------------------------

def test_build_requires_matches_the_guard(guard):
    """pyproject and setup.py must ask for the same setuptools.

    Two places name the minimum; if they ever disagree, one of them is
    wrong and the guard stops meaning what its message says.
    """
    text = (SETUP_PY.parent / "pyproject.toml").read_text()
    assert 'requires = ["setuptools>=%d"]' % guard.MINIMUM_SETUPTOOLS in text


def test_scripts_are_declared():
    """The entry points the guard exists to protect."""
    text = (SETUP_PY.parent / "pyproject.toml").read_text()
    assert "[project.scripts]" in text
    assert "meridian = " in text


def test_the_two_versions_agree():
    """pyproject and __init__ must name the same version.

    Releasing means bumping both by hand, and the halves fail differently
    when they drift: pyproject decides what PyPI is asked to accept, while
    ``__version__`` is what ``meridian --version`` reports to the user. A
    stale ``__version__`` ships a package that misreports itself; a stale
    pyproject version asks PyPI to accept a filename it already has, which
    it refuses with a 400 that only says "Bad Request" until you read the
    body. Cheaper to catch here.
    """
    from meridian_commander import __version__

    text = (SETUP_PY.parent / "pyproject.toml").read_text()
    declared = re.search(r'(?m)^version = "([^"]+)"$', text)
    assert declared, "no version in pyproject.toml"
    assert declared.group(1) == __version__
