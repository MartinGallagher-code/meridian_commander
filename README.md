# Meridian Commander

[![PyPI](https://img.shields.io/pypi/v/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![Python versions](https://img.shields.io/pypi/pyversions/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![Downloads](https://img.shields.io/pypi/dm/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![CI](https://github.com/MartinGallagher-code/martin_commander/actions/workflows/publish.yml/badge.svg)](https://github.com/MartinGallagher-code/martin_commander/actions/workflows/publish.yml)

*The meridian is noon — the other end of the clock from midnight.*

A two-pane terminal file manager in the spirit of **Midnight Commander**,
written in pure Python. It browses local **and networked** locations, copies and
moves files between the two panes regardless of where each side lives,
synchronizes directories so both panes hold the newest version of every file,
and ships with a built-in file viewer and editor.

```
+- local:/home/user ---------------+- sftp://me@server:/srv/www ------+
| Name                Size  Modify | Name                Size  Modify |
| ..                               | ..                               |
| projects/          <DIR>  Jul 20 | assets/            <DIR>  Jul 19 |
|*report.pdf          1.2M  Jul 21 | index.html          4.3K  Jul 18 |
| notes.txt           842   Jul 22 | style.css           1.1K  Jul 18 |
+----------------------------------+----------------------------------+
 F1 Help  F5 Copy  F6 Move  F9 Sync  F10 Quit
```

## Features

- **Two independent panes** — browse two locations side by side, `Tab` between
  them, and swap them with `Ctrl-U`.
- **Local and networked locations** — each pane can point at the local disk, an
  **SFTP** server, an **SSH (shell)** host, or an **FTP** server. Press `F2` to
  open a location. The **SSH (shell)** mode lists and transfers files by running
  ordinary commands (`ls`, `cat`, …) over the SSH channel, so it works even on
  servers that permit SSH login but have the SFTP subsystem disabled.
- **Copy & move across any pair of panes** — local→remote, remote→local,
  remote→remote and local→local all work through one streaming engine, with a
  cancellable progress bar (`F5` copy, `F6` move).
- **Bidirectional directory sync** (`F9`) — compares the two panes and copies
  the newest version of each file in whichever direction is needed, so both
  sides end up holding the latest of everything. Nothing is deleted; you get a
  preview and confirmation before anything is written.
- **File viewer** (`F3`) — scrollable, with **line numbers you can toggle**
  (`N`) and horizontal scrolling; works on remote files too.
- **File editor** (`F4`) — a real in-place editor (insert/delete, Enter/Backspace
  line handling, save with `Ctrl-S`), also with toggleable line numbers.
- **Tag multiple files** (`Insert`/`Space`, `+` all, `-` none) for batch
  copy/move/delete.
- **Per-pane hidden-file toggle** (`.`) — show or hide dotfiles independently in
  each pane.
- **Terminal inside the pane** (`t`) — the pane itself becomes a
  pseudo-terminal running a shell in the pane's directory, while the other pane
  keeps working normally. Works for **local panes** (a real pty) and for
  **SFTP/SSH panes** (an interactive shell on the pane's existing SSH
  connection). `Ctrl-]` switches to the other pane while the shell keeps
  running (Tab back to return); `F10` or exiting the shell closes it. For
  full-screen programs (vim, htop) use
  `!`, which suspends the UI into a real terminal instead.
- **Mouse support** — click to select, double-click to open, wheel to scroll,
  and **right-click for a context menu** of actions (view, edit, copy, move,
  rename, delete, tag, mkdir, terminal).
- **Works even when F-keys are hijacked** — every function key has a digit alias
  (`1`–`0` → `F1`–`F10`) and the common actions have mnemonic letters.
- **Pane plug-ins** (`p`) — put a pane into plug-in mode: pick from discovered
  plug-ins and it takes over the pane, with access to the opposite pane's
  contents. Writing one takes a dozen lines (see *Plug-ins* below); built-ins
  include remote JSON push and run-remote-script over SSH.
- **In-app configuration** (`C`) — edit `config.ini` and plug-in files in the
  built-in editor without leaving the app.
- **No required dependencies** for local + FTP use — it runs on the Python
  standard library. SFTP uses the optional [`paramiko`](https://www.paramiko.org/)
  package.

## Install

```bash
pip install meridian-commander            # once published to PyPI

# with SFTP/SSH support (remote panes, in-pane remote terminal, SSH plug-ins)
pip install "meridian-commander[ssh]"

# or from a checkout
pip install ".[ssh]"
```

This installs the `meridian-commander` command and its short alias
`meridian`.

You can also run it straight from the source tree without installing:

```bash
python -m meridian_commander
```

## Usage

```bash
meridian-commander                 # both panes start in your home directory
meridian-commander /etc /var/log   # left pane in /etc, right pane in /var/log
```

### Connecting to a remote location

Press **F2** in the pane you want to change and choose **SFTP**, **SSH (shell)**
or **FTP**. You will be asked for host, username, port and credentials:

- **SFTP** authenticates with an SSH key file, your SSH agent, or a password,
  and browses through the SFTP subsystem.
- **SSH (shell)** authenticates the same way but does not use SFTP at all — it
  drives `ls`/`mkdir`/`rm`/`mv` over the SSH channel. Use it when a server
  allows SSH login but has SFTP disabled. File contents are transferred with a
  fallback chain — `cat`, then `dd`, then the raw **scp protocol** — so viewing
  and editing work even on restricted appliance shells that answer
  `Command 'cat' not supported` (most embedded SSH servers still implement
  scp). The method that works is remembered for the rest of the session.
- **FTP** prefers the modern `MLSD` listing command and automatically falls
  back to parsing classic `LIST` output on older servers that don't support it
  (which otherwise answer `500 Unknown command`). Log in anonymously by leaving
  the defaults, or supply a username and password.

Once connected, that pane behaves exactly like a local one — navigate, view,
edit, and copy/move/sync to and from it.

### Key bindings

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `Tab` | switch active pane | `F1` | help |
| `↑`/`↓` `j`/`k` | move cursor | `F2` | open / connect location |
| `PgUp`/`PgDn` | page | `F3` | view file |
| `Home`/`End` | first / last | `F4` | edit file |
| `Enter` / `→` | enter dir / view file | `F5` | copy to other pane |
| `Backspace` / `←` | parent directory | `F6` | move to other pane |
| `Insert` / `Space` | tag file | `F7` | make directory |
| `+` / `-` | tag all / untag all | `F8` | delete |
| `Ctrl-U` | swap panes | `F9` | synchronize panes |
| `Ctrl-R` | reload panes | `F10` | quit |
| `Ctrl-G` | go to path | `Ctrl-T` | change sort order |
| `.` | show/hide hidden files | `t` | terminal inside this pane |
| `p` / F11 | plug-in mode (this pane) | `!` | full-screen shell |
| `C` | configuration menu | `Ctrl-]` | terminal: switch to other pane |

**F-key aliases** (for terminals that swallow function keys): press the digit
`1`–`0` for `F1`–`F10`, or the mnemonic letter — `?`/`1` help, `o` open/connect,
`v` view, `e` edit, `c` copy, `m` move, `d` delete, `s` sync, `q` quit.

**Mouse**: click to select and focus a pane, double-click to open a
file/directory, scroll wheel to move through the listing, and **right-click** for
a context menu of actions.

In the **viewer**: `N` toggles line numbers, `W` toggles wrap, arrows/PgUp/PgDn
scroll, `Q` quits.
In the **editor**: `F2` / `Ctrl-S` / `Ctrl-O` save, `F10` / `Ctrl-Q` quit,
`Ctrl-Y` / `Ctrl-K` delete a line, `Ctrl-L` toggles line numbers. Esc does
not quit — only `q`-style keys and `F10` leave the app, so a stray Esc never
throws you out.

### Running inside VS Code's integrated terminal

VS Code intercepts some control keys before they reach terminal programs:
`Ctrl-K` is a chord prefix (`terminal.integrated.allowChords`), and keys bound
to workbench commands in `terminal.integrated.commandsToSkipShell` (on some
platforms `Ctrl-Q`) never arrive. Every editor command therefore has a
VS Code-safe alias — use **`F2` to save, `F10` to quit, `Ctrl-Y` to delete a
line** and you'll never notice the difference. If you prefer the control-key
bindings, add this to your VS Code `settings.json`:

```json
{
  "terminal.integrated.allowChords": false,
  "terminal.integrated.commandsToSkipShell": ["-workbench.action.quit"]
}
```

## Plug-ins

Press **`p`** (or F11) to put the active pane into **plug-in mode**: a menu
lists the discovered plug-ins and the chosen one takes over that pane. The
plug-in can see the **opposite pane** — its filesystem (local or remote), its
directory and entries — so it can do work on whatever you have open next to it.
`Esc` closes the plug-in and returns the pane to its file listing; `Tab` still
switches panes while a plug-in is open.

Built-in plug-ins:

- **Terminal** — the in-pane pseudo-terminal (also on the `t` key); a shell in
  the pane's directory, local or over the pane's SSH connection.
- **Find in other pane** — recursively search the other pane's directory by
  glob pattern (works on remote panes too).
- **JSON push** — the user enters input in the bottom line; the plug-in logs
  into a remote server over SSH, delivers the input as JSON to a TCP listener
  on that server (via an SSH channel, so the listener can stay on loopback),
  waits for the reply and shows it in the output area.
- **Run remote script** — on each input, logs into an SSH server, copies a
  configured local script into a configured remote directory, runs it with the
  input as arguments, and shows its output.

### Writing a plug-in

Drop a `.py` file into `~/.config/meridian-commander/plugins/` (or into
`meridian_commander/plugins/` inside the framework — both are scanned, plus any
extra directories listed in the config file). A complete plug-in is:

```python
from meridian_commander.plugin_api import InputOutputPlugin

class Shout(InputOutputPlugin):
    name = "Shout"
    description = "Uppercase whatever you type"
    prompt = "say> "

    def process(self, line):
        return [line.upper()]
```

`InputOutputPlugin` provides the classic two-part layout: a scrolling output
area on top and an input line at the bottom; `process()` is called on Enter,
and `self.print(...)` emits output at any time. The plug-in context is at
`self.ctx` — `ctx.other_fs`, `ctx.other_path`, `ctx.other_entries()`,
`ctx.refresh_other()` give access to the opposite pane. For full control of
drawing and keys, subclass `PanePlugin` instead.

## Configuration

Press **`C`** for the configuration menu:

- **Edit configuration** opens `~/.config/meridian-commander/config.ini` in the
  built-in editor (created with commented defaults on first use). Plug-ins read
  their settings from `[plugin:<name>]` sections; `[plugins] dirs` adds extra
  plug-in directories.
- **Edit a plug-in file** lists every discovered plug-in file (built-in and
  user) and opens the chosen one in the editor.
- **Open user plug-in folder in this pane** jumps the pane to
  `~/.config/meridian-commander/plugins/` so you can manage plug-ins like any
  other files.

## How synchronization works

`F9` builds a plan by walking both panes' directory trees:

- a file present on only one side is copied to the other;
- a file present on both sides is compared by modification time, and the
  **newer** copy overwrites the older one (times within 2 seconds are treated as
  equal to avoid needless copies);
- the copied file is stamped with the **source file's modification time**, so
  both sides stay identical in age — a second sync finds nothing to do instead
  of copying the file back the other way;
- nothing is ever deleted.

You see the full list of planned copies and the total byte count before
confirming, and the operation can be cancelled mid-way.

## Architecture

Every location — local, SFTP, SSH shell, FTP — implements one small `FileSystem`
interface (`listdir`, `stat`, streaming `open_read`/`open_write`, and the
mutating operations). Because the interface is uniform, the copy/move engine and
the sync engine are written once and work across any pair of backends.

| Module | Responsibility |
| --- | --- |
| `filesystems.py` | `FileSystem` interface + Local / SFTP / SSH / FTP backends |
| `operations.py` | streaming copy, recursive copy, move |
| `sync.py` | bidirectional sync plan + execution |
| `plugin_api.py` | pane plug-in API (`PanePlugin`, `InputOutputPlugin`, context) |
| `plugins/` | plug-in discovery + built-in plug-ins |
| `config.py` | `config.ini` handling (per-plug-in sections, plug-in dirs) |
| `panel.py` | one pane's listing, cursor, selection, sorting |
| `viewer.py` / `editor.py` | file viewer and editor |
| `dialogs.py` | prompts, menus, confirmations, progress bars |
| `app.py` | curses UI, key bindings, orchestration |

## Utility scripts

`scripts/merge.sh` bundles a directory tree into a single text file and
`scripts/split.sh` expands it again:

```bash
scripts/merge.sh bundle.txt some/dir     # bundle a tree (default: current dir)
scripts/split.sh bundle.txt restored/    # expand it (default: current dir)
```

The bundle format inlines text files verbatim and base64-encodes binaries
(and any text file whose content would collide with the section markers).
Permissions, symlinks, empty directories and missing trailing newlines are
preserved; every file carries a sha256 that `split.sh` verifies on expansion.
`split.sh` refuses bundles containing absolute or `..` paths and never passes
bundle-controlled strings to a shell.

## Development

```bash
pip install ".[dev]"
pytest
```

The test suite covers the filesystem-agnostic core (copy, move, sync, panel
logic) using the local backend and temporary directories.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
