"""The SSH-shell backend: ls parsing, the cat/dd/scp fallback chain, writes."""

from __future__ import annotations

import time

import pytest

from meridian_commander import filesystems as fsmod
from meridian_commander.filesystems import (
    FileSystemError,
    SSHFileSystem,
    _parse_scp_header,
    _scp_expect_ok,
    _scp_read_line,
    _SCPReader,
    _SSHReader,
)


# -- paramiko-shaped fakes -----------------------------------------------------

class _Channel:
    def __init__(self, status=0):
        self.status = status
        self.shutdown = False

    def recv_exit_status(self):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def shutdown_write(self):
        self.shutdown = True


class _Stdout:
    def __init__(self, data=b"", status=0):
        self._data = data
        self.channel = _Channel(status)

    def read(self, n=-1):
        if n is None or n < 0:
            data, self._data = self._data, b""
            return data
        data, self._data = self._data[:n], self._data[n:]
        return data


class _Stdin:
    def __init__(self, fail=False):
        self.written = b""
        self.fail = fail
        self.channel = _Channel()

    def write(self, data):
        if self.fail:
            raise OSError("broken pipe")
        self.written += data


class _Result:
    """One scripted answer to exec_command."""

    def __init__(self, out=b"", err=b"", status=0, stdin_fail=False):
        self.out = out
        self.err = err
        self.status = status
        self.stdin_fail = stdin_fail


class _SCPChannel:
    """A session channel speaking the scp wire protocol."""

    def __init__(self, script=b""):
        self.script = bytearray(script)
        self.sent = b""
        self.command = None
        self.closed = False
        self.close_error = None

    def exec_command(self, command):
        self.command = command

    def sendall(self, data):
        self.sent += data

    def recv(self, n):
        if not self.script:
            return b""
        data = bytes(self.script[:n])
        del self.script[:n]
        return data

    def close(self):
        if self.close_error:
            raise self.close_error
        self.closed = True


class _Transport:
    def __init__(self, username="deploy", session=None):
        self._username = username
        self.session = session

    def get_username(self):
        return self._username

    def open_session(self):
        if isinstance(self.session, Exception):
            raise self.session
        return self.session


class _Client:
    def __init__(self, results=None, transport=None):
        # results maps a command prefix to a _Result (or a callable).
        self.results = results or {}
        self.commands: list[str] = []
        self.stdins: list[_Stdin] = []
        self._transport = transport or _Transport()
        self.closed = False
        self.default = _Result()

    def get_transport(self):
        return self._transport

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        result = self.default
        for prefix, value in self.results.items():
            if command.startswith(prefix):
                result = value(command) if callable(value) else value
                break
        stdin = _Stdin(fail=result.stdin_fail)
        self.stdins.append(stdin)
        return stdin, _Stdout(result.out, result.status), _Stdout(result.err)

    def close(self):
        self.closed = True


@pytest.fixture
def ssh_fs(monkeypatch):
    """Factory building an SSHFileSystem over a fake client."""

    def make(results=None, transport=None, pwd=b"/home/deploy\n"):
        results = dict(results or {})
        results.setdefault("pwd", _Result(out=pwd))
        client = _Client(results, transport)
        monkeypatch.setattr(fsmod, "_open_ssh_client", lambda *a, **k: client)
        return SSHFileSystem("web1", username="deploy"), client

    return make


# -- identity ------------------------------------------------------------------

def test_ssh_identity_and_home(ssh_fs):
    fs, _ = ssh_fs()
    assert fs.scheme == "ssh"
    assert fs.label() == "ssh://deploy@web1"
    assert fs.home() == "/home/deploy"


def test_ssh_home_falls_back_to_the_root(ssh_fs):
    fs, _ = ssh_fs(pwd=b"\n")
    assert fs.home() == "/"


def test_ssh_reports_the_username_it_authenticated_as(ssh_fs):
    fs, _ = ssh_fs(transport=_Transport(username="realuser"))
    assert fs.username == "realuser"


