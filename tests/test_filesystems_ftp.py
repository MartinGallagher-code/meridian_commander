"""The FTP backend, against an ftplib-shaped stand-in."""

from __future__ import annotations

import calendar
import time

import pytest

from meridian_commander.filesystems import (
    FTPFileSystem,
    FileSystemError,
    _parse_unix_ls_line,
)


class _FTPError(Exception):
    """Stands in for ftplib.error_perm, which is what the code catches."""


class _FakeFTP:
    """Enough of ftplib.FTP to drive the backend."""

    def __init__(self):
        self.calls: list = []
        self.cwd_path = "/"
        self.feat = "211-Features:\n MLSD\n MDTM\n211 End"
        self.feat_error = None
        self.mlsd_entries: list = []
        self.mlsd_error = None
        self.list_lines: list[str] = []
        self.list_needs_path = False
        self.connect_error = None
        self.login_error = None
        self.quit_error = None
        self.close_error = None
        self.sendcmd_error = None
        self.pwd_error = None
        self.closed = False

    # -- connection
    def connect(self, host, port, timeout=None):
        self.calls.append(("connect", host, port))
        if self.connect_error:
            raise self.connect_error

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if self.login_error:
            raise self.login_error

    def set_pasv(self, flag):
        self.calls.append(("set_pasv", flag))

    def sendcmd(self, command):
        self.calls.append(("sendcmd", command))
        if command == "FEAT":
            if self.feat_error:
                raise self.feat_error
            return self.feat
        if self.sendcmd_error:
            raise self.sendcmd_error
        return "200 OK"

    def pwd(self):
        if self.pwd_error:
            raise self.pwd_error
        return self.cwd_path

    def cwd(self, path):
        self.calls.append(("cwd", path))
        if self.list_needs_path:
            raise _FTPError("550 no such directory")
        self.cwd_path = path

    # -- listing
    def mlsd(self, path):
        self.calls.append(("mlsd", path))
        if self.mlsd_error:
            raise self.mlsd_error
        return list(self.mlsd_entries)

    def retrlines(self, command, callback):
        self.calls.append(("retrlines", command))
        for line in self.list_lines:
            callback(line)

    # -- mutations
    def mkd(self, path):
        self.calls.append(("mkd", path))

    def delete(self, path):
        self.calls.append(("delete", path))

    def rmd(self, path):
        self.calls.append(("rmd", path))

    def rename(self, src, dst):
        self.calls.append(("rename", src, dst))

    def quit(self):
        self.calls.append(("quit",))
        if self.quit_error:
            raise self.quit_error
        self.closed = True

    def close(self):
        self.calls.append(("close",))
        if self.close_error:
            raise self.close_error
        self.closed = True

    # -- transfers
    def retrbinary(self, command, callback, blocksize=8192):
        self.calls.append(("retrbinary", command))
        data = getattr(self, "download", b"")
        for i in range(0, len(data), blocksize or 1):
            callback(data[i:i + (blocksize or 1)])

    def storbinary(self, command, fileobj):
        self.calls.append(("storbinary", command))
        chunks = []
        while True:
            chunk = fileobj.read(8192)
            if not chunk:
                break
            chunks.append(chunk)
        self.uploaded = b"".join(chunks)


@pytest.fixture
def ftp(monkeypatch):
    """Factory returning (FTPFileSystem, fake) with ftplib.FTP replaced."""

    def make(fake=None, **kwargs):
        fake = fake or _FakeFTP()
        import ftplib

        monkeypatch.setattr(ftplib, "FTP", lambda: fake)
        monkeypatch.setattr(ftplib, "error_perm", _FTPError)
        return FTPFileSystem("files.example", **kwargs), fake

    return make


# -- connecting ----------------------------------------------------------------

def test_ftp_connects_logs_in_and_goes_passive(ftp):
    fs, fake = ftp(username="deploy", password="secret", port=2121)
    assert ("connect", "files.example", 2121) in fake.calls
    assert ("login", "deploy", "secret") in fake.calls
    assert ("set_pasv", True) in fake.calls
    assert fs.label() == "ftp://deploy@files.example"
    assert fs.scheme == "ftp"


def test_ftp_logs_in_anonymously_by_default(ftp):
    _, fake = ftp()
    assert ("login", "anonymous", "") in fake.calls


def test_a_blank_username_becomes_anonymous(ftp):
    _, fake = ftp(username="", password="")
    assert ("login", "anonymous", "") in fake.calls


def test_a_refused_connection_is_reported(ftp):
    fake = _FakeFTP()
    fake.connect_error = OSError("connection refused")
    with pytest.raises(FileSystemError, match="Could not connect"):
        ftp(fake)


