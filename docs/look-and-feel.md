# Look and feel

The screen is laid out the way a Borland IDE laid one out, and for the same
reasons:

| Part | What it is |
| --- | --- |
| Menu bar | Grey, along the top, with the clock in the corner. `Esc`, `Alt+`letter, or a click. |
| Desktop | The shaded blue field the windows sit on (character `░`, as Turbo Vision used). |
| Panes | Framed windows: double-line and a yellow caption for the pane with the keyboard, single and grey for the other. The path is the caption, what is tagged (or under the cursor) is the footer, and the scrollbar rides the right-hand frame. |
| Listing | Directories white, symlinks cyan, tagged files yellow, the cursor a cyan bar. |
| Dialogs | Grey, double-framed, `╡ captioned ╞`, with red accelerators, blue input fields, green buttons with their own shadow, and a drop shadow two columns right and one row down. |
| Key bar | Grey, along the bottom, the F-keys and what they do. |

`meridian_commander/theme.py` holds the whole palette: the sixteen EGA colours,
a table of *roles* ("what colour is a dialog button"), and the drawing
primitives — frames, shadows, scrollbars, hot-key captions — that everything
else is built from. Three things follow from having it in one place:

- **Schemes.** `turbo` is Turbo C++ 3.0; `midnight` keeps the chrome and puts
  it on black; `mono` is the monochrome adapter. Switch with **Options ▸
  Colours** (it is remembered in `config.ini`, `[ui] scheme`).
- **No colour, no problem.** Every role carries a monochrome fallback, so a
  terminal with no colour — or one that has run out of colour pairs — still
  gets the shape of the screen: which strip is chrome, which row is the bar.
- **ASCII frames when the encoding needs them.** The box-drawing characters are
  used when the locale can encode them and `+-|` when it cannot. Set
  `MERIDIAN_ASCII=1` to force the plain set on a terminal that claims UTF-8 and
  then draws it badly.