def test_ssh_falls_back_to_the_typed_username(monkeypatch):
    class _NoTransport(_Client):
        def get_transport(self):
            raise OSError("transport gone")

    client = _NoTransport({"pwd": _Result(out=b"/\n")})
    monkeypatch.setattr(fsmod, "_open_ssh_client", lambda *a, **k: client)
    assert SSHFileSystem("web1", username="deploy").username == "deploy"


def test_ssh_without_a_username_shows_a_question_mark(monkeypatch):
    class _NoTransport(_Client):
        def get_transport(self):
            raise OSError("transport gone")

    client = _NoTransport({"pwd": _Result(out=b"/\n")})
    monkeypatch.setattr(fsmod, "_open_ssh_client", lambda *a, **k: client)
    assert SSHFileSystem("web1").username == "?"


def test_ssh_takes_the_username_from_a_user_at_host(monkeypatch):
    client = _Client({"pwd": _Result(out=b"/\n")})
    monkeypatch.setattr(fsmod, "_open_ssh_client", lambda *a, **k: client)
    assert SSHFileSystem("deploy@web1").typed_username == "deploy"


# -- running commands ----------------------------------------------------------

def test_a_failing_command_raises_with_the_error_text(ssh_fs):
    fs, _ = ssh_fs({"mkdir": _Result(err=b"mkdir: permission denied\n",
                                     status=1)})
    with pytest.raises(FileSystemError, match="permission denied"):
        fs.mkdir("/root/new")


def test_a_failing_command_with_no_message_still_reports(ssh_fs):
    fs, _ = ssh_fs({"mkdir": _Result(status=3)})
    with pytest.raises(FileSystemError, match=r"command failed \(3\)"):
        fs.mkdir("/x")


def test_paths_are_shell_quoted(ssh_fs):
    fs, client = ssh_fs({"ls -lA": _Result(out=b"")})
    fs.listdir("/srv/my files")
    assert "'/srv/my files'" in client.commands[-1]


# -- listing -------------------------------------------------------------------

LISTING = (b"total 12\n"
           b"drwxr-xr-x 2 o g 4096 Jan 15 10:30 pub\n"
           b"-rw-r--r-- 1 o g  842 Jan 15 10:31 readme.txt\n"
           b"drwxr-xr-x 2 o g 4096 Jan 15 10:30 .\n"
           b"\n")


def test_ssh_listing(ssh_fs):
    fs, _ = ssh_fs({"ls -lA": _Result(out=LISTING)})
    by_name = {e.name: e for e in fs.listdir("/srv")}
    assert set(by_name) == {"pub", "readme.txt"}
    assert by_name["pub"].is_dir is True
    assert by_name["readme.txt"].size == 842


def test_ssh_stat(ssh_fs):
    fs, _ = ssh_fs({"ls -ldA": _Result(
        out=b"-rw-r--r-- 1 o g 842 Jan 15 10:31 /srv/readme.txt\n")})
    entry = fs.stat("/srv/readme.txt")
    assert entry.name == "readme.txt"
    assert entry.size == 842


def test_ssh_stat_of_the_root(ssh_fs):
    fs, _ = ssh_fs()
    assert fs.stat("/").name == "/"


def test_ssh_stat_of_an_unparsable_reply_raises(ssh_fs):
    fs, _ = ssh_fs({"ls -ldA": _Result(out=b"nonsense\n")})
    with pytest.raises(FileSystemError, match="No such path"):
        fs.stat("/srv/x")


def test_ssh_is_dir_and_exists_use_test(ssh_fs):
    fs, client = ssh_fs({"test -d": _Result(), "test -e": _Result()})
    assert fs.is_dir("/srv") is True
    assert fs.exists("/srv") is True
    assert "test -d /srv" in client.commands
    assert "test -e /srv" in client.commands


def test_ssh_is_dir_and_exists_are_false_when_test_fails(ssh_fs):
    fs, _ = ssh_fs({"test -d": _Result(status=1),
                    "test -e": _Result(status=1)})
    assert fs.is_dir("/nope") is False
    assert fs.exists("/nope") is False


