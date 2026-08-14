# Plug-ins

Press **`p`** (or F11) to put the active pane into **plug-in mode**: a menu
lists the discovered plug-ins and the chosen one takes over that pane. The
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
