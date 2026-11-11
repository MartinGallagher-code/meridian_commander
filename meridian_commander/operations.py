# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""File transfer operations that work across any pair of filesystems.

Because every location implements the :class:`~meridian_commander.filesystems.FileSystem`
interface, a single streaming copy routine handles local->local, local->remote,
remote->local and even remote->remote transfers.  Moves and recursive directory
copies are layered on top.

Every routine accepts an optional ``progress`` callback which is invoked as
``progress(current_bytes, total_bytes, label)`` so the UI can draw a progress
bar.  A ``cancel`` callable returning ``True`` aborts long operations between
chunks/files.

:func:`copy_path` and :func:`move_path` can also be asked to be careful, which
is what ``F5`` and ``F6`` ask for: to put each existing target to an
``on_conflict`` callback before replacing it, to give each copy its source's
permission bits (``keep_mode``), and to copy a symbolic link as a link
(``links``).  What they could not do exactly as asked goes into a
:class:`TransferReport`.  Left unasked they behave as they always have, which
is what the other callers -- sync above all -- rely on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .filesystems import CHUNK_SIZE, DirEntry, FileSystem, FileSystemError

ProgressCB = Callable[[int, int, str], None]
CancelCB = Callable[[], bool]

#: What an ``on_conflict`` callback answers for one existing target.
OVERWRITE = "overwrite"
SKIP = "skip"

#: ``on_conflict(target, source_entry, existing_entry)`` -> OVERWRITE or SKIP.
#: It raises :class:`OperationCancelled` to stop the whole transfer there.
ConflictCB = Callable[[str, DirEntry, DirEntry], str]


@dataclass
class TransferReport:
    """What a careful transfer could not do exactly as asked.

    Gathered rather than raised, because none of it is a failure of the item
    it happened to: the caller says it once, at the end, in one place.
    """

    #: Targets that were already there and were kept, because the answer
    #: was Skip.
    kept: list[str] = field(default_factory=list)
    #: Source links to directories the far side could not make -- left out.
    links_left: list[str] = field(default_factory=list)
    #: Source links to files the far side could not make -- copied as the
    #: file they point at.
    links_as_files: list[str] = field(default_factory=list)
    #: Targets whose permission bits could not be set.
    modes_lost: list[str] = field(default_factory=list)


@dataclass
class _Care:
    """How careful one :func:`copy_path` is being, and where it reports."""

    on_conflict: ConflictCB | None
    keep_mode: bool
    links: bool
    preserve_mtime: bool
    report: TransferReport


class OperationCancelled(Exception):
    """Raised when a caller-supplied cancel callback aborts an operation."""


class OperationRefused(Exception):
    """Raised for a transfer that must not be started at all."""


def same_place(a: FileSystem, b: FileSystem) -> bool:
    """Whether paths on ``a`` and ``b`` name files on the same filesystem.

    :meth:`FileSystem.same_fs` compares *identity*, which is the right question
    for "can this move be a rename?" and the wrong one here: each pane builds
    its own :class:`LocalFileSystem`, so two panes on this machine are two
    objects naming one disk.  Every guard written against ``same_fs`` was
    therefore off between the panes, which is where a transfer starts.

    Deliberately only the local case beyond identity.  Two connections to one
    server are the same place too, but "one server" would have to be inferred
    from a typed host name, and an FTP account rooted somewhere of its own
    would then be told its perfectly good copy is a copy onto itself.  A guard
    that misses is better here than one that refuses real work -- and the
    application hands both panes a single connection anyway, which identity
    already catches.
    """
    if a.same_fs(b):
        return True
    return getattr(a, "scheme", "") == getattr(b, "scheme", "") == "local"


def overlapping(src_fs: FileSystem, src: str,
                dst_fs: FileSystem, dst: str) -> bool:
    """Whether ``dst`` is ``src`` itself, or a path inside it.

    Both readings destroy something.  A file copied onto itself is *emptied*:
    the destination is opened for writing, which truncates it, and the read
    that follows then finds nothing to copy.  A directory copied into its own
    subtree never finishes: the walk keeps finding the copy it is making, one
    level deeper each time, until the disk is full.

    Paths are compared as written (normalised, not resolved), so a symlink
    pointing back into the source is not caught -- that would mean resolving a
    path through a backend that may have no way to do it.
    """
    if not same_place(src_fs, dst_fs):
        return False
    source = src_fs.normpath(src)
    target = dst_fs.normpath(dst)
    if source == target:
        return True
    return target.startswith(source.rstrip(dst_fs.sep) + dst_fs.sep)


def _noop_progress(current: int, total: int, label: str) -> None:
    pass


def _noop_cancel() -> bool:
    return False


