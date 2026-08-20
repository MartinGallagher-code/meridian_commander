# Usage

```bash
meridian-commander                 # left pane here, right pane in your home directory
meridian-commander /etc /var/log   # left pane in /etc, right pane in /var/log
meridian-commander /etc            # left pane in /etc, right pane in your home directory
```

With no arguments the **left pane opens in the current directory** — you `cd`
somewhere, type `meridian`, and the directory you were already thinking about is
the one under the cursor. The right pane starts at your home directory, so the
pair is "here" and "somewhere to put things" rather than the same directory
twice. Press **`~`** in either pane to send it home, and **`Ctrl-G`** to go to a
path you type.

## Connecting to a remote location

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

**Host keys** are checked against your `~/.ssh/known_hosts` (and the system
file). A server you have never connected to is trusted on first use and its key
is pinned to `known_hosts`; a server whose key has **changed** since — the thing
host-key checking exists to catch — is refused with a clear message telling you
how to clear the old key once you understand why it changed (`ssh-keygen -R
<host>`). If `~/.ssh` is read-only, connecting still works — the key is trusted
for the session but simply not saved. Keys that fail with the OpenSSL 3.0
*digital envelope routines::unsupported* error are diagnosed and fixed by the
**SSH doctor** plug-in (below).

## Presets — saved locations

Press **`b`** for the preset list. It shows every saved location and offers
**Save this location as a preset…** and **Delete a preset…**.

A preset remembers a pane's *connection and directory*, so a place you visit
often is two keystrokes away instead of a trip through the connect dialog.
Choosing one points the active pane at it; combine with `=` to bring the other
pane along.

- **Every preset has a letter**, shown down the left of the list. Pressing it
  opens that preset there and then — no arrowing down, no Enter — so a saved
  location really is `b` and one keystroke:

  ```
  ┌───────────────── Presets ──────────────────┐
  │ p  photos    --  local:/srv/photos         │
  │ w  work      --  sftp://deploy@web1:/srv   │
  │ a  www-logs  --  sftp://deploy@web1:/log   │
  │ s  Save this location as a preset...       │
  │ d  Delete a preset...                      │
  │ c  Cancel                                  │
  └────────────────────────────────────────────┘
  ```

  The letter is the preset's own initial wherever that is free — `w` for
  `work` — and the next free letter otherwise, which is why `www-logs` above
  answers to `a`. `j` and `k` are never handed out, because they still move the
  highlight, and `s`/`d`/`c` are reserved so **Save**, **Delete** and **Cancel**
  keep their letters whatever your presets are called. Arrow keys and Enter work
  as before, and a list longer than the alphabet leaves the last few without a
  letter rather than giving one to two of them.
- Opening a preset **reuses a connection that is already open** in either pane,
  so a saved remote location appears instantly and without authenticating a
  second time.
- **Passwords are never saved.** A remote preset reconnects the way `ssh`
  would — your agent, `~/.ssh/config` and default keys — and you are asked for
  a password only if that fails.
- If a preset's directory has since disappeared, the pane opens that location's
  home directory instead of going nowhere.
- The list is **alphabetical**, in the menu and in the file, so a preset stays
  where you last saw it. Saving over an existing one leaves it in place rather
  than moving it to the bottom, and a hand-edited file is re-sorted the next
  time the app writes it.

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

## Archives

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

## Documents

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

## PDF

`F3` on a `.pdf` shows one page at a time. There are no lines in a PDF — a page
is a program that paints glyphs at coordinates — so the lines are rebuilt from
where the glyphs landed:

- runs are grouped into lines by **baseline**, then ordered by x;
- **spaces are inferred from the gaps**, because plenty of producers emit one
  draw call per word (or per kerning pair) with no space characters anywhere
  in the file;
- a gap wide enough to be a column break becomes several spaces, so **columns
  stay side by side** rather than interleaving into nonsense.

Encoding is where extraction usually goes wrong. A string in a content stream
holds character codes in the *font's own* encoding, and a subset font commonly
numbers its glyphs from 1 in the order they appear. The font's `/ToUnicode`
CMap is the only way back; without it a subset font yields confident mojibake.
That CMap, `/Differences` arrays and WinAnsi are all followed. A composite font
with no CMap at all emits **nothing** rather than a guess.

The object layer handles what modern files actually contain: cross-reference
streams and `/ObjStm` object streams (without which a PDF made this century
looks almost empty), incremental updates, linearised files, and Flate, LZW,
ASCII85, ASCIIHex and RunLength with PNG and TIFF predictors. A file whose
cross-reference table is wrong — hand-edited, truncated — is recovered by
scanning for the objects. Encrypted files are reported as such rather than
producing rubbish.

A **scanned page** has no text at all, only one large image. Those pages say so
and offer `i`, which opens the picture in the image viewer; `DCTDecode` images
are JPEGs and go straight to the JPEG decoder.

## Images

`F3` on an image draws it — with the terminal's **real pixels** where that is
possible, and half-block characters everywhere else.

### Real pixels: Sixel and kitty

A terminal that speaks a graphics protocol is handed the actual bitmap, at the
terminal's own resolution and in its own colours. Two protocols cover the
field, and Meridian picks whichever the terminal has:

