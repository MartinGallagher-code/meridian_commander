"""A single directory panel: listing, cursor, selection and scrolling.

A panel owns a :class:`~meridian_commander.filesystems.FileSystem` and a current
directory within it.  It knows nothing about curses -- the application draws it
-- but it holds all the state a pane needs: the sorted entries, where the
highlight bar is, which items are tagged for a batch operation, and the scroll
offset.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .filesystems import DirEntry, FileSystem
from .usage import TreeSizer


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
    plugin: object | None = None  # active pane plugin, or None for the listing
    #: Whether subdirectories show what is under them rather than "<DIR>".
    show_sizes: bool = False
    #: Totals by full path, kept across refreshes and directory changes so
    #: walking back up a tree you have already measured costs nothing.  The
    #: figures are a snapshot, which is why Ctrl-R (reload) drops them.
    sizes: dict[str, int] = field(default_factory=dict)
    #: The walk in progress, or None when nothing is being counted.
    sizer: object | None = None
    #: The backend :attr:`sizes` was measured against.  Paths are not unique
    #: across connections -- /etc on this machine and /etc on a server are
    #: different directories with the same name -- so the totals have to be
    #: dropped when the pane is pointed at another backend, or the remote
    #: listing would quietly show the local machine's figures.
    _sized_fs: object | None = None
    #: The largest entry in the listing, kept as a running maximum rather than
    #: rescanned per frame: this is asked on every draw and the listing can
    #: hold tens of thousands of entries, while the answer only moves when a
    #: total does.
    _scale: int = 0

    # ".." pseudo-entry so the user can always step up a directory.
    PARENT = ".."

    def __post_init__(self) -> None:
        self.refresh()

    # -- loading ----------------------------------------------------------
    def refresh(self, keep_name: str | None = None) -> None:
        """Reload the current directory, trying to keep the cursor in place.

        The cursor follows ``keep_name`` -- or whatever it was sitting on --
        by name.  When that entry has gone, deleted or renamed or filtered out
        from under it, the cursor holds its *position* rather than springing
        back to the top of the listing: after removing something you are almost
        always still interested in what was around it, and hunting your way back
        down a long directory is a poor reward for a deletion that worked.
        """
        target = keep_name or self.current_name()
        fallback = self.cursor
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
        self._restore_cursor(target, fallback)
        self.start_sizing()

    # -- directory sizes --------------------------------------------------
    def toggle_sizes(self) -> bool:
        """Turn the subdirectory sizes on or off; returns the new state."""
        self.show_sizes = not self.show_sizes
        if self.show_sizes:
            self.start_sizing()
        else:
            self.sizer = None
        return self.show_sizes

    def forget_sizes(self) -> None:
        """Drop what has been measured, so a reload measures again."""
        self.sizes.clear()
        self.sizer = None
        self.start_sizing()

    def start_sizing(self) -> None:
        """Begin measuring the subdirectories that have no total yet."""
        self.sizer = None
        if not self.show_sizes:
            return
        if self._sized_fs is not self.fs:
            # Another backend: identity, not type, exactly as same_fs compares
            # -- two connections to one server are still two connections, and
            # nothing measured through one describes the other.
            self.sizes.clear()
            self._sized_fs = self.fs
        wanted = [self.fs.join(self.path, e.name) for e in self.entries
                  if e.is_dir and e.name != self.PARENT and not e.is_symlink]
        pending = [path for path in wanted if path not in self.sizes]
        if pending:
            self.sizer = TreeSizer(self.fs, pending)
        self._rescale()

    def _rescale(self) -> None:
        """Recompute the bars' full scale from the listing, once."""
        top = 0
        for entry in self.entries:
            if entry.is_dir:
                top = max(top, self.sizes.get(
                    self.fs.join(self.path, entry.name), 0))
            else:
                top = max(top, entry.size or 0)
        self._scale = top

    def step_sizing(self) -> bool:
        """Count a little more.  True while there is more to count.

        The finished totals are moved into :attr:`sizes` as each one lands, so
        a directory measured while you were looking at it stays measured when
        you come back to it.
        """
        sizer = self.sizer
        if sizer is None:
            return False
        more = sizer.step()
        for path in sizer.done:
            self.sizes[path] = sizer.totals[path]
        if sizer.totals:
            # Totals only grow, so the scale can be carried forward rather
            # than found again over every entry in the listing.
            self._scale = max(self._scale, max(sizer.totals.values()))
        if not more:
            self.sizer = None
        return more

    def size_of(self, entry: DirEntry) -> int | None:
        """What to show in the Size column, or ``None`` for "not known yet".

        A file is its own size, always.  A directory is the total underneath
        it once the sizes are on: the finished figure, the running one while
        it is being counted, and ``None`` before counting has reached it.
        """
        if not entry.is_dir:
            return entry.size or 0
        if entry.name == self.PARENT or not self.show_sizes:
            return None
        path = self.fs.join(self.path, entry.name)
        if path in self.sizes:
            return self.sizes[path]
        sizer = self.sizer
        if sizer is not None and path in sizer.totals:
            return sizer.totals[path]
        return None

    def sizing(self, entry: DirEntry) -> bool:
        """Whether ``entry``'s total is still being counted."""
        if not entry.is_dir or self.sizer is None:
            return False
        path = self.fs.join(self.path, entry.name)
        return path in self.sizer.totals and path not in self.sizes

    def biggest(self) -> int:
        """The largest entry in the listing, as the bars' full scale."""
        return self._scale

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

    def _restore_cursor(self, name: str | None, fallback: int = 0) -> None:
        self.cursor = fallback
        if name:
            for i, e in enumerate(self.entries):
                if e.name == name:
                    self.cursor = i
                    break
        self._clamp()

    def name_after_removing(self, doomed: set[str]) -> str | None:
        """Where the cursor should land if ``doomed`` were to disappear.

        The nearest entry that is not on the list: down first, so the highlight
        settles on whatever followed the removed block, then back up for the
        case where the tail of the listing went with it.  Returning a *name*
        rather than an index is what makes it survive a delete that only partly
        worked -- the entries that were not removed have not moved either.
        """
        below = range(self.cursor, len(self.entries))
        above = range(self.cursor - 1, -1, -1)
        for i in list(below) + list(above):
            if self.entries[i].name not in doomed:
                return self.entries[i].name
        return None

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
        # An index into the directory being left means nothing in its parent,
        # so the cursor starts at the top; normally the name below finds it.
        self.cursor = 0
        self.refresh(keep_name=leaving)
        return True

    def chdir(self, path: str, keep_name: str | None = None) -> bool:
        old = self.path
        self.path = self.fs.normpath(path)
        self.selected.clear()
        self.cursor = 0
        self.top = 0
        self.refresh(keep_name=keep_name)
        if self.error:
            # Failed to open; roll back so the user is not stranded.  The
            # listing is never empty on success (a readable directory still
            # carries the ".." entry), so the error alone is the signal.
            self.path = old
            self.refresh()
            return False
        return True

    def go_home(self) -> bool:
        """Jump to the home directory of *this pane's* filesystem.

        For a remote pane that is the remote account's home, not the local
        one, so the key does the obvious thing on either side.
        """
        return self.chdir(self.fs.home())

    def set_location(self, fs: FileSystem, path: str) -> bool:
        """Point this panel at ``path`` on ``fs``, which may be another backend.

        Passing a live :class:`FileSystem` (rather than connection details)
        lets two panes share one connection, so a remote location is never
        dialled -- or authenticated -- twice.  As with :meth:`chdir` the
        previous location is restored if the new one cannot be listed.
        """
        old_fs, old_path = self.fs, self.path
        self.fs = fs
        self.path = fs.normpath(path)
        self.selected.clear()
        self.cursor = 0
        self.top = 0
        self.refresh()
        if self.error:
            self.fs = old_fs
            self.path = old_path
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