# -- mutations -----------------------------------------------------------------

def test_ssh_mutations_issue_the_expected_commands(ssh_fs):
    fs, client = ssh_fs()
    fs.mkdir("/a")
    fs.makedirs("/a/b/c")
    fs.remove("/f")
    fs.rmdir("/a")
    fs.delete_tree("/tree")
    fs.rename("/a", "/b")
    joined = "\n".join(client.commands)
    assert "mkdir /a" in joined
    assert "mkdir -p /a/b/c" in joined
    assert "rm -f /f" in joined
    assert "rmdir /a" in joined
    assert "rm -rf /tree" in joined
    assert "mv /a /b" in joined


def test_ssh_utime_prefers_the_gnu_form(ssh_fs):
    fs, client = ssh_fs({"touch": _Result()})
    fs.utime("/a", 1705315800.0)
    assert client.commands[-1].startswith("touch -d @1705315800")


def test_ssh_utime_falls_back_to_the_posix_form(ssh_fs):
    def touch(command):
        return _Result(status=1) if "-d @" in command else _Result()

    fs, client = ssh_fs({"touch": touch})
    fs.utime("/a", 1705315800.0)
    assert client.commands[-1].startswith("touch -t ")


def test_ssh_utime_gives_up_quietly(ssh_fs):
    fs, _ = ssh_fs({"touch": _Result(status=1)})
    fs.utime("/a", 1705315800.0)          # must not raise


def test_ssh_close(ssh_fs):
    fs, client = ssh_fs()
    fs.close()
    assert client.closed is True


# -- reading: the cat / dd / scp chain -----------------------------------------

def test_read_uses_cat_when_it_works(ssh_fs):
    fs, client = ssh_fs({"cat ": _Result(out=b"file contents")})
    reader = fs.open_read("/a.txt")
    assert reader.read(-1) == b"file contents"
    assert client.commands[-1].startswith("cat ")


def test_read_falls_back_to_dd_when_cat_is_missing(ssh_fs):
    fs, client = ssh_fs({
        "cat ": _Result(err=b"cat: not found\n", status=127),
        "dd if=": _Result(out=b"file contents"),
    })
    assert fs.open_read("/a.txt").read(-1) == b"file contents"
    assert any(c.startswith("dd if=") for c in client.commands)
    # The strategy that worked is tried first next time.
    assert fs._read_templates[0] == "dd if={p}"


def test_an_empty_file_is_not_mistaken_for_a_failure(ssh_fs):
    fs, _ = ssh_fs({"cat ": _Result(out=b"", status=0)})
    assert fs.open_read("/empty.txt").read(-1) == b""


def test_a_failing_command_with_no_stderr_is_still_reported(ssh_fs):
    fs, _ = ssh_fs({
        "cat ": _Result(status=1),
        "dd if=": _Result(status=1),
        "scp": _Result(status=1),
    })
    with pytest.raises(FileSystemError, match="cat: failed"):
        fs.open_read("/a.txt")


def test_read_falls_through_to_scp(ssh_fs):
    channel = _SCPChannel(b"C0644 5 a.txt\nhello\x00")
    fs, _ = ssh_fs(
        {"cat ": _Result(status=1), "dd if=": _Result(status=1)},
        transport=_Transport(session=channel))
    reader = fs.open_read("/a.txt")
    assert reader.read(-1) == b"hello"
    assert fs._read_templates[0] == "scp"
    assert channel.command == "scp -f /a.txt"


def test_read_reports_when_nothing_works(ssh_fs):
    fs, _ = ssh_fs(
        {"cat ": _Result(err=b"cat: not found\n", status=127),
         "dd if=": _Result(err=b"dd: not found\n", status=127)},
        transport=_Transport(session=OSError("no session")))
    with pytest.raises(FileSystemError) as info:
        fs.open_read("/a.txt")
    assert "cat: not found" in str(info.value)
    assert "use the SFTP mode instead" in str(info.value)


