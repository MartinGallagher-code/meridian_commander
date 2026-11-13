# Meridian Commander

[![PyPI](https://img.shields.io/pypi/v/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![Python versions](https://img.shields.io/pypi/pyversions/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](https://www.gnu.org/licenses/gpl-3.0.html)
[![REUSE status](https://api.reuse.software/badge/github.com/MartinGallagher-code/meridian_commander)](https://api.reuse.software/info/github.com/MartinGallagher-code/meridian_commander)
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
- **Or one, at full width** — `z` (or **Options ▸ Single pane**) gives the
  active pane the whole desktop and keeps the other in reserve, exactly as it
  was: `Tab` flips to it, copy, move, extract and sync name it before using
  it, and `h`, `Ctrl-]` `Tab` from a terminal and plug-ins that work on the
  other pane bring both back while they run. In a terminal, where `z` goes to
  the shell, it is `Ctrl-]` `z`. `[ui] panes = 1` starts that way.
- **Home and mirror shortcuts** — `~` jumps a pane to its own home directory
  (the *remote* account's home on a remote pane), and `=` points the **other
  pane at this pane's directory and connection**, reusing the same live session
  so a remote location is never dialled — or authenticated — twice.
- **Back and forward** — each pane remembers where it has been, and `<` / `>`
  step back and forth through it like a browser: make the panes equal with
  `=` to do something, then `<` in that pane puts it back where it was. A step
  is a place on any backend — a remote connection, the inside of an archive —
  and lands with the cursor on the entry it was on.
- **Search a pane by name** — every letter in a pane is a hotkey, so `/`
  turns on a search mode in which typing is a name instead: the cursor jumps
  to the first entry whose name contains what you have typed (a lowercase
  pattern ignores case, as the viewers' `/` does), `↓`/`↑` or
  `Ctrl-N`/`Ctrl-P` step through the rest, wrapping round, and the pane's
  footer shows the pattern and `2 of 5`, or `not found`. `Insert` tags the
  match and goes on to the next; `Enter`, `Tab` and the F-keys end the search
  and act on what it found; `/` or `Esc` ends it where it is. Also on
  **Command ▸ Search by name**.
- **Filter a pane** — `F` asks for a pattern and the pane then shows only the
  names that match it: text matches anywhere in the name with the same smart
  case, and `*`, `?` or `[` make it a glob (`*.tsv`, `run_?`). The caption
  names the filter and how many entries it hides; a tag on a hidden entry is
  kept, counted, and named by `F5`, `F6` and `F8` ("2 not shown"). An empty
  pattern, or leaving the directory, clears it. Also on **Command ▸ Filter…**.
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
  cancellable progress bar (`F5` copy, `F6` move). A file already at the far
  end is never replaced without asking (overwrite, skip, overwrite if older,
  for one or all), copies keep their permissions, and symbolic links arrive as
  links (see [When the target is already there](https://meridian-commander.readthedocs.io/en/latest/transfers.html)).
  A copy from an SFTP pane to
  the *same* server writes down a second channel, so the read and the write
  never contend (see [Copying on one connection](https://meridian-commander.readthedocs.io/en/latest/transfers.html)).
- **Copy and move in the background** — switch **Background** on in the
  `F5`/`F6` prompt (`Ctrl-B`), say once what to do with a target that is
  already there, and the panes stay usable while it runs. The status line
  shows the jobs, bytes done and speed; `J` lists them, to pause, resume,
  cancel, or read a job's errors. Jobs between the same two places run one at
  a time, and a remote one uses a connection of its own. Quitting with jobs
  left asks whether to wait or cancel them.
- **Bidirectional directory sync** (`F9`) — compares the two panes and copies
  the newest version of each file in whichever direction is needed, so both
  sides end up holding the latest of everything. Nothing is deleted; you get a
  preview and confirmation before anything is written, and a directory big
  enough that syncing it is probably a mistake is queried *before* the scan
  starts rather than after you have waited for it.
- **File viewer** (`F3`) — scrollable, with **search** (`/`, smart case,
  highlighted matches, `n`/`N` next/previous with wrap-around), toggleable
  line numbers (`l`) and horizontal scrolling; works on remote files too.
  **Code and configuration files are coloured by their syntax** — Python,
  shell (sh, bash, zsh, ksh), YAML, JSON, TOML and INI/`.conf`/`.cfg`, known
  by their suffix, their name (`.bashrc`, `Pipfile`) or their `#!` line:
  keywords, strings, comments, numbers, keys and the like each in a colour
  of the scheme's own. `c` turns the colour off and back on; a file over
  2 MiB opens plain, and the title says why. Stdlib-only.
- **Archives as directories** — press Enter on a `.zip`, `.tar`, `.tar.gz`
  (or `.jar`, `.whl`, `.tgz`, `.tar.bz2`, `.tar.xz`) and the pane goes *into*
  it. Browse, tag and copy files out with `F5` exactly as from a directory,
  copy and move files *in* from the other pane, and delete or rename inside
  it; Backspace at the top comes back out. `x` extracts a whole archive into
  the other pane in one go. Stdlib-only.
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
- **Table browser** (`F3` on a `.tsv`, `.csv`, `.tab` or `.psv`) — the file's
  own header pinned over columns that really line up (measured in terminal
  cells, so CJK and emoji too), numbers right-aligned on their decimal points,
  and neighbouring columns in different colours (`c` turns that off). `f`
  filters — a word anywhere, or `column op value` with `= != < > <= >= ~ !~`,
  numbers compared as numbers — and filters stack; `=`/`!` on a cell keep or
  hide the rows holding that value. `s` sorts by the cursor's column, `Enter`
  shows a row as a record, one field per line. For a wide file, `F` freezes
  the columns up to the cursor's at the left while the rest pan past, `-`/`+`
  hide a column and bring them all back, and `i` summarises a column over the
  rows shown — its type, empty and distinct counts, minimum, maximum, mean,
  median and a histogram for numbers, and its most frequent values, which
  `Enter` turns into a filter. TSV is split on tabs with no
  quoting; CSV goes through the `csv` module. `#` comment lines at the top
  (the run notes the suite's tools write) are set aside rather than read as
  the header, and `#` shows them. Rows with the wrong number of fields are
  marked rather than padded, and the read stops at 50,000 rows or 16 MB with
  `[truncated]` in the title.
- **Floor plan browser** (`F3` on a `.dc` layout) — a
  [datacenter_visualization](https://pypi.org/project/datacenter-layout-viewer/)
  layout drawn as the floor it describes, not as its text: eight lines of
  `.dc` can be five hundred servers. The whole site first, a line of cells
  per row and a cell per rack, filled as far as the rack is full and
  coloured by what it mostly holds; `Enter` zooms in to a row, its racks
  standing side by side in half-blocks, then to one rack, U by U with every
  device in its slot and named. The arrows walk the floor rack by rack and
  row by row, `Backspace` zooms back out, and an inspector shows the
  element under the cursor — path, slot, what it holds, attributes and tags
  (inherited ones marked `^`). `c` colours by role, tag, model or not at all,
  with a legend; without 256 colours the floor is drawn in letters. Meridian
  reads the format itself — ranges, placeholders, `{seq}`, `if=`, `model`
  lines, inheritance and rack slots, held to dcviz's own answers on its
  examples — so no Node or dcviz is needed, and anything it cannot read is
  named in the footer rather than drawn wrong. A `.dc` is claimed only when
  it opens like a layout (`dc`, `title` or `model` first); a `dc` calculator
  script opens as text, as before.
- **Your own pager** (**Options ▸ Viewer**) — point `F3` at `less`, `$PAGER` or
  any command you name, for plain text; a spreadsheet, table, document, deck,
  PDF, image or `.dc` layout keeps the browser built for it rather than being
  shown to a pager as bytes. Remote files are fetched to a private copy and
  read there.
- **File editor** (`F4`) — a real in-place editor (insert/delete, Enter/Backspace
  line handling, save with `Ctrl-S`), also with toggleable line numbers. Prefer
  your own editor? **Options > Editor** points `F4` at `vi`, `vim`, `$EDITOR` or
  any command you name — including on a remote pane, which is fetched to a
  private copy and written back if you changed it.
- **Side-by-side compare** (`D`) — the file each pane is offering, in two
  columns aligned line for line and scrolled **as one**: changed lines marked
  `!`, lines only on one side `+`/`-`, and a shaded gap facing them on the
  other. `n`/`N` jump between blocks of differences. Built in rather than a
  call out to `vimdiff`, so a local file compares against an SFTP, SSH, FTP or
  in-archive one with nothing fetched to disk first.
- **Quick view** (`Ctrl-Q`, Command ▸ Quick view) — the other pane shows the
  file under the cursor in the viewer `F3` would open, and follows the cursor:
  text, Markdown, documents, decks, sheets, tables, PDFs, images (in
  half-blocks) and `.dc` floor plans, a directory as a summary. A remote file
  is fetched once the cursor settles, and a large one waits to be asked. `Tab`
  moves into it to scroll, `Enter` opens the full viewer, and `Ctrl-Q` gives
  the pane back as it was. Single mode shows both panes while it is on.
- **Head and tail in a pane** (`h`) — the pane becomes the first and last lines
  of whatever the **other** pane's cursor is on, with a rule between them
  counting what was skipped, and it **follows that cursor** as you move it: ten
  files is ten arrow keys, with the listing keeping its place. Sized to the
  pane so both ends show at once; `+`/`-` pin a larger or smaller count, `r`
  re-reads, `Esc` gives the pane back. The file is streamed once and only the
  two ends are kept, so a multi-gigabyte log costs what a short one does.
- **New file** (`n`) and **rename** (`r`) — `touch` and a rename without
  leaving for a shell; an existing file is restamped, never emptied.
- **Delete to the trash** — `F8` on a local pane offers **Move to trash** first
  (the FreeDesktop.org trash on Linux, `~/.Trash` on macOS), so a mistaken
  delete can be undone; **Delete permanently** is the second choice, and
  remote panes and archives say plainly that it cannot be undone.
  **Options ▸ Trash on delete** turns it off.
- **Look in the trash** (`T`, Command ▸ Trash folder) — the pane goes to the
  trash `F8` uses from where it is, without your having to know where the
  platform keeps it; `<` comes back. View, compare or copy things out as
  anywhere else; `F8` there deletes for good, with the trash's record of it.
- **Open with the system's application** (`w`, File ▸ Open with system app) —
  hands the file under the cursor to the desktop: `open` on macOS, `xdg-open`
  on Linux, `wslview` under WSL. A PDF goes to Preview, an `.xlsx` to Excel. A
  file on a server or inside an archive is copied to a private temporary
  directory first, removed when Meridian exits; edits to that copy are not
  sent back. On a local directory it opens the system's file browser.
- **Symbolic links** (`l`, File ▸ Link…) — a link in the other pane to the
  cursor entry or each tagged one, relative by default; on a link, `l` changes
  where it points. Both panes must be on one machine.
- **Permissions, owner and group** (`a`, File ▸ Attributes…) — nine `rwx`
  boxes and the octal number, kept in step; tagged entries that disagree show
  their mixed bits as `[~]`, which stay each entry's own unless changed;
  owner and group where the pane can change them; recursive into directories
  on request, cancellable, with refusals named at the end. **Options ▸ Long
  listing** adds mode and owner columns, and is remembered.
- **Tag multiple files** for batch copy/move/delete: `Insert`/`Space` one at a
  time; `+` the names matching a pattern (pre-filled with `*`, so `+` Enter
  tags every file), `\` untags by pattern, `-` untags everything and `*`
  inverts — Midnight Commander's keys. Patterns are globs or plain text with
  the search's smart case, and a **Files only** switch (`Ctrl-F`, on by
  default) keeps `*.tsv` from taking a directory called `old.tsv`.
- **Find files** (`f`) — search the pane's tree by substring or glob (remote
  panes included, cancellable) and get a **browsable result list**: view or
  edit a hit right from the list, or press Enter to jump the pane to the
  containing directory with the cursor on the file.
- **Find the space** (`u`) — every subdirectory shows what is under it instead
  of `<DIR>`, with a bar scaled to the biggest thing in the directory, so you
  follow the longest bar down the tree with `Enter` and end up standing in
  whatever is filling the disk. No total, no separate report: the answer is a
  path. The walk runs between keystrokes, so the pane stays usable while it
  counts — remote panes included.
- **Per-pane hidden-file toggle** (`.`) — show or hide dotfiles independently in
  each pane.
- **Terminal inside the pane** (`t`) — the pane itself becomes a terminal
  running a shell in the pane's directory, while the other pane keeps working
  normally. Works for **local panes** (a real pty) and for **SFTP/SSH panes**
  (an interactive shell on the pane's existing SSH connection). It is a real
  terminal emulator: colours, `ls --color`, `git diff`, and full-screen
  programs — **vim, less, man, top, htop** — run in the pane and leave it as it
  was when they exit. Every key goes to the program, F-keys, PgUp/PgDn and
  Ctrl-/Alt-arrows included; **`Ctrl-]`** opens the terminal's menu: `Tab`
  switches to the other pane while the shell keeps running (Tab back to
  return), `z` zooms the terminal to the full width and back (single pane, as
  `z` is in a file pane; vim, less and top redraw to fit), `q` closes it,
  `PgUp` scrolls back through what went off the top (so does the mouse
  wheel). Exiting the shell closes it too. `!` still hands a program the
  whole real terminal instead.
- **Mouse support** — click to select, double-click to open, wheel to scroll,
  and **right-click for a context menu** of actions (view, edit, copy, move,
  rename, delete, tag, mkdir, new file, compare, head+tail, terminal).
- **Works even when F-keys are hijacked** — every function key has a digit alias
  (`1`–`0` → `F1`–`F10`) and the common actions have mnemonic letters.
- **Pane plug-ins** (`p`) — put a pane into plug-in mode: pick from discovered
  plug-ins and it takes over the pane, with access to the opposite pane's
  contents. Writing one takes a dozen lines (see [Plug-ins](https://meridian-commander.readthedocs.io/en/latest/plugins.html)); built-ins
  include remote JSON push and run-remote-script over SSH, plus data tools for
  profiling, cleaning and building CSV/TSV datasets.
- **In-app configuration** (`C`) — edit `config.ini` and plug-in files in the
  built-in editor without leaving the app.
- **A keystroke away from your shell** — `eval "$(meridian --shell-init bash)"`
  binds `Ctrl-O`: browse, quit, and your shell is left in the directory you
  navigated to. bash, zsh and fish.
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
| [Usage](https://meridian-commander.readthedocs.io/en/latest/usage.html) | remote locations, presets, the archive, document, image, PDF and floor plan browsers, and the **full key-binding reference** |
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

Copyright (C) 2026 Martin J. Gallagher

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

Full text: the `LICENSE` file at the repository root, or
<https://www.gnu.org/licenses/gpl-3.0.html> — SPDX identifier
`GPL-3.0-or-later`. This repository follows the
[REUSE Specification](https://reuse.software/): source files carry SPDX
copyright and licence headers, and the licence text lives in `LICENSES/`.
