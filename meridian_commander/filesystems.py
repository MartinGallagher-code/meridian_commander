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
these primitives in :mod:`meridian_commander.operations` and
:mod:`meridian_commander.sync`.
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
    """The machine Meridian Commander is running on."""

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
def _split_user_host(host: str) -> tuple[str | None, str]:
    """Split a ``user@host`` string; returns ``(user_or_None, host)``."""
    if "@" in host:
        user, _, rest = host.rpartition("@")
        if user and rest:
            return user, rest
    return None, host


def _resolve_ssh_connection(
    host: str,
    username: str | None = None,
    port: int | None = None,
    key_filename=None,
    config_path: str | None = None,
) -> dict:
    """Resolve a connection the way the ``ssh`` command would.

    ``host`` may be a real hostname, a ``user@host`` string, or an alias from
    ``~/.ssh/config``.  HostName, User, Port, IdentityFile, ProxyCommand and
    (single-hop) ProxyJump from the config are honoured; anything the caller
    passed explicitly wins over the config.  Returns a dict with ``hostname``,
    ``username``, ``port``, ``key_filename`` (possibly a list), and
    ``proxy_command`` (a string or None).
    """
    at_user, host = _split_user_host(host)
    if not username:
        username = at_user

    resolved = {
        "hostname": host,
        "username": username,
        "port": port if port not in (None, 0) else None,
        "key_filename": key_filename or None,
        "proxy_command": None,
    }

    if config_path is None:
        config_path = os.path.expanduser("~/.ssh/config")
    try:
        import paramiko

        if os.path.exists(config_path):
            with open(config_path) as f:
                conf = paramiko.SSHConfig()
                conf.parse(f)
            found = conf.lookup(host)
            resolved["hostname"] = found.get("hostname", host)
            if not resolved["username"] and "user" in found:
                resolved["username"] = found["user"]
            # A port typed as 22 (the dialog default) counts as "not set", so
            # a Port directive in the config wins -- same feel as plain ssh.
            if resolved["port"] in (None, 22) and "port" in found:
                try:
                    resolved["port"] = int(found["port"])
                except (TypeError, ValueError):
                    pass
            if not resolved["key_filename"] and "identityfile" in found:
                files = [os.path.expanduser(p) for p in found["identityfile"]]
                files = [p for p in files if os.path.exists(p)]
                if files:
                    resolved["key_filename"] = files
            if "proxycommand" in found:
                resolved["proxy_command"] = found["proxycommand"]
            elif "proxyjump" in found:
                # Single-hop ProxyJump via the system ssh client, which will
                # itself use the user's config/agent for the jump host.
                jport = resolved["port"] or 22
                resolved["proxy_command"] = (
                    f"ssh -W {resolved['hostname']}:{jport} {found['proxyjump']}"
                )
    except ImportError:
        pass
    except Exception:
        # An unparsable config should not break connecting by real hostname.
        pass

    if not resolved["username"]:
        import getpass

        try:
            resolved["username"] = getpass.getuser()
        except Exception:  # pragma: no cover - exotic environments
            resolved["username"] = "root"
    if not resolved["port"]:
        resolved["port"] = 22
    return resolved


def _open_ssh_client(
    host: str,
    username: str | None,
    password: str | None,
    port: int | None,
    key_filename=None,
):
    """Open and authenticate a paramiko SSHClient, shared by the SFTP and SSH
    backends and the SSH plug-ins.

    The connection is resolved through ``~/.ssh/config`` first (aliases,
    HostName, User, Port, IdentityFile, ProxyCommand/ProxyJump), then
    authenticated the way ``ssh`` would: explicit key file if given, otherwise
    the SSH agent and the default keys (~/.ssh/id_*), with any password used
    as a last resort.  Raises :class:`FileSystemError` on any failure.
    """
    try:
        import paramiko
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise FileSystemError(
            "SSH/SFTP support requires the 'paramiko' package "
            "(pip install paramiko)."
        ) from exc

    res = _resolve_ssh_connection(host, username, port, key_filename)

    sock = None
    if res["proxy_command"]:
        try:
            sock = paramiko.ProxyCommand(res["proxy_command"])
        except Exception as exc:
            raise FileSystemError(
                f"ProxyCommand failed for {host}: {exc}"
            ) from exc

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=res["hostname"],
            port=res["port"],
            username=res["username"],
            password=password or None,
            key_filename=res["key_filename"],
            # Try agent + default keys whenever no password was given, even
            # alongside an explicit key file -- like ssh, more ways to succeed.
            look_for_keys=password is None,
            allow_agent=True,
            sock=sock,
            timeout=20,
        )
    except Exception as exc:
        raise FileSystemError(f"Could not connect to {host}: {exc}") from exc
    return client