def copy_file(
    src_fs: FileSystem,
    src: str,
    dst_fs: FileSystem,
    dst: str,
    progress: ProgressCB = _noop_progress,
    cancel: CancelCB = _noop_cancel,
    total_override: int | None = None,
    preserve_mtime: bool = False,
) -> None:
    """Stream a single file from ``src`` to ``dst``.

    The destination's parent directory is created if necessary.  When
    ``preserve_mtime`` is set, the destination is stamped with the source
    file's modification time after the copy, so the two stay identical in age
    (this is what keeps a synchronized pair from drifting on the next run).
    """
    if overlapping(src_fs, src, dst_fs, dst):
        raise OperationRefused(f"source and target are the same: {src}")

    parent = dst_fs.dirname(dst)
    if parent and not dst_fs.exists(parent):
        dst_fs.makedirs(parent)

    src_mtime: float | None = None
    total = total_override
    if total is None or preserve_mtime:
        try:
            st = src_fs.stat(src)
            total = st.size if total is None else total
            src_mtime = st.mtime
        except Exception:
            total = total or 0
    if total is None:
        total = 0

    label = dst_fs.basename(dst)
    done = 0
    reader = src_fs.open_read(src)
    try:
        writer = dst_fs.open_write(dst)
        try:
            while True:
                if cancel():
                    raise OperationCancelled()
                chunk = reader.read(CHUNK_SIZE)
                if not chunk:
                    break
                writer.write(chunk)
                done += len(chunk)
                progress(done, total, label)
        finally:
            writer.close()
    except OperationCancelled:
        # A file cut short is not a copy of anything: leave no half of one
        # behind to be taken for the real thing.
        try:
            dst_fs.remove(dst)
        except Exception:
            pass
        raise
    finally:
        try:
            reader.close()
        except Exception:
            pass

    if preserve_mtime and src_mtime is not None:
        try:
            dst_fs.utime(dst, src_mtime)
        except Exception:
            # Best-effort: a backend that cannot set times still copied fine.
            pass

    progress(max(done, total), total, label)


def _iter_tree(fs: FileSystem, root: str):
    """Yield ``(relpath, DirEntry)`` for every node under ``root`` (dirs first).

    ``relpath`` is relative to ``root`` and uses the source filesystem's
    separator; the root itself is yielded with an empty relpath.
    """
    stack = [("", fs.stat(root))]
    while stack:
        rel, entry = stack.pop()
        yield rel, entry
        if entry.is_dir and not entry.is_symlink:
            abspath = root if rel == "" else fs.join(root, rel)
            for child in sorted(fs.listdir(abspath), key=lambda e: e.name):
                child_rel = child.name if rel == "" else fs.join(rel, child.name)
                stack.append((child_rel, child))


def copy_path(
    src_fs: FileSystem,
    src: str,
    dst_fs: FileSystem,
    dst: str,
    progress: ProgressCB = _noop_progress,
    cancel: CancelCB = _noop_cancel,
    preserve_mtime: bool = False,
    *,
    on_conflict: ConflictCB | None = None,
    keep_mode: bool = False,
    links: bool = False,
    report: TransferReport | None = None,
) -> list[str]:
    """Copy a file or a whole directory tree from ``src`` to ``dst``.

    ``preserve_mtime`` gives each copied file the same modification time as its
    source (see :func:`copy_file`).

    Returns the source paths that were **not** reproduced, so a move knows to
    keep its source: symlinked directories left behind, and -- with
    ``on_conflict`` -- items whose existing target was kept.

    A link to a directory is not a directory: :func:`_iter_tree` does not
    descend into one (that would copy the target twice, or for ever if it
    points at an ancestor), and it is not a file either.  With ``links`` and a
    far side that can make links, it is copied as what it is, a link with the
    same target.  Otherwise it is left behind and named, as it always was.

    ``on_conflict`` is put every target that already exists, file or link,
    before it is replaced; a directory that already exists is merged into, and
    its contents asked about one by one.  ``keep_mode`` gives each file, and
    each directory the copy made, its source's permission bits.  ``report``
    collects what could not be done exactly (see :class:`TransferReport`).
    """
    if overlapping(src_fs, src, dst_fs, dst):
        raise OperationRefused(
            f"source and target are the same: {src}"
            if src_fs.normpath(src) == dst_fs.normpath(dst)
            else f"cannot copy {src} into itself: {dst} is inside it")

    care = _Care(on_conflict, keep_mode, links, preserve_mtime,
                 report if report is not None else TransferReport())
    checking = on_conflict is not None
    entry = src_fs.stat(src)
    if entry.is_symlink and (links or entry.is_dir):
        # A link has no copy_file to make its directory for it.
        parent = dst_fs.dirname(dst)
        if links and parent and not dst_fs.exists(parent):
            dst_fs.makedirs(parent)
        done = _copy_link(src_fs, src, entry, dst_fs, dst, progress, cancel,
                          care, checking)
        return [] if done else [src]
    if not entry.is_dir:
        done = _copy_one(src_fs, src, entry, dst_fs, dst, progress, cancel,
                         care, checking)
        return [] if done else [src]

    # Directory: recreate the tree on the destination side.  ``fresh`` holds
    # the directories this copy made, so nothing beneath them can be in the
    # way -- the existence check, a round trip per file on a remote pane, is
    # only spent where it can find something.
    tracking = checking or keep_mode
    left: list[str] = []
    fresh: set[str] = set()
    made: list[tuple[str, int]] = []
    if tracking and not dst_fs.exists(dst):
        fresh.add("")
        made.append((dst, entry.mode))
    dst_fs.makedirs(dst)
    for rel, node in _iter_tree(src_fs, src):
        if cancel():
            raise OperationCancelled()
        if rel == "":
            continue
        s = src_fs.join(src, rel)
        # Translate the relative path into the destination's separator scheme.
        d = dst_fs.join(dst, *_split_rel(src_fs, rel))
        check = checking and src_fs.dirname(rel) not in fresh
        if node.is_symlink and (links or node.is_dir):
            if not _copy_link(src_fs, s, node, dst_fs, d, progress, cancel,
                              care, check):
                left.append(s)
        elif node.is_dir:
            if tracking and (src_fs.dirname(rel) in fresh
                             or not dst_fs.exists(d)):
                fresh.add(rel)
                made.append((d, node.mode))
            dst_fs.makedirs(d)
        elif not _copy_one(src_fs, s, node, dst_fs, d, progress, cancel,
                           care, check):
            left.append(s)
    if keep_mode:
        # Deepest first, and only once everything is in: a directory given a
        # read-only mode before its contents arrived would refuse them.
        for path, mode in reversed(made):
            _set_mode(dst_fs, path, mode, care.report)
    return left


