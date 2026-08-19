# Plug-ins

Press **`p`** (or F11) to put the active pane into **plug-in mode**: a menu
lists the discovered plug-ins and the chosen one takes over that pane. Each
plug-in answers to a letter in that menu (just like the presets menu), so once
the list is familiar, opening one is `p` then a single keystroke rather than a
walk down the list. The
plug-in can see the **opposite pane** — its filesystem (local or remote), its
directory and entries — so it can do work on whatever you have open next to
it. `Esc` closes the plug-in and returns the pane to its file listing; `Tab`
still switches panes while a plug-in is open.

## Built-in plug-ins

- **Terminal** — the in-pane pseudo-terminal (also on the `t` key); a shell
  in the pane's directory, local or over the pane's SSH connection.
- **Processes** — a process browser and killer for the pane's host, local or
  over the pane's SSH connection. Auto-refreshing; sort with `c`/`m`/`t`/`p`/
  `n`/`u`, filter with `/`, and kill the process under the cursor with
  `F8`/`k`/`Del` (choosing TERM, KILL, HUP or INT).
- **Find in other pane** — recursively search the other pane's directory by
  glob pattern (works on remote panes too).
- **Grep in other pane** — the content-search sibling of Find: recursively
  search the other pane's *files* for text (case-insensitive) or a `re:`
  regular expression, reporting `path:line: match`. Skips binary files and
  works on remote panes too.
- **Compare panes** — diff this pane's directory tree against the other's,
  listing what is only-left, only-right, or differs (by name+size+time, or
  `hash` to compare contents). The *seeing* half of the `s` sync.
- **Disk usage** — size each item under the other pane, biggest-first with a
  bar (an `ncdu`-lite); works over SFTP/SSH too.
- **Find duplicates** — group files with identical contents under the other
  pane (size buckets first, then hash to confirm), and say how much is
  recoverable.
- **Normalise text** — batch-fix line endings (`lf`/`crlf`), tabs (`untabs`),
  trailing space (`trim`) and final newline in the other pane's tagged files,
  with `preview`.
- **Tail file** — show the end of a file, once (`tail`) or streaming as it
  grows (`follow`, like `tail -f`), from whichever pane it lives on.
- **Inspect file** — identify the other pane's cursor file from its magic
  bytes and show an offset/hex/ASCII dump of its start.
- **Git** — status, log, diff, add, unstage, branch and commit in a local
  pane's repository (no network commands, so it can't block on a prompt).
- **Provost data** — browse a [provost](https://pypi.org/project/provost/)
  store from the other pane's location: datasets as aligned, scrollable,
  filterable tables; the capture log (`l`), each capture with its metadata and
  original output; and `s` on a dataset row to see the exact captured input
  behind its `#N` index (provost's provenance). The store is found the way
  provost finds it — the pane's directory, a controlled parent's `.provost`,
  or `~/.provost` — and read through the pane's filesystem, so remote stores
  browse like local ones. `store =` under `[plugin:provost]` in `config.ini`
  points it somewhere explicit.
- **Multi-rename** — rename the other pane's tagged files in bulk by one rule:
  `replace`, `prefix`, `suffix`, `case` or a `number` template. Prefix a rule
  with `preview` to see the mapping first; a rule that would collide two names
  or overwrite an existing file is refused in full.
- **Make archive** — pack the other pane's tagged files and directories into a
  `zip`, `tar` or `tgz` beside them (the counterpart to browsing *into* an
  archive). Works on remote panes.
- **Checksum / verify** — hash the other pane's tagged files (`sha256`, `sha1`,
  `md5`, `sha512`), `write` a `SHA256SUMS`-style file, or `verify` files
  against one, reporting `OK`/`FAILED`/`missing`.
- **SSH doctor** — scan `~/.ssh`, classify each private key's format, and flag
  the legacy ones OpenSSL 3.0 may refuse to load; show what `ssh-add -l`
  reports; and `convert` a key to the modern OpenSSH format (automatically for
  an unencrypted key, backing the original up first; by handing you the
  `ssh-keygen` line for an encrypted one).
- **JSON push** — delivers each line of input as JSON to a TCP listener on a
  remote server (via an SSH channel, so the listener can stay on loopback)
  and shows the reply.
- **Run remote script** — on each input, copies a configured local script to
  a remote directory over SSH, runs it with the input as arguments, and shows
  its output.
- **Profile table**, **Clean table**, **Build dataset** — the data plug-ins
  for CSV/TSV/JSON-lines files; see the [data tools tutorial](DATA_TOOLS.md)
  for a step-by-step walkthrough.

## Writing a plug-in

Drop a `.py` file into `~/.config/meridian-commander/plugins/` (or into
`meridian_commander/plugins/` inside the source tree — both are scanned, plus
any extra directories listed in the config file). A complete plug-in is:

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

The [API reference](api.md) documents `PluginContext`, `PanePlugin` and
`InputOutputPlugin` in full.
