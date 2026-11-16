# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""A single directory panel: listing, cursor, selection and scrolling.

A panel owns a :class:`~meridian_commander.filesystems.FileSystem` and a current
directory within it.  It knows nothing about curses -- the application draws it
-- but it holds all the state a pane needs: the sorted entries, where the
highlight bar is, which items are tagged for a batch operation, and the scroll
offset.  :class:`Search` is the type-to-jump search over a panel's listing.

A panel can also hold a list of find results in place of its directory's
listing (:meth:`Panel.show_results`): the hits from the whole tree under the
directory, each named by its path below it.  :meth:`Panel.path_of` is how
every action turns a listed name into a path, so they all work on either.
"""

from __future__ import annotations

import bisect
import fnmatch
import re
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Optional

from .filesystems import DirEntry, FileSystem
from .usage import TreeSizer

#: One place a pane has been: the backend, the directory, and the entry the
#: cursor was on, so going back lands on it rather than at the top.
#: (A runtime alias, so ``Optional`` rather than ``|``: Python 3.9 has no
#: union operator for types.)
Place = tuple[FileSystem, str, Optional[str]]

#: How far back a pane remembers.  Walking down a tree records every step, so
#: this is a few sessions' wandering rather than a few jumps.
HISTORY_LIMIT = 100

#: The characters that make a filter a glob rather than a plain substring.
GLOB_CHARS = "*?["


def is_hidden(name: str) -> bool:
    """Whether ``name`` is a dotfile, or -- a find result -- is inside one.

    A hit's name is its path below the directory searched, so a dot at the
    start of any part of it hides it, as the directory it is in would be.
    """
    return name.startswith(".") or "/." in name


def name_matches(pattern: str, name: str) -> bool:
    """Whether ``name`` matches a filter or search ``pattern``.

    The viewers' smart case: a pattern in lowercase ignores case, and one
    with a capital in it means it.  Plain text matches anywhere in the name,
    as the search does; a pattern holding ``*``, ``?`` or ``[`` is a shell
    glob (``*.tsv``, ``run_?``), matched against the whole name.
    """
    fold = pattern == pattern.lower()
    subject = name.lower() if fold else name
    if any(char in pattern for char in GLOB_CHARS):
        return fnmatch.fnmatchcase(subject, pattern)
    return pattern in subject


_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> tuple:
    """Sort ``name`` with its runs of digits as numbers: run_2 < run_10.

    The rest compares case-folded, as the plain name order does.  The plain
    name comes last, so ``file01`` and ``file1`` -- equal as numbers -- always
    come out in the same order rather than in whatever order they were listed.
    """
    parts: list = _DIGITS.split(name.lower())
    # Text and numbers alternate, text first, so like always meets like.
    parts[1::2] = map(int, parts[1::2])
    return (parts, name)


@dataclass
class Panel:
    fs: FileSystem
    path: str
    #: What the pane shows: the listing, less what a filter hides.
    entries: list[DirEntry] = field(default_factory=list)
    #: The whole sorted listing, filter or none.  A filter only hides, so a
    #: tag on an entry it hides is still a tag, and a copy still takes it.
    listed: list[DirEntry] = field(default_factory=list)
    #: Only names matching this are shown (see :func:`name_matches`); empty
    #: shows everything.  One per pane, and cleared by leaving the directory.
    filter: str = ""
    cursor: int = 0            # index into ``entries`` of the highlight bar
    top: int = 0               # index of the first visible row (scroll offset)
    selected: set[str] = field(default_factory=set)  # tagged entry names
    sort_key: str = "name"     # name | natural | size | mtime | ext
    sort_reverse: bool = False
    show_hidden: bool = True    # whether dotfiles are listed
    error: str | None = None
    #: Why the last :meth:`chdir` or :meth:`set_location` stayed put, or None
    #: if it went.  :attr:`error` cannot say: the rollback lists the old place
    #: again, which clears it.
    failure: str | None = None
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
    #: Where this pane has been, oldest first, and -- after going back --
    #: where it can go forward to again, nearest first at the end.  Every
    #: change of location records one, on any backend: "=" pointing the pane
    #: elsewhere, F2 connecting it, Enter going into an archive.
    history: list[Place] = field(default_factory=list)
    future: list[Place] = field(default_factory=list)
    #: The hits of a find, listed in place of the directory (MC-026), or
    #: None for the directory's own listing.  Each is named by its path below
    #: :attr:`path`, the directory searched.  Held rather than listed, so the
    #: pane redraws, sorts and filters without asking the backend again;
    #: :meth:`recheck` asks.
    results: list[DirEntry] | None = None
    #: What the find looked for, and whether it stopped at its cap.
    results_pattern: str = ""
    results_truncated: bool = False

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
        if self.results is not None:
            # A list of find results is held, not listed: asking the backend
            # about every hit is Ctrl-R's job (recheck), not every redraw's.
            entries = list(self.results)
        else:
            try:
                entries = self.fs.listdir(self.path)
            except Exception as exc:
                entries = []
                self.error = str(exc)
        if not self.show_hidden:
            entries = [e for e in entries if not is_hidden(e.name)]
        self.listed = self._sorted(entries)
        self.entries = self._filtered()
        # Drop selections for entries that no longer exist -- not for those a
        # filter hides, which are there and still tagged.
        names = {e.name for e in self.listed}
        self.selected &= names
        self._restore_cursor(target, fallback)
        self.start_sizing()

    # -- filtering --------------------------------------------------------
    def _filtered(self) -> list[DirEntry]:
        if not self.filter:
            return list(self.listed)
        return [e for e in self.listed
                if e.name == self.PARENT or name_matches(self.filter, e.name)]

    def set_filter(self, pattern: str) -> None:
        """Show only the names matching ``pattern``; empty shows them all.

        The listing is already in memory, so this asks the backend nothing --
        a remote pane filters as fast as a local one.  The cursor lands on the
        first match, which is what was being looked for.
        """
        keep = self.current_name()
        self.filter = pattern
        self.entries = self._filtered()
        first = next((i for i, e in enumerate(self.entries)
                      if e.name != self.PARENT), 0)
        if pattern:
            self.cursor = first
            self._clamp()
        else:
            self._restore_cursor(keep, self.cursor)
        self.top = 0
        self.start_sizing()

    @property
    def hidden_count(self) -> int:
        """How many entries the filter is hiding."""
        return len(self.listed) - len(self.entries)

    @property
    def hidden_tagged(self) -> int:
        """How many tagged entries the filter is hiding."""
        shown = {e.name for e in self.entries}
        return sum(1 for name in self.selected if name not in shown)

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
        if not self.show_sizes or self.results is not None:
            # A list of find results is not measured: u refuses there, and
            # the setting waits for the directory to come back.
            return
        if self._sized_fs is not self.fs:
            # Another backend: identity, not type, exactly as same_fs compares
            # -- two connections to one server are still two connections, and
            # nothing measured through one describes the other.
            self.sizes.clear()
            self._sized_fs = self.fs
        wanted = [self.path_of(e) for e in self.entries
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
                top = max(top, self.sizes.get(self.path_of(entry), 0))
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
        if (entry.name == self.PARENT or not self.show_sizes
                or self.results is not None):
            return None
        path = self.path_of(entry)
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
        path = self.path_of(entry)
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
            if self.sort_key == "natural":
                return natural_key(e.name)
            if self.sort_key == "ext":
                name = e.name.rsplit("/", 1)[-1]    # a hit's own name
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
        # A list of find results always has "..", its way back to the
        # directory searched -- the root itself included.
        return self.results is None and self.fs.parent(self.path) == self.path

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
        return self.path_of(entry)

    def path_of(self, entry: DirEntry | str) -> str:
        """The full path of ``entry``, or of a listed name, in this pane.

        The one place a pane turns what it lists into a path, so that every
        action reaches the real file.  A name may be a path below
        :attr:`path`, with ``/`` between its parts as find reports one; it is
        split and joined in the backend's own terms rather than handed to
        ``join`` whole.  For a plain name in a directory this is the
        directory joined to the name, as it always was.
        """
        name = entry if isinstance(entry, str) else entry.name
        return self.fs.join(self.path, *name.split("/"))

    def selected_entries(self) -> list[DirEntry]:
        """Tagged entries, or the entry under the cursor if none are tagged."""
        if self.selected:
            return [e for e in self.listed
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
            return self.chdir(self.path_of(entry))
        return False

    def go_parent(self) -> bool:
        if self.leave_results():
            return True
        parent = self.fs.parent(self.path)
        leaving = self.fs.basename(self.path)
        if parent == self.path:
            return False
        self._left(self._here())
        self.path = parent
        self.selected.clear()
        self.filter = ""
        # An index into the directory being left means nothing in its parent,
        # so the cursor starts at the top; normally the name below finds it.
        self.cursor = 0
        self.refresh(keep_name=leaving)
        return True

    def resolve(self, path: str) -> str:
        """A typed path made absolute against *this pane*.

        Where else a relative path could be measured from, there is only one
        answer a file manager can give: the directory being looked at.  It was
        measured from the process's own working directory instead -- wherever
        the shell that started the application happened to be -- so "sub"
        typed into "Go to directory" looked for a sibling of that, missed the
        subdirectory sitting in the pane, and left the pane where it was.
        Every other typed name in the application is joined to its pane
        already; this is that rule, written once.
        """
        if path.startswith(self.fs.sep):
            return path
        return self.fs.join(self.path, path)

    def chdir(self, path: str, keep_name: str | None = None) -> bool:
        here = self._here()
        old = self.path
        self.path = self.fs.normpath(self.resolve(path))
        held = self._set_aside()
        self.selected.clear()
        self.filter = ""
        self.cursor = 0
        self.top = 0
        self.refresh(keep_name=keep_name)
        self.failure = self.error
        if self.error:
            # Failed to open; roll back so the user is not stranded.  The
            # listing is never empty on success (a readable directory still
            # carries the ".." entry), so the error alone is the signal.
            self.path = old
            self._put_back(held)
            self.refresh()
            return False
        if self.path != old:
            self._left(here)
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
        here = self._here()
        if not self._point(fs, path):
            return False
        if (self.fs, self.path) != here[:2]:
            self._left(here)
        return True

    def _point(self, fs: FileSystem, path: str,
               keep_name: str | None = None) -> bool:
        old_fs, old_path = self.fs, self.path
        held = self._set_aside()
        self.fs = fs
        self.path = fs.normpath(path)
        self.selected.clear()
        self.filter = ""
        self.cursor = 0
        self.top = 0
        self.refresh(keep_name=keep_name)
        self.failure = self.error
        if self.error:
            self.fs = old_fs
            self.path = old_path
            self._put_back(held)
            self.refresh()
            return False
        return True

    # -- a list of find results (MC-026) -------------------------------------
    def show_results(self, pattern: str, matches, truncated: bool = False
                     ) -> None:
        """List find's hits in place of the directory they were found under.

        ``matches`` are find's ``(relative path, DirEntry)`` pairs.  Each hit
        is listed under its relative path, so tagging, the filter, sorting
        and the search all work on it as on any name, and :meth:`path_of`
        takes it to the real file.  :attr:`path` stays the directory
        searched, and nothing is recorded in the history: the list is a view
        of that directory, not a place of its own.
        """
        self.results = [replace(entry, name=rel) for rel, entry in matches]
        self.results_pattern = pattern
        self.results_truncated = truncated
        self.selected.clear()
        self.filter = ""
        self.cursor = 0
        self.top = 0
        self.refresh()

    def leave_results(self) -> bool:
        """Show the directory searched again; False if this is not a list.

        The cursor lands on the way to the hit it was on: ``run_07`` for
        ``run_07/out/results.tsv``.
        """
        if self.results is None:
            return False
        name = self.current_name() or ""
        self.results = None
        self.selected.clear()
        self.filter = ""
        self.cursor = 0
        self.top = 0
        self.refresh(keep_name=name.split("/")[0])
        return True

    def _set_aside(self) -> tuple:
        """Leave a list of find results for somewhere new; what to put back.

        Every move -- :meth:`chdir`, :meth:`set_location`, going back or
        forward -- leaves the list, and one that fails comes back to it.
        """
        held = (self.results, self.results_pattern, self.results_truncated)
        self.results = None
        return held

    def _put_back(self, held: tuple) -> None:
        self.results, self.results_pattern, self.results_truncated = held

    def recheck(self, names: Iterable[str] | None = None) -> int:
        """Ask the backend about the hits again; how many have gone.

        The hits named in ``names`` and those inside them -- a directory
        deleted or moved takes what was found in it -- or, with no names,
        all of them.  Each directory that holds one is listed once -- the
        question find itself asked, so a hit comes back just as find
        described it, a link as a link -- rather than each hit asked about
        alone, which on a server would be a round trip apiece.  A hit no
        longer there, or in a directory that can no longer be listed, leaves
        the list.  The listing is not rebuilt here; :meth:`reload` does both.
        """
        hits = self.results
        if hits is None:
            return 0
        wanted = None if names is None else set(names)

        def asked(name: str) -> bool:
            parts = name.split("/")
            return wanted is None or any(
                "/".join(parts[:i]) in wanted for i in range(1, len(parts) + 1))

        by_parent: dict[str, list[int]] = {}
        for index, entry in enumerate(hits):
            if asked(entry.name):
                parent = entry.name.rpartition("/")[0]
                by_parent.setdefault(parent, []).append(index)
        fresh: dict[int, DirEntry | None] = {}
        for parent, indexes in by_parent.items():
            try:
                listed = {e.name: e for e in self.fs.listdir(
                    self.path_of(parent) if parent else self.path)}
            except Exception:
                listed = {}
            for index in indexes:
                name = hits[index].name
                found = listed.get(name.rpartition("/")[2])
                fresh[index] = None if found is None \
                    else replace(found, name=name)
        kept: list[DirEntry] = []
        for index, entry in enumerate(hits):
            now = fresh.get(index, entry)
            if now is not None:
                kept.append(now)
        self.results = kept
        return len(hits) - len(kept)

    def reload(self, names: Iterable[str] | None = None,
               keep_name: str | None = None) -> int:
        """Refresh after a change, asking again about the hits it touched.

        In a directory this is :meth:`refresh`.  In a list of find results
        the hits in ``names`` -- all of them when it is ``None`` -- are asked
        about first (:meth:`recheck`): those deleted or moved away leave the
        list, the rest show their new size, time or mode.  Returns how many
        left it.
        """
        gone = self.recheck(names) if self.results is not None else 0
        self.refresh(keep_name=keep_name)
        return gone

    def result_name(self, typed: str) -> str | None:
        """The name a path typed in a list of find results is listed by.

        Taken from the directory searched, as any typed name is; ``None``
        when it lands outside that directory, where the list has no name
        for it.
        """
        root = self.fs.normpath(self.path).rstrip(self.fs.sep) + self.fs.sep
        full = self.fs.normpath(self.resolve(typed))
        if not full.startswith(root):
            return None
        return full[len(root):].replace(self.fs.sep, "/")

    def add_result(self, name: str) -> bool:
        """List ``name``, made under the directory searched; True if it is there.

        Asked about like any other hit, so nothing that is not there is
        listed.  A list of find results only.
        """
        hits = self.results
        assert hits is not None
        if all(entry.name != name for entry in hits):
            hits.append(DirEntry(name=name, is_dir=False))
        self.recheck([name])
        return any(entry.name == name for entry in self.results or ())

    def rename_result(self, old: str, new: str) -> None:
        """The hit ``old`` is now called ``new``: list it under that name.

        What was found inside it, a directory, moves with it.  Anything
        listed as ``new`` already was replaced by the rename, so it goes; the
        hit is then asked about under its new name.
        """
        hits = self.results
        assert hits is not None
        inside = old + "/"
        self.results = [
            replace(entry, name=new + entry.name[len(old):])
            if entry.name == old or entry.name.startswith(inside) else entry
            for entry in hits if entry.name != new]
        self.recheck([new])

    @staticmethod
    def outermost(entries: list[DirEntry]) -> list[DirEntry]:
        """``entries`` less any that are inside another of them, a directory.

        Only a list of find results can hold both a directory and something
        in it.  What is inside goes with its directory -- copied, moved or
        deleted as part of it -- so taking it again on its own would copy
        it twice, or fail to find what had already gone.
        """
        dirs = {e.name for e in entries if e.is_dir and not e.is_symlink}

        def inside(name: str) -> bool:
            parts = name.split("/")
            return any("/".join(parts[:i]) in dirs
                       for i in range(1, len(parts)))

        return [e for e in entries if not inside(e.name)]

    # -- history ------------------------------------------------------------
    def _here(self) -> Place:
        # In a list of find results the place is the directory searched, and
        # the cursor goes back to the way to the hit it was on.
        name = self.current_name()
        return (self.fs, self.path, name.split("/")[0] if name else None)

    def _left(self, place: Place) -> None:
        """``place`` has just been left for somewhere new."""
        self.history.append(place)
        del self.history[:-HISTORY_LIMIT]
        # As in a browser: going somewhere new from the middle of the history
        # gives up the way forward.
        self.future.clear()

    def back(self, revive: Callable[[FileSystem], FileSystem | None]
             = lambda fs: fs) -> bool:
        """Return to the last place this pane left; ``False`` if none is left.

        A place that can no longer be opened -- a directory since deleted, a
        connection since dropped -- is passed over for the one before it, so
        going back means going somewhere that is still there.  ``revive``
        gets each place's backend first and answers the one to use, or
        ``None`` for a backend that is gone: it is how an archive that was
        closed after the pane left it is opened again.

        From a list of find results it is the directory searched: the list is
        a view of that directory, not a place of its own.
        """
        if self.leave_results():
            return True
        return self._travel(self.history, self.future, revive)

    def forward(self, revive: Callable[[FileSystem], FileSystem | None]
                = lambda fs: fs) -> bool:
        """Undo a :meth:`back`, on the same terms."""
        return self._travel(self.future, self.history, revive)

    def _travel(self, source: list[Place], other: list[Place],
                revive: Callable[[FileSystem], FileSystem | None]) -> bool:
        here = self._here()
        while source:
            fs, path, name = source.pop()
            live = revive(fs)
            if live is not None and self._point(live, path, keep_name=name):
                other.append(here)
                return True
        return False

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

    def select_matching(self, pattern: str, tag: bool = True,
                        files_only: bool = True) -> int:
        """Tag (or untag) every shown entry whose name matches ``pattern``.

        Midnight Commander's Select group and Unselect group.  The rule is
        :func:`name_matches`, as the filter's is: ``*.tsv`` is a glob, plain
        text matches anywhere in the name, both with smart case.  Only what
        the pane shows -- a filter's hidden names are left as they are -- and
        never ``..``.  ``files_only`` leaves directories out, so ``*.tsv``
        does not take a directory called ``old.tsv`` with it.  Returns how
        many entries matched.
        """
        names = {e.name for e in self.entries
                 if e.name != self.PARENT
                 and not (files_only and e.is_dir)
                 and name_matches(pattern, e.name)}
        if tag:
            self.selected |= names
        else:
            self.selected -= names
        return len(names)

    def invert_selection(self) -> None:
        """Tag what is untagged and untag what is tagged, over what is shown."""
        shown = {e.name for e in self.entries if e.name != self.PARENT}
        self.selected = (self.selected - shown) | (shown - self.selected)

    # -- searching --------------------------------------------------------
    def matches(self, pattern: str) -> list[int]:
        """The entries whose names contain ``pattern``, as listing indexes.

        Anywhere in the name, with the viewers' smart case: a pattern in
        lowercase ignores case, and one with a capital in it means it.  ".."
        is never a match: it is a way up rather than a name in the
        directory, and a pattern of "." would otherwise land on it first.
        One pass over the listing already in memory, so a remote pane costs
        no round trip.
        """
        if not pattern:
            return []
        fold = pattern == pattern.lower()
        return [index for index, entry in enumerate(self.entries)
                if entry.name != self.PARENT
                and pattern in (entry.name.lower() if fold else entry.name)]

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


class Search:
    """Type-to-jump over one pane's listing: the pattern, and where it is.

    The state of the search mode ``/`` turns on.  Each change of pattern sends
    the cursor to the first match counting from the top, and :meth:`step`
    walks to the next or previous one, wrapping round at either end.  Like
    the rest of this module it knows nothing about curses: the application
    feeds it keys and draws what it says.
    """

    def __init__(self, panel: Panel) -> None:
        self.panel = panel
        self.pattern = ""
        self._hits: list[int] = []
        # What _hits was found for.  The listing is compared by identity:
        # every refresh builds a new list, so a reload, a sort or the hidden
        # files toggle is noticed without walking it to find out.
        self._found_in: list | None = None
        self._found_for = ""

    def hits(self) -> list[int]:
        """The matching entries' indexes, found once per pattern and listing.

        Asked on every frame, for the footer's count, so it is worked out
        when the pattern or the listing changes and not again until then:
        a directory of tens of thousands of entries costs one pass per key,
        not one per frame.
        """
        entries = self.panel.entries
        if self._found_in is not entries or self._found_for != self.pattern:
            self._hits = self.panel.matches(self.pattern)
            self._found_in, self._found_for = entries, self.pattern
        return self._hits

    def type(self, char: str) -> None:
        """Add ``char`` to the pattern and go to the first match."""
        self.pattern += char
        self._first()

    def backspace(self) -> None:
        """Take the last character off and go to the first match again."""
        self.pattern = self.pattern[:-1]
        self._first()

    def _first(self) -> None:
        # With no match the cursor stays where the last one left it, so
        # Backspace finds the way back from a character too many.
        hits = self.hits()
        if hits:
            self.panel.move_to(hits[0])

    def step(self, direction: int) -> None:
        """Go to the next match (``direction`` 1) or the previous one (-1)."""
        hits = self.hits()
        if not hits:
            return
        cursor = self.panel.cursor
        if direction > 0:
            at = bisect.bisect_right(hits, cursor)
        else:
            at = bisect.bisect_left(hits, cursor) - 1
        # Past either end it comes round to the other, as the viewers' n/N do.
        self.panel.move_to(hits[at % len(hits)])

    def position(self) -> tuple[int, int]:
        """Which match the cursor is on, counting from 1, and how many."""
        hits = self.hits()
        return bisect.bisect_right(hits, self.panel.cursor), len(hits)
