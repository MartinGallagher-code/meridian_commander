"""File transfer operations that work across any pair of filesystems.

Because every location implements the :class:`~martin_commander.filesystems.FileSystem`
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

from .filesystems import CHUNK_SIZE, DirEntry, FileSystem

ProgressCB = Callable[[int, int, str], None]
CancelCB = Callable[[], bool]


class OperationCancelled(Exception):
    """Raised when a caller-supplied cancel callback aborts an operation."""


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
) -> None:
    """Stream a single file from ``src`` to ``dst``.

    The destination's parent directory is created if necessary.
    """
    parent = dst_fs.dirname(dst)
    if parent and not dst_fs.exists(parent):
        dst_fs.makedirs(parent)

    try:
        total = total_override if total_override is not None else src_fs.stat(src).size
    except Exception:
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
) -> None:
    """Copy a file or a whole directory tree from ``src`` to ``dst``."""
    entry = src_fs.stat(src)
    if not entry.is_dir:
        copy_file(src_fs, src, dst_fs, dst, progress, cancel)
        return

    # Directory: recreate the tree on the destination side.
    dst_fs.makedirs(dst)
    for rel, node in _iter_tree(src_fs, src):
        if cancel():
            raise OperationCancelled()
        if rel == "":
            continue
        s = src_fs.join(src, rel)
        # Translate the relative path into the destination's separator scheme.
        d = dst_fs.join(dst, *_split_rel(src_fs, rel))
        if node.is_dir and not node.is_symlink:
            dst_fs.makedirs(d)
        else:
            copy_file(src_fs, s, dst_fs, d, progress, cancel)


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
) -> None:
    """Move a file or directory tree.

    When both sides are the same live filesystem this is a cheap rename;
    otherwise it is a copy followed by deleting the source.
    """
    if src_fs.same_fs(dst_fs):
        parent = dst_fs.dirname(dst)
        if parent and not dst_fs.exists(parent):
            dst_fs.makedirs(parent)
        src_fs.rename(src, dst)
        return

    copy_path(src_fs, src, dst_fs, dst, progress, cancel)
    src_fs.delete_tree(src)


def count_tree(fs: FileSystem, path: str) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for a file or directory tree."""
    files = 0
    total = 0
    for _rel, node in _iter_tree(fs, path):
        if node.is_file:
            files += 1
            total += node.size or 0
    return files, total
