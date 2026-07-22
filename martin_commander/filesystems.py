"""Filesystem abstraction layer.

Every browsable location -- local disk, an SFTP server, an FTP server --
is exposed through the same :class:`FileSystem` interface.  This is what lets
the rest of the application copy, move and synchronize files between panes
without caring whether either side happens to be local or remote.

Backends
--------
* :class:`LocalFileSystem`  -- the machine we are running on (stdlib only).
* :class:`SFTPFileSystem`   -- SSH/SFTP servers (requires ``paramiko``).
* :class:`FTPFileSystem`    -- FTP servers that speak ``MLSD`` (stdlib ftplib).

The abstraction is deliberately small: directory listing, stat, streaming
read/write, and the handful of mutating operations (mkdir, remove, rename).
Higher level behaviour -- recursive copy, move, sync -- is built on top of
these primitives in :mod:`martin_commander.operations` and
:mod:`martin_commander.sync`.
"""

from __future__ import annotations

import abc
import os
import posixpath
import shutil
import stat as stat_mod
from dataclasses import dataclass


# Streaming transfers are done in reasonably large chunks so that pushing a big
# file over the network does not turn into millions of tiny reads.
CHUNK_SIZE = 64 * 1024


@dataclass
class DirEntry:
    """A single entry in a directory listing.

    ``mtime`` is a POSIX timestamp (seconds).  It may be ``None`` when a
    backend cannot report it, in which case sync treats the file as "unknown
    age" and copies conservatively.
    """

    name: str
    is_dir: bool
    is_symlink: bool = False
    size: int = 0
    mtime: float | None = None
    mode: int = 0

    @property
    def is_file(self) -> bool:
        return not self.is_dir


class FileSystemError(Exception):
    """Raised for any backend level failure (connection, permission, ...)."""


class FileSystem(abc.ABC):
    """Abstract base class shared by every location backend."""

    #: Short scheme identifier, e.g. ``"local"`` or ``"sftp"``.
    scheme: str = "abstract"

    # -- identity ---------------------------------------------------------
    @abc.abstractmethod
    def label(self) -> str:
        """Human readable label shown in the panel header."""

    def same_fs(self, other: "FileSystem") -> bool:
        """Whether ``self`` and ``other`` are the *same* live connection.

        When true, moves can use an in-place rename instead of a
        copy-then-delete.  We compare identity rather than type so that two
        distinct SFTP connections are (correctly) not considered the same.
        """
        return self is other

    # -- path helpers (may be overridden for remote/posix semantics) ------
    @property
    def sep(self) -> str:
        return "/"

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def dirname(self, path: str) -> str:
        return posixpath.dirname(path)

    def basename(self, path: str) -> str:
        return posixpath.basename(path)

    def normpath(self, path: str) -> str:
        return posixpath.normpath(path)

    def parent(self, path: str) -> str:
        """Directory containing ``path`` (its own parent at the root)."""
        return self.normpath(self.join(path, ".."))

    @abc.abstractmethod
    def home(self) -> str:
        """A sensible starting directory for this backend."""

    # -- queries ----------------------------------------------------------
    @abc.abstractmethod
    def listdir(self, path: str) -> list[DirEntry]:
        ...

    @abc.abstractmethod
    def stat(self, path: str) -> DirEntry:
        ...

    def is_dir(self, path: str) -> bool:
        try:
            return self.stat(path).is_dir
        except Exception:
            return False

    def exists(self, path: str) -> bool:
        try:
            self.stat(path)
            return True
        except Exception:
            return False

    # -- streaming I/O ----------------------------------------------------
    @abc.abstractmethod
    def open_read(self, path: str):
        """Return a binary file-like object supporting ``read(n)``."""

    @abc.abstractmethod
    def open_write(self, path: str):
        """Return a binary file-like object supporting ``write(bytes)``."""

    def utime(self, path: str, mtime: float) -> None:
        """Set ``path``'s modification (and access) time to ``mtime``.

        Used to give a copied file the same timestamp as its source.  This is
        best-effort: backends that cannot set times (or servers that reject the
        request) leave the default no-op in place rather than failing the copy.
        """

    # -- mutations --------------------------------------------------------
    @abc.abstractmethod
    def mkdir(self, path: str) -> None:
        ...

    def makedirs(self, path: str) -> None:
        """Create ``path`` and any missing parents (like ``mkdir -p``)."""
        if not path or self.exists(path):
            return
        parent = self.parent(path)
        if parent and parent != path and not self.exists(parent):
            self.makedirs(parent)
        if not self.exists(path):
            self.mkdir(path)

    @abc.abstractmethod
    def remove(self, path: str) -> None:
        """Remove a single file."""

    @abc.abstractmethod
    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""

    def delete_tree(self, path: str) -> None:
        """Recursively delete a file or directory tree."""
        try:
            entry = self.stat(path)
        except Exception:
            return
        if entry.is_dir and not entry.is_symlink:
            for child in self.listdir(path):
                self.delete_tree(self.join(path, child.name))
            self.rmdir(path)
        else:
            self.remove(path)

    @abc.abstractmethod
    def rename(self, src: str, dst: str) -> None:
        """Rename within the same filesystem (used for same-fs moves)."""

    def close(self) -> None:  # pragma: no cover - trivial default
        """Release any resources (network connections)."""


