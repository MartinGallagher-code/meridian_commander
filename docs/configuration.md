# Configuration

Press **`C`** for the configuration menu:

- **Edit configuration** opens `~/.config/meridian-commander/config.ini` in the
  built-in editor (created with commented defaults on first use). `[ui] scheme`
  is the colour scheme; `[ui] editor` is the external editor `F4` should use
  (blank for the built-in one — see below); `[ui] panes` is how many panes
  are on screen at start (below); `[ui] listing` is `long` for the mode and
  owner columns (below); plug-ins read their settings from
  `[plugin:<name>]` sections; `[plugins] dirs` adds extra plug-in directories.
- **Edit a plug-in file** lists every discovered plug-in file (built-in and
  user) and opens the chosen one in the editor.
- **Open user plug-in folder in this pane** jumps the pane to
  `~/.config/meridian-commander/plugins/` so you can manage plug-ins like any
  other files.

Saved locations are kept separately, in
`~/.config/meridian-commander/presets.ini` — see
[Presets](usage.md#presets--saved-locations). They are written by the app (`b`) rather
than by hand, which is why they are not part of `config.ini`.

## `[ui] panes` — starting with one pane

`2`, the default, starts with the two panes side by side. `1` starts with the
active pane at full width and the other kept out of sight, as **`z`** gives
you while running — see [One pane at a time](usage.md#one-pane-at-a-time):

```ini
[ui]
panes = 1
```

Only `1` does this; anything else, blank included, is two panes. `z` and
**Options ▸ Single pane** change it for the session and do not write the file,
the way the hidden-files toggle does not.

## `[ui] trash` — whether F8 offers the trash

`yes`, the default, has `F8` on a local pane offer **Move to trash** first;
`no` has it delete permanently, as it did before the trash existed:

```ini
[ui]
trash = no
```

**Options ▸ Trash on delete** switches it and writes the file. Remote panes and
archives have no trash either way. See
[Deleting to the trash](usage.md#deleting-to-the-trash).

## `[ui] listing` — the mode and owner columns

`short`, the default, lists each entry's name, size and time. `long` adds its
mode (`-rwxr-xr-x`) and its owner, between the size and the time:

```ini
[ui]
listing = long
```

**Options ▸ Long listing** switches it while running, and does write the file,
so the choice outlasts the session. Where a pane is too narrow for every
column — two panes on an 80-column terminal — the time gives way first, so
names keep their room. The owner is a name on a local, SSH or FTP pane and a
number on an SFTP one, which is all SFTP says.

## `[ui] editor` — using vi, vim or your own editor

Blank, the default, means `F4` opens the built-in editor. Set it to a command
line in shell syntax and `F4` hands the file to that instead:

```ini
[ui]
editor = vim
```

`vi`, `nano`, `emacs -nw` and `code --wait` all work, as does `$EDITOR` to
follow the environment. Anything the shell would split, this splits the same
way, so an `EDITOR` of `code --wait` arrives as a command and its argument
rather than one impossible filename. A setting that cannot work — unbalanced
quotes, or `$EDITOR` when nothing sets it — is reported when you press `F4`,
and the built-in editor is used for that edit.

**Options > Editor** sets this from inside the app, and warns straight away if
the command you chose is not on your `PATH`. See
[Editing with your own editor](usage.md#editing-with-your-own-editor) for what
happens on a remote pane.

A `%` in a value is an ordinary character, here and in `presets.ini`: a pager
of `less -Ps%f`, an editor of `vim -c "set titlestring=%f"` and a saved
location under `/srv/reports/100%complete` all mean what they say. Both files
are read with INI interpolation switched off, since every value in them is a
literal rather than a template.