def _existing(fs: FileSystem, path: str) -> DirEntry | None:
    """What is at ``path`` already, or ``None`` when nothing is."""
    try:
        return fs.stat(path)
    except Exception:
        return None


def _keeps(care: _Care, target: str, source: DirEntry,
           existing: DirEntry) -> bool:
    """Ask about an existing target; True when it is to be kept as it is."""
    assert care.on_conflict is not None
    if care.on_conflict(target, source, existing) == OVERWRITE:
        return False
    care.report.kept.append(target)
    return True


def _copy_one(src_fs: FileSystem, s: str, node: DirEntry, dst_fs: FileSystem,
              d: str, progress: ProgressCB, cancel: CancelCB, care: _Care,
              check: bool) -> bool:
    """Copy one file's contents; False when an existing target was kept."""
    if check:
        existing = _existing(dst_fs, d)
        if existing is not None and _keeps(care, d, node, existing):
            return False
    copy_file(src_fs, s, dst_fs, d, progress, cancel,
              preserve_mtime=care.preserve_mtime)
    if care.keep_mode:
        _set_mode(dst_fs, d, node.mode, care.report)
    return True


def _copy_link(src_fs: FileSystem, s: str, node: DirEntry, dst_fs: FileSystem,
               d: str, progress: ProgressCB, cancel: CancelCB, care: _Care,
               check: bool) -> bool:
    """Reproduce one symbolic link; False when it was left or kept out.

    A link the far side cannot make is still not lost: one to a file is
    copied as the file it points at, and one to a directory is left and named,
    and either way the report says which.
    """
    target = None
    if care.links and src_fs.can_symlink and dst_fs.can_symlink:
        try:
            target = src_fs.readlink(s)
        except Exception:
            target = None
    if target is None:
        if node.is_dir:
            care.report.links_left.append(s)
            return False
        care.report.links_as_files.append(s)
        return _copy_one(src_fs, s, node, dst_fs, d, progress, cancel, care,
                         check)
    if check:
        existing = _existing(dst_fs, d)
        if existing is not None:
            if _keeps(care, d, node, existing):
                return False
            if existing.is_dir and not existing.is_symlink:
                raise FileSystemError(
                    f"a directory is in the way of the link: {d}")
            dst_fs.remove(d)
    dst_fs.symlink(target, d)
    return True


def _set_mode(fs: FileSystem, path: str, mode: int,
              report: TransferReport) -> None:
    """Give ``path`` the permission bits of ``mode``, or say why not.

    A ``mode`` of 0 is a listing that did not say -- an FTP server that does
    not speak ``ls -l`` -- and there is then nothing to keep.
    """
    if not mode:
        return
    if not fs.can_chmod:
        report.modes_lost.append(path)
        return
    try:
        fs.chmod(path, mode & 0o777)
    except Exception:
        report.modes_lost.append(path)