# ---------------------------------------------------------------------------
# Local filesystem
# ---------------------------------------------------------------------------
class LocalFileSystem(FileSystem):
    """The machine Martin Commander is running on."""

    scheme = "local"

    def label(self) -> str:
        return "local"

    # Use the host's native path semantics.
    @property
    def sep(self) -> str:
        return os.sep

    def join(self, *parts: str) -> str:
        return os.path.join(*parts)

    def dirname(self, path: str) -> str:
        return os.path.dirname(path)

    def basename(self, path: str) -> str:
        return os.path.basename(path)

    def normpath(self, path: str) -> str:
        return os.path.normpath(path)

    def parent(self, path: str) -> str:
        return os.path.dirname(os.path.normpath(path)) or os.path.normpath(path)

    def home(self) -> str:
        return os.path.expanduser("~")

    def listdir(self, path: str) -> list[DirEntry]:
        entries: list[DirEntry] = []
        with os.scandir(path) as it:
            for de in it:
                try:
                    st = de.stat(follow_symlinks=False)
                    is_link = de.is_symlink()
                    # Resolve symlinks for the is_dir flag so that following
                    # them behaves as the user expects.
                    is_dir = de.is_dir(follow_symlinks=True)
                    entries.append(
                        DirEntry(
                            name=de.name,
                            is_dir=is_dir,
                            is_symlink=is_link,
                            size=st.st_size,
                            mtime=st.st_mtime,
                            mode=st.st_mode,
                        )
                    )
                except OSError:
                    # Broken symlink or vanished entry -- list it as a plain
                    # file so the user can still see and remove it.
                    entries.append(DirEntry(name=de.name, is_dir=False, is_symlink=True))
        return entries

    def stat(self, path: str) -> DirEntry:
        st = os.stat(path)
        return DirEntry(
            name=os.path.basename(os.path.normpath(path)),
            is_dir=stat_mod.S_ISDIR(st.st_mode),
            is_symlink=os.path.islink(path),
            size=st.st_size,
            mtime=st.st_mtime,
            mode=st.st_mode,
        )

    def open_read(self, path: str):
        return open(path, "rb")

    def open_write(self, path: str):
        return open(path, "wb")

    def utime(self, path: str, mtime: float) -> None:
        os.utime(path, (mtime, mtime))

    def mkdir(self, path: str) -> None:
        os.mkdir(path)

    def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def remove(self, path: str) -> None:
        os.remove(path)

    def rmdir(self, path: str) -> None:
        os.rmdir(path)

    def delete_tree(self, path: str) -> None:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)

    def rename(self, src: str, dst: str) -> None:
        os.rename(src, dst)


