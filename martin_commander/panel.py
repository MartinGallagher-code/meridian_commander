"""A single directory panel: listing, cursor, selection and scrolling.

A panel owns a :class:`~martin_commander.filesystems.FileSystem` and a current
directory within it.  It knows nothing about curses -- the application draws it
-- but it holds all the state a pane needs: the sorted entries, where the
highlight bar is, which items are tagged for a batch operation, and the scroll
offset.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .filesystems import DirEntry, FileSystem


@dataclass
class Panel:
    fs: FileSystem
    path: str
    entries: list[DirEntry] = field(default_factory=list)
    cursor: int = 0            # index into ``entries`` of the highlight bar
    top: int = 0               # index of the first visible row (scroll offset)
    selected: set[str] = field(default_factory=set)  # tagged entry names
    sort_key: str = "name"     # name | size | mtime | ext
    sort_reverse: bool = False
    show_hidden: bool = True    # whether dotfiles are listed
    error: str | None = None

    # ".." pseudo-entry so the user can always step up a directory.
    PARENT = ".."

    def __post_init__(self) -> None:
        self.refresh()

    # -- loading ----------------------------------------------------------
    def refresh(self, keep_name: str | None = None) -> None:
        """Reload the current directory, trying to keep the cursor in place."""
        target = keep_name or self.current_name()
        self.error = None
        try:
            entries = self.fs.listdir(self.path)
        except Exception as exc:
            entries = []
            self.error = str(exc)
        if not self.show_hidden:
            entries = [e for e in entries if not e.name.startswith(".")]
        self.entries = self._sorted(entries)
        # Drop selections for entries that no longer exist.
        names = {e.name for e in self.entries}
        self.selected &= names
        self._restore_cursor(target)

    def _sorted(self, entries: list[DirEntry]) -> list[DirEntry]:
        parent = [] if self._at_root() else [DirEntry(name=self.PARENT, is_dir=True)]

        def key(e: DirEntry):
            if self.sort_key == "size":
                return (e.size or 0)
            if self.sort_key == "mtime":
                return (e.mtime or 0)
            if self.sort_key == "ext":
                name = e.name
                dot = name.rfind(".")
                return name[dot:].lower() if dot > 0 else ""
            return e.name.lower()

        dirs = sorted((e for e in entries if e.is_dir), key=key,
                      reverse=self.sort_reverse)
        files = sorted((e for e in entries if not e.is_dir), key=key,
                       reverse=self.sort_reverse)
        # Directories always sort above files, MC-style.
        return parent + dirs + files

    def _at_root(self) -> bool:
        return self.fs.parent(self.path) == self.path

    def _restore_cursor(self, name: str | None) -> None:
        self.cursor = 0
        if name:
            for i, e in enumerate(self.entries):
                if e.name == name:
                    self.cursor = i
                    break
        self._clamp()

    # -- current selection ------------------------------------------------
    def current(self) -> DirEntry | None:
        if 0 <= self.cursor < len(self.entries):
            return self.entries[self.cursor]
        return None

    def current_name(self) -> str | None:
        entry = self.current()
        return entry.name if entry else None

    def current_path(self) -> str | None:
        entry = self.current()
        if entry is None or entry.name == self.PARENT:
            return None
        return self.fs.join(self.path, entry.name)

    def selected_entries(self) -> list[DirEntry]:
        """Tagged entries, or the entry under the cursor if none are tagged."""
        if self.selected:
            return [e for e in self.entries
                    if e.name in self.selected and e.name != self.PARENT]
        entry = self.current()
        if entry and entry.name != self.PARENT:
            return [entry]
        return []

    # -- navigation -------------------------------------------------------
    def enter(self) -> bool:
        """Descend into the highlighted directory.  Returns True if we moved."""
        entry = self.current()
        if entry is None:
            return False
        if entry.name == self.PARENT:
            return self.go_parent()
        if entry.is_dir:
            child = self.fs.join(self.path, entry.name)
            return self.chdir(child)
        return False

    def go_parent(self) -> bool:
        parent = self.fs.parent(self.path)
        leaving = self.fs.basename(self.path)
        if parent == self.path:
            return False
        self.path = parent
        self.selected.clear()
        self.refresh(keep_name=leaving)
        return True

    def chdir(self, path: str, keep_name: str | None = None) -> bool:
        old = self.path
        self.path = self.fs.normpath(path)
        self.selected.clear()
        self.cursor = 0
        self.top = 0
        self.refresh(keep_name=keep_name)
        if self.error and not self.entries:
            # Failed to open; roll back so the user is not stranded.
            self.path = old
            self.refresh()
            return False
        return True

    def move(self, delta: int) -> None:
        self.cursor += delta
        self._clamp()

    def move_to(self, index: int) -> None:
        self.cursor = index
        self._clamp()

    def _clamp(self) -> None:
        if not self.entries:
            self.cursor = 0
            self.top = 0
            return
        self.cursor = max(0, min(self.cursor, len(self.entries) - 1))

    def ensure_visible(self, height: int) -> None:
        """Adjust the scroll offset so the cursor is within ``height`` rows."""
        if height <= 0:
            return
        if self.cursor < self.top:
            self.top = self.cursor
        elif self.cursor >= self.top + height:
            self.top = self.cursor - height + 1
        self.top = max(0, min(self.top, max(0, len(self.entries) - height)))

    # -- selection --------------------------------------------------------
    def toggle_select(self) -> None:
        entry = self.current()
        if entry is None or entry.name == self.PARENT:
            return
        if entry.name in self.selected:
            self.selected.discard(entry.name)
        else:
            self.selected.add(entry.name)

    def select_all(self) -> None:
        self.selected = {e.name for e in self.entries if e.name != self.PARENT}

    def clear_selection(self) -> None:
        self.selected.clear()

    def toggle_hidden(self) -> None:
        """Show or hide dotfiles in this pane, keeping the cursor in place."""
        keep = self.current_name()
        self.show_hidden = not self.show_hidden
        self.refresh(keep_name=keep)

    # -- sorting ----------------------------------------------------------
    def set_sort(self, key: str) -> None:
        if self.sort_key == key:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_key = key
            self.sort_reverse = False
        self.refresh()