def test_a_rejected_login_is_reported(ftp):
    fake = _FakeFTP()
    fake.login_error = _FTPError("530 login incorrect")
    with pytest.raises(FileSystemError, match="Could not connect"):
        ftp(fake)


def test_feat_decides_whether_mlsd_is_used(ftp):
    fs, _ = ftp()
    assert fs._use_mlsd is True

    fake = _FakeFTP()
    fake.feat = "211-Features:\n MDTM\n211 End"
    fs, _ = ftp(fake)
    assert fs._use_mlsd is False


def test_a_server_without_feat_is_given_the_benefit_of_the_doubt(ftp):
    fake = _FakeFTP()
    fake.feat_error = _FTPError("500 Unknown command")
    fs, _ = ftp(fake)
    assert fs._use_mlsd is True


def test_ftp_home_is_the_working_directory(ftp):
    fake = _FakeFTP()
    fake.cwd_path = "/pub"
    fs, _ = ftp(fake)
    assert fs.home() == "/pub"


def test_ftp_home_falls_back_to_the_root(ftp):
    fake = _FakeFTP()
    fake.pwd_error = _FTPError("500 not supported")
    fs, _ = ftp(fake)
    assert fs.home() == "/"


# -- MLSD listing --------------------------------------------------------------

def test_mlsd_listing(ftp):
    fake = _FakeFTP()
    fake.mlsd_entries = [
        (".", {"type": "cdir"}),
        ("..", {"type": "pdir"}),
        ("readme.txt", {"type": "file", "size": "1234",
                        "modify": "20240115103000"}),
        ("pub", {"type": "dir"}),
    ]
    fs, _ = ftp(fake)
    entries = fs.listdir("/")
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"readme.txt", "pub"}
    assert by_name["readme.txt"].size == 1234
    assert by_name["readme.txt"].is_dir is False
    assert by_name["pub"].is_dir is True
    expected = calendar.timegm(time.strptime("20240115103000", "%Y%m%d%H%M%S"))
    assert by_name["readme.txt"].mtime == expected


def test_mlsd_entries_without_facts_still_list(ftp):
    fake = _FakeFTP()
    fake.mlsd_entries = [("odd", {})]
    fs, _ = ftp(fake)
    entry = fs.listdir("/")[0]
    assert entry.is_dir is False
    assert entry.size == 0
    assert entry.mtime is None


@pytest.mark.parametrize("value, expected_ok", [
    ("20240115103000", True),
    ("20240115103000.250", True),
    ("", False),
    ("not-a-date", False),
    ("2024-01-15 10:30", False),
])
def test_the_modify_fact_parser(value, expected_ok):
    result = FTPFileSystem._parse_modify(value)
    assert (result is not None) is expected_ok


def test_mlsd_is_abandoned_when_the_server_rejects_it(ftp):
    fake = _FakeFTP()
    fake.mlsd_error = _FTPError("500 Unknown command MLSD")
    fake.list_lines = ["-rw-r--r-- 1 u g 42 Jan 15 10:30 readme.txt"]
    fs, _ = ftp(fake)
    entries = fs.listdir("/")
    assert [e.name for e in entries] == ["readme.txt"]
    # The decision sticks: the next listing goes straight to LIST.
    assert fs._use_mlsd is False
    fs.listdir("/")
    assert [c for c in fake.calls if c[0] == "mlsd"] == [("mlsd", "/")]


def test_another_mlsd_error_is_not_swallowed(ftp):
    fake = _FakeFTP()
    fake.mlsd_error = _FTPError("550 permission denied")
    fs, _ = ftp(fake)
    with pytest.raises(_FTPError, match="550"):
        fs.listdir("/private")
    assert fs._use_mlsd is True


# -- LIST fallback -------------------------------------------------------------

@pytest.fixture
def list_fs(ftp):
    def make(lines):
        fake = _FakeFTP()
        fake.feat = "211 no features"
        fake.list_lines = lines
        return ftp(fake)

    return make


def test_list_parsing_unix_style(list_fs):
    fs, _ = list_fs([
        "drwxr-xr-x   2 owner group     4096 Jan 15 10:30 pub",
        "-rw-r--r--   1 owner group      842 Jan 15 10:31 readme.txt",
        "lrwxrwxrwx   1 owner group        7 Jan 15 10:32 link -> readme",
        "total 12",
        "",
    ])
    by_name = {e.name: e for e in fs.listdir("/")}
    assert by_name["pub"].is_dir is True
    assert by_name["readme.txt"].size == 842
    assert by_name["link"].is_symlink is True
    assert by_name["link"].name == "link"