# ---------------------------------------------------------------------------
# SFTP filesystem (paramiko)
# ---------------------------------------------------------------------------
class SFTPFileSystem(FileSystem):
    """An SSH/SFTP server, browsed through paramiko.

    All remote paths are POSIX, so the inherited ``posixpath`` helpers are
    exactly right and are not overridden.
    """

    scheme = "sftp"

    def __init__(
        self,
        host: str,
        username: str,
        password: str | None = None,
        port: int = 22,
        key_filename: str | None = None,
    ) -> None:
        try:
            import paramiko  # imported lazily so the app runs without it
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise FileSystemError(
                "SFTP support requires the 'paramiko' package "
                "(pip install paramiko)."
            ) from exc

        self.host = host
        self.port = port
        self.username = username
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            self._client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password or None,
                key_filename=key_filename or None,
                look_for_keys=key_filename is None and password is None,
                allow_agent=True,
                timeout=20,
            )
            self._sftp = self._client.open_sftp()
        except Exception as exc:
            raise FileSystemError(f"Could not connect to {host}: {exc}") from exc
        try:
            self._home = self._sftp.normalize(".")
        except Exception:
            self._home = "/"

    def label(self) -> str:
        return f"sftp://{self.username}@{self.host}"

    def home(self) -> str:
        return self._home

    def listdir(self, path: str) -> list[DirEntry]:
        entries: list[DirEntry] = []
        for attr in self._sftp.listdir_attr(path):
            mode = attr.st_mode or 0
            is_link = stat_mod.S_ISLNK(mode)
            is_dir = stat_mod.S_ISDIR(mode)
            if is_link:
                # Follow the link to decide whether it behaves as a directory.
                try:
                    target = self._sftp.stat(self.join(path, attr.filename))
                    is_dir = stat_mod.S_ISDIR(target.st_mode or 0)
                except Exception:
                    is_dir = False
            entries.append(
                DirEntry(
                    name=attr.filename,
                    is_dir=is_dir,
                    is_symlink=is_link,
                    size=attr.st_size or 0,
                    mtime=float(attr.st_mtime) if attr.st_mtime else None,
                    mode=mode,
                )
            )
        return entries

    def stat(self, path: str) -> DirEntry:
        attr = self._sftp.stat(path)
        mode = attr.st_mode or 0
        return DirEntry(
            name=self.basename(self.normpath(path)),
            is_dir=stat_mod.S_ISDIR(mode),
            is_symlink=False,
            size=attr.st_size or 0,
            mtime=float(attr.st_mtime) if attr.st_mtime else None,
            mode=mode,
        )

    def open_read(self, path: str):
        f = self._sftp.open(path, "rb")
        # Paramiko's prefetch dramatically speeds up sequential downloads.
        try:
            f.prefetch()
        except Exception:
            pass
        return f

    def open_write(self, path: str):
        return self._sftp.open(path, "wb")

    def utime(self, path: str, mtime: float) -> None:
        # paramiko takes an (atime, mtime) tuple of POSIX timestamps.
        self._sftp.utime(path, (mtime, mtime))

    def mkdir(self, path: str) -> None:
        self._sftp.mkdir(path)

    def remove(self, path: str) -> None:
        self._sftp.remove(path)

    def rmdir(self, path: str) -> None:
        self._sftp.rmdir(path)

    def rename(self, src: str, dst: str) -> None:
        try:
            self._sftp.posix_rename(src, dst)
        except Exception:
            # Older servers lack the posix-rename extension.
            self._sftp.rename(src, dst)

    def close(self) -> None:
        try:
            self._sftp.close()
        finally:
            self._client.close()