def test_promote_moves_a_strategy_to_the_front(ssh_fs):
    fs, _ = ssh_fs()
    templates = ["a", "b", "c"]
    fs._promote(templates, "c")
    assert templates == ["c", "a", "b"]
    fs._promote(templates, "c")           # already first: unchanged
    assert templates == ["c", "a", "b"]
    fs._promote(templates, "absent")      # unknown: unchanged
    assert templates == ["c", "a", "b"]


# -- the scp wire protocol -----------------------------------------------------

def test_scp_header_parsing():
    assert _parse_scp_header("C0644 1234 name.txt\n") == 1234
    assert _parse_scp_header("C0755 0 empty\n") == 0
    with pytest.raises(FileSystemError, match="malformed header"):
        _parse_scp_header("garbage\n")


def test_scp_read_line_raises_on_an_error_frame():
    assert _scp_read_line(_SCPChannel(b"C0644 1 a\n")) == "C0644 1 a\n"
    with pytest.raises(FileSystemError, match="no such file"):
        _scp_read_line(_SCPChannel(b"\x01no such file\n"))
    with pytest.raises(FileSystemError, match="fatal"):
        _scp_read_line(_SCPChannel(b"\x02fatal\n"))


def test_scp_read_line_raises_when_the_channel_closes():
    with pytest.raises(FileSystemError, match="connection closed"):
        _scp_read_line(_SCPChannel(b""))


def test_scp_expect_ok():
    _scp_expect_ok(_SCPChannel(b"\x00"))
    with pytest.raises(FileSystemError, match="denied"):
        _scp_expect_ok(_SCPChannel(b"\x01denied\n"))
    with pytest.raises(FileSystemError, match="connection closed"):
        _scp_expect_ok(_SCPChannel(b""))


def test_scp_expect_ok_on_an_unterminated_error():
    with pytest.raises(FileSystemError, match="partial"):
        _scp_expect_ok(_SCPChannel(b"\x01partial"))


def test_scp_read_skips_a_timestamp_preamble(ssh_fs):
    channel = _SCPChannel(b"T1700000000 0 1700000000 0\nC0644 2 a\nhi\x00")
    fs, _ = ssh_fs({"cat ": _Result(status=1), "dd if=": _Result(status=1)},
                   transport=_Transport(session=channel))
    assert fs.open_read("/a.txt").read(-1) == b"hi"


def test_scp_read_rejects_an_unexpected_response(ssh_fs):
    channel = _SCPChannel(b"D0755 0 dir\n")
    fs, _ = ssh_fs({"cat ": _Result(status=1), "dd if=": _Result(status=1)},
                   transport=_Transport(session=channel))
    with pytest.raises(FileSystemError, match="use the SFTP mode"):
        fs.open_read("/a.txt")
    assert channel.closed is True


def test_scp_read_closes_the_channel_even_if_closing_fails(ssh_fs):
    channel = _SCPChannel(b"garbage\n")
    channel.close_error = OSError("already gone")
    fs, _ = ssh_fs({"cat ": _Result(status=1), "dd if=": _Result(status=1)},
                   transport=_Transport(session=channel))
    with pytest.raises(FileSystemError):
        fs.open_read("/a.txt")


def test_scp_reader_reads_in_pieces():
    channel = _SCPChannel(b"hello world\x00")
    reader = _SCPReader(channel, 11)
    assert reader.read(5) == b"hello"
    assert reader.read(-1) == b" world"
    assert reader.read(10) == b""


def test_scp_reader_stops_at_a_short_channel():
    reader = _SCPReader(_SCPChannel(b"abc"), 10)
    assert reader.read(-1) == b"abc"


def test_scp_reader_closes_quietly():
    channel = _SCPChannel(b"")
    channel.close_error = OSError("gone")
    _SCPReader(channel, 0).close()        # must not raise


# -- the plain SSH reader ------------------------------------------------------

def test_ssh_reader_keeps_the_probe_byte():
    reader = _SSHReader(_Stdout(b"ello"), first=b"h")
    assert reader.read(-1) == b"hello"


