# Changelog

Notable changes to Meridian Commander, newest first.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version numbers follow [Semantic Versioning](https://semver.org/).

Releases before this file existed are reconstructed from the commit history,
so they group what shipped rather than reproducing every commit. Dates are the
day the version was cut.

## [Unreleased]

### Added

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
-->

[Unreleased]: https://github.com/MartinGallagher-code/meridian_commander/compare/552dc66...HEAD
[1.3.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/552dc66
[1.2.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/7c6792d
[1.1.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/b49bd6f
[1.0.0]: https://github.com/MartinGallagher-code/meridian_commander/commit/be40a0d
