# Changelog

Notable changes to Meridian Commander, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version numbers follow [Semantic Versioning](https://semver.org/).

Releases before this file existed are reconstructed from the commit history,
so they group what shipped rather than reproducing every commit. Dates are the
day the version was cut.

## [Unreleased]

### Fixed

- **A GIF frame inherited the previous frame's transparency, delay and
  disposal.** The spec is explicit that a Graphic Control Extension applies to
  the one image that follows it, and the decoder carried its fields forward
  instead: a frame written without an extension of its own drew nothing where
  the previous frame's transparent index fell, ran at the previous frame's
  speed, and had the canvas cleared under it when the previous frame asked for
  that. Found by decoding 500 randomised animations against a reference
  painter; all 500 agree now.
- **Cancelling a sync during a file gave an error dialog with nothing in it.**
  `copy_file` polls the same cancel callback every chunk and raises when it
  trips, and `F9` treated that as a failure: `Sync error`, with the empty text
  of an `OperationCancelled`. Pressing Esc during the one file big enough to be
  worth cancelling was exactly when it happened. A cancel is a stop rather than
  a failure now, and a run that was stopped says how far it got — `Sync
  cancelled -- 1 of 3 file(s) copied` — instead of reporting itself finished.

- **Leaving an archive both panes were in broke it for the other one.** `=`
  points both panes at one filesystem object, and stepping out of the archive
  in one pane closed it — leaving the other pane listing an archive whose every
  file then failed to open with `'NoneType' object has no attribute
  'extractfile'`. The archive is closed by whichever pane leaves it last now,
  and a closed one says so plainly if anything still asks it for a file.
- **A write that failed as it was sent was reported as a success.** The
  plug-in write helper swallowed errors from `close()` — which is exactly
  where a remote write fails, since that is when the bytes are flushed — so
  "Normalise text" said `changed a.txt` for a file it had emptied, and
  `write`, "Make archive" and the CSV tools all reported files that never
  arrived. Failures on the way out are heard now; the read side, where a
  close error costs nothing, is unchanged.
- **`join` shifted every column right of a gap.** A row whose trailing empty
  fields were left out of the file — which is ordinary CSV — is shorter than
  the header, and the joined columns were appended straight onto it: an
  `amount` landed under `city`, in a written file, reported as a clean
  `2/2 rows matched`. Short rows are padded to the header first.

- **The other pane kept showing files that had gone, and missed ones that had
  arrived.** Only the pane doing the work was reloaded, so with both panes on
  one directory — which is what `=` makes, and what a preset reusing a
  connection makes — a deleted file stayed listed in the other pane (`Enter` on
  it then failed, and `F5` offered to copy something that was no longer there)
  and a file made with `n` or `F7` never appeared there at all. Delete, touch,
  mkdir and rename now reload the other pane when the change reached it: the
  same directory, or a directory inside something that was just deleted. A pane
  somewhere else, or on another backend, is left alone, so a remote pane costs
  no round trip for a change on this machine.
- **A cancelled delete reported everything as deleted.** Stopping after two of
  ten still said "Deleted 10 item(s)" — the count came from what was asked for
  rather than from what went. It now counts what was actually removed and says
  the run was cancelled.
- **`verify` gave a clean bill of health for a sums file it had only partly
  read.** The read is capped, and the cap was applied silently: a `SHA256SUMS`
  past it reported "2 OK" for six files, with nothing to say the other four
  were never looked at — in the one tool whose whole job is to be sure. It now
  drops the cut final line (whose name is not any file's, and would have been
  reported as missing) and says in the summary that the rest was not checked.

- **Copying a file onto itself emptied it.** The destination is opened for
  writing, which truncates it, and the read that follows then finds nothing to
  copy. There was a guard, but it asked `same_fs` — which compares *identity*,
  the right question for "can this move be a rename?" and the wrong one here,
  because each pane builds its own `LocalFileSystem`. Between the two panes it
  never fired, so pointing the other pane at the same directory (or typing the
  source's own path at the `F5` prompt) destroyed the file it was asked to
  copy. Copy, move and sync now compare where the paths *are*, not which
  object they came through.
- **Copying a directory into its own subtree never finished.** The walk kept
  finding the copy it was making, one level deeper each time, writing until
  the disk filled — `F5` on a project directory into its own `backups/` was
  enough. It is refused now, by name, before anything is written. `F9` between
  a directory and one inside it is refused for the same reason: every file
  under the inner one is in both indexes under two different names, so the
  "merge" copied the inner tree into itself and back up into the outer one.
- **A byte-order mark made the first column of a CSV unaddressable.** A
  spreadsheet exporting UTF-8 writes one, and decoding left it as the first
  character of the first header cell: the column called `id` arrived as
  `\ufeffid`, so `drop id`, `filter id == 1` and every other verb that takes a
  column reported "no such column: 'id'" for a perfectly ordinary file. A
  leading mark is now dropped, where it always was a marker rather than
  content.

- **Multi-rename could destroy a file and report success.** A rule that moves
  names *among* the tagged set is allowed — that is what makes renumbering and
  swapping work — but the moves were carried out in the order the plan was
  built. `a.txt -> n1.txt` together with `n1.txt -> n2.txt` is a safe *set*,
  since no two files end up sharing a name, and fatal in that order: the first
  rename writes over `n1.txt` before the second can move it. Two files went in,
  one came out, and the plug-in said "Renamed 2 file(s)". The moves are now
  ordered so none lands on a file that has not moved yet, and a true cycle (a
  swap) is broken by parking one file under a name nothing uses, so swapping
  two names now works instead of losing one of them.
- **A `%` anywhere in the config or presets file was fatal.** Both files are
  read with `configparser`, whose interpolation treats `%` as the start of a
  substitution — but every value in them is a literal. Saving a preset for a
  directory called `100%complete` raised `ValueError` straight out of the Save
  dialog, and since the main loop does not catch it, the file manager exited on
  a traceback. A viewer of `less -Ps%f` or an editor of `vim -c "set
  titlestring=%f"` did the same on the way in, from the lookup `F3` and `F4`
  do on every keystroke. Both files are now parsed with interpolation off, so a
  `%` is just a character.
- **Key codes were still being typed as text in three more places.** The same
  defect fixed in the editor last release, found by grepping for what it
  actually is rather than for the line that expressed it: the process browser's
  filter, the provost browser's filter, and — worst — the terminal running in a
  pane, which encoded an unbound key code as a character and *sent it to the
  shell*, where the next Enter would have run it. Resizing the window was
  enough to do it. All five places now ask one helper, `util.typed_char`,
  whether a key is text.

- **Deleting a symlink on an SFTP pane deleted the directory it pointed at.**
  The listing had it right — the pane drew it as a link — but `stat` on that
  backend followed the link and then reported `is_symlink=False`, and
  `delete_tree` believes `stat`. Told a link to a directory was a plain
  directory, it listed it, deleted everything it found and emptied the target,
  which typically belongs to somebody else entirely. `stat` now asks `lstat`
  first, so the link flag is the truth and the rest of the answer still
  describes the target (a symlinked directory is still enterable, a symlinked
  file still readable). A plain file still costs one round trip; only a link
  pays for the second.
- **One symlinked directory could stop a copy or a sync dead.** A link to a
  directory is not a directory — the walk does not descend into one, or it
  would copy the target twice and loop for ever on a link to an ancestor — and
  it was therefore treated as a file. Opening one for reading is an
  `IsADirectoryError`, which escaped `copy_path` and took everything the walk
  had not yet reached with it; in a sync it ended the run at whichever file the
  plan reached first, sometimes before anything at all was copied. Both now
  leave symlinked directories alone and finish the rest of the tree. There is
  no way to *make* a symlink through the filesystem interface, so `F5` says
  which ones it left rather than turning them into empty directories, and an
  `F6` move across filesystems that could not reproduce one keeps its source
  instead of deleting a link it failed to copy.
- **Resizing the terminal typed a character into the file being edited.** The
  editor inserted any key above ASCII as text, and from `KEY_MIN` up those
  numbers are not characters but *key codes*: a resize, a mouse report,
  Shift-Up, an unbound function key. `KEY_RESIZE` arrived as 410 and put `ƚ`
  in the buffer, marked it unsaved, and would have saved it there. The same
  line was in the plug-in input widget, where it fed junk to the command about
  to be run. Both now stop at the top of the character range.

- **Directory sizes could show one machine's totals for another's directory.**
  The measured totals were cached by path alone, and a path is not unique
  across connections: `/etc` here and `/etc` on a server are different
  directories with the same name. Pointing a pane at another backend — `F2`
  to connect, leaving an archive — left the old figures in place, so the new
  listing quietly showed the previous one's. The cache now belongs to the
  connection it was measured through and is dropped when that changes.
- **A pane showing sizes redrew a large listing twelve times slower than one
  without.** The bars' scale was found by scanning every entry on every
  frame; in a directory of 20,000 files that was 2.4 ms a frame against 0.2,
  which is felt as soon as an arrow key is held down. Totals only grow, so
  the scale is now carried forward and the scan happens once per listing.
- **A plug-in that closed itself never got its `on_exit`.** The base class
  promises the call, and it is where a plug-in releases a pty or an SSH
  channel, but it was only made on the way out of the application: a plug-in
  closing itself (`F10` in the terminal), or being replaced by another in the
  same pane, was simply dropped. The built-in terminal called `on_exit`
  itself and so leaked nothing; anything written to the documented contract
  would have. All four paths now go through one routine that keeps the
  promise.

- **`u` was counting at a sixtieth of the speed it can.** The walk was paced
  at eight directory listings per 120 ms poll, so a tree it can size in 0.04
  seconds took 33 on screen: 67 listings a second against the fifty thousand
  the same code manages unpaced. The slice is now a *time* box — work for
  about 15 ms, then hand the keyboard back — and the loop offers a pane that
  is still counting a poll every 5 ms rather than the 120 ms a terminal
  plug-in is happy to wait. The same 2,221-directory tree now finishes in
  0.06 s, in two poll cycles. A time box also retires the guess: a fixed
  count cannot suit both a local listing (microseconds) and an SFTP one (a
  network round trip), while a slice fits whatever the backend can do in it.

### Added

- **`u` shows what each subdirectory is holding**, in the listing itself: the
  `<DIR>` marker becomes a real total and the `Modify time` column becomes a
  bar scaled to the biggest entry here, files included. Deliberately no total
  and no separate report — the answer to "where has the space gone?" is a
  path, so you follow the longest bar down with `Enter` and end up standing in
  it. The walk happens between keystrokes, in slices of about 15 ms, so the
  pane keeps answering the keyboard while it counts (it matters most on a
  remote pane, where each listing is a round trip); a directory still being
  counted shows its running figure in the dimmer colour. Totals are kept as
  you move around, so walking back up a measured tree is free, and `Ctrl-R`
  is what forgets them.

- **`Options > Viewer` points `F3` at your own pager** — `less`, `less -R`,
  `more`, `$PAGER`, or any command line you name — remembered in
  `[ui] viewer`, with *Built-in viewer* on the same menu to put it back. It
  applies to plain text only: a spreadsheet, document, deck, PDF, image or
  markdown file keeps the browser built for it, because a pager handed a
  `.xlsx` shows the bytes of a zip file. On a remote pane the file is fetched
  to a private temporary copy and read there, and nothing is written back --
  a pager has nothing to send, so the copy does not outlive it.

- **`n` makes an empty file** where you are standing, and `r` renames the one
  under the cursor — `touch` and `mv` without leaving for a shell. A name that
  already exists is **restamped, never emptied**, so a mistyped name cannot
  cost you a file's contents; unlike `F7` it does not invent the directories
  on the way, which is what `touch a/b` does at a shell too. Both are on
  `File`, in the right-click menu, and work on every backend: a remote pane
  restamps over SFTP or with the server's own `touch`, and a read-only archive
  says so rather than half-doing it.
- **`D` compares both panes' files side by side.** The two files are aligned
  line for line first, so a screen row holds a line from each side or a line
  facing a shaded gap, and the pair scrolls **as one** — nothing to fall out
  of step, which is where two independently scrolled views go wrong as soon as
  the files differ in length. Changed lines are marked `!`, one-sided lines
  `+`/`-`, and `n`/`N` jump between *blocks* of differences rather than rows.
  Built in rather than a call out to `vimdiff`: both sides are read through
  their own pane's connection, so a local file compares against an SFTP, SSH,
  FTP or in-archive one with nothing fetched to disk first.
- **`h` turns a pane into the head and tail of the other pane's file**, with a
  rule between them saying how many lines were skipped — the two ends of a log
  without `head`, `tail` and a shell to run them in. It stays *in the pane*
  and **follows the other pane's cursor**, so looking into ten files costs ten
  arrow keys rather than ten windows opened and closed, and the listing you
  are choosing from keeps its place. The count sizes itself to the pane so
  both ends are on screen at once; `+`/`-` pin it larger or smaller and scroll
  the overflow, `r` re-reads a file that has grown, and `Esc` gives the pane
  back to its listing. The file is streamed once, keeping only the first lines
  and a rolling window of the last, so peeking at a multi-gigabyte log costs
  what peeking at a short one does.
- **`Ctrl-O` from your shell, and the shell follows you back.** `--printwd`
  writes the active pane's directory on exit, and `--shell-init bash|zsh|fish`
  prints the function and key binding that reads it — so browsing ends with
  your prompt in the directory you navigated to. Nothing is written when the
  pane was SFTP, FTP or an archive: no shell can `cd` to those, so it leaves
  you where you were rather than guessing.

## [1.4.0] — 2026-08-27

### Added

- **`F4` can hand the file to your own editor.** `Options > Editor` points it
  at `vi`, `vim`, `nano`, `$EDITOR` or any command line you name, remembered
  in `[ui] editor`; blank keeps the built-in editor, which stays the default.
  On a remote pane the file is fetched to a private temporary copy, edited,
  and written back only if it changed — and if that write-back fails, the copy
  is kept and the message says where, so the work is not lost.
- A tag pushed for a version already on PyPI is now a no-op with a notice
  rather than a failure. `PUBLISHING.md` asks for exactly that — the
  `[release]` route publishes without leaving a tag — and a tag that records
  a release which already happened has nothing to upload. An intentional
  publish that cannot succeed still stops loudly.
- A **Provost data plug-in**: browse a provost store's datasets, its log and
  the sources behind each row. Its pane browser follows the other pane to
  whichever store that pane is standing in.
- **Run files from the panels** — `Enter` on a script or an executable runs
  it, rather than trying to view it.
- Seven more plug-ins: compare, du, dupes, normalise, tail, inspect and git.
- Five plug-ins before those, alongside real SSH host-key checking.
- A **Processes** plug-in: browse and kill processes, locally or over SSH.
- **Read the Docs** integration, so the manual builds on every push.
- `ruff` and `mypy` gates in CI, and a `lint` job to run them.
- Python 3.10 and 3.11 in the test matrix, and two macOS jobs — every version
  and platform the package's own classifiers promise.

### Changed

- **Plug-ins offer a keystroke command menu** instead of expecting a
  remembered word. Fourteen of them were small CLIs behind a prompt, each with
  its own vocabulary; `F2` now lists what a plug-in accepts and takes one key
  per command.
- `--version` reports the right copyright name, and the wording is consistent
  with the rest of the application.
- **The manual is the manual.** The README had grown to 1,211 lines and held
  the whole reference — usage, key bindings, configuration, architecture —
  while the Sphinx site linked back to it for "the complete feature guide".
  Two copies of the plug-in and installation chapters existed and had already
  drifted apart in wording. The reference now lives on
  [Read the Docs](https://meridian-commander.readthedocs.io/) as
  `usage`, `look-and-feel`, `configuration`, `transfers` and `development`
  pages; the README keeps the tour, the feature list and installation, and
  points at the rest. A CI job builds the manual with `-W`, so a broken
  cross-reference fails rather than shipping.

### Fixed

- A wedged curses screen no longer hangs the entire test run. The pseudo-
  terminal drain treated an empty read as end-of-file and retired itself while
  the terminal was still live, so a later screen large enough to fill the
  buffer blocked inside curses for good. Tests now carry a timeout and CI jobs
  a `timeout-minutes`, so a hang fails instead of waiting.
- A duplicated key in the PDF fallback width table, a `raise` inside an
  `except` that dropped the exception chain, and three locals assigned and
  never read.
- Four tests in `test_app_draw.py` passed only because a test earlier in the
  same file had left a curses screen initialised behind them; run on their
  own, all four failed. They stub the two globals the loop reaches for and
  now stand alone.
- The coverage gate no longer depends on a race. A terminal's shell exiting
  is noticed either by the pty master reporting `EIO` or by `waitpid` seeing
  the child, whichever wins on the day; one test reached whichever path won,
  so the losing branch was covered by luck, and the luck ran out on `main`
  at 99.9%. The `waitpid` branch now has a test that picks the winner.
- **A dropped SSH connection is reported as one.** `get_transport()` answers
  `None` once the connection has gone, and four call sites dereferenced it
  immediately — both scp paths and both ends of the ProxyJump chain — so an
  idle timeout or a suspended laptop surfaced as `AttributeError: 'NoneType'
  object has no attribute 'open_session'`. They now raise a `FileSystemError`
  that names the connection and says to reopen the pane with `F2`. The JSON
  push plug-in had the same hole between its redial and its channel.

### Known limitation

- The 378 tests that drive a real curses screen on a pseudo-terminal are
  skipped on macOS, where that arrangement deadlocks inside Apple's ncurses.
  The rest of the suite runs there, and the coverage gate stays on Linux.
  See `CURSES_SCREENS` in `tests/support.py`.

## [1.3.0] — 2026-08-13

### Added

- **The 1991 face.** A grey menu bar with a clock, a shaded blue desktop, each
  pane a framed window — double-line for the active one, single for the other
  — with its path in the caption and a scrollbar down its edge; dialogs in grey
  with red accelerators, green buttons and drop shadows; and the F-key bar
  along the bottom. Three schemes: `turbo`, `midnight` and `mono`.

## [1.2.0] — 2026-08-10

### Added

- **Colour images in the terminal**, drawn as half-blocks — or as real pixels
  where the terminal can show them (sixel). Decoders for PNG, GIF, BMP and
  Netpbm, written against the standard library, plus a JPEG decoder that reads
  DC coefficients for a 1/8-scale preview.
- **PDF browsing**: page text, and the images on a scanned page.
- **`.xlsx` workbooks** in a full-screen grid, **`.docx` documents**,
  **`.pptx` presentations** a slide at a time, and **rendered Markdown**.
- **Zip and tar archives browsable as directories.**
- **Presets** — saved locations, local or remote, each reachable by its own
  letter from the preset list.
- Home-directory jump (`~`) and mirror-location (`=`) keys.
- Data plug-ins: profile, clean and build CSV/TSV datasets.
- Viewer search, and a find-files result list you can browse.
- A website with real screenshots, deployed to GitHub Pages.

### Changed

- A copy from an SFTP pane to the same server writes down a second channel, so
  the read and the write cannot contend.
- The sync scan is interruptible, and asks before syncing a directory that
  looks too big to sync.
- After deleting, the cursor stays where the files were instead of springing
  back to the top.
- The left pane opens where the command was run.
- The build refuses a setuptools too old to read `pyproject.toml`, rather than
  silently installing a package called `UNKNOWN` with no `meridian` command.

### Fixed

- Two sixel bugs a real decoder found, and three Adam7 passes in the PNG
  decoder.
- Dialogs are clamped to the terminal height, and `<DIR>` is no longer
  truncated in listings.

## [1.1.0] — 2026-07-23

### Added

- **SSH-config support**, including alias-aware native ProxyJump.
- A GNU-style `--version` with copyright.
- A pane divider, and a terminal laid out in line with the file panes.

### Changed

- Packaging requires `setuptools>=61`, the first that reads PEP 621 metadata.

## [1.0.0] — 2026-07-22

The first release under the name Meridian Commander.

### Added

- Two independent panes over local, **SFTP**, **SSH (shell)** and **FTP**
  locations, with copy and move working across any pair of them.
- Bidirectional directory sync, comparing both panes and copying the newest
  version of each file in whichever direction is needed.
- A built-in viewer and editor, an in-pane pseudo-terminal, a configuration
  file editable from inside the application, and a pane plug-in system.

<!--
Every release so far was cut by merging with "[release]" in the commit
message, which publishes to PyPI but leaves no tag behind, so these link to
the release commit rather than to a "v1.2.0" that does not exist. Tagging each
release would make these ordinary compare links and give the repository the
same history PyPI already has; see PUBLISHING.md.

1.4.0 is the first to link to a tag rather than a commit, because PUBLISHING.md
asks for this one to be cut by pushing "v1.4.0". Those two links resolve once
that tag exists; cutting it the "[release]" way instead would leave them
pointing at nothing.
-->

[Unreleased]: https://github.com/MartinGallagher-code/meridian_commander/compare/v1.4.0...HEAD
[1.4.0]: https://github.com/MartinGallagher-code/meridian_commander/compare/552dc66...v1.4.0
[1.3.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/552dc66
[1.2.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/7c6792d
[1.1.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/b49bd6f
[1.0.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/be40a0d
