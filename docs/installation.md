# Installation

Meridian Commander needs Python 3.9 or newer on a POSIX system (Linux or
macOS; on Windows it runs under WSL). The core runs on the standard library
alone — the optional extras add remote access and faster data crunching.

```bash
pip install meridian-commander

# with SFTP/SSH support (remote panes, in-pane remote terminal, SSH plug-ins)
pip install "meridian-commander[ssh]"

# optional: pandas acceleration for the data plug-ins' group-by on large files
pip install "meridian-commander[data]"

# or from a checkout
pip install ".[ssh]"
```

This installs the `meridian-commander` command and its short alias
`meridian`.

**Recommended: [pipx](https://pipx.pypa.io/).** It puts the commands somewhere
on your `PATH` and keeps the app in its own environment, which avoids the
problem below entirely:

```bash
pipx install "meridian-commander[ssh]"
```

## If `meridian` is "command not found" after installing

**It always works as a module**, whatever the packaging did — same program,
same arguments, no `PATH` involved:

```bash
python -m meridian_commander
```

To get the short command back, first find out which of the two possible
faults you have:

```bash
pip show -f meridian-commander | grep -iE 'bin/|Scripts/'
```

- **It lists `meridian`** — the commands exist and your shell cannot see them.
  That is a `PATH` problem; see below.
- **It lists nothing**, or `pip show` reports no such package at all — the
  commands were never created. Skip to *No commands were created* further down.

### The scripts are not on your `PATH`

`pip` put the commands somewhere your shell does not look. This happens
whenever pip cannot write to the interpreter's own directory — no virtualenv,
not root, or a Python that is "externally managed" (Debian/Ubuntu 23.04+,
Fedora 38+, Homebrew), in which case pip falls back to a **per-user** scripts
directory. It does warn, in the middle of the install output:

```
WARNING: The scripts meridian and meridian-commander are installed in
'/home/you/.local/bin' which is not on PATH.
```

If you have installed plenty of Python packages before and never hit this, it
is not that this one is unusual — it is that most packages are libraries with
no command to lose. `import` resolves through `sys.path`, so a scripts
directory missing from `PATH` costs a library nothing and is invisible until
the first package that ships a command.

On Debian and Ubuntu there is a second reason the *first* such package looks
uniquely broken. The stock `~/.profile` adds the directory only if it already
exists:

```sh
# set PATH so it includes user's private bin if it exists
if [ -d "$HOME/.local/bin" ] ; then
    PATH="$HOME/.local/bin:$PATH"
fi
```

That test runs at **login**. A first-ever `--user` install of anything with a
command creates `~/.local/bin` afterwards, too late to be picked up, so the
command is missing until you log out and back in — and every package installed
after it works fine.

To fix it now, find the directory and add it to `PATH`:

```bash
python -m site --user-base        # scripts are in the "bin" under this
                                  # ("Scripts" on Windows)
```

Typically `~/.local/bin` on Linux, `~/Library/Python/3.x/bin` on macOS, and
`%APPDATA%\Python\Python3xx\Scripts` on Windows. Add it in your shell's
startup file (`~/.bashrc`, `~/.zshrc`):

```bash
export PATH="$HOME/.local/bin:$PATH"
```

Then `hash -r` (or open a new terminal) — a shell that has already failed to
find a command may have cached that fact.

Two other things worth ruling out:

- **`pip` and `python` disagreeing.** If `pip` belongs to a different
  interpreter than the `python` you run, the package lands somewhere unrelated.
  Use `python -m pip install ...` so both are the same one.
- **A virtualenv that is not active.** Installing into a venv puts the
  commands in *its* `bin/`, reachable only while it is activated.

### No commands were created

If `pip show -f` lists no scripts, `PATH` is a red herring — there is nothing
to find. Check what actually got installed:

```bash
pip list | grep -i -E 'meridian|UNKNOWN'
```

An entry reading `UNKNOWN 0.0.0` is the giveaway. This project's metadata is a
PEP 621 `[project]` table in `pyproject.toml`, which **setuptools reads only
from version 61**. An older setuptools ignores that table rather than
rejecting it, so the build falls back to defaults: the modules install and
import perfectly well, but the name, the version and `[project.scripts]` all
go missing together.

`[build-system] requires` asks for `setuptools>=61`, and pip honours it by
building in an isolated environment — so this cannot happen on a normal
`pip install`. It happens where that isolation is bypassed: `--no-build-isolation`,
distro packaging, air-gapped mirrors, CI images with a pinned toolchain.
The repo's `setup.py` exists only to refuse such a build outright, with an
explanatory message, so it fails loudly instead of half-succeeding. An
`UNKNOWN` left behind by an earlier attempt has to be cleared out by hand:

```bash
pip uninstall UNKNOWN
pip install --upgrade "setuptools>=61"   # or drop --no-build-isolation
pip install meridian-commander
```

For development use an **editable** install from the repo root, so your code
changes take effect without reinstalling:

```bash
python -m pip install --upgrade pip   # editable installs need pip >= 21.3
pip install -e ".[ssh]"
```

You can also run it straight from the source tree without installing — and
this same form works for an installed copy whose commands are not on `PATH`:

```bash
python -m meridian_commander
```
