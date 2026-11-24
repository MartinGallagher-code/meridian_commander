# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""What F1 shows: every key, in titled sections, as data.

The help was one string, wrapped by hand to 70 columns, and the dialog that
showed it kept only the lines that fitted on the screen -- on an 80x24
terminal, the first 17 of 143.  Kept as data instead, each description is
plain prose that :func:`meridian_commander.dialogs.help_screen` wraps to
whatever column width the terminal leaves it, and lays out in two columns
where there is room for them.

The descriptions are ASCII on purpose: the column arithmetic counts one
column per character, and a wide glyph would push its row out of line.
"""

from __future__ import annotations

from .dialogs import HelpSection

#: The line above the sections.
INTRO = "Meridian Commander -- two-pane terminal file manager"

SECTIONS = (
    HelpSection("Moving around", (
        ("Tab", "switch active pane"),
        ("Up/Down j/k", "move cursor"),
        ("PgUp/PgDn", "page"),
        ("Home/End", "first / last"),
        ("Enter / Right",
         "enter dir / archive (zip, tar); run a script ('#!' line) or "
         "executable, with an arguments prompt (Esc cancels); view the rest"),
        ("Backspace/Left", "parent directory"),
        ("~", "home directory of this pane's location"),
        ("< / >",
         "back / forward through the places this pane has been: '=', F2 and "
         "archives included"),
        ("=", "other pane: same directory and connection"),
        ("b", "presets: go to / save / delete a location"),
        ("Ctrl-G", "go to path"),
        ("/",
         "search this pane: type a name and the cursor jumps to the first "
         "one containing it (all lowercase ignores case); Down/Up or "
         "Ctrl-N/P next/previous, Ins tag, Enter/F-keys act on it, / or Esc "
         "ends.  Every letter and digit is part of the name, so no hotkey "
         "runs meanwhile"),
        ("Esc",
         "the menu bar (Alt+F, Alt+C, Alt+O, Alt+H open a menu by name; "
         "click one to open it)"),
    )),
    HelpSection("Tagging", (
        ("Insert / Space", "tag file"),
        ("-", "untag all"),
        ("*", "invert the tags"),
        ("+ / \\", "tag / untag the names matching a pattern"),
        ("F", "filter: show only the names matching a pattern"),
    )),
    HelpSection("Working on files", (
        ("F5/F6",
         "copy / move to the other pane.  They ask before replacing a file "
         "(overwrite, skip, if older; once or for all), keep permissions "
         "(Ctrl-P in the prompt drops them) and copy symbolic links as links"),
        ("F8",
         "on a local pane: to the trash, or delete for good; in the trash "
         "itself, for good only"),
        ("T",
         "the trash, in this pane: the one F8 uses from here (from a server "
         "or an archive, this computer's); < goes back"),
        ("r", "rename the file under the cursor"),
        ("n",
         "new empty file here (an existing one is restamped, never emptied)"),
        ("a", "permissions, owner and group of what is tagged"),
        ("l", "link in the other pane; on a link, change its target"),
        ("w",
         "open with the system's application (a copy, if remote or in an "
         "archive)"),
        ("x",
         "extract the archive under the cursor (or the tagged ones) into "
         "the other pane, each in a folder of its own; F5/F6 into a pane "
         "that is inside an archive add the files to it"),
        ("f",
         "find files: browsable results (view/edit/goto); p there, or "
         "Command>Find files to pane, lists every hit in this pane by its "
         "path below the directory searched: tag, F3/F4, a, l, w, r, F8 work "
         "on the real files, F5/F6 keep that path under the target.  Ctrl-R "
         "drops hits gone since (no new search); <, .. or any move goes back "
         "to a directory, and < > history skips the list.  F9, D, u and "
         "plug-ins on it are refused"),
    )),
    HelpSection("Panes and views", (
        ("Ctrl-U", "swap panes"),
        ("Ctrl-R", "reload panes"),
        ("Ctrl-T",
         "sort order: name, natural (run_2 < run_10), ext, size, time"),
        ("Ctrl-Q", "quick view: the other pane shows the cursor's file"),
        (".", "show/hide hidden files (this pane)"),
        ("u",
         "directory sizes: each subdirectory shows what is under it, with a "
         "bar against the biggest here -- follow them down to find the "
         "space"),
        ("z",
         "single pane: this pane at full width, the other kept as it was "
         "(Tab flips to it, Ctrl-U brings it out); copy, move, extract and "
         "sync name it.  h, Ctrl-] Tab in the terminal and plug-ins that use "
         "the other pane show both while they run.  z again: two panes.  In "
         "the terminal it is Ctrl-] z, and closing the terminal ends it"),
        ("t",
         "terminal in this pane: every key goes to the program; Ctrl-] then "
         "Tab other pane, z zoom to the full width and back, q close, PgUp "
         "scroll back (or the mouse wheel)"),
        ("!", "full-screen shell: the whole real terminal"),
        ("p / F11", "plug-in mode: run a plug-in in this pane"),
        ("D",
         "compare both panes' files side by side: n/N next/previous "
         "difference, one scrollbar"),
        ("K",
         "join both panes' tables: File A (the left pane's) top left, File "
         "B top right, the rows they make joined below.  m marks a join "
         "column in each, Tab moves between the three, o which rows are "
         "kept, e how strict a match is, z zoom, F2 saves the join, F1 its "
         "keys"),
        ("h",
         "this pane becomes head + tail of the file the other pane is on, "
         "and follows its cursor; +/- more/fewer lines, Esc gives the pane "
         "back"),
    )),
    HelpSection("Viewers: F3 on a file", (
        (".xlsx",
         "spreadsheet grid: Tab sheet, / find, w width, F2/Ctrl-S save the "
         "sheet as a .tsv (or .csv)"),
        (".tsv .csv",
         "table: f filter, =/! this value, Bksp/0 unfilter, s sort, Enter "
         "row, H header, c colour, # the comment lines above the table, i "
         "column summary, F freeze columns, -/+ hide/show columns, "
         "F2/Ctrl-S save the rows and columns shown as a new file: .tsv .tab "
         ".psv or .csv, by its name"),
        (".docx", "document view: headings, lists, tables, w wrap"),
        (".pptx", "slide browser: Tab slide, t notes, / find"),
        (".md", "markdown rendered; r shows the source"),
        ("code",
         ".py .sh .yml .json .toml .ini .conf and #! scripts in colour; c "
         "turns the colour off"),
        ("an image", "arrows pan, +/- zoom, c colour/ASCII, n frame"),
        (".pdf", "page text: Tab page, / find, i scanned image"),
        (".dc",
         "datacenter floor plan (a layout, not a dc script): arrows move, "
         "Enter zooms in to a row and a rack, Bksp/Esc out, c colours by "
         "role/tag/model/off"),
    )),
    HelpSection("Background work", (
        ("F5/F6 Ctrl-B", "in the prompt: run it in the background"),
        ("J",
         "background jobs: p pause, c cancel, Enter errors; and the "
         "background commands: Enter shows one in this pane, k stops it, d "
         "removes an ended one"),
        ("&",
         "background commands: a saved one by its letter, Type a command, or "
         "Add/Edit/Delete the list (commands.ini).  It runs with your shell "
         "in its own directory, or this pane's (local panes only), and keeps "
         "running whatever the panes do.  Shown in a pane, every key goes to "
         "it (Ctrl-C too); Ctrl-] then q hides it, k stops it, Tab, z and "
         "PgUp as in the terminal.  Quitting stops them"),
    )),
    HelpSection("Options menu and configuration", (
        ("C", "configuration: edit config.ini / plug-ins"),
        ("Colours", "turbo (blue), midnight (black), mono"),
        ("Editor", "F4 opens vi/vim/$EDITOR instead of the built-in editor"),
        ("Viewer",
         "F3 opens less/$PAGER instead of the built-in viewer, for plain "
         "text and code (a spreadsheet, table, deck, document, PDF, image or "
         ".dc layout keeps its own)"),
        ("View and edit in a pane",
         "F3 and F4 open in a pane, alone at full width, rather than "
         "full-screen (outside pagers and editors too, in the pane's "
         "terminal).  z in a viewer, or Ctrl-] z, shows two panes: the file "
         "beside the listing it came from, and back.  Ctrl-] Tab gives the "
         "listing the keys, Tab comes back; F3 there shows the next file in "
         "the same view.  Closing puts the panes back as they were"),
    )),
    HelpSection("Function keys", (
        ("F1/1/?", "Help"),
        ("F2/2/o",
         "Open/Connect: SFTP, SSH (shell) or FTP; copy/move/sync work "
         "across local and remote panes alike"),
        ("F3/3/v", "View"),
        ("F4/4/e", "Edit"),
        ("F5/5/c", "Copy"),
        ("F6/6/m", "Move"),
        ("F7/7", "Mkdir"),
        ("F8/8/d", "Delete"),
        ("F9/9/s", "Sync: both panes hold the newest version of every file"),
        ("F10/0/q", "Quit"),
    ), note="Each also has digit and letter aliases, for terminals that "
            "swallow the F-keys."),
    HelpSection("Mouse", (
        ("click", "select"),
        ("double-click", "open"),
        ("wheel", "scroll"),
        ("right-click", "a context menu of actions"),
    )),
)