# ---------------------------------------------------------------------------
# FTP filesystem (stdlib ftplib, MLSD based)
# ---------------------------------------------------------------------------
class FTPFileSystem(FileSystem):
    """An FTP server.

    Listing relies on the ``MLSD`` command (RFC 3659) so that we get machine
    readable ``type``/``size``/``modify`` facts instead of trying to parse the
    free-form ``LIST`` output.  Practically every FTP server from the last two
    decades supports it.
    """

    scheme = "ftp"

    def __init__(
        self,
        host: str,
        username: str = "anonymous",
        password: str = "",
        port: int = 21,
    ) -> None:
        from ftplib import FTP

        self.host = host
        self.port = port
        self.username = username
        self._ftp = FTP()
        try:
            self._ftp.connect(host, port, timeout=20)
            self._ftp.login(username or "anonymous", password or "")
            self._ftp.set_pasv(True)
        except Exception as exc:
            raise FileSystemError(f"Could not connect to {host}: {exc}") from exc

    def label(self) -> str:
        return f"ftp://{self.username}@{self.host}"

    def home(self) -> str:
        try:
            return self._ftp.pwd()
        except Exception:
            return "/"

    @staticmethod
    def _parse_modify(value: str) -> float | None:
        # MLSD 'modify' fact: YYYYMMDDHHMMSS(.sss) in UTC.
        import calendar
        import time as _time

        if not value:
            return None
        value = value.split(".")[0]
        try:
            tm = _time.strptime(value, "%Y%m%d%H%M%S")
            return calendar.timegm(tm)
        except ValueError:
            return None

    def listdir(self, path: str) -> list[DirEntry]:
        entries: list[DirEntry] = []
        for name, facts in self._ftp.mlsd(path):
            if name in (".", ".."):
                continue
            typ = facts.get("type", "file")
            is_dir = typ in ("dir", "cdir", "pdir")
            entries.append(
                DirEntry(
                    name=name,
                    is_dir=is_dir,
                    is_symlink=False,
                    size=int(facts.get("size", 0) or 0),
                    mtime=self._parse_modify(facts.get("modify", "")),
                )
            )
        return entries

    def stat(self, path: str) -> DirEntry:
        path = self.normpath(path)
        parent = self.dirname(path) or "/"
        name = self.basename(path)
        if not name:  # root
            return DirEntry(name="/", is_dir=True)
        for entry in self.listdir(parent):
            if entry.name == name:
                return entry
        raise FileSystemError(f"No such path: {path}")

    def open_read(self, path: str):
        return _FTPReader(self._ftp, path)

    def open_write(self, path: str):
        return _FTPWriter(self._ftp, path)

    def utime(self, path: str, mtime: float) -> None:
        # MFMT (RFC 3659) sets the modify time; the argument is UTC.
        # Not every server implements it, so failures are swallowed.
        import time as _time

        stamp = _time.strftime("%Y%m%d%H%M%S", _time.gmtime(mtime))
        try:
            self._ftp.sendcmd(f"MFMT {stamp} {path}")
        except Exception:
            pass

    def mkdir(self, path: str) -> None:
        self._ftp.mkd(path)

    def remove(self, path: str) -> None:
        self._ftp.delete(path)

    def rmdir(self, path: str) -> None:
        self._ftp.rmd(path)

    def rename(self, src: str, dst: str) -> None:
        self._ftp.rename(src, dst)

    def close(self) -> None:
        try:
            self._ftp.quit()
        except Exception:
            try:
                self._ftp.close()
            except Exception:
                pass


class _FTPReader:
    """Adapt FTP ``RETR`` (callback based) to a blocking ``read(n)`` API.

    ftplib delivers a download by invoking a callback with chunks.  We run that
    transfer on a background thread and hand the bytes across through a bounded
    queue, so the caller sees an ordinary read-until-EOF stream.
    """

    def __init__(self, ftp, path: str) -> None:
        import queue
        import threading

        self._buf = b""
        self._eof = False
        self._error: Exception | None = None
        self._queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=16)

        def worker() -> None:
            try:
                ftp.retrbinary(f"RETR {path}", self._queue.put, blocksize=CHUNK_SIZE)
            except Exception as exc:  # pragma: no cover - network dependent
                self._error = exc
            finally:
                self._queue.put(None)

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def read(self, n: int = -1) -> bytes:
        while not self._eof and (n < 0 or len(self._buf) < n):
            chunk = self._queue.get()
            if chunk is None:
                self._eof = True
                if self._error:
                    raise self._error
                break
            self._buf += chunk
        if n < 0:
            data, self._buf = self._buf, b""
        else:
            data, self._buf = self._buf[:n], self._buf[n:]
        return data

    def close(self) -> None:
        self._eof = True

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _FTPWriter:
    """Adapt FTP ``STOR`` to a blocking ``write(bytes)`` API."""

    def __init__(self, ftp, path: str) -> None:
        import queue
        import threading

        self._error: Exception | None = None
        self._queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=16)

        def reader() -> bytes:
            chunk = self._queue.get()
            return chunk if chunk is not None else b""

        def worker() -> None:
            try:
                ftp.storbinary(f"STOR {path}", _QueueFile(self._queue))
            except Exception as exc:  # pragma: no cover - network dependent
                self._error = exc

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()

    def write(self, data: bytes) -> int:
        if self._error:
            raise self._error
        self._queue.put(bytes(data))
        return len(data)

    def close(self) -> None:
        self._queue.put(None)
        self._thread.join()
        if self._error:
            raise self._error

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class _QueueFile:
    """A minimal read-only file object backed by a queue, for ``storbinary``."""

    def __init__(self, q) -> None:
        self._queue = q

    def read(self, _n: int = -1) -> bytes:
        chunk = self._queue.get()
        return chunk if chunk is not None else b""


def local_fs() -> LocalFileSystem:
    """Return a fresh local filesystem handle."""
    return LocalFileSystem()