class SFTPFileSystem(FileSystem):
    """An SSH/SFTP server, browsed through paramiko.

    All remote paths are POSIX, so the inherited ``posixpath`` helpers are
    exactly right and are not overridden.
    """

    scheme = "sftp"

    def __init__(
        self,
        host: str,
        username: str | None = None,
        password: str | None = None,
        port: int = 22,
        key_filename: str | None = None,
    ) -> None:
        self.host = host              # as typed: may be an ssh-config alias
        self.port = port
        self.typed_username = username or _split_user_host(host)[0]
        self._client = _open_ssh_client(host, username, password, port,
                                        key_filename)
        # Show the username the connection actually authenticated as.
        try:
            self.username = self._client.get_transport().get_username()
        except Exception:
            self.username = username or "?"
        try:
            self._sftp = self._client.open_sftp()
        except Exception as exc:
            self._client.close()
            raise FileSystemError(
                f"Connected to {host} but could not open the SFTP subsystem: "
                f"{exc}. If SFTP is disabled on the server, try the SSH (shell) "
                f"mode instead."
            ) from exc
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
# Shared ``ls -l`` parsing (used by the FTP LIST fallback and the SSH backend)
# ---------------------------------------------------------------------------
def _parse_ls_date(value: str) -> float | None:
    """Parse the three-token date from ``ls -l`` output into a timestamp."""
    import time as _time

    parts = value.split()
    if len(parts) != 3:
        return None
    month, day, last = parts
    try:
        if ":" in last:  # current year, e.g. "Jul 20 12:00"
            year = _time.localtime().tm_year
            tm = _time.strptime(f"{month} {day} {year} {last}", "%b %d %Y %H:%M")
            mtime = _time.mktime(tm)
            # A date more than a day in the future was really last year.
            if mtime > _time.time() + 86400:
                tm = _time.strptime(f"{month} {day} {year - 1} {last}",
                                    "%b %d %Y %H:%M")
                mtime = _time.mktime(tm)
            return mtime
        tm = _time.strptime(f"{month} {day} {last}", "%b %d %Y")
        return _time.mktime(tm)
    except ValueError:
        return None


def _parse_unix_ls_line(line: str) -> "DirEntry | None":
    """Parse one ``ls -l`` line into a :class:`DirEntry`, or None if it is junk.

    Handles regular files, directories and symlinks (including the trailing
    ``-> target``), tolerating an ACL/xattr marker (``+``/``@``/``.``) after the
    permission bits.
    """
    import re

    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    m = re.match(
        r"^([\-dlbcps])[rwxsStT\-]{9}[\+@\.]?\s+\d+\s+\S+\s+\S+\s+"
        r"(\d+)\s+(\w{3}\s+\d+\s+[\d:]+)\s+(.+)$",
        line,
    )
    if not m:
        return None
    type_ch, size_s, date_s, name = m.groups()
    is_link = type_ch == "l"
    is_dir = type_ch == "d"
    if is_link:
        name = name.split(" -> ", 1)[0]
    return DirEntry(
        name=name.strip(),
        is_dir=is_dir,
        is_symlink=is_link,
        size=int(size_s),
        mtime=_parse_ls_date(date_s),
    )


