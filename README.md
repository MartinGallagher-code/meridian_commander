# Martin Commander

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
- **Drop into a terminal** (`t` or `!`) in the current directory — a local shell
  for local panes, or an `ssh` session into the same directory for SFTP panes.
- **Mouse support** — click to select, double-click to open, wheel to scroll,
  and **right-click for a context menu** of actions (view, edit, copy, move,
  rename, delete, tag, mkdir, terminal).
- **Works even when F-keys are hijacked** — every function key has a digit alias
  (`1`–`0` → `F1`–`F10`) and the common actions have mnemonic letters.
- **No required dependencies** for local + FTP use — it runs on the Python
  standard library. SFTP uses the optional [`paramiko`](https://www.paramiko.org/)
  package.

## Install

```bash
# from a checkout
pip install .

# with SFTP/SSH support
pip install ".[sftp]"
```

This installs the `martin-commander` (and short alias `mmc`) commands.

You can also run it straight from the source tree without installing:

```bash
python -m martin_commander
```

## Usage

```bash
martin-commander                 # both panes start in your home directory
martin-commander /etc /var/log   # left pane in /etc, right pane in /var/log
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
| `.` | show/hide hidden files | `t` / `!` | terminal in current dir |

**F-key aliases** (for terminals that swallow function keys): press the digit
`1`–`0` for `F1`–`F10`, or the mnemonic letter — `?`/`1` help, `o` open/connect,
`v` view, `e` edit, `c` copy, `m` move, `d` delete, `s` sync, `q` quit.

**Mouse**: click to select and focus a pane, double-click to open a
file/directory, scroll wheel to move through the listing, and **right-click** for
a context menu of actions.

In the **viewer**: `N` toggles line numbers, `W` toggles wrap, arrows/PgUp/PgDn
scroll, `Q` quits.
In the **editor**: `Ctrl-S` saves, `Ctrl-Q` quits, `Ctrl-K` deletes a line,
`Ctrl-L` toggles line numbers.

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
| `panel.py` | one pane's listing, cursor, selection, sorting |
| `viewer.py` / `editor.py` | file viewer and editor |
| `dialogs.py` | prompts, menus, confirmations, progress bars |
| `app.py` | curses UI, key bindings, orchestration |

## Development

```bash
pip install ".[dev]"
pytest
```

The test suite covers the filesystem-agnostic core (copy, move, sync, panel
logic) using the local backend and temporary directories.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
