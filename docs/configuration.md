# Configuration

Press **`C`** for the configuration menu:

- **Edit configuration** opens `~/.config/meridian-commander/config.ini` in the
  built-in editor (created with commented defaults on first use). `[ui] scheme`
  is the colour scheme; plug-ins read their settings from `[plugin:<name>]`
  sections; `[plugins] dirs` adds extra plug-in directories.
- **Edit a plug-in file** lists every discovered plug-in file (built-in and
  user) and opens the chosen one in the editor.
- **Open user plug-in folder in this pane** jumps the pane to
  `~/.config/meridian-commander/plugins/` so you can manage plug-ins like any
  other files.

Saved locations are kept separately, in
`~/.config/meridian-commander/presets.ini` — see
[Presets](usage.md#presets--saved-locations). They are written by the app (`b`) rather
than by hand, which is why they are not part of `config.ini`.