def test_list_parsing_dos_style(list_fs):
    fs, _ = list_fs([
        "07-22-25  09:15AM       <DIR>          uploads",
        "07-22-25  09:16AM                  842 notes.txt",
        "07-22-2025  14:20                  100 later.txt",
    ])
    by_name = {e.name: e for e in fs.listdir("/")}
    assert by_name["uploads"].is_dir is True
    assert by_name["uploads"].size == 0
    assert by_name["notes.txt"].size == 842
    assert by_name["notes.txt"].mtime is not None
    assert by_name["later.txt"].size == 100


def test_a_dos_line_with_an_unparsable_date_still_lists(list_fs):
    fs, _ = list_fs(["99-99-99  99:99AM                  10 odd.txt"])
    entries = fs.listdir("/")
    assert entries[0].name == "odd.txt"
    assert entries[0].mtime is None


def test_dot_entries_are_dropped_from_a_list_listing(list_fs):
    fs, _ = list_fs([
        "drwxr-xr-x 2 o g 4096 Jan 15 10:30 .",
        "drwxr-xr-x 2 o g 4096 Jan 15 10:30 ..",
        "-rw-r--r-- 1 o g   10 Jan 15 10:30 real.txt",
    ])
    assert [e.name for e in fs.listdir("/")] == ["real.txt"]


def test_listing_falls_back_to_a_path_argument_when_cwd_fails(ftp):
    fake = _FakeFTP()
    fake.feat = "211 no features"
    fake.list_needs_path = True
    fake.list_lines = ["-rw-r--r-- 1 o g 10 Jan 15 10:30 a.txt"]
    fs, _ = ftp(fake)
    assert [e.name for e in fs.listdir("/pub")] == ["a.txt"]
    assert ("retrlines", "LIST /pub") in fake.calls


# -- the shared ls -l parser ---------------------------------------------------

def test_unix_ls_parser_handles_names_with_spaces():
    entry = _parse_unix_ls_line(
        "-rw-r--r--   1 owner group      842 Jan 15 10:31 my notes.txt")
    assert entry.name == "my notes.txt"
    assert entry.size == 842


def test_unix_ls_parser_resolves_a_symlink_name():
    entry = _parse_unix_ls_line(
        "lrwxrwxrwx   1 o g   7 Jan 15 10:32 current -> releases/v2")
    assert entry.name == "current"
    assert entry.is_symlink is True


@pytest.mark.parametrize("line", [
    "",
    "   ",
    "total 12",
    "garbage without enough fields",
])
def test_unix_ls_parser_rejects_what_is_not_a_listing(line):
    assert _parse_unix_ls_line(line) is None


def test_unix_ls_parser_reads_a_year_instead_of_a_time():
    entry = _parse_unix_ls_line("-rw-r--r-- 1 o g 10 Jan 15  2001 old.txt")
    assert entry.name == "old.txt"
    assert entry.mtime is not None


def test_unix_ls_parser_treats_a_future_time_as_last_year():
    # A "Mon DD HH:MM" stamp has no year; one far in the future was last year.
    future = time.localtime(time.time() + 60 * 24 * 3600)
    stamp = time.strftime("%b %d %H:%M", future)
    entry = _parse_unix_ls_line(f"-rw-r--r-- 1 o g 10 {stamp} soon.txt")
    assert entry.mtime is not None
    assert entry.mtime < time.time() + 86400


def test_unix_ls_parser_copes_with_a_size_that_is_not_a_number():
    entry = _parse_unix_ls_line("-rw-r--r-- 1 o g notasize Jan 15 10:30 a.txt")
    assert entry is None or entry.size == 0


# -- stat ----------------------------------------------------------------------

def test_ftp_stat_finds_an_entry_in_its_parent(ftp):
    fake = _FakeFTP()
    fake.mlsd_entries = [("a.txt", {"type": "file", "size": "5"})]
    fs, _ = ftp(fake)
    entry = fs.stat("/pub/a.txt")
    assert entry.name == "a.txt"
    assert entry.size == 5


def test_ftp_stat_of_the_root(ftp):
    fs, _ = ftp()
    entry = fs.stat("/")
    assert entry.name == "/"
    assert entry.is_dir is True


def test_ftp_stat_of_something_absent_raises(ftp):
    fake = _FakeFTP()
    fake.mlsd_entries = []
    fs, _ = ftp(fake)
    with pytest.raises(FileSystemError, match="No such path"):
        fs.stat("/pub/absent.txt")