| Protocol | Terminals | What it costs |
| --- | --- | --- |
| **kitty** | kitty, Ghostty, Konsole, WezTerm | nothing — raw RGB goes over as-is |
| **Sixel** | xterm, foot, mlterm, WezTerm, mintty, contour, iTerm2, Windows Terminal | a 256-colour palette |

kitty is preferred where both are available, because it takes RGB directly and
so neither quantises nor pays for a palette. Both are written from the same
decoded buffer the half-block renderer uses, in the standard library alone —
`base64` is the only import either needs, and there is no `img2sixel` or
Pillow anywhere near it.

`g` switches between the real pixels and the half-blocks at any time, so the
two are always comparable on the same picture. The footer names whichever is
drawing.

**Detection is from the environment**, not by asking the terminal: a query
means writing an escape and reading the reply, which races with curses over
the same input and hangs outright on a terminal that answers neither way.
`MERIDIAN_GRAPHICS` overrides it — `off` forces half-blocks, `sixel` or
`kitty` forces a protocol:

```bash
MERIDIAN_GRAPHICS=off meridian        # never use graphics
MERIDIAN_GRAPHICS=sixel meridian      # a terminal we guessed wrong about
```

Sizing needs the terminal's cell size in pixels, from `TIOCGWINSZ`. Terminals
that do not report it get an assumed 8×16 cell and the footer says so, since a
picture that looks stretched should explain itself rather than read as a
decoding bug.

The catch worth knowing: **curses cannot see any of this.** The escape
sequences go straight to the terminal after curses has flushed, so the picture
is wiped whenever curses repaints those cells — which is why it is redrawn on
every pass rather than kept alive, and why redrawing is also how the previous
frame gets cleaned up.

### Half-blocks: the universal fallback

A terminal cell is about twice as tall as it is
wide, so one cell means one squashed pixel; writing `▀` (upper half block)
instead gives **two** pixels per cell — the foreground paints the top half and
the background the bottom. An 80×24 terminal becomes 80×48 square pixels.

Colours are quantised to the xterm-256 palette (the 6×6×6 cube plus its 24
greys — the greys matter, or midtones visibly step), and curses colour pairs
are allocated lazily as the picture needs them. On a terminal without 256
colours it falls back to a ten-step luminance ramp, which reads line art,
diagrams and screenshots perfectly well.

| Format | How |
| --- | --- |
| PNG | full decode: every colour type and bit depth, Adam7 interlace, alpha |
| GIF | full decode, including animation — `n`/`p` step the frames |
| BMP, PNM | full decode |
| JPEG | **DC coefficients only**, giving the image at 1/8 scale |
| WebP, AVIF, HEIC, TIFF, ICO | named and measured, not drawn |

The JPEG approach is worth explaining. A full decoder in pure Python runs an
inverse DCT per 8×8 block per component — three million of them for a 12
megapixel photo — to produce an image the terminal then throws 99% of away.
Each block's DC coefficient *is* that block's mean, so reading DC alone gives
the picture at exactly 1/8 scale with no IDCT at all, which is still more
pixels than a terminal can show. Baseline, extended sequential and progressive
files all work, as do restart markers, chroma subsampling, CMYK, and EXIF
orientation (phone photos are rarely stored upright).

Transparency is composited against a checkerboard, the way image editors show
it. Keys: arrows or `hjkl` pan, `+`/`-` zoom (up to 8×), `f` fits, `n`/`p`
step animation frames, `c` toggles colour and ASCII, `g` switches between real
pixels and half-blocks, `q` quits.

## Markdown

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

## Presentations

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

## Spreadsheets

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

## Key bindings

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
| `Esc` | the menu bar | `Alt+F`/`C`/`O`/`H` | a menu by name |
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
file/directory, scroll wheel to move through the listing, **click the menu bar**
to open a menu, and **right-click** for a context menu of actions.

In the **menus and dialogs**: `Tab`/arrows move, the red letter of a caption
chooses it outright, `Enter` accepts and `Esc` closes. In a drop-down, `←`/`→`
walk to the neighbouring menu without closing the bar.

In the **viewer**: `/` (or F7) searches — smart case (a lowercase pattern is
case-insensitive), matches highlighted, `n`/`N` next/previous with wrap-around;
`l` toggles line numbers, `w` toggles wrapping, arrows/PgUp/PgDn scroll, `Q`
quits. Wrapping breaks at spaces and keeps the file's own line numbering, so a
paragraph occupying six rows is still one numbered line to search and jump to.
In the **Markdown viewer**: everything the text viewer does, plus `r` to
switch between the rendered view and the file as it was written.
In the **PDF viewer**: `Tab`/`Shift-Tab` (or `[`/`]`, `←`/`→`) page through,
arrows/PgUp/PgDn scroll a long page, `/` searches the whole document with
`n`/`N`, `i` opens a scanned page's image, `q` quits.
In the **image viewer**: arrows or `hjkl` pan, `+`/`-` zoom and `f` fits,
`n`/`p` (or Space) step animation frames, `c` switches between colour
half-blocks and the ASCII ramp, `g` between the terminal's real pixels
(Sixel/kitty) and half-blocks, `q` quits.
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

## Running inside VS Code's integrated terminal

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
