# Development

How the program is put together, and what a change to it has to pass.
[CONTRIBUTING.md](https://github.com/MartinGallagher-code/meridian_commander/blob/main/CONTRIBUTING.md)
in the repository covers the same ground from the contributor's side.

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
| `browsers.py` | picks the browser for a file: grid, document, image or text |
| `ooxml.py` | the Office Open XML package layer, shared by the readers |
| `xlsx.py` / `sheetview.py` | stdlib `.xlsx` reader and the full-screen grid |
| `docx.py` | stdlib `.docx` reader and the document viewer |
| `pptx.py` | stdlib `.pptx` reader and the slide browser |
| `markdown.py` | Markdown rendered to styled lines, and its viewer |
| `image.py` / `jpeg.py` | stdlib PNG/GIF/BMP/Netpbm decoders, and JPEG |
| `pdfobj.py` | PDF lexer, object graph, cross-references and filters |
| `pdf.py` | PDF text extraction, page images, and the PDF browser |
| `imageview.py` | colour quantising, half-block drawing, the image browser |
| `termimage.py` | Sixel and kitty graphics: detection, encoding, placement |
| `theme.py` | the EGA palette, the role table, and the chrome: frames, shadows, scrollbars, buttons |
| `dialogs.py` | prompts, menus, drop-downs, confirmations, progress bars |
| `app.py` | curses UI, the menu bar, key bindings, orchestration |

## Working on it

```bash
pip install ".[dev]"
pytest

# with a coverage report
coverage run -m pytest && coverage report

# what CI checks before either of those
ruff check .
mypy meridian_commander
```

**Lint and types are gated too.** `ruff` runs the pycodestyle and pyflakes
rules plus bugbear, chosen for the things that are *wrong* rather than merely
unfashionable — import sorting and the modernising rewrites are deliberately
left off. `mypy` starts as a ratchet: everything it can already prove is
enforced, and the categories still to be worked off are listed, with counts,
in `[tool.mypy]` in `pyproject.toml`. Deleting a line from that list as its
last case goes is the intended way to tighten it.

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

## Utility scripts

The bundle utilities — `merge.sh`, which packs a directory tree into a single
text file, and `split.sh`, which expands it again — live in the
[shared_tools](https://github.com/MartinGallagher-code/shared_tools)
repository rather than here. This repository used to carry its own copy; the
two drifted apart, each growing a feature the other lacked, so the copy was
deleted and the features merged upstream.

```bash
shared_tools/scripts/merge.sh bundle.txt some/dir   # bundle a tree
shared_tools/scripts/split.sh bundle.txt restored/  # expand it again
```

To bundle this repository, exclude `docs/` — the screenshots in `docs/assets`
are the bulk of the bytes and base64 badly:

```bash
git archive HEAD | tar -x -C /tmp/tree && rm -rf /tmp/tree/docs
shared_tools/scripts/merge.sh bundle.txt /tmp/tree
```

Some places that accept a text file will not accept a large one, so
`-m`/`--max-size` writes a set of parts instead; expand them all into one
directory, in any order.
