# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Browse a zip or tar archive as though it were a directory.

An archive is not a document, so this is not a viewer: it is a
:class:`~meridian_commander.filesystems.FileSystem`.  Pressing Enter on a
``.zip`` points the pane at it and the archive lists, scrolls, tags and copies
exactly like a directory, because copy, move and sync are already written
against that interface and know nothing about where a pane happens to point.

That goes for copying *into* one as well: files and directories can be added,
and a file already there can be replaced.  Neither format can be changed in
place -- a zip's index sits at the end, and a compressed tar is one stream --
so a change means writing the whole archive out again.  Two rules follow:

* **What is written is staged first.**  Each file lands in a private temporary
  directory, and the archive is rewritten from that once, when the operation
  ends -- :meth:`FileSystem.batch` is how a copy of a hundred files says
  "ends", and asks for one rewrite rather than a hundred.  Outside a batch a
  change is written as soon as it is made.
* **The archive is replaced, never edited.**  The new one is built beside the
  old and renamed over it, so a failure halfway leaves the original as it was.
  Members this module does not show -- the unsafe ones below -- are carried
  across too: rewriting an archive must not be a way of losing part of it.

Deleting and renaming inside an archive are refused, and say so.

Two things about archives that shape the implementation:

* **Directories are implied.**  An archive stores paths, not a tree, and may
  hold ``a/b/c.txt`` with no entry for ``a/``.  The tree is rebuilt from the
  member names, adding the directories nobody wrote down.
* **A member name is not to be trusted.**  A name containing ``..`` would
  escape the archive the moment it was copied out, so such members are refused
  at the door and counted in the label rather than dropped in silence.