# ---------------------------------------------------------------------------
# FTP filesystem (stdlib ftplib, MLSD based)
# ---------------------------------------------------------------------------
class FTPFileSystem(FileSystem):
    """An FTP server.

    Listing prefers the ``MLSD`` command (RFC 3659), which returns machine
    readable ``type``/``size``/``modify`` facts.  Many servers support it, but
    plenty of older or minimal ones do not and answer ``MLSD`` with
    ``500 Unknown command``.  For those we transparently fall back to parsing
    the classic ``LIST`` (``ls -l`` style, and MS-DOS/IIS style) output, and use
    ``MDTM`` to recover a precise modification time per file where possible.
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

        # Decide once whether the server supports MLSD. FEAT is advisory; if the
        # server has no FEAT either we optimistically try MLSD and fall back the
        # first time it is refused.
        self._use_mlsd = True
        try:
            feat = self._ftp.sendcmd("FEAT")
            self._use_mlsd = "MLSD" in feat.upper()
        except Exception:
            pass

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
        from ftplib import error_perm

        if self._use_mlsd:
            try:
                return self._listdir_mlsd(path)
            except error_perm as exc:
                # 500/502 = command not implemented -> switch to LIST for good.
                if str(exc)[:3] in ("500", "502"):
                    self._use_mlsd = False
                else:
                    raise
        return self._listdir_list(path)

    def _listdir_mlsd(self, path: str) -> list[DirEntry]:
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

    def _listdir_list(self, path: str) -> list[DirEntry]:
        """Fallback listing by parsing ``LIST`` output for servers without MLSD."""
        lines: list[str] = []
        # Some servers need the connection positioned in the directory first.
        try:
            self._ftp.cwd(path)
            self._ftp.retrlines("LIST", lines.append)
        except Exception:
            lines = []
            self._ftp.retrlines(f"LIST {path}", lines.append)

        entries: list[DirEntry] = []
        for line in lines:
            entry = self._parse_list_line(line)
            if entry is None or entry.name in (".", ".."):
                continue
            entries.append(entry)
        return entries

    @staticmethod
    def _parse_list_line(line: str) -> "DirEntry | None":
        import re
        import time as _time

        line = line.rstrip("\r\n")
        if not line.strip():
            return None

        # MS-DOS / IIS style:  "07-22-25  09:15AM   <DIR>   name"
        #                      "07-22-25  09:15AM        842 name"
        m = re.match(
            r"^(\d{2}-\d{2}-\d{2,4})\s+(\d{2}:\d{2}(?:[AP]M)?)\s+"
            r"(<DIR>|\d+)\s+(.+)$",
            line,
        )
        if m:
            date_s, time_s, size_s, name = m.groups()
            is_dir = size_s == "<DIR>"
            mtime = None
            for fmt in ("%m-%d-%y %I:%M%p", "%m-%d-%Y %I:%M%p",
                        "%m-%d-%y %H:%M", "%m-%d-%Y %H:%M"):
                try:
                    mtime = _time.mktime(_time.strptime(f"{date_s} {time_s}", fmt))
                    break
                except ValueError:
                    continue
            return DirEntry(
                name=name.strip(),
                is_dir=is_dir,
                is_symlink=False,
                size=0 if is_dir else int(size_s),
                mtime=mtime,
            )

        # Otherwise assume Unix "ls -l" style.
        return _parse_unix_ls_line(line)

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


# ---------------------------------------------------------------------------
# SSH (shell) filesystem
# ---------------------------------------------------------------------------
class SSHFileSystem(FileSystem):
    """A remote host browsed purely over an SSH shell channel.

    Unlike :class:`SFTPFileSystem`, this backend never uses the SFTP subsystem.
    It runs ordinary POSIX commands over ``exec_command`` -- ``ls`` to list,
    ``mkdir``/``rm``/``mv`` to mutate -- so it works against servers that permit
    SSH login but have SFTP disabled.

    File contents are transferred with a fallback chain, because restricted
    appliance shells frequently lack coreutils: ``cat`` is tried first, then
    ``dd``, and finally the raw **scp wire protocol** (``scp -f``/``scp -t``),
    which most embedded SSH servers implement even when the shell offers almost
    no commands.  Whichever method succeeds is remembered and tried first for
    subsequent transfers on this connection.
    """

    scheme = "ssh"

    #: Read/write strategies, in preference order.  "scp" is the wire-protocol
    #: fallback; the other entries are shell command templates.
    READ_TEMPLATES = ("cat {p}", "dd if={p}", "scp")
    WRITE_TEMPLATES = ("cat > {p}", "dd of={p}", "scp")

    def __init__(
        self,
        host: str,
        username: str | None = None,
        password: str | None = None,
        port: int = 22,
        key_filename: str | None = None,
    ) -> None:
        self.host = host              # as typed: may be an ssh-config alias
        self.port = port
        self.typed_username = username or _split_user_host(host)[0]
        self._read_templates = list(self.READ_TEMPLATES)
        self._write_templates = list(self.WRITE_TEMPLATES)
        self._client = _open_ssh_client(host, username, password, port,
                                        key_filename)
        try:
            self.username = self._client.get_transport().get_username()
        except Exception:
            self.username = username or "?"
        self._home = self._run("pwd").strip() or "/"

    def label(self) -> str:
        return f"ssh://{self.username}@{self.host}"

    def home(self) -> str:
        return self._home

    # -- command execution ------------------------------------------------
    def _run(self, command: str) -> str:
        """Run a command, returning stdout; raise FileSystemError on failure."""
        stdin, stdout, stderr = self._client.exec_command(command, timeout=30)
        out = stdout.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()
        if status != 0:
            err = stderr.read().decode("utf-8", errors="replace").strip()
            raise FileSystemError(err or f"command failed ({status}): {command}")
        return out

    @staticmethod
    def _q(path: str) -> str:
        import shlex

        return shlex.quote(path)

    # -- queries ----------------------------------------------------------
    def listdir(self, path: str) -> list[DirEntry]:
        # -l long format, -A include dotfiles (but not . and ..), -Q would quote
        # but is GNU-only, so we rely on the parser tolerating spaces in names.
        out = self._run(f"ls -lA {self._q(path)}")
        entries: list[DirEntry] = []
        for line in out.splitlines():
            if line.startswith("total ") or not line.strip():
                continue
            entry = _parse_unix_ls_line(line)
            if entry is not None and entry.name not in (".", ".."):
                entries.append(entry)
        return entries

    def stat(self, path: str) -> DirEntry:
        path = self.normpath(path)
        if path in ("", "/"):
            return DirEntry(name="/", is_dir=True)
        # -d lists the item itself rather than a directory's contents.
        out = self._run(f"ls -ldA {self._q(path)}")
        for line in out.splitlines():
            entry = _parse_unix_ls_line(line)
            if entry is not None:
                # ls -d prints the path as given; normalise to the basename.
                entry.name = self.basename(path)
                return entry
        raise FileSystemError(f"No such path: {path}")

    def is_dir(self, path: str) -> bool:
        try:
            self._run(f"test -d {self._q(path)}")
            return True
        except Exception:
            return False

    def exists(self, path: str) -> bool:
        try:
            self._run(f"test -e {self._q(path)}")
            return True
        except Exception:
            return False

    # -- streaming I/O ----------------------------------------------------
    def _promote(self, templates: list[str], tmpl: str) -> None:
        """Move the strategy that just worked to the front of the list."""
        if tmpl in templates and templates[0] != tmpl:
            templates.remove(tmpl)
            templates.insert(0, tmpl)

    def open_read(self, path: str):
        """Open ``path`` for reading, trying cat, then dd, then scp.

        Unlike a naive ``cat``, this checks the command's exit status, so a
        restricted shell answering "Command 'cat' not supported" produces a
        clean fallback (and ultimately a clear error) instead of the error text
        being shown as if it were the file's contents.
        """
        errors: list[str] = []
        for tmpl in list(self._read_templates):
            try:
                if tmpl == "scp":
                    reader = self._scp_read(path)
                    self._promote(self._read_templates, tmpl)
                    return reader
                cmd = tmpl.format(p=self._q(path))
                _in, stdout, stderr = self._client.exec_command(cmd, timeout=None)
                # Probe: first byte of output, or a clean zero exit (empty file),
                # proves the command exists and the file is readable.
                first = stdout.read(1)
                if first:
                    self._promote(self._read_templates, tmpl)
                    return _SSHReader(stdout, first)
                if stdout.channel.recv_exit_status() == 0:
                    self._promote(self._read_templates, tmpl)
                    return _SSHReader(stdout, b"")
                err = stderr.read().decode("utf-8", "replace").strip()
                errors.append(err.splitlines()[0] if err
                              else f"{tmpl.split()[0]}: failed")
            except FileSystemError as exc:
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - network dependent
                errors.append(f"{type(exc).__name__}: {exc}")
        detail = "; ".join(dict.fromkeys(errors)) or "no transfer method worked"
        raise FileSystemError(
            f"Cannot read file ({detail}). The remote shell lacks cat/dd and "
            f"scp failed -- if the server offers SFTP, use the SFTP mode instead."
        )

    def open_write(self, path: str):
        return _SSHWriter(self, path)

    # -- scp wire protocol -------------------------------------------------
    # Used when the restricted remote shell has no usable file commands. The
    # protocol is tiny: after "scp -f file" the server sends a header line
    # "C<mode> <size> <name>\n" followed by <size> raw bytes; each step is
    # acknowledged with a zero byte. "scp -t file" is the mirror image.
    def _scp_read(self, path: str):
        chan = self._client.get_transport().open_session()
        try:
            chan.exec_command(f"scp -f {self._q(path)}")
            chan.sendall(b"\x00")
            line = _scp_read_line(chan)
            if line.startswith("T"):
                # Timestamp preamble (sent when -p is in effect); ack and skip.
                chan.sendall(b"\x00")
                line = _scp_read_line(chan)
            if not line.startswith("C"):
                raise FileSystemError(f"scp: {line.strip() or 'unexpected response'}")
            size = _parse_scp_header(line)
            chan.sendall(b"\x00")
            return _SCPReader(chan, size)
        except Exception:
            try:
                chan.close()
            except Exception:
                pass
            raise

    def _scp_write(self, path: str, data: bytes) -> None:
        chan = self._client.get_transport().open_session()
        try:
            chan.exec_command(f"scp -t {self._q(path)}")
            _scp_expect_ok(chan)
            name = self.basename(path) or "file"
            chan.sendall(f"C0644 {len(data)} {name}\n".encode())
            _scp_expect_ok(chan)
            if data:
                chan.sendall(data)
            chan.sendall(b"\x00")
            _scp_expect_ok(chan)
        finally:
            try:
                chan.close()
            except Exception:
                pass

    # -- mutations --------------------------------------------------------
    def utime(self, path: str, mtime: float) -> None:
        import time as _time

        stamp = _time.strftime("%Y%m%d%H%M.%S", _time.localtime(mtime))
        try:
            # GNU form first (epoch), then the portable POSIX -t form.
            self._run(f"touch -d @{int(mtime)} {self._q(path)}")
        except Exception:
            try:
                self._run(f"touch -t {stamp} {self._q(path)}")
            except Exception:
                pass

    def mkdir(self, path: str) -> None:
        self._run(f"mkdir {self._q(path)}")

    def makedirs(self, path: str) -> None:
        self._run(f"mkdir -p {self._q(path)}")

    def remove(self, path: str) -> None:
        self._run(f"rm -f {self._q(path)}")

    def rmdir(self, path: str) -> None:
        self._run(f"rmdir {self._q(path)}")

    def delete_tree(self, path: str) -> None:
        self._run(f"rm -rf {self._q(path)}")

    def rename(self, src: str, dst: str) -> None:
        self._run(f"mv {self._q(src)} {self._q(dst)}")

    def close(self) -> None:
        self._client.close()


def _parse_scp_header(line: str) -> int:
    """Extract the size field from an scp ``C<mode> <size> <name>`` header."""
    import re

    m = re.match(r"^C[0-7]{3,4}\s+(\d+)\s+", line)
    if not m:
        raise FileSystemError(f"scp: malformed header: {line.strip()!r}")
    return int(m.group(1))


def _scp_read_line(chan) -> str:
    """Read one protocol line, raising on scp error frames (\\x01/\\x02)."""
    buf = b""
    while not buf.endswith(b"\n"):
        c = chan.recv(1)
        if not c:
            raise FileSystemError("scp: connection closed")
        buf += c
    if buf[:1] in (b"\x01", b"\x02"):
        raise FileSystemError(f"scp: {buf[1:].decode('utf-8', 'replace').strip()}")
    return buf.decode("utf-8", "replace")


def _scp_expect_ok(chan) -> None:
    """Consume one scp acknowledgement byte, raising on an error frame."""
    b = chan.recv(1)
    if b == b"\x00":
        return
    if b in (b"\x01", b"\x02"):
        msg = b""
        while not msg.endswith(b"\n"):
            c = chan.recv(1)
            if not c:
                break
            msg += c
        raise FileSystemError(f"scp: {msg.decode('utf-8', 'replace').strip()}")
    raise FileSystemError("scp: connection closed")


class _SCPReader:
    """Read a file's bytes from an ``scp -f`` channel as a ``read(n)`` stream."""

    def __init__(self, chan, size: int) -> None:
        self._chan = chan
        self._remaining = size

    def read(self, n: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        want = self._remaining if (n is None or n < 0) else min(n, self._remaining)
        data = b""
        while len(data) < want:
            chunk = self._chan.recv(want - len(data))
            if not chunk:
                break
            data += chunk
        self._remaining -= len(data)
        if self._remaining <= 0:
            # Consume the trailing status byte and acknowledge it.
            try:
                self._chan.recv(1)
                self._chan.sendall(b"\x00")
            except Exception:
                pass
        return data

    def close(self) -> None:
        try:
            self._chan.close()
        except Exception:
            pass


class _SSHReader:
    """Wrap a paramiko exec stdout channel as a blocking ``read(n)`` stream.

    ``first`` carries the probe byte :meth:`SSHFileSystem.open_read` consumed
    while checking that the command actually works.
    """

    def __init__(self, stdout, first: bytes = b"") -> None:
        self._stdout = stdout
        self._buf = first

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            data = self._buf + self._stdout.read()
            self._buf = b""
            return data
        data, self._buf = self._buf[:n], self._buf[n:]
        # Channel.read honours the requested size; loop until we have n or EOF.
        while len(data) < n:
            chunk = self._stdout.read(n - len(data))
            if not chunk:
                break
            data += chunk
        return data

    def close(self) -> None:
        try:
            self._stdout.channel.recv_exit_status()
        except Exception:
            pass


class _SSHWriter:
    """A ``write(bytes)`` stream with fallbacks for shells lacking ``cat``.

    Bytes are streamed to the current shell command while also being kept in a
    replay buffer (up to a cap).  If the command turns out not to exist -- which
    only becomes certain when the channel dies or reports a non-zero exit at
    close -- the write is retried with the next strategy and finally with the
    scp wire protocol.  The successful strategy is promoted for next time.
    """

    #: Replay-buffer cap. Beyond this we can no longer retry a failed command;
    #: comfortably above the editor's 8 MiB file limit.
    _CAP = 32 * 1024 * 1024

    def __init__(self, fs: "SSHFileSystem", path: str) -> None:
        self._fs = fs
        self._path = path
        self._order = list(fs._write_templates)
        self._buf = bytearray()
        self._buf_ok = True
        self._dead = False
        self._closed = False
        self._start_next()

    def _start_next(self) -> None:
        self._tmpl = self._order.pop(0)
        self._dead = False
        if self._tmpl == "scp":
            # scp needs the size up front, so it sends everything at close.
            self._stdin = None
            self._stdout = None
            return
        cmd = self._tmpl.format(p=self._fs._q(self._path))
        self._stdin, self._stdout, _err = self._fs._client.exec_command(
            cmd, timeout=None
        )

    def write(self, data: bytes) -> int:
        data = bytes(data)
        if self._buf_ok:
            if len(self._buf) + len(data) <= self._CAP:
                self._buf.extend(data)
            else:
                self._buf = bytearray()
                self._buf_ok = False
        if self._tmpl == "scp":
            if not self._buf_ok:
                raise FileSystemError(
                    "scp transfer needs the whole file buffered; file exceeds "
                    f"{self._CAP // (1024 * 1024)} MiB."
                )
            return len(data)
        if not self._dead:
            try:
                self._stdin.write(data)
            except Exception:
                # Command probably does not exist; keep buffering for the
                # retry at close (unless the buffer already overflowed).
                self._dead = True
                if not self._buf_ok:
                    raise
        return len(data)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._tmpl == "scp":
            self._fs._scp_write(self._path, bytes(self._buf))
            self._fs._promote(self._fs._write_templates, "scp")
            return

        status = 1
        if not self._dead:
            try:
                self._stdin.channel.shutdown_write()
            except Exception:
                pass
            try:
                status = self._stdout.channel.recv_exit_status()
            except Exception:
                status = 1
        if status == 0:
            self._fs._promote(self._fs._write_templates, self._tmpl)
            return

        if not self._buf_ok:
            raise FileSystemError(
                f"write with '{self._tmpl.split()[0]}' failed and the file is "
                f"too large to retry with another method."
            )

        # Replay the buffered bytes through the remaining strategies.
        data = bytes(self._buf)
        errors: list[str] = [f"{self._tmpl.split()[0]}: exit {status}"]
        while self._order:
            self._start_next()
            if self._tmpl == "scp":
                try:
                    self._fs._scp_write(self._path, data)
                    self._fs._promote(self._fs._write_templates, "scp")
                    return
                except Exception as exc:
                    errors.append(str(exc))
                    break
            try:
                if data:
                    self._stdin.write(data)
                self._stdin.channel.shutdown_write()
                if self._stdout.channel.recv_exit_status() == 0:
                    self._fs._promote(self._fs._write_templates, self._tmpl)
                    return
                errors.append(f"{self._tmpl.split()[0]}: failed")
            except Exception as exc:
                errors.append(f"{self._tmpl.split()[0]}: {exc}")
        raise FileSystemError(
            "Cannot write file (" + "; ".join(dict.fromkeys(errors)) + "). "
            "If the server offers SFTP, use the SFTP mode instead."
        )


def local_fs() -> LocalFileSystem:
    """Return a fresh local filesystem handle."""
    return LocalFileSystem()
