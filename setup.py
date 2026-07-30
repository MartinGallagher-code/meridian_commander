"""Build-time guard against a setuptools too old to read pyproject.toml.

All of this project's packaging metadata lives in ``pyproject.toml`` under the
PEP 621 ``[project]`` table, which setuptools only learned to read in version
61.  An older setuptools does not reject that table -- it ignores it.  The
build then succeeds against nothing but the defaults, and pip cheerfully
installs a distribution named ``UNKNOWN`` version ``0.0.0``: the modules land
on ``sys.path`` and import fine, but ``[project.scripts]`` went with the rest
of the ignored table, so no ``meridian`` command is ever written.  The symptom
reaches the user as "installed fine, command not found", which sends them
hunting through their ``PATH`` for a script that was never created.

``[build-system] requires`` already asks for ``setuptools>=61``, and that is
enough whenever pip builds in an isolated environment, which it does by
default.  This file is for the builds that bypass it -- ``--no-build-isolation``,
distro packaging, air-gapped mirrors, CI images with a pinned toolchain --
where whatever setuptools happens to be installed is what builds the wheel.
There, this turns a silent wrong answer into a loud one.

The check fails open on purpose.  It refuses the build only when it can read
the installed version and see that it is genuinely too old; an unparseable
version string is assumed to be some future scheme and allowed through.  A
guard that blocks a *new* setuptools would be a worse bug than the one it
guards against.
"""

import re
import sys

# The first setuptools that reads [project] out of pyproject.toml.
MINIMUM_SETUPTOOLS = 61


def too_old(version):
    """Return true when ``version`` is a setuptools known to predate PEP 621.

    Only the leading integer is consulted, so development and post-release
    suffixes ("61.0.0.post0", "62.0b1") sort with their major version.  A
    string with no leading integer at all reads as "not known to be old".
    """
    match = re.match(r"\d+", version or "")
    return bool(match) and int(match.group()) < MINIMUM_SETUPTOOLS


MESSAGE = """\
meridian-commander cannot be built with setuptools {found}.

Its packaging metadata is a PEP 621 [project] table, which setuptools reads
only from version {minimum}.  Older versions ignore the table silently: the build
would succeed, and pip would install a package called UNKNOWN with no
"meridian" command in it.

Build it with a current setuptools instead. Either let pip fetch one itself
by dropping --no-build-isolation, or upgrade this environment:

    {python} -m pip install --upgrade "setuptools>={minimum}"
"""


def main():
    import setuptools

    found = getattr(setuptools, "__version__", "")
    if too_old(found):
        sys.stderr.write(MESSAGE.format(
            found=found, minimum=MINIMUM_SETUPTOOLS, python=sys.executable))
        raise SystemExit(1)

    # Everything else -- name, version, scripts, the lot -- comes from
    # pyproject.toml. Nothing is repeated here, so nothing here can drift
    # out of step with it.
    setuptools.setup()


if __name__ == "__main__":
    main()
