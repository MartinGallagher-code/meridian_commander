"""The Martin Commander application: a two-pane terminal file manager.

This module wires the pieces together -- two :class:`~martin_commander.panel.Panel`
objects, the filesystem backends, the viewer/editor, dialogs and the file
operations -- into a curses event loop styled after Midnight Commander.

Key bindings (also shown in the F1 help screen)::

    Tab            switch active pane          F1   help
    Up/Down        move cursor                 F2   open / connect location
    PgUp/PgDn      page                        F3   view file
    Home/End       first / last                F4   edit file
    Enter / Right  enter dir / view file       F5   copy  ->  other pane
    Backspace/Left parent directory            F6   move  ->  other pane
    Insert / Space tag file                    F7   make directory
    + / -          tag all / untag all         F8   delete
    Ctrl-U         swap panes                  F9   synchronize panes
    Ctrl-R         reload both panes           F10  quit
    Ctrl-G         go to path
    Ctrl-T         change sort order
"""

from __future__ import annotations

import curses
import os
import shlex
import subprocess

from . import dialogs
from .editor import Editor
from .filesystems import (
    FileSystem,
    FileSystemError,
    FTPFileSystem,
    LocalFileSystem,
    SFTPFileSystem,
    SSHFileSystem,
)
from .operations import (
    OperationCancelled,
    copy_path,
    count_tree,
    move_path,
)
from .panel import Panel
from .sync import build_sync_plan, execute_sync_plan
from .util import human_size, human_time, ljust, rjust
from .viewer import Viewer


