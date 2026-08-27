# Configuration

Press **`C`** for the configuration menu:

- **Edit configuration** opens `~/.config/meridian-commander/config.ini` in the
  built-in editor (created with commented defaults on first use). `[ui] scheme`
  is the colour scheme; `[ui] editor` is the external editor `F4` should use
  (blank for the built-in one — see below); plug-ins read their settings from
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
