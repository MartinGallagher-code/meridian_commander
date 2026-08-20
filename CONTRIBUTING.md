# Contributing to Meridian Commander

Bug reports, patches and plug-ins are all welcome. This file says what the
project expects of a change, so that neither of us finds out in review.

## Getting set up

```bash
git clone https://github.com/MartinGallagher-code/meridian_commander
cd meridian_commander
pip install -e ".[dev]"
```

The core runs on the standard library alone. The `dev` extra adds what the
checks need: `paramiko` (so the SSH and SFTP paths are exercised rather than
skipped), `pandas` (so the accelerated group-by path is tested alongside the
stdlib fallback), pytest, coverage, ruff and mypy.

## What CI will check

Run all four before opening a pull request; they are quick, and they are
exactly what the workflow runs.

```bash
pytest                                  # about half a minute
coverage run -m pytest && coverage report
ruff check .
mypy meridian_commander
```

### Coverage is 100%, and the gate is real

Every statement in the package is executed by the suite, and `fail_under =
100` fails the build if that stops being true. There are no `exclude_lines`
rules and exactly one `# pragma: no cover` in the tree — the forked child of
`pty.fork()`, which `exec`s into the shell before any in-process tracer could
record it.

This is not a target to be met by adding a test that touches the line. A new
branch wants a test that would fail if the branch were wrong. If a statement
is genuinely unreachable, the right change is usually to delete it.

On Python 3.9 the suite measures 99.9% and the gate is not enforced: precise
line events arrived with PEP 626 in 3.10, and before that the tracer misses a
`break` that follows an if-block ending in its own jump.

### Lint and types

`ruff` runs the pycodestyle and pyflakes rules plus bugbear and two small
comprehension sets — the things that are *wrong* rather than merely
unfashionable. Import ordering and the modernising rewrites are deliberately
off; do not turn them on in a change that is about something else.

`mypy` is a ratchet. Everything it can already prove is enforced; the
categories still to be worked off are listed with counts in `[tool.mypy]` in
`pyproject.toml`. Clearing the last case in a category and deleting its line
is a welcome change on its own.

## How the tests are built

The guiding rule in `tests/support.py` is that **only the terminal and the
blocking dialogs are ever replaced**. Panels, filesystems, plug-ins and
on-disk files stay real, so an assertion is about what the application did
rather than about which mock it called.

- Filesystem logic runs against the local backend in temporary directories.
- Drawing and key loops run against a *real* curses screen on a pseudo-
  terminal, with only the keystrokes scripted. A stand-in window does not
  report the errors that overflowing a real one produces, so it would not
  catch the regressions this code is most prone to.
- Remote backends run against paramiko- and ftplib-shaped stand-ins, so the
  full fallback chains are exercised without a network.
- Office files are built as real zip archives with the part layout a producer
  writes, damage and all.

A test must not need the network, a server, or a particular machine.

## Style

Match the file you are editing. Across the tree that means:

- Lines wrap around 79 columns; 100 is the enforced ceiling, not the target.
- Comments explain *why*, at the point where a reader would otherwise wonder.
  The tree is unusually heavily commented and that is deliberate — a comment
  that restates the code is worse than none, but the reason a guard exists is
  worth a sentence.
- Names are words. A variable called `l` will be sent back.

## Writing a plug-in

Plug-ins live in `meridian_commander/plugins/` in the source tree, or in
`~/.config/meridian-commander/plugins/` for your own. Subclass
`InputOutputPlugin`, give it `name` and `description`, and implement
`process()`. If its input is a vocabulary rather than free text, list its
`Command`s so `F2` offers them for a keystroke each instead of expecting the
user to remember the word. `docs/plugins.md` and `meridian_commander/
plugin_api.py` have the details.

## Pull requests

- One change per pull request. A lint sweep and a new feature in the same
  branch make both harder to review.
- Describe what the change does and why the old behaviour was wrong. The
  commit history is the project's explanation of itself; a message that says
  only "fix bug" throws that away.
- Add the changelog entry under `## [Unreleased]` in `CHANGELOG.md`.
- Do not bump the version. Releases are cut separately — see
  [PUBLISHING.md](PUBLISHING.md).

## Reporting bugs

Open an issue with the terminal and `$TERM`, the Python version, how
Meridian Commander was installed, and what you did. For anything involving a
remote pane, say which backend (SFTP, SSH shell, FTP) and, if you can, what
the server is.

Security issues go to [SECURITY.md](SECURITY.md) instead — please do not open
a public issue for those.
