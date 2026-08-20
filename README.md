# Meridian Commander

[![PyPI](https://img.shields.io/pypi/v/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![Python versions](https://img.shields.io/pypi/pyversions/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![CI](https://github.com/MartinGallagher-code/meridian_commander/actions/workflows/publish.yml/badge.svg)](https://github.com/MartinGallagher-code/meridian_commander/actions/workflows/publish.yml)
[![Documentation](https://app.readthedocs.org/projects/meridian-commander/badge/?version=latest)](https://meridian-commander.readthedocs.io/en/latest/)

**Website:** <https://martingallagher-code.github.io/meridian_commander/> — screenshots and a tour.
**Manual:** <https://meridian-commander.readthedocs.io/> — installation, the full key-binding reference, tutorials and the plug-in API.

*The meridian is noon — the other end of the clock from midnight.*

A two-pane terminal file manager with the keys of **Midnight Commander** and
the face of **Turbo Vision**, written in pure Python. It browses local **and
networked** locations, copies and moves files between the two panes regardless
of where each side lives, synchronizes directories so both panes hold the
newest version of every file, and ships with a built-in file viewer and editor.

```
  ≡  File  Command  Options  Help                                    14:06
╔══[■]═╡ local:/home/user ╞═══════╗┌──╡ sftp://me@server:/srv/www ╞───┐
║ Name                Size  Modify║│ Name                Size  Modify │
║ ..                <DIR>       ..▲│ ..                <DIR>       .. ▲
║ projects/         <DIR> Jul 20 ..█│ assets/           <DIR> Jul 19 ..░
║*report.pdf         1.2M Jul 21 ..░│ index.html        4.3K Jul 18 ..░
║ notes.txt           842 Jul 22 ..░│ style.css         1.1K Jul 18 ..▼
╚═╡ 1 tagged, 1.2M ╞══════════════╝└─╡ 12 items ╞──────────────────────┘
░Esc menu   Tab switch pane   F1 Help   F9 Sync   F10 Quit░░░░░░░░░░░░░░
1Help  2Conn  3View  4Edit  5Copy  6Move  7Mkdir 8Del   9Sync  10Quit
```

Yellow on Borland blue, grey chrome, a shaded desktop, double-line frames on
the pane that has the keyboard and single on the one that does not, red
accelerator letters, green buttons and drop shadows under every dialog — the
1991 look, on a terminal that was not built in 1991.

## Features

- **Turbo Vision looks** — a grey menu bar with a clock, a shaded blue desktop,
  each pane a framed window (double-line for the active one, single for the
  other) with its path in the caption and a scrollbar down its edge, dialogs in
  grey with red accelerators, green buttons and drop shadows, and the F-key bar
  along the bottom. Three schemes — `turbo` (Borland blue), `midnight` (black
  ground) and `mono` — switch from **Options ▸ Colours**, and everything falls
  back to plain attributes on a terminal with no colour and to ASCII frames on
  one that cannot encode box-drawing characters. See
  [Look and feel](https://meridian-commander.readthedocs.io/en/latest/look-and-feel.html).
- **A menu bar that reaches everything** — press `Esc` for the menus (or
  `Alt+F`, `Alt+C`, `Alt+O`, `Alt+H`, or click one), arrow between them, and
  pick an entry by its red letter. Every entry is something a key binding also
  does, so the menu is a way to *find* the keys rather than a place features
  hide.
- **Two independent panes** — browse two locations side by side, `Tab` between
  them, and swap them with `Ctrl-U`.
- **Home and mirror shortcuts** — `~` jumps a pane to its own home directory
  (the *remote* account's home on a remote pane), and `=` points the **other
  pane at this pane's directory and connection**, reusing the same live session
  so a remote location is never dialled — or authenticated — twice.
- **The cursor stays put** — after deleting (`F8`) the highlight settles on the
  next entry down, stepping over a whole tagged block rather than springing back
  to the top of the listing. The same holds whenever an entry disappears from
  under the bar: the pane keeps its position instead of losing your place.
- **Presets** (`b`) — save the places you keep coming back to, local or remote,
  and reopen one with a single letter from the list. A preset stores the
  connection *and* the directory (never a password) and reuses a connection
  that is already open.
- **Local and networked locations** — each pane can point at the local disk, an
  **SFTP** server, an **SSH (shell)** host, or an **FTP** server. Press `F2` to
  open a location. The **SSH (shell)** mode lists and transfers files by running
  ordinary commands (`ls`, `cat`, …) over the SSH channel, so it works even on
  servers that permit SSH login but have the SFTP subsystem disabled.
- **Copy & move across any pair of panes** — local→remote, remote→local,
  remote→remote and local→local all work through one streaming engine, with a
  cancellable progress bar (`F5` copy, `F6` move). A copy from an SFTP pane to
  the *same* server writes down a second channel, so the read and the write
  never contend (see [Copying on one connection](https://meridian-commander.readthedocs.io/en/latest/transfers.html)).
- **Bidirectional directory sync** (`F9`) — compares the two panes and copies
  the newest version of each file in whichever direction is needed, so both
  sides end up holding the latest of everything. Nothing is deleted; you get a
  preview and confirmation before anything is written, and a directory big
  enough that syncing it is probably a mistake is queried *before* the scan
  starts rather than after you have waited for it.
- **File viewer** (`F3`) — scrollable, with **search** (`/`, smart case,
  highlighted matches, `n`/`N` next/previous with wrap-around), toggleable
  line numbers (`l`) and horizontal scrolling; works on remote files too.
- **Archives as directories** — press Enter on a `.zip`, `.tar`, `.tar.gz`
  (or `.jar`, `.whl`, `.tgz`, `.tar.bz2`, `.tar.xz`) and the pane goes *into*
  it. Browse, tag and copy files out with `F5` exactly as from a directory;
  Backspace at the top comes back out. Read-only, and stdlib-only.
- **Run files** — press Enter on a shell script, a Python file, anything with
  a `#!` line, or any executable, and it *runs* (also **File ▸ Run…** and the
  right-click menu). A small dialog takes arguments — Esc there cancels, so
  nothing runs by accident — then the program gets the real terminal: output
  streams live, a script that prompts can be answered, and the screen waits
  for Enter before returning so the tail of the output stays readable. A
  non-executable file with a `#!` line is handed to the interpreter that line
  names, so a fresh script runs without `chmod +x`; a file with neither opens
  in the viewer as before. On an SFTP/SSH pane the file runs on the *remote*
  host (over `ssh -t`, like the full-screen shell); FTP panes only view.
- **PDF viewer** (`F3` on a `.pdf`) — the text of each page, laid out as the
  page has it: lines rebuilt from glyph positions, spaces inferred from the
  gaps, columns kept side by side. `Tab` pages through, `/` searches the whole
  document. A **scanned** page has no text — press `i` and its image opens in
  the image viewer. Standard library alone: object streams, cross-reference
  streams, Flate/LZW/ASCII85/RunLength and `/ToUnicode` CMaps all included.
- **Image viewer** (`F3` on a `.png`, `.jpg`, `.gif`, `.bmp`, `.pnm`) — the
  picture itself, in the terminal's **real pixels** on terminals that speak
  **Sixel** or the **kitty** graphics protocol, and half-block characters
  (two pixels per cell) everywhere else. `g` switches between them. Pan, zoom,
  step through GIF frames, or drop to an ASCII
  luminance ramp. `.webp`/`.avif`/`.heic`/`.tiff` are named and measured
  rather than drawn — their pixels need a video codec the standard library
  does not ship.
- **Markdown viewer** (`F3` on a `.md`) — rendered rather than raw: markers
  gone, **bold** and *italic* and `code` shown with real terminal attributes,
  headings ruled, lists and quotes laid out, tables aligned, links numbered
  with a reference list at the end. `r` shows the source instead.
- **Document viewer** (`F3` on a `.docx`/`.docm`) — the Word document as text,
  with headings ruled, bullets and numbered lists indented, and tables laid out
  as aligned columns. Wrapped to the terminal, searchable like any other file,
  and read with the **standard library alone**.
- **Slide browser** (`F3` on a `.pptx`/`.pptm`) — one slide per screen with
  its title, bullets at their outline levels and tables; `Tab` between slides,
  `t` for the speaker notes, `/` to search the whole deck. Standard library
  alone, like the others.
- **Spreadsheet browser** (`F3` on an `.xlsx`/`.xlsm`) — a full-screen grid
  with row numbers, column letters, right-aligned numbers, dates rendered
  through the workbook's own number formats, `Tab` between sheets and the same
  `/` search. It reads the format with the **standard library alone**, so
  spreadsheets open with nothing installed — on remote panes too.
- **File editor** (`F4`) — a real in-place editor (insert/delete, Enter/Backspace
  line handling, save with `Ctrl-S`), also with toggleable line numbers.
- **Tag multiple files** (`Insert`/`Space`, `+` all, `-` none) for batch
  copy/move/delete.
- **Find files** (`f`) — search the pane's tree by substring or glob (remote
  panes included, cancellable) and get a **browsable result list**: view or
  edit a hit right from the list, or press Enter to jump the pane to the
  containing directory with the cursor on the file.
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
  contents. Writing one takes a dozen lines (see [Plug-ins](https://meridian-commander.readthedocs.io/en/latest/plugins.html)); built-ins
  include remote JSON push and run-remote-script over SSH, plus data tools for
  profiling, cleaning and building CSV/TSV datasets.
- **In-app configuration** (`C`) — edit `config.ini` and plug-in files in the
  built-in editor without leaving the app.
- **No required dependencies** for local + FTP use — it runs on the Python
  standard library. SFTP uses the optional [`paramiko`](https://www.paramiko.org/)
  package.

## Install

```bash
pip install meridian-commander

# with SFTP/SSH support (remote panes, in-pane remote terminal, SSH plug-ins)
pip install "meridian-commander[ssh]"

# optional: pandas acceleration for the data plug-ins' group-by on large files
pip install "meridian-commander[data]"

# or from a checkout
pip install ".[ssh]"
```

This installs the `meridian-commander` command and its short alias `meridian`.
**[pipx](https://pipx.pypa.io/) is recommended** — it puts the commands
somewhere on your `PATH` and keeps the app in its own environment:

```bash
pipx install "meridian-commander[ssh]"
```

Python 3.9 or newer, on Linux or macOS (on Windows, under WSL). If `meridian`
comes back "command not found", the manual's
[installation page](https://meridian-commander.readthedocs.io/en/latest/installation.html) diagnoses both causes — it always
works as `python -m meridian_commander` in the meantime.

## Usage

```bash
meridian-commander                 # left pane here, right pane in your home directory
meridian-commander /etc /var/log   # left pane in /etc, right pane in /var/log
```

Press **F1** for help, **F2** to connect a pane to a remote location, **Tab**
to switch panes, **F5**/**F6** to copy/move between them, **F9** to
synchronize, and **F10** to quit. `Esc` opens the menu bar, which reaches
everything a key does.

## Documentation

The manual is at **<https://meridian-commander.readthedocs.io/>**:

| Page | What is in it |
| --- | --- |
| [Installation](https://meridian-commander.readthedocs.io/en/latest/installation.html) | the extras, pipx, and "command not found" |
| [Usage](https://meridian-commander.readthedocs.io/en/latest/usage.html) | remote locations, presets, the archive, document, image and PDF browsers, and the **full key-binding reference** |
| [Look and feel](https://meridian-commander.readthedocs.io/en/latest/look-and-feel.html) | the three colour schemes, and terminals without colour |
| [Configuration](https://meridian-commander.readthedocs.io/en/latest/configuration.html) | `config.ini` and where it lives |
| [Plug-ins](https://meridian-commander.readthedocs.io/en/latest/plugins.html) | what ships, and how to write one |
| [Data tools](https://meridian-commander.readthedocs.io/en/latest/DATA_TOOLS.html) | profiling, cleaning and building datasets |
| [Copying and synchronizing](https://meridian-commander.readthedocs.io/en/latest/transfers.html) | the second channel, and what the sync planner decides |
| [Development](https://meridian-commander.readthedocs.io/en/latest/development.html) | the architecture, and what CI checks |
| [Plug-in API](https://meridian-commander.readthedocs.io/en/latest/api.html) | `PluginContext`, `PanePlugin`, `InputOutputPlugin` |

The [website](https://martingallagher-code.github.io/meridian_commander/) has
screenshots and a tour.

## Contributing

Bug reports, patches and plug-ins are welcome.
[CONTRIBUTING.md](CONTRIBUTING.md) covers the setup, what CI checks and why,
how the test harness is built, and what a pull request should look like.
[CHANGELOG.md](CHANGELOG.md) records what changed in each release, and
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) is short and says what you would
expect.

Found a security problem? Please report it privately —
[SECURITY.md](SECURITY.md) says how, and describes what this program touches.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
