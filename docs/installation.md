# Installation

Meridian Commander needs Python 3.9 or newer on a POSIX system (Linux or
macOS). The core runs on the standard library alone; the optional extras add
remote access and faster data crunching.

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
"command not found" problem below entirely:

```bash
pipx install "meridian-commander[ssh]"
```

## If `meridian` is "command not found" after installing

**It always works as a module**, whatever the packaging did — same program,
same arguments, no `PATH` involved:

```bash
python -m meridian_commander
```

To restore the short command, find out which of the two possible faults you
have:

```bash
pip show -f meridian-commander | grep -iE 'bin/|Scripts/'
```

- **It lists `meridian`** — the commands exist but your shell cannot see
  them: a `PATH` problem. Add the directory that `pip show` printed (for user
  installs, typically `~/.local/bin`) to your `PATH`.
- **It lists nothing**, or `pip show` reports no such package at all — the
  commands were never created, usually because the install went into a
  different Python than the one you expect. Reinstall with the interpreter
  you actually run: `python -m pip install "meridian-commander[ssh]"`, or use
  pipx.

The [README's install
section](https://github.com/MartinGallagher-code/meridian_commander#install)
walks through the diagnosis in more detail.