def _split_rel(fs: FileSystem, rel: str) -> list[str]:
    """Break a relative path into its components, separator-agnostically."""
    return [p for p in rel.replace("\\", "/").split("/") if p]


def move_path(
    src_fs: FileSystem,
    src: str,
    dst_fs: FileSystem,
    dst: str,
    progress: ProgressCB = _noop_progress,
    cancel: CancelCB = _noop_cancel,
    deferred: list[str] | None = None,
    *,
    on_conflict: ConflictCB | None = None,
    keep_mode: bool = False,
    links: bool = False,
    report: TransferReport | None = None,
) -> list[str]:
    """Move a file or directory tree.

    When both sides are the same live filesystem this is a cheap rename;
    otherwise it is a copy followed by deleting the source.

    Returns what :func:`copy_path` did not reproduce.  A move across
    filesystems that could not reproduce everything does **not** delete the
    source: a symlink the copy could not make is one the delete would destroy,
    and a file whose existing target was kept was not copied at all.  Naming
    them rather than dropping them silently only means something because the
    source is still there to be dealt with.  A rename within one filesystem
    moves links intact and so has nothing to skip -- but it is asked about an
    existing file or link in its way, which a rename would otherwise replace
    without a word.

    ``deferred``, when given, collects the source instead of deleting it.
    Inside a :meth:`~FileSystem.batch` the destination may not hold the copy
    yet -- an archive writes it when the batch ends -- and a source deleted
    before then is lost if that write fails.  The caller deletes what was
    collected once the batch has closed without error.

    ``on_conflict``, ``keep_mode``, ``links`` and ``report`` are as for
    :func:`copy_path`.
    """
    if overlapping(src_fs, src, dst_fs, dst):
        raise OperationRefused(
            f"source and target are the same: {src}"
            if src_fs.normpath(src) == dst_fs.normpath(dst)
            else f"cannot move {src} into itself: {dst} is inside it")

    if report is None:
        report = TransferReport()
    if src_fs.same_fs(dst_fs):
        if on_conflict is not None:
            existing = _existing(dst_fs, dst)
            # A directory in the way is not asked about: a rename onto a
            # directory that holds anything fails by itself, and says why.
            if (existing is not None
                    and not (existing.is_dir and not existing.is_symlink)
                    and on_conflict(dst, src_fs.stat(src), existing) != OVERWRITE):
                report.kept.append(dst)
                return [src]
        parent = dst_fs.dirname(dst)
        if parent and not dst_fs.exists(parent):
            dst_fs.makedirs(parent)
        src_fs.rename(src, dst)
        return []

    skipped = copy_path(src_fs, src, dst_fs, dst, progress, cancel,
                        on_conflict=on_conflict, keep_mode=keep_mode,
                        links=links, report=report)
    if not skipped:
        if deferred is None:
            src_fs.delete_tree(src)
        else:
            deferred.append(src)
    return skipped


def copy_contents(
    src_fs: FileSystem,
    src: str,
    dst_fs: FileSystem,
    dst: str,
    progress: ProgressCB = _noop_progress,
    cancel: CancelCB = _noop_cancel,
    preserve_mtime: bool = False,
) -> list[str]:
    """Copy everything inside directory ``src`` into ``dst``, past failures.

    This is unpacking an archive, and the difference from :func:`copy_path`
    is what happens when one file will not come: it is named in the result,
    as ``"relative/path: reason"``, and the walk carries on.  An archive holds
    plenty that is not a file at all -- a tar's symlinks to places outside it,
    its device nodes, a zip's encrypted members -- and with ``copy_path`` the
    first of them ended the unpacking with everything after it left behind.

    Cancelling still stops the whole run: it raises
    :class:`OperationCancelled` as every other routine here does.
    """
    failed: list[str] = []
    dst_fs.makedirs(dst)
    for rel, node in _iter_tree(src_fs, src):
        if cancel():
            raise OperationCancelled()
        if rel == "":
            continue
        target = dst_fs.join(dst, *_split_rel(src_fs, rel))
        try:
            if node.is_dir and not node.is_symlink:
                dst_fs.makedirs(target)
            else:
                copy_file(src_fs, src_fs.join(src, rel), dst_fs, target,
                          progress, cancel, preserve_mtime=preserve_mtime)
        except OperationCancelled:
            raise
        except Exception as exc:
            failed.append(f"{rel}: {exc}")
    return failed


def count_tree(fs: FileSystem, path: str) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for a file or directory tree."""
    files = 0
    total = 0
    for _rel, node in _iter_tree(fs, path):
        if node.is_file:
            files += 1
            total += node.size or 0
    return files, total
