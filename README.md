# Meridian Commander

[![PyPI](https://img.shields.io/pypi/v/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![Python versions](https://img.shields.io/pypi/pyversions/meridian-commander)](https://pypi.org/project/meridian-commander/)
[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)](LICENSE)
[![CI](https://github.com/MartinGallagher-code/meridian_commander/actions/workflows/publish.yml/badge.svg)](https://github.com/MartinGallagher-code/meridian_commander/actions/workflows/publish.yml)

**Website:** <https://martingallagher-code.github.io/meridian_commander/> — screenshots and a tour.

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
- **Home and mirror shortcuts** — `~` jumps a pane to its own home directory
  (the *remote* account's home on a remote pane), and `=` points the **other
  pane at this pane's directory and connection**, reusing the same live session
  so a remote location is never dialled — or authenticated — twice.
- **Presets** (`b`) — save the places you keep coming back to, local or remote,
  and reopen one from a list. A preset stores the connection *and* the
  directory (never a password) and reuses a connection that is already open.
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
- **File viewer** (`F3`) — scrollable, with **search** (`/`, smart case,
  highlighted matches, `n`/`N` next/previous with wrap-around), toggleable
  line numbers (`l`) and horizontal scrolling; works on remote files too.
- **Archives as directories** — press Enter on a `.zip`, `.tar`, `.tar.gz`
  (or `.jar`, `.whl`, `.tgz`, `.tar.bz2`, `.tar.xz`) and the pane goes *into*
  it. Browse, tag and copy files out with `F5` exactly as from a directory;
  Backspace at the top comes back out. Read-only, and stdlib-only.
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
  contents. Writing one takes a dozen lines (see *Plug-ins* below); built-ins
  include remote JSON push and run-remote-script over SSH, plus data tools for
  profiling, cleaning and building CSV/TSV datasets.
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

# optional: pandas acceleration for the data plug-ins' group-by on large files
pip install "meridian-commander[data]"

# or from a checkout
pip install ".[ssh]"
```

This installs the `meridian-commander` command and its short alias
`meridian`.

For development use an **editable** install from the repo root, so your code
changes take effect without reinstalling:

```bash
python -m pip install --upgrade pip   # editable installs need pip >= 21.3
pip install -e ".[ssh]"
```

(Older pip versions answer `-e` with `file 'setup.py' not found`; upgrading
pip inside the virtualenv fixes it.)

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

Meridian reads your **`~/.ssh/config`**, so you can enter a **host alias**
(with its `HostName`, `User`, `Port`, `IdentityFile`, `ProxyJump`/`ProxyCommand`
all applied) or a `user@host` string, and leave username, port, key file and
password **blank** — it authenticates through your **SSH agent** and default
keys (`~/.ssh/id_*`) just like the `ssh` command. In other words, if `ssh mybox`
works in your shell, typing `mybox` here works too.

**`ProxyJump` is native and alias-aware**: given

```
Host A
    HostName a.example.com
    User usera
Host B
    HostName b.internal
    ProxyJump A
```

connecting to `B` first connects to `A` (with *A's* user, port and keys),
opens a tunnel through it, and reaches `B` — entirely inside the app, no
external `ssh` process. Chains (`ProxyJump A,B`) and `user@host:port` hop
specs work; jump hops authenticate via your agent/keys (a hop's password
cannot be prompted mid-connection).

- **SFTP** authenticates with your SSH agent / default keys / a per-host
  `IdentityFile` (or an explicit key file or password if you supply one), and
  browses through the SFTP subsystem.
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

### Presets — saved locations

Press **`b`** for the preset list. It shows every saved location and offers
**Save this location as a preset…** and **Delete a preset…**.

A preset remembers a pane's *connection and directory*, so a place you visit
often is two keystrokes away instead of a trip through the connect dialog.
Choosing one points the active pane at it; combine with `=` to bring the other
pane along.

- Opening a preset **reuses a connection that is already open** in either pane,
  so a saved remote location appears instantly and without authenticating a
  second time.
- **Passwords are never saved.** A remote preset reconnects the way `ssh`
  would — your agent, `~/.ssh/config` and default keys — and you are asked for
  a password only if that fails.
- If a preset's directory has since disappeared, the pane opens that location's
  home directory instead of going nowhere.

Presets live in `~/.config/meridian-commander/presets.ini` (one section each,
`$XDG_CONFIG_HOME` honoured), which is plain text and safe to edit by hand:

```ini
[www]
scheme = sftp
host = web1
username = deploy
port = 22
path = /srv/www
```

### Archives

Press **Enter** on an archive and the pane goes into it:

```
+- zip:project.zip:/src -----------+- local:/home/user/out -----------+
| Name                Size  Modify | Name                Size  Modify |
| ..                               | ..                               |
| main.py               9  Jul 28  |                                  |
| util.py               4  Jul 28  |                                  |
+----------------------------------+----------------------------------+
 zip:project.zip -- read-only; Backspace to leave
```

It is not a viewer but a **read-only filesystem**, so everything that already
works between panes keeps working: list, sort, tag, and `F5` files or whole
subtrees out to the other pane — local or remote. Backspace at the archive's
root steps back out to the directory holding it, with the cursor on the file.

`.zip`, `.jar`, `.whl`, `.egg`, and `.tar` with any of gzip, bzip2 or xz
compression. A bare `.gz` is one compressed file rather than a tree, so it is
left alone.

**Writing is refused, not half-done.** Copying *into* an archive reports that
it is read-only rather than appearing to work.

Two details worth knowing:

- **Directories are implied.** An archive stores paths, not a tree, and often
  holds `a/b/c.txt` with no entry for `a/`. The missing directories are
  reconstructed from the member names.
- **Unsafe members are refused.** A member whose name contains `..` would
  escape the destination when copied out, so it is dropped — and *counted in
  the pane header*, rather than disappearing quietly.

A local archive is opened in place, so browsing a multi-gigabyte tar costs
nothing; one on a remote pane has to be fetched whole first (both formats need
to seek), which is capped and refused with a message above the cap.

### Documents

`F3` on a `.docx` or `.docm` shows the document as text, wrapped to the
terminal:

```
Quarterly Report
================
Revenue grew across every region this year, with the North
leading and the South trailing behind.

Detail
------
  * a bullet
    * a nested one
  1. then a numbered item

  Region  Revenue
  ------  -------
  North   1234.5
```

Headings are ruled, list items indented by depth, and tables laid out as
aligned columns — structure carried by layout, so the ordinary viewer's
scrolling and smart-case search work on it unchanged.

A list's marker comes from the document's `numbering.xml`, because in a real
Word document a bullet and a numbered item are both style *ListParagraph* and
nothing else tells them apart. The numbers themselves are this reader's own
count: Word's restart rules depend on state a linear reader does not keep, so a
list that restarts mid-document is numbered straight through here.

Headers, footers, footnotes, comments and tracked changes are skipped, and
character formatting is dropped — this shows what the document *says*. Legacy
`.doc`, like `.xls`, is a different format entirely and stays in the text
viewer.

### Markdown

`F3` on a `.md` shows it rendered rather than raw:

```
Meridian Commander
==================

[image: PyPI][1] [image: CI][2]

A two-pane terminal file manager, in pure Python with no required
dependencies.  Press F3 to view a file, and see the website[3].

  • Local and remote panes
    • SFTP, SSH shell and FTP
  • ☑ archives browsable as directories

│ Nothing is deleted; you get a preview first.

  Format  Reader
  ──────  ───────
  xlsx    xlsx.py

Links
  [1] https://pypi.org/project/meridian-commander/
```

Three things do the work, in order of how much they contribute: **the markers
go away** (`**bold**` becoming bold text is most of the effect); **terminal
attributes** carry inline emphasis — bold, italic, reverse for code, dim for
struck-out text and underline for link text; and **layout** carries structure.

**Links are numbered, not inlined.** A URL in the middle of a sentence is
unreadable, and there is no way to make text clickable that curses can drive,
so the destinations are listed at the end the way `lynx` and `w3m` do it.
Reference definitions (`[label]: url`) resolve, and a linked badge —
`[![alt](img)](url)`, the commonest thing in a README — reads as one item.

**Press `r` for the source.** A renderer you cannot turn off is one you have
to trust.

Tables and code blocks keep their shape when wrapping is on: reflowing a
table destroys the thing being shown, so those lines are left whole and
horizontal scrolling reaches the rest.

This is a **pragmatic subset, not CommonMark** — that specification is
enormous because its edge cases are, and claiming compliance would be wrong in
ways you would have to discover. Headings (both styles), lists including
nested and task lists, blockquotes, fenced code, tables with alignment,
thematic breaks, front matter, hard breaks, escapes and the inline spans are
covered. Anything unrecognised is shown as the plain text it is, never
swallowed.

### Presentations

`F3` on a `.pptx` or `.pptm` shows one slide per screen:

```
 Slides: deck.pptx -- 2/3
Agenda
======
  * Why panes
    * Remote locations
      * Plug-ins, café — dash

Notes
-----
  Mention the grid browser.

 Agenda  Tab slide  [t]notes  [/]find  n/N  [q]uit
```

Slides are discrete, so this is paged rather than scrolled: moving between
slides is the main gesture and scrolling only matters for a slide with more on
it than fits. Titles are ruled, bullets indented by their outline level,
tables laid out as aligned columns, and long lines wrapped to the terminal.

**Slide order comes from the deck, not the file names.** A presentation stores
its running order in `sldIdLst`; reordering slides in PowerPoint leaves
`slide1.xml` where it always was. Reading by file name would show a reordered
deck in the wrong order.

Placeholder text inherited from a layout or master is not pulled in, so a slide
shows what was typed on *it*, and slide numbers and dates — fields rather than
content — stay out of the notes. Images, charts, animations and geometry are
ignored. Shapes are read in the order the file lists them, which is usually but
not always reading order; there is no layout engine here to do better.

### Spreadsheets

`F3` on a file whose name ends `.xlsx` or `.xlsm` opens a full-screen grid
instead of the text viewer:

```
 Sheet: quarterly.xlsx -- Sales (1/2)
    A      B          C          D          E         F
  1 Region Q1 Revenue Q2 Revenue Opened     Manager   Notes
  2 North      1234.5    1402.75 2023-03-15 Ada       steady growth
  3 South         -98          0 2024-12-31 Grace     under review
  4 East      88123.5      91002 2020-01-01 Alan
  5 West           42        7.5 2019-06-30 Katherine new territory this year

 B3  -98  Tab sheet  [/]find  n/N  [w]idth  [q]uit
```

It is full-screen rather than in-pane on purpose: half of an 80-column terminal
is about 39 columns, which is two or three spreadsheet columns.

**No dependency.** An `.xlsx` is a zip archive of XML, so `zipfile` and
`xml.etree` are the whole of it — spreadsheets open on a machine with nothing
installed, and on a remote pane the file arrives through the same `open_read`
as any other. Values are shown the way the spreadsheet shows them: shared and
inline strings, numbers, booleans, cached formula results, and date serials
rendered through the workbook's number formats (including the 1904 date system
and Excel's phantom 29 February 1900).

**Legacy `.xls` is not supported.** Despite the name it is an unrelated
format — a compound-document binary, not zipped XML — with no standard-library
path, so reading it would mean a third-party dependency. Such files stay in the
text viewer, where they look like the binary they are.

Styling, charts, images and merged-cell geometry are ignored; this is a reader
for looking at data. Very large workbooks are capped (50,000 rows and 1,024
columns per sheet) and the title says `[truncated]` when a cap was hit. The
whole file has to be read before anything can be shown — a zip's index lives at
its end, so a partial read parses as nothing — which is why an oversized
workbook is refused outright rather than shown in part.

### Key bindings

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `Tab` | switch active pane | `F1` | help |
| `↑`/`↓` `j`/`k` | move cursor | `F2` | open / connect location |
| `PgUp`/`PgDn` | page | `F3` | view (`.md`, `.xlsx`, `.docx`, `.pptx`) |
| `Home`/`End` | first / last | `F4` | edit file |
| `Enter` / `→` | enter dir / view file | `F5` | copy to other pane |
| `Backspace` / `←` | parent directory | `F6` | move to other pane |
| `Insert` / `Space` | tag file | `F7` | make directory |
| `+` / `-` | tag all / untag all | `F8` | delete |
| `Ctrl-U` | swap panes | `F9` | synchronize panes |
| `Ctrl-R` | reload panes | `F10` | quit |
| `Ctrl-G` | go to path | `Ctrl-T` | change sort order |
| `~` | home directory (this pane) | `=` | other pane: same location |
| `b` | presets: saved locations | | |
| `.` | show/hide hidden files | `t` | terminal inside this pane |
| `p` / F11 | plug-in mode (this pane) | `!` | full-screen shell |
| `f` | find files (browsable results) | | |
| `C` | configuration menu | `Ctrl-]` | terminal: switch to other pane |

**F-key aliases** (for terminals that swallow function keys): press the digit
`1`–`0` for `F1`–`F10`, or the mnemonic letter — `?`/`1` help, `o` open/connect,
`v` view, `e` edit, `c` copy, `m` move, `d` delete, `s` sync, `q` quit.

**Mouse**: click to select and focus a pane, double-click to open a
file/directory, scroll wheel to move through the listing, and **right-click** for
a context menu of actions.

In the **viewer**: `/` (or F7) searches — smart case (a lowercase pattern is
case-insensitive), matches highlighted, `n`/`N` next/previous with wrap-around;
`l` toggles line numbers, `w` toggles wrapping, arrows/PgUp/PgDn scroll, `Q`
quits. Wrapping breaks at spaces and keeps the file's own line numbering, so a
paragraph occupying six rows is still one numbered line to search and jump to.
In the **Markdown viewer**: everything the text viewer does, plus `r` to
switch between the rendered view and the file as it was written.
In the **slide browser**: `Tab`/`Shift-Tab` (or `←`/`→`, `[`/`]`, Space)
change slide, arrows/PgUp/PgDn scroll a slide that overflows, `t` shows the
speaker notes, `/` searches every slide's title, body and notes with `n`/`N`,
`q` quits.
In the **spreadsheet grid**: arrows/`hjkl` move a cell at a time, `PgUp`/`PgDn`
page, `g`/`G` jump to the first/last row, `Home`/`End` to the first/last column,
`Tab`/`Shift-Tab` (or `]`/`[`) change sheet, `/` searches the sheet with the
same smart case and `n`/`N`, `w` cycles the column-width limit, `q` quits. The
footer shows the cursor's cell reference and its value in full, which is where
to look when a column is too narrow for what is in it.
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

Data plug-ins (for CSV/TSV/JSON-lines files; select the file in the *other*
pane, then open the plug-in). These are pure standard library and read at most a
capped number of bytes, so a huge file cannot hang the interface; they work on
remote panes too. See the [data tools tutorial](docs/DATA_TOOLS.md) for a
step-by-step walkthrough with examples.

- **Profile table** — reports the table's shape and a per-column profile:
  inferred type, null count/percentage, distinct count and, for numeric columns,
  min/max/mean/median. `col <name>` drills into one column (histogram or value
  counts); `head`/`tail [n]` preview rows.
- **Clean table** — cleaning verbs written to a *new* sibling file (the source
  is never touched): `trim`, `dedupe`, `dropnull`, `fillnull`, `drop`, `keep`,
  `rename`, `filter <col> <op> <value>`, `retype <col> int|float`,
  `normalize-headers`. Prefix any command with `preview` to test it without
  writing.
- **Build dataset** — compose files tagged in the other pane: `concat` (union of
  columns), `join <key>` (first two tagged files), `sample <n|n%>`,
  `split <col>` (one file per value), `to-jsonl` / `from-jsonl`, and
  `groupby <col> <agg>[:col]` (count/sum/mean/min/max). `groupby` uses pandas
  when the optional `meridian-commander[data]` extra is installed and otherwise
  falls back to a pure-stdlib implementation with identical output.

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

Saved locations are kept separately, in
`~/.config/meridian-commander/presets.ini` — see
[Presets](#presets--saved-locations). They are written by the app (`b`) rather
than by hand, which is why they are not part of `config.ini`.

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
| `presets.py` | saved locations: `presets.ini` load/save, live-connection reuse |
| `panel.py` | one pane's listing, cursor, selection, sorting |
| `archive.py` | zip/tar archives as a read-only `FileSystem` |
| `viewer.py` / `editor.py` | file viewer (scroll, search, wrap) and editor |
| `browsers.py` | picks the browser for a file: grid, document or text |
| `ooxml.py` | the Office Open XML package layer, shared by the readers |
| `xlsx.py` / `sheetview.py` | stdlib `.xlsx` reader and the full-screen grid |
| `docx.py` | stdlib `.docx` reader and the document viewer |
| `pptx.py` | stdlib `.pptx` reader and the slide browser |
| `markdown.py` | Markdown rendered to styled lines, and its viewer |
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

# with a coverage report
coverage run -m pytest && coverage report
```

**Coverage is 100%, and CI fails if it drops.** Every statement in the package
is executed by the suite; `fail_under = 100` in `pyproject.toml` enforces it.
There are no `exclude_lines` rules and exactly one `# pragma: no cover` in the
tree — the forked child of `pty.fork()`, which `exec`s into the shell before
any in-process tracer could record it.

On Python 3.9 the same suite measures 99.9%. Precise line events arrived with
PEP 626 in 3.10; before that the tracer does not record a `break` following an
if-block that ends in its own jump, so two such statements read as unexecuted
even though they run. The gate is enforced on 3.10+ and the report is printed
on 3.9.

How the awkward parts are reached:

- **Filesystem logic** (copy, move, sync, panel behaviour) runs against the
  local backend in temporary directories.
- **Drawing and key loops** — panes, dialogs, the viewer, the editor, the
  find browser, the in-pane terminal — run against a *real* curses screen on a
  pseudo-terminal, with only the keystrokes scripted. Overflow errors are only
  reported by a real window, so a stand-in would not catch a regression.
- **Remote backends** (SFTP, SSH-shell, FTP) run against paramiko- and
  ftplib-shaped stand-ins, so the full fallback chains — `cat`/`dd`/scp,
  `MLSD`/`LIST`, ProxyJump tunnels — are exercised without a network.
- **The local terminal** really forks a shell on a real pty.
- **Office files** are built as real zip archives with the part layout a
  producer writes, so the readers are tested against the format rather than a
  stand-in — including damaged parts, oversized parts and the caps.

`tests/support.py` holds the shared harness and `tests/conftest.py` the
fixtures; the guiding rule is that only the terminal and the blocking dialogs
are ever replaced, so assertions are about what the application did rather
than which mock it called.

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE).
