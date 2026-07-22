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

from . import dialogs
from .editor import Editor
from .filesystems import (
    FileSystem,
    FileSystemError,
    FTPFileSystem,
    LocalFileSystem,
    SFTPFileSystem,
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
        self.message = "F1 Help   Tab switch pane   F9 Sync   F10 Quit"
        self.running = True

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

        self._draw_panel(self.left, 0, 0, panel_h, left_w,
                         active=self.active is self.left)
        self._draw_panel(self.right, 0, left_w, panel_h, right_w,
                         active=self.active is self.right)

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
        elif key in (curses.KEY_F1, ord("?")):
            self._help()
        elif key == curses.KEY_F2:
            self._open_location()
        elif key == curses.KEY_F3:
            self._view()
        elif key == curses.KEY_F4:
            self._edit()
        elif key == curses.KEY_F5:
            self._copy()
        elif key == curses.KEY_F6:
            self._move()
        elif key == curses.KEY_F7:
            self._mkdir()
        elif key in (curses.KEY_F8, curses.KEY_DC):
            self._delete()
        elif key == curses.KEY_F9:
            self._sync()
        elif key in (curses.KEY_F10, 27):
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
            "\n"
            "  F1 Help    F2 Open/Connect   F3 View   F4 Edit\n"
            "  F5 Copy    F6 Move           F7 Mkdir  F8 Delete\n"
            "  F9 Sync    F10 Quit\n"
            "\n"
            "F2 connects to SFTP or FTP; copy/move/sync work across\n"
            "local and remote panes alike. F9 makes both panes hold\n"
            "the newest version of every file."
        )
        dialogs.message(self.stdscr, "Help", text)

    def _close_backends(self) -> None:
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