class App:
    def __init__(self, stdscr, left_path: str | None = None,
                 right_path: str | None = None) -> None:
        self.stdscr = stdscr
        self._backends: list[FileSystem] = []

        left_fs = LocalFileSystem()
        right_fs = LocalFileSystem()
        self._backends += [left_fs, right_fs]
        self.left = Panel(left_fs, left_fs.normpath(left_path or left_fs.home()))
        self.right = Panel(right_fs, right_fs.normpath(right_path or right_fs.home()))
        self.active = self.left
        self.message = "F1/? Help   Tab switch   F9/s Sync   F10/q Quit   right-click: menu"
        self.running = True
        # (panel, y, x, h, w) rectangles, refreshed on every draw for the mouse.
        self._panel_boxes: list[tuple] = []

    # -- helpers ----------------------------------------------------------
    @property
    def other(self) -> Panel:
        return self.right if self.active is self.left else self.left

    def _set_message(self, text: str) -> None:
        self.message = text

    # -- drawing ----------------------------------------------------------
    def draw(self) -> None:
        stdscr = self.stdscr
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 6 or width < 20:
            stdscr.addstr(0, 0, "Terminal too small")
            stdscr.noutrefresh()
            curses.doupdate()
            return

        panel_h = height - 2
        left_w = width // 2
        right_w = width - left_w

        # Remember each pane's screen rectangle so the mouse handler can map a
        # click back to a pane and a row.
        self._panel_boxes = [
            (self.left, 0, 0, panel_h, left_w),
            (self.right, 0, left_w, panel_h, right_w),
        ]

        for panel, py, px, ph, pw in self._panel_boxes:
            if panel.plugin is not None:
                try:
                    panel.plugin.draw(stdscr, py, px, ph, pw)
                except Exception as exc:
                    # A broken draw() must not take down the whole UI.
                    try:
                        stdscr.addstr(py + 1, px + 1,
                                      f"plugin draw error: {exc}"[: pw - 2])
                    except curses.error:
                        pass
            else:
                self._draw_panel(panel, py, px, ph, pw,
                                 active=self.active is panel)

        # Status line.
        stdscr.attrset(curses.A_NORMAL)
        status = self.message[: width - 1]
        try:
            stdscr.addstr(height - 2, 0, status.ljust(width - 1))
        except curses.error:
            pass

        self._draw_function_bar(height - 1, width)
        stdscr.noutrefresh()
        curses.doupdate()

    def _draw_panel(self, panel: Panel, y: int, x: int, h: int, w: int,
                    active: bool) -> None:
        stdscr = self.stdscr
        body_h = h - 3  # header, column titles, footer
        panel.ensure_visible(body_h)

        border = curses.A_BOLD if active else curses.A_DIM
        # Header: filesystem label + current path.
        header = f"{panel.fs.label()}:{panel.path}"
        head_attr = curses.A_REVERSE if active else curses.A_NORMAL
        try:
            stdscr.addstr(y, x, ljust(header, w), head_attr)
        except curses.error:
            pass

        # Column titles.
        title = f" {'Name'.ljust(w - 22)}{'Size':>6} {'Modify time':>12}"
        try:
            stdscr.addstr(y + 1, x, ljust(title, w), curses.A_UNDERLINE | border)
        except curses.error:
            pass

        # Entries.
        for row in range(body_h):
            idx = panel.top + row
            ry = y + 2 + row
            if idx >= len(panel.entries):
                try:
                    stdscr.addstr(ry, x, " " * w)
                except curses.error:
                    pass
                continue
            entry = panel.entries[idx]
            is_cursor = active and idx == panel.cursor
            tagged = entry.name in panel.selected

            name = entry.name
            if name == Panel.PARENT:
                display = ".."
            elif entry.is_dir:
                display = name + "/"
            else:
                display = name
            marker = "*" if tagged else " "
            if entry.is_symlink:
                marker = "@" if not tagged else "*"

            size_s = "  <DIR>" if entry.is_dir else human_size(entry.size)
            time_s = human_time(entry.mtime)
            name_w = w - 22
            line = f"{marker}{ljust(display, name_w)}{rjust(size_s, 6)} {time_s[:12]:>12}"
            line = ljust(line, w)

            attr = curses.A_NORMAL
            if entry.is_dir:
                attr |= curses.A_BOLD
            if entry.is_symlink:
                attr |= curses.A_DIM
            if tagged:
                attr = curses.A_BOLD | (curses.color_pair(1)
                                        if curses.has_colors() else 0)
            if is_cursor:
                attr = curses.A_REVERSE | (curses.A_BOLD if entry.is_dir else 0)
            try:
                stdscr.addstr(ry, x, line, attr)
            except curses.error:
                pass

        # Footer: selection summary or error.
        if panel.error:
            footer = f" ! {panel.error}"
        elif panel.selected:
            total = sum(e.size or 0 for e in panel.entries
                        if e.name in panel.selected)
            footer = f" {len(panel.selected)} tagged, {human_size(total).strip()}"
        else:
            cur = panel.current()
            if cur and cur.name != Panel.PARENT and not cur.is_dir:
                footer = f" {cur.name}  {human_size(cur.size).strip()}"
            else:
                footer = f" {len(panel.entries) - (0 if panel._at_root() else 1)} items"
        try:
            stdscr.addstr(y + h - 1, x, ljust(footer, w), border | curses.A_REVERSE)
        except curses.error:
            pass

    def _draw_function_bar(self, y: int, width: int) -> None:
        keys = [
            ("1", "Help"), ("2", "Conn"), ("3", "View"), ("4", "Edit"),
            ("5", "Copy"), ("6", "Move"), ("7", "Mkdir"), ("8", "Del"),
            ("9", "Sync"), ("10", "Quit"),
        ]
        seg = max(1, width // len(keys))
        x = 0
        for num, label in keys:
            if x >= width:
                break
            text = f"{num}{label}"
            try:
                self.stdscr.addstr(y, x, str(num), curses.A_NORMAL)
                self.stdscr.addstr(y, x + len(num),
                                   label.ljust(seg - len(num))[: max(0, seg - len(num))],
                                   curses.A_REVERSE)
            except curses.error:
                pass
            x += seg

    # -- main loop --------------------------------------------------------
    def run(self) -> None:
        curses.curs_set(0)
        while self.running:
            self.draw()
            try:
                key = self.stdscr.getch()
            except KeyboardInterrupt:
                if dialogs.confirm(self.stdscr, "Quit", "Exit Martin Commander?"):
                    break
                continue
            self.handle_key(key)
        self._close_backends()

    def handle_key(self, key: int) -> None:
        panel = self.active
        body_h = self.stdscr.getmaxyx()[0] - 5

        # A plugin owning the active pane gets first crack at every key.
        if panel.plugin is not None and key != curses.KEY_RESIZE:
            try:
                res = panel.plugin.handle_key(key)
            except Exception as exc:
                self._set_message(f"Plugin error: {exc}")
                res = True
            if res is False:
                panel.plugin = None
                panel.refresh()
                self._set_message("Plugin closed")
                return
            if res is not None:
                return
            # res is None: fall through so the app can handle the key (Tab).

        if key in (curses.KEY_UP, ord("k")):
            panel.move(-1)
        elif key in (curses.KEY_DOWN, ord("j")):
            panel.move(1)
        elif key == curses.KEY_NPAGE:
            panel.move(body_h)
        elif key == curses.KEY_PPAGE:
            panel.move(-body_h)
        elif key == curses.KEY_HOME:
            panel.move_to(0)
        elif key == curses.KEY_END:
            panel.move_to(len(panel.entries) - 1)
        elif key == 9:  # Tab
            self.active = self.other
        elif key in (curses.KEY_ENTER, 10, 13, curses.KEY_RIGHT):
            self._activate_entry()
        elif key in (curses.KEY_BACKSPACE, 127, 8, curses.KEY_LEFT):
            panel.go_parent()
        elif key in (curses.KEY_IC, ord(" ")):
            panel.toggle_select()
            panel.move(1)
        elif key == ord("+"):
            panel.select_all()
        elif key in (ord("-"), ord("\\")):
            panel.clear_selection()
        elif key == 21:  # Ctrl-U swap panes
            self._swap_panes()
        elif key == 18:  # Ctrl-R reload
            self.left.refresh()
            self.right.refresh()
            self._set_message("Reloaded")
        elif key == 7:  # Ctrl-G go to path
            self._go_to_path()
        elif key == 20:  # Ctrl-T sort
            self._sort_menu()
        elif key == ord("."):  # toggle hidden files in the active pane
            panel.toggle_hidden()
            self._set_message("Hidden files "
                              + ("shown" if panel.show_hidden else "hidden"))
        elif key in (ord("t"), ord("!"), 15):  # terminal here (15 = Ctrl-O)
            self._open_terminal()
        elif key in (ord("p"), curses.KEY_F11):  # plug-in mode
            self._plugin_mode()
        elif key == ord("C"):  # configuration menu
            self._config_menu()
        elif key == curses.KEY_MOUSE:
            self._handle_mouse()
        # F-keys, with digit (1..0 -> F1..F10) and mnemonic-letter alternates
        # so the app is fully usable on terminals that swallow the F-keys.
        elif key in (curses.KEY_F1, ord("1"), ord("?")):
            self._help()
        elif key in (curses.KEY_F2, ord("2"), ord("o")):
            self._open_location()
        elif key in (curses.KEY_F3, ord("3"), ord("v")):
            self._view()
        elif key in (curses.KEY_F4, ord("4"), ord("e")):
            self._edit()
        elif key in (curses.KEY_F5, ord("5"), ord("c")):
            self._copy()
        elif key in (curses.KEY_F6, ord("6"), ord("m")):
            self._move()
        elif key in (curses.KEY_F7, ord("7")):
            self._mkdir()
        elif key in (curses.KEY_F8, curses.KEY_DC, ord("8"), ord("d")):
            self._delete()
        elif key in (curses.KEY_F9, ord("9"), ord("s")):
            self._sync()
        elif key in (curses.KEY_F10, ord("0"), ord("q"), 27, 3):  # 3 = Ctrl-C
            if dialogs.confirm(self.stdscr, "Quit", "Exit Martin Commander?",
                               default_yes=True):
                self.running = False
        elif key == curses.KEY_RESIZE:
            pass

    # -- actions ----------------------------------------------------------
    def _activate_entry(self) -> None:
        panel = self.active
        entry = panel.current()
        if entry is None:
            return
        if entry.is_dir:
            panel.enter()
        else:
            self._view()

    def _swap_panes(self) -> None:
        self.left, self.right = self.right, self.left
        # Keep the highlighted (active) pane pointing at the same panel object.
        self.active = self.left if self.active is self.left else self.right
        self._set_message("Panes swapped")

    def _open_terminal(self) -> None:
        """Suspend the TUI and drop the user into a shell in this directory.

        For a local pane this is a shell with the working directory set to the
        pane's path.  For an SFTP pane it opens an interactive ``ssh`` session
        into the same remote directory.  FTP has no shell, so it is declined.
        """
        panel = self.active
        fs = panel.fs
        cwd = None
        if isinstance(fs, LocalFileSystem):
            shell = os.environ.get("SHELL", "/bin/sh")
            cmd = [shell]
            cwd = panel.path
        elif isinstance(fs, (SFTPFileSystem, SSHFileSystem)):
            remote = f"cd {shlex.quote(panel.path)} && exec ${{SHELL:-/bin/sh}}"
            cmd = ["ssh", "-t", "-p", str(fs.port),
                   f"{fs.username}@{fs.host}", remote]
        else:
            dialogs.message(self.stdscr, "Terminal",
                            "A terminal is only available for local or SFTP panes.")
            return

        # Hand the real terminal back to the shell, then restore curses after.
        curses.def_prog_mode()
        curses.endwin()
        try:
            os.write(1, (f"\n[Martin Commander] Shell in {fs.label()}:{panel.path}\n"
                         "Type 'exit' to return to Martin Commander.\n\n").encode())
            subprocess.call(cmd, cwd=cwd)
        except Exception as exc:
            os.write(2, f"\nCould not start terminal: {exc}\n".encode())
            try:
                input("Press Enter to return...")
            except EOFError:
                pass
        finally:
            curses.reset_prog_mode()
            self.stdscr.clearok(True)
            curses.curs_set(0)
            self.stdscr.refresh()
        panel.refresh()
        self._set_message("Returned from shell")

    # -- plug-ins -----------------------------------------------------------
    def _plugin_mode(self) -> None:
        """Offer the list of discovered plug-ins and open one in this pane."""
        from .plugin_api import PluginContext
        from .plugins import discover

        panel = self.active
        classes, errors = discover()
        if not classes:
            text = "No plug-ins found."
            if errors:
                text += "\nLoad errors:\n" + "\n".join(errors[:5])
            dialogs.message(self.stdscr, "Plug-ins", text)
            return

        labels = [f"{cls.name} -- {cls.description}"[:56] for cls in classes]
        labels.append("Cancel")
        choice = dialogs.menu(self.stdscr, "Open plug-in", labels)
        if choice is None or choice == len(classes):
            return
        cls = classes[choice]

        ctx = PluginContext(app=self, own_panel=panel, other_panel=self.other)
        try:
            panel.plugin = cls(ctx)
        except Exception as exc:
            dialogs.message(self.stdscr, "Plug-in error",
                            f"{cls.name} failed to start:\n{exc}", error=True)
            panel.plugin = None
            return
        self._set_message(
            f"Plug-in '{cls.name}' opened -- Esc closes it, Tab switches panes")
        if errors:
            self._set_message(f"Plug-in '{cls.name}' opened "
                              f"({len(errors)} plug-in(s) failed to load)")

    # -- configuration ------------------------------------------------------
    def _config_menu(self) -> None:
        """Edit the app configuration or plug-in files from inside the app."""
        from . import config as config_mod
        from .plugins import plugin_dirs, user_plugin_dir

        choice = dialogs.menu(self.stdscr, "Configuration", [
            "Edit configuration (config.ini)",
            "Edit a plug-in file",
            "Open user plug-in folder in this pane",
            "Cancel",
        ])
        if choice is None or choice == 3:
            return

        local = LocalFileSystem()
        if choice == 0:
            path = config_mod.ensure_config()
            self._edit_local_file(local, path)
            self._set_message("Configuration saved -- reopen plug-ins to apply")
        elif choice == 1:
            files: list[str] = []
            for pdir in plugin_dirs():
                if not os.path.isdir(pdir):
                    continue
                for fn in sorted(os.listdir(pdir)):
                    if fn.endswith(".py") and not fn.startswith("_"):
                        files.append(os.path.join(pdir, fn))
            if not files:
                dialogs.message(self.stdscr, "Plug-ins", "No plug-in files found.")
                return
            labels = [f"{os.path.basename(f)}  ({os.path.dirname(f)})"[:56]
                      for f in files]
            pick = dialogs.menu(self.stdscr, "Edit plug-in", labels + ["Cancel"])
            if pick is None or pick == len(files):
                return
            self._edit_local_file(local, files[pick])
            self._set_message("Plug-in saved -- reopen it to apply changes")
        elif choice == 2:
            udir = user_plugin_dir()
            os.makedirs(udir, exist_ok=True)
            panel = self.active
            if not isinstance(panel.fs, LocalFileSystem):
                panel.fs = local
                self._backends.append(local)
            panel.chdir(udir)
            self._set_message(
                "User plug-in folder -- drop .py files here to add plug-ins")

    def _edit_local_file(self, fs: LocalFileSystem, path: str) -> None:
        try:
            editor = Editor(fs, path)
            editor.run(self.stdscr)
        except Exception as exc:
            dialogs.message(self.stdscr, "Edit error", str(exc), error=True)
        curses.curs_set(0)

    # -- mouse ------------------------------------------------------------
    def _handle_mouse(self) -> None:
        try:
            _id, mx, my, _z, bstate = curses.getmouse()
        except curses.error:
            return

        target = None
        for panel, y, x, h, w in self._panel_boxes:
            if x <= mx < x + w and y <= my < y + h:
                target = (panel, y, x, h, w)
                break
        if target is None:
            return
        panel, y, x, h, w = target
        body_h = h - 3
        self.active = panel  # clicking a pane focuses it

        # Mouse wheel scrolls the pane under the pointer.
        wheel_up = getattr(curses, "BUTTON4_PRESSED", 0)
        wheel_down = getattr(curses, "BUTTON5_PRESSED", 0)

        # A pane owned by a plugin: focus it, and let the wheel scroll its
        # output; clicks otherwise stay with the plugin's own key handling.
        if panel.plugin is not None:
            try:
                if wheel_up and bstate & wheel_up:
                    panel.plugin.handle_key(curses.KEY_PPAGE)
                elif wheel_down and bstate & wheel_down:
                    panel.plugin.handle_key(curses.KEY_NPAGE)
            except Exception:
                pass
            return
        if wheel_up and bstate & wheel_up:
            panel.move(-3)
            return
        if wheel_down and bstate & wheel_down:
            panel.move(3)
            return

        row = my - (y + 2)
        index = panel.top + row if 0 <= row < body_h else None
        on_entry = index is not None and index < len(panel.entries)

        if on_entry:
            panel.move_to(index)
            if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                self._activate_entry()
            elif bstate & (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED):
                self._context_menu()
        elif bstate & (curses.BUTTON3_CLICKED | curses.BUTTON3_PRESSED):
            # Right-click on the header/empty area: still offer the menu.
            self._context_menu()

    def _context_menu(self) -> None:
        panel = self.active
        entry = panel.current()
        name = entry.name if entry and entry.name != Panel.PARENT else None
        header = name or panel.fs.basename(panel.path) or panel.path

        labels = ["View", "Edit", "Copy to other pane", "Move to other pane",
                  "Rename", "Delete", "Tag / untag", "New directory",
                  "Open terminal here", "Cancel"]
        actions = ["view", "edit", "copy", "move", "rename", "delete",
                   "tag", "mkdir", "terminal", None]
        choice = dialogs.menu(self.stdscr, header[:40], labels)
        if choice is None:
            return
        action = actions[choice]
        if action == "view":
            self._view()
        elif action == "edit":
            self._edit()
        elif action == "copy":
            self._copy()
        elif action == "move":
            self._move()
        elif action == "rename":
            self._rename()
        elif action == "delete":
            self._delete()
        elif action == "tag":
            panel.toggle_select()
        elif action == "mkdir":
            self._mkdir()
        elif action == "terminal":
            self._open_terminal()

    def _rename(self) -> None:
        panel = self.active
        entry = panel.current()
        if entry is None or entry.name == Panel.PARENT:
            return
        new_name = dialogs.prompt(self.stdscr, "Rename", "New name:",
                                  default=entry.name)
        if not new_name or new_name == entry.name:
            return
        src = panel.fs.join(panel.path, entry.name)
        dst = panel.fs.join(panel.path, new_name)
        try:
            panel.fs.rename(src, dst)
            panel.refresh(keep_name=new_name)
            self._set_message(f"Renamed to {new_name}")
        except Exception as exc:
            dialogs.message(self.stdscr, "Rename error", str(exc), error=True)

    def _go_to_path(self) -> None:
        panel = self.active
        target = dialogs.prompt(self.stdscr, "Go to directory", "Path:",
                                default=panel.path)
        if not target:
            return
        if not panel.chdir(target):
            dialogs.message(self.stdscr, "Error",
                            f"Cannot open:\n{target}", error=True)

    def _sort_menu(self) -> None:
        options = ["Name", "Extension", "Size", "Modify time"]
        keys = ["name", "ext", "size", "mtime"]
        choice = dialogs.menu(self.stdscr, "Sort by", options)
        if choice is not None:
            self.active.set_sort(keys[choice])
            self._set_message(f"Sorted by {options[choice].lower()}")

    def _open_location(self) -> None:
        info = dialogs.connect_dialog(self.stdscr)
        if not info:
            return
        panel = self.active
        try:
            if info["scheme"] == "local":
                fs: FileSystem = LocalFileSystem()
            elif info["scheme"] == "sftp":
                fs = SFTPFileSystem(
                    host=info["host"], username=info["username"],
                    password=info.get("password"), port=info["port"],
                    key_filename=info.get("key_filename"),
                )
            elif info["scheme"] == "ssh":
                fs = SSHFileSystem(
                    host=info["host"], username=info["username"],
                    password=info.get("password"), port=info["port"],
                    key_filename=info.get("key_filename"),
                )
            else:
                fs = FTPFileSystem(
                    host=info["host"], username=info["username"],
                    password=info.get("password", ""), port=info["port"],
                )
        except FileSystemError as exc:
            dialogs.message(self.stdscr, "Connection failed", str(exc), error=True)
            return
        except Exception as exc:
            dialogs.message(self.stdscr, "Connection failed",
                            f"{type(exc).__name__}: {exc}", error=True)
            return

        self._backends.append(fs)
        # Replace the panel's filesystem and jump to its home directory.
        panel.fs = fs
        panel.path = fs.normpath(fs.home())
        panel.cursor = 0
        panel.top = 0
        panel.clear_selection()
        panel.refresh()
        self._set_message(f"Connected: {fs.label()}")

    def _view(self) -> None:
        panel = self.active
        target = panel.current_path()
        if not target:
            return
        entry = panel.current()
        if entry and entry.is_dir:
            panel.enter()
            return
        try:
            viewer = Viewer(panel.fs, target)
            viewer.run(self.stdscr)
        except Exception as exc:
            dialogs.message(self.stdscr, "View error", str(exc), error=True)
        curses.curs_set(0)

    def _edit(self) -> None:
        panel = self.active
        entry = panel.current()
        if entry and entry.is_dir:
            self._set_message("Cannot edit a directory")
            return
        target = panel.current_path()
        if not target:
            # Offer to create a new file.
            name = dialogs.prompt(self.stdscr, "Edit new file", "File name:")
            if not name:
                return
            target = panel.fs.join(panel.path, name)
        try:
            editor = Editor(panel.fs, target)
            editor.run(self.stdscr)
        except Exception as exc:
            dialogs.message(self.stdscr, "Edit error", str(exc), error=True)
        curses.curs_set(0)
        panel.refresh()

    def _copy(self) -> None:
        self._transfer(move=False)

    def _move(self) -> None:
        self._transfer(move=True)

    def _transfer(self, move: bool) -> None:
        src_panel = self.active
        dst_panel = self.other
        sources = src_panel.selected_entries()
        if not sources:
            return
        verb = "Move" if move else "Copy"

        # Ask for the destination directory, defaulting to the other pane.
        default_dest = dst_panel.path
        if len(sources) == 1:
            prompt_label = f"{verb} '{sources[0].name}' to:"
        else:
            prompt_label = f"{verb} {len(sources)} items to:"
        dest = dialogs.prompt(self.stdscr, f"{verb}", prompt_label,
                              default=default_dest)
        if dest is None:
            return
        dst_fs = dst_panel.fs
        dest = dst_fs.normpath(dest)

        # Pre-count for an overall byte total across all sources.
        try:
            grand_total = 0
            file_total = 0
            for s in sources:
                fc, bc = count_tree(src_panel.fs, src_panel.fs.join(src_panel.path, s.name))
                file_total += fc
                grand_total += bc
        except Exception:
            grand_total = 0

        dlg = dialogs.ProgressDialog(self.stdscr, verb)
        moved_bytes = [0]

        def progress(cur: int, total: int, label: str) -> None:
            dlg.update(cur, total, label)

        cancelled = False
        errors: list[str] = []
        try:
            for i, entry in enumerate(sources):
                if dlg.cancelled():
                    cancelled = True
                    break
                src = src_panel.fs.join(src_panel.path, entry.name)
                target = dst_fs.join(dest, entry.name)
                dlg.set_overall(f"{verb} {i + 1}/{len(sources)}: {entry.name}")

                # Guard against copying a directory into itself.
                if src_panel.fs.same_fs(dst_fs) and \
                        dst_fs.normpath(target) == src_panel.fs.normpath(src):
                    errors.append(f"{entry.name}: source and target are the same")
                    continue
                try:
                    if move:
                        move_path(src_panel.fs, src, dst_fs, target,
                                  progress, dlg.cancelled)
                    else:
                        copy_path(src_panel.fs, src, dst_fs, target,
                                  progress, dlg.cancelled)
                except OperationCancelled:
                    cancelled = True
                    break
                except Exception as exc:
                    errors.append(f"{entry.name}: {exc}")
        finally:
            dlg.close()

        src_panel.clear_selection()
        src_panel.refresh()
        dst_panel.refresh()

        if errors:
            dialogs.message(self.stdscr, f"{verb} errors",
                            "\n".join(errors[:8]), error=True)
        elif cancelled:
            self._set_message(f"{verb} cancelled")
        else:
            self._set_message(f"{verb} complete: {len(sources)} item(s)")

    def _mkdir(self) -> None:
        panel = self.active
        name = dialogs.prompt(self.stdscr, "Make directory", "Directory name:")
        if not name:
            return
        target = panel.fs.join(panel.path, name)
        try:
            panel.fs.makedirs(target)
            panel.refresh(keep_name=name.split(panel.fs.sep)[0].split("/")[0])
            self._set_message(f"Created {name}")
        except Exception as exc:
            dialogs.message(self.stdscr, "Mkdir error", str(exc), error=True)

    def _delete(self) -> None:
        panel = self.active
        targets = panel.selected_entries()
        if not targets:
            return
        if len(targets) == 1:
            text = f"Delete '{targets[0].name}'?"
        else:
            text = f"Delete {len(targets)} selected items?"
        has_dir = any(t.is_dir for t in targets)
        if has_dir:
            text += "\n(directories are removed recursively)"
        if not dialogs.confirm(self.stdscr, "Delete", text):
            return

        errors: list[str] = []
        dlg = dialogs.ProgressDialog(self.stdscr, "Delete")
        try:
            for i, entry in enumerate(targets):
                if dlg.cancelled():
                    break
                dlg.set_overall(f"Deleting {i + 1}/{len(targets)}: {entry.name}")
                dlg.update(i, len(targets), entry.name)
                target = panel.fs.join(panel.path, entry.name)
                try:
                    panel.fs.delete_tree(target)
                except Exception as exc:
                    errors.append(f"{entry.name}: {exc}")
        finally:
            dlg.close()

        panel.clear_selection()
        panel.refresh()
        if errors:
            dialogs.message(self.stdscr, "Delete errors",
                            "\n".join(errors[:8]), error=True)
        else:
            self._set_message(f"Deleted {len(targets)} item(s)")

    def _sync(self) -> None:
        left, right = self.left, self.right
        self._set_message("Scanning for differences ...")
        self.draw()
        try:
            plan = build_sync_plan(left.fs, left.path, right.fs, right.path)
        except Exception as exc:
            dialogs.message(self.stdscr, "Sync error", str(exc), error=True)
            return

        if not plan:
            dialogs.message(self.stdscr, "Synchronize",
                            "The two directories are already in sync.")
            self._set_message("Already in sync")
            return

        # Build a preview.
        lines = [
            f"Left : {left.fs.label()}:{left.path}",
            f"Right: {right.fs.label()}:{right.path}",
            "",
            f"{len(plan.actions)} file(s), {human_size(plan.total_bytes).strip()} to copy:",
            "",
        ]
        for action in plan.actions[:10]:
            lines.append(action.render()[:60])
        if len(plan.actions) > 10:
            lines.append(f" ... and {len(plan.actions) - 10} more")
        lines.append("")
        lines.append("'->' copy left->right   '<-' copy right->left")

        if not dialogs.confirm(self.stdscr, "Synchronize panes",
                               "\n".join(lines), default_yes=True):
            self._set_message("Sync cancelled")
            return

        dlg = dialogs.ProgressDialog(self.stdscr, "Synchronize")

        def on_action(action, index, total):
            dlg.set_overall(f"{index + 1}/{total}  {action.direction} {action.rel}")

        try:
            copied = execute_sync_plan(
                plan, left.fs, right.fs,
                progress=dlg.update, cancel=dlg.cancelled, on_action=on_action,
            )
        except Exception as exc:
            dlg.close()
            dialogs.message(self.stdscr, "Sync error", str(exc), error=True)
            return
        finally:
            dlg.close()

        left.refresh()
        right.refresh()
        self._set_message(f"Synchronized: {copied} file(s) copied")

    def _help(self) -> None:
        text = (
            "Martin Commander -- two-pane terminal file manager\n"
            "\n"
            "  Tab            switch active pane\n"
            "  Up/Down j/k    move cursor      PgUp/PgDn  page\n"
            "  Home/End       first / last\n"
            "  Enter / Right  enter dir / view file\n"
            "  Backspace/Left parent directory\n"
            "  Insert / Space tag file    +/-  tag all / untag all\n"
            "  Ctrl-U         swap panes   Ctrl-R  reload panes\n"
            "  Ctrl-G         go to path   Ctrl-T  sort order\n"
            "  .              show/hide hidden files (this pane)\n"
            "  t / !          open a terminal in the current directory\n"
            "  p / F11        plug-in mode: run a plug-in in this pane\n"
            "  C              configuration: edit config.ini / plug-ins\n"
            "\n"
            "  Function keys -- each also has digit and letter aliases,\n"
            "  for terminals that swallow the F-keys:\n"
            "  F1/1/?  Help      F2/2/o  Open/Connect   F3/3/v  View\n"
            "  F4/4/e  Edit      F5/5/c  Copy           F6/6/m  Move\n"
            "  F7/7    Mkdir     F8/8/d  Delete          F9/9/s  Sync\n"
            "  F10/0/q Quit\n"
            "\n"
            "  Mouse: click to select, double-click to open, wheel to\n"
            "  scroll, right-click for a context menu of actions.\n"
            "\n"
            "F2 connects to SFTP, SSH (shell) or FTP; copy/move/sync\n"
            "work across local and remote panes alike. F9 makes both\n"
            "panes hold the newest version of every file."
        )
        dialogs.message(self.stdscr, "Help", text)

    def _close_backends(self) -> None:
        for panel in (self.left, self.right):
            if panel.plugin is not None:
                try:
                    panel.plugin.on_exit()
                except Exception:
                    pass
                panel.plugin = None
        for fs in self._backends:
            try:
                fs.close()
            except Exception:
                pass


def _main(stdscr, args) -> None:
    curses.use_default_colors()
    if curses.has_colors():
        curses.start_color()
        try:
            curses.init_pair(1, curses.COLOR_YELLOW, -1)  # tagged files
        except curses.error:
            pass
    stdscr.keypad(True)
    # Raw mode: without it the tty's XON/XOFF flow control eats Ctrl-S (the
    # editor's save key!) and Ctrl-Q, and Ctrl-S can freeze the display.
    # Ctrl-C consequently arrives as key 3, handled as quit in handle_key.
    curses.raw()
    # Enable mouse reporting (clicks, wheel, right-click menus). Harmless on
    # terminals that do not support it -- the mask simply stays empty.
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(200)  # ms window for double-click detection
    except curses.error:
        pass
    app = App(stdscr, left_path=args.left, right_path=args.right)
    app.run()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="martin-commander",
        description="A two-pane terminal file manager (Midnight Commander style) "
                    "with local + SFTP/FTP browsing, copy/move/sync, viewer and editor.",
    )
    parser.add_argument("left", nargs="?", default=None,
                        help="starting directory for the left pane")
    parser.add_argument("right", nargs="?", default=None,
                        help="starting directory for the right pane")
    args = parser.parse_args(argv)

    try:
        curses.wrapper(_main, args)
    except KeyboardInterrupt:
        pass
    return 0