def test_ssh_reader_reads_an_exact_count():
    reader = _SSHReader(_Stdout(b"llo world"), first=b"he")
    assert reader.read(5) == b"hello"
    assert reader.read(100) == b" world"


def test_ssh_reader_close_waits_for_the_exit_status():
    stdout = _Stdout(b"", status=0)
    _SSHReader(stdout).close()
    stdout.channel.status = OSError("channel gone")
    _SSHReader(stdout).close()            # must not raise


# -- writing -------------------------------------------------------------------

def test_write_uses_cat_when_it_works(ssh_fs):
    fs, client = ssh_fs({"cat >": _Result()})
    writer = fs.open_write("/a.txt")
    assert writer.write(b"hello ") == 6
    writer.write(b"world")
    writer.close()
    assert client.stdins[-1].written == b"hello world"
    assert fs._write_templates[0] == "cat > {p}"


def test_closing_a_writer_twice_is_harmless(ssh_fs):
    fs, _ = ssh_fs({"cat >": _Result()})
    writer = fs.open_write("/a.txt")
    writer.write(b"x")
    writer.close()
    writer.close()


def test_write_retries_with_dd_when_cat_reports_failure(ssh_fs):
    fs, client = ssh_fs({"cat >": _Result(status=127), "dd of=": _Result()})
    writer = fs.open_write("/a.txt")
    writer.write(b"payload")
    writer.close()
    assert any(c.startswith("dd of=") for c in client.commands)
    assert client.stdins[-1].written == b"payload"
    assert fs._write_templates[0] == "dd of={p}"


def test_write_retries_when_the_channel_dies_mid_stream(ssh_fs):
    fs, client = ssh_fs({"cat >": _Result(stdin_fail=True), "dd of=": _Result()})
    writer = fs.open_write("/a.txt")
    writer.write(b"payload")              # the pipe breaks, quietly buffered
    writer.close()
    assert client.stdins[-1].written == b"payload"


def test_write_falls_through_to_scp(ssh_fs):
    channel = _SCPChannel(b"\x00\x00\x00")
    fs, _ = ssh_fs({"cat >": _Result(status=127), "dd of=": _Result(status=127)},
                   transport=_Transport(session=channel))
    writer = fs.open_write("/a.txt")
    writer.write(b"hello")
    writer.close()
    assert b"C0644 5 a.txt\n" in channel.sent
    assert channel.sent.endswith(b"hello\x00")
    assert fs._write_templates[0] == "scp"


def test_an_empty_file_over_scp_sends_no_body(ssh_fs):
    channel = _SCPChannel(b"\x00\x00\x00")
    fs, _ = ssh_fs({"cat >": _Result(status=1), "dd of=": _Result(status=1)},
                   transport=_Transport(session=channel))
    writer = fs.open_write("/a.txt")
    writer.close()
    assert b"C0644 0 a.txt\n" in channel.sent


def test_scp_write_names_an_unnamed_path(ssh_fs):
    channel = _SCPChannel(b"\x00\x00\x00")
    fs, _ = ssh_fs(transport=_Transport(session=channel))
    fs._scp_write("/", b"x")
    assert b"C0644 1 file\n" in channel.sent


def test_write_reports_when_every_method_fails(ssh_fs):
    channel = _SCPChannel(b"\x01scp refused\n")
    fs, _ = ssh_fs({"cat >": _Result(status=1), "dd of=": _Result(status=1)},
                   transport=_Transport(session=channel))
    writer = fs.open_write("/a.txt")
    writer.write(b"payload")
    with pytest.raises(FileSystemError, match="Cannot write file"):
        writer.close()


def test_write_reports_a_retry_that_also_dies(ssh_fs):
    fs, _ = ssh_fs({"cat >": _Result(status=1),
                    "dd of=": _Result(stdin_fail=True)},
                   transport=_Transport(session=OSError("no session")))
    writer = fs.open_write("/a.txt")
    writer.write(b"payload")
    with pytest.raises(FileSystemError, match="Cannot write file"):
        writer.close()


