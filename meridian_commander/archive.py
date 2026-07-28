"""Browse a zip or tar archive as though it were a directory.

An archive is not a document, so this is not a viewer: it is a read-only
:class:`~meridian_commander.filesystems.FileSystem`.  Pressing Enter on a
``.zip`` points the pane at it and the archive lists, scrolls, tags and copies
exactly like a directory, because copy, move and sync are already written
against that interface and know nothing about where a pane happens to point.

Read-only is enforced rather than implied: every mutating call raises, so an
attempt to copy *into* an archive says so instead of half-working.

Two things about archives that shape the implementation:

* **Directories are implied.**  An archive stores paths, not a tree, and may
  hold ``a/b/c.txt`` with no entry for ``a/``.  The tree is rebuilt from the
  member names, adding the directories nobody wrote down.
* **A member name is not to be trusted.**  A name containing ``..`` would
  escape the archive the moment it was copied out, so such members are refused
  at the door and counted in the label rather than dropped in silence.
"""

from __future__ import annotations

import io
import posixpath
import tarfile
import time
import zipfile

from .filesystems import DirEntry, FileSystem, FileSystemError

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


def is_archive(name: str) -> bool:
    """True if ``name`` looks like an archive this module can open."""
    return name.lower().endswith(ARCHIVE_SUFFIXES)


def _kind(name: str) -> str:
    return "zip" if name.lower().endswith(ZIP_SUFFIXES) else "tar"


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
        self._open()

    # -- opening ----------------------------------------------------------
    def _source(self):
        """The archive as something zipfile/tarfile can read.

        A local archive is opened in place -- browsing a multi-gigabyte tar on
        this machine should not mean holding it in memory.  Anything else has
        to be fetched, because both formats need to seek.
        """
        local = getattr(self.host_fs, "local_path", None)
        if callable(local):
            return local(self.archive_path)
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
            self.close()
            raise FileSystemError(f"cannot open {self.name}: {exc}") from exc

    def _build_zip(self) -> None:
        for info in self._zip.infolist():
            if not self._room():
                break
            rel = clean_member(info.filename)
            if rel is None:
                self.skipped += 1
                continue
            self._add(rel, info.is_dir(), info.file_size,
                      zip_mtime(info.date_time))

    def _build_tar(self) -> None:
        for member in self._tar.getmembers():
            if not self._room():
                break
            rel = clean_member(member.name)
            if rel is None:
                self.skipped += 1
                continue
            self._add(rel, member.isdir(), member.size, member.mtime,
                      is_symlink=member.issym() or member.islnk())

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
             is_symlink: bool = False) -> None:
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
        self._members[full] = rel
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
    def open_read(self, path: str):
        key = self.normpath(path)
        member = self._members.get(key)
        if member is None:
            raise FileSystemError(f"no such file in {self.name}: {path}")
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

    # -- mutations --------------------------------------------------------
    def _read_only(self, what: str):
        raise FileSystemError(
            f"{self.name} is an archive, opened read-only; cannot {what}")

    def open_write(self, path: str):
        self._read_only("write to it")

    def mkdir(self, path: str) -> None:
        self._read_only("create directories in it")

    def remove(self, path: str) -> None:
        self._read_only("delete from it")

    def rmdir(self, path: str) -> None:
        self._read_only("delete from it")

    def rename(self, src: str, dst: str) -> None:
        self._read_only("rename inside it")

    def close(self) -> None:
        for handle in (self._zip, self._tar):
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._zip = self._tar = None


def open_archive(host_fs: FileSystem, path: str) -> ArchiveFileSystem:
    """Open the archive at ``path`` on ``host_fs`` as a browsable filesystem."""
    return ArchiveFileSystem(host_fs, path)