# -- mutations -----------------------------------------------------------------

def test_ftp_mutations(ftp):
    fs, fake = ftp()
    fs.mkdir("/new")
    fs.remove("/a.txt")
    fs.rmdir("/old")
    fs.rename("/a", "/b")
    assert ("mkd", "/new") in fake.calls
    assert ("delete", "/a.txt") in fake.calls
    assert ("rmd", "/old") in fake.calls
    assert ("rename", "/a", "/b") in fake.calls


def test_ftp_utime_sends_mfmt(ftp):
    fs, fake = ftp()
    fs.utime("/a.txt", 1705315800.0)
    sent = [c for c in fake.calls if c[0] == "sendcmd" and "MFMT" in c[1]]
    assert sent
    assert sent[0][1].endswith("/a.txt")


def test_ftp_utime_on_a_server_without_mfmt_is_quiet(ftp):
    fake = _FakeFTP()
    fake.sendcmd_error = _FTPError("500 Unknown command")
    fs, _ = ftp(fake)
    fs.utime("/a.txt", 1705315800.0)      # must not raise


def test_ftp_close_quits(ftp):
    fs, fake = ftp()
    fs.close()
    assert ("quit",) in fake.calls
    assert fake.closed is True


def test_ftp_close_falls_back_when_quit_fails(ftp):
    fake = _FakeFTP()
    fake.quit_error = OSError("connection already dropped")
    fs, _ = ftp(fake)
    fs.close()
    assert ("close",) in fake.calls


def test_ftp_close_survives_a_connection_that_will_not_go(ftp):
    fake = _FakeFTP()
    fake.quit_error = OSError("gone")
    fake.close_error = OSError("also gone")
    fs, _ = ftp(fake)
    fs.close()                            # must not raise


# -- streaming transfers -------------------------------------------------------

def test_ftp_download_streams_through_a_queue(ftp):
    fake = _FakeFTP()
    fake.download = b"x" * 5000
    fs, _ = ftp(fake)
    reader = fs.open_read("/big.bin")
    chunk = reader.read(100)
    assert chunk == b"x" * 100
    rest = reader.read(-1)
    assert len(chunk) + len(rest) == 5000
    reader.close()


def test_ftp_download_of_an_empty_file(ftp):
    fake = _FakeFTP()
    fake.download = b""
    fs, _ = ftp(fake)
    with fs.open_read("/empty.bin") as reader:
        assert reader.read(10) == b""


def test_ftp_download_reads_to_the_end(ftp):
    fake = _FakeFTP()
    fake.download = b"hello world"
    fs, _ = ftp(fake)
    reader = fs.open_read("/a.txt")
    assert reader.read(-1) == b"hello world"
    assert reader.read(-1) == b""


def test_ftp_upload_streams_through_a_queue(ftp):
    fs, fake = ftp()
    writer = fs.open_write("/new.bin")
    assert writer.write(b"hello ") == 6
    writer.write(b"world")
    writer.close()
    assert fake.uploaded == b"hello world"


def test_ftp_upload_as_a_context_manager(ftp):
    fs, fake = ftp()
    with fs.open_write("/new.bin") as writer:
        writer.write(b"payload")
    assert fake.uploaded == b"payload"


# -- transfer failures ---------------------------------------------------------

def test_a_download_that_fails_raises_on_read(ftp):
    fake = _FakeFTP()

    def broken(command, callback, blocksize=8192):
        raise OSError("data connection failed")

    fake.retrbinary = broken
    fs, _ = ftp(fake)
    reader = fs.open_read("/a.bin")
    with pytest.raises(OSError, match="data connection failed"):
        reader.read(-1)


def test_an_upload_that_fails_raises_on_close(ftp):
    fake = _FakeFTP()

    def broken(command, fileobj):
        raise OSError("no write permission")

    fake.storbinary = broken
    fs, _ = ftp(fake)
    writer = fs.open_write("/a.bin")
    with pytest.raises(OSError, match="no write permission"):
        writer.close()


def test_an_upload_that_has_already_failed_refuses_more_writes(ftp):
    fake = _FakeFTP()

    def broken(command, fileobj):
        raise OSError("disk full")

    fake.storbinary = broken
    fs, _ = ftp(fake)
    writer = fs.open_write("/a.bin")
    # The worker thread fails as soon as it starts; the next write reports it.
    for _ in range(200):
        try:
            writer.write(b"x")
        except OSError as exc:
            assert "disk full" in str(exc)
            break
        time.sleep(0.005)
    else:
        pytest.fail("the write never reported the failure")