"""

from __future__ import annotations

import contextlib
import copy
import io
import os
import posixpath
import shutil
import stat
import tarfile
import tempfile
import time
import zipfile

from .filesystems import CHUNK_SIZE, DirEntry, FileSystem, FileSystemError

# Suffixes are matched longest-first, so ".tar.gz" wins over ".gz".
ZIP_SUFFIXES = (".zip", ".jar", ".whl", ".egg")
TAR_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz", ".tbz2",
                ".tar.xz", ".txz", ".tar.zst")
ARCHIVE_SUFFIXES = tuple(sorted(ZIP_SUFFIXES + TAR_SUFFIXES,
                                key=len, reverse=True))

# A remote archive has to be fetched whole before anything can be listed, so
# this bounds what browsing one can cost.  A local archive is opened in place
# and is not affected.
MAX_BYTES = 256 * 1024 * 1024
MAX_MEMBERS = 200_000

#: How to write a tar back the way it was compressed.  Reading needs no table
#: -- ``r:*`` works the compression out -- but writing has to be told, and the
#: name is what says what the file is meant to be.  Anything not listed here
#: is a plain ``.tar``.
TAR_WRITE_MODES = (
    ((".tar.gz", ".tgz"), "w:gz"),
    ((".tar.bz2", ".tbz", ".tbz2"), "w:bz2"),
    ((".tar.xz", ".txz"), "w:xz"),
    ((".tar.zst",), "w:zst"),
)

#: The span of dates a zip entry can carry: a DOS timestamp is a year counted
#: from 1980 in seven bits, so 1980-01-01 to 2107-12-31 and nothing outside it.
#: A file stamped before 1980 -- restored from old media, or ``touch -t``ed --
#: is not an error to refuse the whole archive over, so its date is clamped.
ZIP_EARLIEST = (1980, 1, 1, 0, 0, 0)
ZIP_LATEST = (2107, 12, 31, 23, 59, 59)


def is_archive(name: str) -> bool:
    """True if ``name`` looks like an archive this module can open."""
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def _kind(name: str) -> str:
    return "zip" if name.lower().endswith(ZIP_SUFFIXES) else "tar"


def _tar_write_mode(name: str) -> str:
    lower = name.lower()
    for suffixes, mode in TAR_WRITE_MODES:
        if lower.endswith(suffixes):
            return mode
    return "w"


def archive_stem(name: str) -> str:
    """``name`` without its archive suffix: ``project.tar.gz`` -> ``project``.

    What a folder unpacked from the archive is called.  A name that is
    nothing *but* a suffix keeps it, since an empty folder name is no name.
    """
    lower = name.lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return name[: -len(suffix)] or name
    return name


def zip_mtime(date_time) -> float | None:
    """A zip entry's modification time, or ``None`` when it has none.

    Zip stores a DOS timestamp, and plenty of writers leave it zero, which
    decodes to month 0 and day 0.  ``time.mktime`` would take that quietly and
    normalise it into a date in 1979; reporting no time at all is honest, and
    sync already treats an unknown age conservatively.  The remaining fields
    are bounded by the DOS encoding, so there is nothing else to guard.
    """
    year, month, day = date_time[:3]
    if not (month and day):
        return None
    return time.mktime((year, month, day) + tuple(date_time[3:6]) + (0, 0, -1))


def zip_date_time(mtime: float) -> tuple[int, int, int, int, int, int]:
    """``mtime`` as a zip date, clamped to what the format can hold.

    The inverse of :func:`zip_mtime`.  ``ZipInfo.date_time`` is written with
    ``struct``, which is where an out-of-range year ends up: not a "zip cannot
    store this date" but a raw ``ushort format requires 0 <= number <=
    65535``, out of the middle of a write that had already read every file
    going into it.  One odd timestamp is not worth losing the archive over.
    """
    try:
        stamp = time.localtime(mtime)[:6]
    except (OSError, OverflowError, ValueError):
        # A time_t the platform cannot represent at all.
        return ZIP_EARLIEST
    return min(max(stamp, ZIP_EARLIEST), ZIP_LATEST)


def clean_member(name: str) -> str | None:
    """A member name as a clean relative path, or ``None`` to refuse it.

    Refused: anything with a ``..`` component, which would climb out of the
    destination directory when the file was copied out, and anything that
    names nothing at all.  Leading slashes and ``.`` components are stripped
    rather than refused -- they are merely untidy, not dangerous.
    """
    parts = []
    for part in name.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts) or None


class ArchiveFileSystem(FileSystem):
    """A read-only filesystem over the contents of one archive."""

    def __init__(self, host_fs: FileSystem, path: str) -> None:
        self.host_fs = host_fs
        self.archive_path = path
        self.name = host_fs.basename(path)
        self.scheme = _kind(self.name)
        self.skipped = 0
        self.truncated = False
        # Counted as the tree is built: totalling it per member would make
        # opening a large archive quadratic.
        self._count = 0
        self._zip: zipfile.ZipFile | None = None
        self._tar: tarfile.TarFile | None = None
        #: directory path -> {entry name: DirEntry}
        self._dirs: dict[str, dict[str, DirEntry]] = {"/": {}}
        #: file path -> the member name to read it back by
        self._members: dict[str, str] = {}
        #: What has been written and not yet put into the archive: file path
        #: -> (its staged copy on this machine, mtime), and the directories
        #: made, path -> mtime.  See :meth:`commit`.
        self._staged: dict[str, tuple[str, float]] = {}
        self._made: dict[str, float] = {}
        #: New times for files already in the archive, from :meth:`utime`.
        self._stamps: dict[str, float] = {}
        self._stage_dir: str | None = None
        #: How many :meth:`batch` blocks are open; nothing is committed
        #: while any is.
        self._depth = 0
        self._open()

    # -- opening ----------------------------------------------------------
    def _local_file(self) -> str | None:
        """The archive's real path on this machine, or ``None`` if remote."""
        local = getattr(self.host_fs, "local_path", None)
        return local(self.archive_path) if callable(local) else None

    def _source(self):
        """The archive as something zipfile/tarfile can read.

        A local archive is opened in place -- browsing a multi-gigabyte tar on
        this machine should not mean holding it in memory.  Anything else has
        to be fetched, because both formats need to seek.
        """
        local = self._local_file()
        if local is not None:
            return local
        stream = self.host_fs.open_read(self.archive_path)
        try:
            data = stream.read(MAX_BYTES + 1)
        finally:
            stream.close()
        if len(data) > MAX_BYTES:
            raise FileSystemError(
                f"archive is larger than {MAX_BYTES // (1024 * 1024)} MiB; "
                "it would have to be downloaded whole to be browsed")
        return io.BytesIO(data)

    def _open(self) -> None:
        source = self._source()
        try:
            if self.scheme == "zip":
                self._zip = zipfile.ZipFile(source)
                self._build_zip()
            else:
                self._tar = tarfile.open(
                    name=source if isinstance(source, str) else None,
                    fileobj=None if isinstance(source, str) else source,
                    mode="r:*")
                self._build_tar()
        except Exception as exc:
            # _source() runs before this, so its own refusals are not caught
            # and reworded here.
            self._close_handles()
            raise FileSystemError(f"cannot open {self.name}: {exc}") from exc

    def _reload(self) -> None:
        """Read the archive again, as it now is, and rebuild the tree."""
        self._close_handles()
        self.skipped = 0
        self.truncated = False
        self._count = 0
        self._dirs = {"/": {}}
        self._members = {}
        self._open()

    def _build_zip(self) -> None:
        for info in self._zip.infolist():
            if not self._room():
                break
            rel = clean_member(info.filename)
            if rel is None:
                self.skipped += 1
                continue
            self._add(rel, info.is_dir(), info.file_size,
                      zip_mtime(info.date_time), member=info.filename)

    def _build_tar(self) -> None:
        for member in self._tar.getmembers():
            if not self._room():
                break
            rel = clean_member(member.name)
            if rel is None:
                self.skipped += 1
                continue
            self._add(rel, member.isdir(), member.size, member.mtime,
                      is_symlink=member.issym() or member.islnk(),
                      member=member.name)

    def _room(self) -> bool:
        if self._count < MAX_MEMBERS:
            return True
        self.truncated = True
        return False

    # -- building the tree ------------------------------------------------
    def _ensure_dir(self, path: str) -> None:
        """Create ``path`` and every parent, for the ones nobody wrote down."""
        if path in self._dirs:
            return
        parent = posixpath.dirname(path) or "/"
        self._ensure_dir(parent)
        self._dirs.setdefault(path, {})
        name = posixpath.basename(path)
        self._dirs[parent].setdefault(name, DirEntry(name, True))
        self._count += 1

    def _add(self, rel: str, is_dir: bool, size: int, mtime,
             is_symlink: bool = False, member: str | None = None) -> None:
        """Put one member into the tree.

        ``rel`` is the tidied path it is *shown* at; ``member`` is the name it
        is stored under, which is the only name the archive will open it by.
        The two differ whenever :func:`clean_member` had to tidy something --
        and ``tar czf out.tgz .``, the ordinary way to make one, writes every
        member as ``./name``, so the whole archive listed correctly and not
        one file in it could be read.
        """
        full = "/" + rel
        parent = posixpath.dirname(full) or "/"
        self._ensure_dir(parent)
        name = posixpath.basename(full)
        if is_dir:
            self._ensure_dir(full)
            self._dirs[parent][name] = DirEntry(name, True, mtime=mtime)
            return
        self._dirs[parent][name] = DirEntry(name, False, is_symlink=is_symlink,
                                            size=size, mtime=mtime)
        self._members[full] = rel if member is None else member
        self._count += 1

    # -- identity ---------------------------------------------------------
    def label(self) -> str:
        label = f"{self.scheme}:{self.name}"
        if self.skipped:
            label += f" ({self.skipped} unsafe member(s) skipped)"
        if self.truncated:
            label += " (truncated)"
        return label

    def home(self) -> str:
        return "/"

    def normpath(self, path: str) -> str:
        normal = posixpath.normpath("/" + path.lstrip("/"))
        return normal if normal.startswith("/") else "/" + normal

    # -- queries ----------------------------------------------------------
    def listdir(self, path: str) -> list[DirEntry]:
        key = self.normpath(path)
        if key not in self._dirs:
            raise FileSystemError(f"no such directory in {self.name}: {path}")
        return list(self._dirs[key].values())

    def stat(self, path: str) -> DirEntry:
        key = self.normpath(path)
        if key == "/":
            return DirEntry("/", True)
        parent = posixpath.dirname(key) or "/"
        entry = self._dirs.get(parent, {}).get(posixpath.basename(key))
        if entry is None:
            raise FileSystemError(f"no such entry in {self.name}: {path}")
        return entry

    # -- reading ----------------------------------------------------------
    def _require_open(self) -> None:
        if self._zip is None and self._tar is None:
            # The tree is still in memory, so the listing goes on working after
            # close() -- but there is nothing left to read the bytes from or to
            # write them back into, and saying so beats the AttributeError that
            # came out of the handle.
            raise FileSystemError(f"{self.name} has been closed")

    def open_read(self, path: str):
        key = self.normpath(path)
        member = self._members.get(key)
        if member is None:
            raise FileSystemError(f"no such file in {self.name}: {path}")
        self._require_open()
        try:
            if self._zip is not None:
                return self._zip.open(member)
            stream = self._tar.extractfile(member)
        except Exception as exc:
            raise FileSystemError(f"cannot read {path}: {exc}") from exc
        if stream is None:
            # tarfile returns None for entries with no data of their own,
            # such as a symlink or a device node.
            raise FileSystemError(f"{path} has no contents to read")
        return stream

    # -- writing ----------------------------------------------------------
    @contextlib.contextmanager
    def batch(self):
        """Hold every change until the block ends, then rewrite once.

        Blocks nest; the rewrite comes when the outermost one closes, and it
        comes even when the block is left by an exception, because what was
        written before it is as real as anything copied onto a disk before a
        failure.  A rewrite that fails raises from here.
        """
        self._depth += 1
        try:
            yield
        finally:
            self._depth -= 1
            if not self._depth:
                self.commit()

    def _changed(self) -> None:
        if not self._depth:
            self.commit()

    def _parent_dir(self, key: str) -> tuple[str, str]:
        """``key``'s directory and name, refusing a directory not here."""
        parent, name = posixpath.split(key)
        if parent not in self._dirs:
            raise FileSystemError(
                f"no such directory in {self.name}: {parent}")
        return parent, name

    def open_write(self, path: str):
        self._require_open()
        key = self.normpath(path)
        if key in self._dirs:
            raise FileSystemError(f"{path} is a directory in {self.name}")
        self._parent_dir(key)
        if self._stage_dir is None:
            self._stage_dir = tempfile.mkdtemp(prefix="meridian-archive-")
        fd, staged = tempfile.mkstemp(dir=self._stage_dir)
        return _StagedFile(fd, lambda: self._stage(key, staged))

    def _stage(self, key: str, staged: str) -> None:
        """A staged file is complete: show it, and commit unless batching."""
        previous = self._staged.get(key)
        if previous is not None:
            # Written twice in one batch: only the last one goes in.
            os.remove(previous[0])
        now = time.time()
        self._staged[key] = (staged, now)
        self._stamps.pop(key, None)
        parent, name = self._parent_dir(key)
        self._dirs[parent][name] = DirEntry(
            name, False, size=os.path.getsize(staged), mtime=now)
        self._changed()

    def mkdir(self, path: str) -> None:
        self._require_open()
        key = self.normpath(path)
        if self.exists(key):
            raise FileSystemError(f"{path} already exists in {self.name}")
        parent, name = self._parent_dir(key)
        now = time.time()
        self._dirs[key] = {}
        self._dirs[parent][name] = DirEntry(name, True, mtime=now)
        self._made[key] = now
        self._changed()

    def utime(self, path: str, mtime: float) -> None:
        """Restamp a file; a directory has no member of its own to carry it.

        Directories are passed over rather than refused, which is the bargain
        :meth:`FileSystem.utime` already makes for a backend that cannot set a
        time: the copy it is part of has still succeeded.
        """
        self._require_open()
        key = self.normpath(path)
        if key in self._staged:
            self._staged[key] = (self._staged[key][0], mtime)
        elif key in self._members:
            self._stamps[key] = mtime
        else:
            return
        self.stat(key).mtime = mtime
        self._changed()

    def commit(self) -> None:
        """Rewrite the archive with everything staged, if anything is.

        The new archive is built beside the old one when it is on this
        machine, so the swap at the end is a rename and there is never a
        moment when the archive is half-written; it takes the old one's
        permissions with it, and a symlink to the archive is followed rather
        than replaced.  An archive the user cannot write to is refused, as
        writing to it directly would be: the rename only needs the directory
        to be writable, and quietly replacing a file someone marked read-only
        is not a favour.  A remote archive is built here and then uploaded
        over the original -- unless it has grown past :data:`MAX_BYTES`, when
        it is refused, since it could not be opened again -- and if that
        upload fails the finished archive is kept on this machine and the
        error says where.

        Whatever happens, the staged changes are finished with afterwards:
        on success they are in the archive, and on failure the tree is rebuilt
        from what the archive actually holds, so the pane does not go on
        listing files that never arrived.
        """
        if not (self._staged or self._made or self._stamps):
            return
        local = self._local_file()
        if local is not None:
            local = os.path.realpath(local)
        built = ""
        uploading = False
        try:
            if local is not None and not os.access(local, os.W_OK):
                raise PermissionError("permission denied")
            fd, built = tempfile.mkstemp(
                prefix=f".{self.name}.", suffix=".part",
                dir=os.path.dirname(local) if local is not None else None)
            os.close(fd)
            self._rewrite(built)
            if local is None and os.path.getsize(built) > MAX_BYTES:
                # It would upload and then not open again: a remote archive
                # is read whole, and this one has grown past what that allows.
                raise FileSystemError(
                    f"it would grow past {MAX_BYTES // (1024 * 1024)} MiB, "
                    "more than a remote archive can be opened at")
            self._close_handles()
            if local is not None:
                shutil.copymode(local, built)
                os.replace(built, local)
            else:
                uploading = True
                self._upload(built)
        except Exception as exc:
            self._discard()
            if uploading:
                problem = (f"cannot write {self.name} back: {exc}; the "
                           f"updated archive is kept at {built}")
            else:
                if built:
                    os.remove(built)
                problem = f"cannot write {self.name}: {exc}"
            try:
                self._reload()
            except FileSystemError:
                # The upload broke the archive it was replacing.  The tree is
                # empty and reads say the archive is closed, which is true.
                pass
            raise FileSystemError(problem) from exc
        if local is None:
            os.remove(built)
        self._discard()
        self._reload()

    def _upload(self, built: str) -> None:
        with open(built, "rb") as reader:
            writer = self.host_fs.open_write(self.archive_path)
            try:
                shutil.copyfileobj(reader, writer, CHUNK_SIZE)
            finally:
                writer.close()

    def _discard(self) -> None:
        """Forget every staged change and the files that held them."""
        self._staged = {}
        self._made = {}
        self._stamps = {}
        if self._stage_dir is not None:
            shutil.rmtree(self._stage_dir, ignore_errors=True)
            self._stage_dir = None

    @staticmethod
    def _shown_at(stored: str) -> str | None:
        """The tree path a stored member is listed at, or ``None`` if none.

        A member refused at the door has no path, so nothing written can
        replace it, and it is carried into the new archive as it was.
        """
        rel = clean_member(stored)
        return None if rel is None else "/" + rel

    def _rewrite(self, built: str) -> None:
        if self._zip is not None:
            self._rewrite_zip(self._zip, built)
        elif self._tar is not None:
            self._rewrite_tar(self._tar, built)

    def _rewrite_zip(self, source: zipfile.ZipFile, built: str) -> None:
        with zipfile.ZipFile(built, "w") as out:
            out.comment = source.comment
            for info in source.infolist():
                key = self._shown_at(info.filename)
                if key in self._staged:
                    continue
                # The extra field is left behind.  It can hold a zip64 record
                # describing the old layout, which would contradict the one
                # written for the new, and the rest of what it carries --
                # finer timestamps, Unix owners -- is not worth an archive
                # that will not open.
                kept = zipfile.ZipInfo(info.filename, info.date_time)
                if key in self._stamps:
                    kept.date_time = zip_date_time(self._stamps[key])
                kept.compress_type = info.compress_type
                kept.external_attr = info.external_attr
                kept.create_system = info.create_system
                kept.comment = info.comment
                # Known up front, so a member over 2 GiB is written as zip64.
                kept.file_size = info.file_size
                with source.open(info) as reader, \
                        out.open(kept, "w") as writer:
                    shutil.copyfileobj(reader, writer, CHUNK_SIZE)
            for key, mtime in sorted(self._made.items()):
                made = zipfile.ZipInfo(key[1:] + "/", zip_date_time(mtime))
                # The Unix mode in the high half; 0x10 is the MS-DOS flag
                # that a Windows tool reads to know it is a directory.
                made.external_attr = (stat.S_IFDIR | 0o755) << 16 | 0x10
                out.writestr(made, b"")
            for key, (staged, mtime) in sorted(self._staged.items()):
                added = zipfile.ZipInfo(key[1:], zip_date_time(mtime))
                added.compress_type = zipfile.ZIP_DEFLATED
                added.external_attr = (stat.S_IFREG | 0o644) << 16
                added.file_size = os.path.getsize(staged)
                with open(staged, "rb") as reader, \
                        out.open(added, "w") as writer:
                    shutil.copyfileobj(reader, writer, CHUNK_SIZE)

    def _rewrite_tar(self, source: tarfile.TarFile, built: str) -> None:
        with tarfile.open(built, _tar_write_mode(self.name)) as out:
            for member in source.getmembers():
                key = self._shown_at(member.name)
                if key in self._staged:
                    continue
                if key in self._stamps:
                    member = copy.copy(member)
                    member.mtime = int(self._stamps[key])
                # Only a regular file has data of its own; a link, a
                # directory or a device is all header, and copied as one.
                data = source.extractfile(member) if member.isreg() else None
                out.addfile(member, data)
            for key, mtime in sorted(self._made.items()):
                made = tarfile.TarInfo(key[1:])
                made.type = tarfile.DIRTYPE
                made.mode = 0o755
                made.mtime = int(mtime)
                out.addfile(made)
            for key, (staged, mtime) in sorted(self._staged.items()):
                added = tarfile.TarInfo(key[1:])
                added.size = os.path.getsize(staged)
                added.mode = 0o644
                added.mtime = int(mtime)
                with open(staged, "rb") as reader:
                    out.addfile(added, reader)

    # -- what is still refused -----------------------------------------------
    def _refused(self, what: str):
        raise FileSystemError(
            f"{self.name} is an archive: files can be added to it, "
            f"but not {what}")

    def remove(self, path: str) -> None:
        self._refused("deleted from it")

    def rmdir(self, path: str) -> None:
        self._refused("deleted from it")

    def rename(self, src: str, dst: str) -> None:
        self._refused("renamed inside it")

    def _close_handles(self) -> None:
        for handle in (self._zip, self._tar):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._zip = self._tar = None

    def close(self) -> None:
        self._close_handles()
        self._discard()


class _StagedFile(io.FileIO):
    """A file being written into an archive, staged on this machine.

    Closing it is what hands it to the archive -- and, outside a batch, what
    rewrites the archive -- so a failure to do that raises from ``close()``,
    exactly where a remote backend reports a write that did not arrive.
    """

    def __init__(self, fd: int, on_close) -> None:
        super().__init__(fd, "wb")
        self._on_close = on_close

    def close(self) -> None:
        if self.closed:
            return
        super().close()
        self._on_close()


def open_archive(host_fs: FileSystem, path: str) -> ArchiveFileSystem:
    """Open the archive at ``path`` on ``host_fs`` as a browsable filesystem."""
    return ArchiveFileSystem(host_fs, path)