def test_a_writer_whose_exit_status_cannot_be_read_retries(ssh_fs):
    def cat(command):
        return _Result(status=OSError("channel closed"))

    fs, client = ssh_fs({"cat >": cat, "dd of=": _Result()})
    writer = fs.open_write("/a.txt")
    writer.write(b"payload")
    writer.close()
    assert client.stdins[-1].written == b"payload"


def test_a_file_past_the_replay_cap_cannot_be_retried(ssh_fs, monkeypatch):
    from meridian_commander.filesystems import _SSHWriter

    monkeypatch.setattr(_SSHWriter, "_CAP", 4)
    fs, _ = ssh_fs({"cat >": _Result(status=1), "dd of=": _Result()})
    writer = fs.open_write("/a.txt")
    writer.write(b"far too long to buffer")
    with pytest.raises(FileSystemError, match="too large to retry"):
        writer.close()


def test_a_broken_pipe_past_the_cap_is_raised_immediately(ssh_fs, monkeypatch):
    from meridian_commander.filesystems import _SSHWriter

    monkeypatch.setattr(_SSHWriter, "_CAP", 4)
    fs, _ = ssh_fs({"cat >": _Result(stdin_fail=True)})
    writer = fs.open_write("/a.txt")
    with pytest.raises(OSError, match="broken pipe"):
        writer.write(b"far too long to buffer")


def test_scp_needs_the_whole_file_buffered(ssh_fs, monkeypatch):
    from meridian_commander.filesystems import _SSHWriter

    monkeypatch.setattr(_SSHWriter, "_CAP", 4)
    fs, _ = ssh_fs()
    fs._write_templates = ["scp"]
    writer = fs.open_write("/a.txt")
    with pytest.raises(FileSystemError, match="needs the whole file buffered"):
        writer.write(b"far too long to buffer")


def test_a_writer_that_cannot_half_close_still_finishes(ssh_fs):
    fs, client = ssh_fs({"cat >": _Result()})
    writer = fs.open_write("/a.txt")
    writer.write(b"x")
    writer._stdin.channel.shutdown_write = lambda: 1 / 0
    writer.close()                        # must not raise


@pytest.mark.parametrize("value", ["Jan 15", "Jan 15 10:30 extra", ""])
def test_the_ls_date_parser_needs_exactly_three_tokens(value):
    from meridian_commander.filesystems import _parse_ls_date

    assert _parse_ls_date(value) is None


def test_the_ls_date_parser_rejects_nonsense():
    from meridian_commander.filesystems import _parse_ls_date

    assert _parse_ls_date("Xxx 99 99:99") is None
    assert _parse_ls_date("Xxx 99 1999") is None


def test_scp_write_closes_the_channel_even_if_closing_fails(ssh_fs):
    channel = _SCPChannel(b"\x00\x00\x00")
    channel.close_error = OSError("already gone")
    fs, _ = ssh_fs(transport=_Transport(session=channel))
    fs._scp_write("/a.txt", b"x")         # must not raise


def test_the_scp_reader_ignores_a_channel_that_will_not_acknowledge():
    class _Deaf(_SCPChannel):
        def sendall(self, data):
            raise OSError("connection reset")

    reader = _SCPReader(_Deaf(b"hello"), 5)
    assert reader.read(-1) == b"hello"    # the trailing ack failure is ignored


def test_a_writer_that_starts_with_scp_sends_at_close(ssh_fs):
    channel = _SCPChannel(b"\x00\x00\x00")
    fs, _ = ssh_fs(transport=_Transport(session=channel))
    fs._write_templates = ["scp", "cat > {p}"]
    writer = fs.open_write("/a.txt")
    assert writer.write(b"hello") == 5    # buffered, nothing sent yet
    assert channel.sent == b""
    writer.close()
    assert channel.sent.endswith(b"hello\x00")
    assert fs._write_templates[0] == "scp"
