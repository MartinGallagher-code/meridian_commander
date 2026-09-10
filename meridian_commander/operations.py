"""File transfer operations that work across any pair of filesystems.

Because every location implements the :class:`~meridian_commander.filesystems.FileSystem`
interface, a single streaming copy routine handles local->local, local->remote,
remote->local and even remote->remote transfers.  Moves and recursive directory
copies are layered on top.

Every routine accepts an optional ``progress`` callback which is invoked as
``progress(current_bytes, total_bytes, label)`` so the UI can draw a progress
bar.  A ``cancel`` callable returning ``True`` aborts long operations between
chunks/files.
"""

from __future__ import annotations

from typing import Callable

from .filesystems import CHUNK_SIZE, FileSystem

ProgressCB = Callable[[int, int, str], None]
CancelCB = Callable[[], bool]


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
) -> list[str]:
    """Copy a file or a whole directory tree from ``src`` to ``dst``.

    ``preserve_mtime`` gives each copied file the same modification time as its
    source (see :func:`copy_file`).

    Returns the source paths of any **symlinked directories** that were left
    behind, so the caller can say so.  A link to a directory is not a
    directory: :func:`_iter_tree` does not descend into one (that would copy
    the target twice, or for ever if it points at an ancestor), and it is not a
    file either -- opening one for reading is an ``IsADirectoryError``, which
    is what used to come out of here and take the rest of the tree with it.
    Nor is it an empty directory, which is what recreating it would produce.
    There is no way to make a symlink through the filesystem interface, so the
    honest answer is to copy everything else and name what was skipped.
    """
    if overlapping(src_fs, src, dst_fs, dst):
        raise OperationRefused(
            f"source and target are the same: {src}"
            if src_fs.normpath(src) == dst_fs.normpath(dst)
            else f"cannot copy {src} into itself: {dst} is inside it")

    entry = src_fs.stat(src)
    if entry.is_dir and entry.is_symlink:
        return [src]
    if not entry.is_dir:
        copy_file(src_fs, src, dst_fs, dst, progress, cancel,
                  preserve_mtime=preserve_mtime)
        return []

    # Directory: recreate the tree on the destination side.
    skipped: list[str] = []
    dst_fs.makedirs(dst)
    for rel, node in _iter_tree(src_fs, src):
        if cancel():
            raise OperationCancelled()
        if rel == "":
            continue
        s = src_fs.join(src, rel)
        # Translate the relative path into the destination's separator scheme.
        d = dst_fs.join(dst, *_split_rel(src_fs, rel))
        if node.is_dir and node.is_symlink:
            skipped.append(s)
        elif node.is_dir:
            dst_fs.makedirs(d)
        else:
            copy_file(src_fs, s, dst_fs, d, progress, cancel,
                      preserve_mtime=preserve_mtime)
    return skipped


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
) -> list[str]:
    """Move a file or directory tree.

    When both sides are the same live filesystem this is a cheap rename;
    otherwise it is a copy followed by deleting the source.

    Returns what :func:`copy_path` skipped.  A move across filesystems that
    could not reproduce everything does **not** delete the source: a symlink
    the copy could not make is one the delete would destroy, and the point of
    naming it rather than dropping it silently is that it is still there to be
    dealt with.  A rename within one filesystem moves links intact and so has
    nothing to skip.
    """
    if overlapping(src_fs, src, dst_fs, dst):
        raise OperationRefused(
            f"source and target are the same: {src}"
            if src_fs.normpath(src) == dst_fs.normpath(dst)
            else f"cannot move {src} into itself: {dst} is inside it")

    if src_fs.same_fs(dst_fs):
        parent = dst_fs.dirname(dst)
        if parent and not dst_fs.exists(parent):
            dst_fs.makedirs(parent)
        src_fs.rename(src, dst)
        return []

    skipped = copy_path(src_fs, src, dst_fs, dst, progress, cancel)
    if not skipped:
        src_fs.delete_tree(src)
    return skipped


def count_tree(fs: FileSystem, path: str) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for a file or directory tree."""
    files = 0
    total = 0
    for _rel, node in _iter_tree(fs, path):
        if node.is_file:
            files += 1
            total += node.size or 0
    return files, total
