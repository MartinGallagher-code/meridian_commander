"""SSH connection resolution and the SFTP backend, against paramiko fakes."""

from __future__ import annotations

import stat as stat_mod

import pytest

from meridian_commander import filesystems as fsmod
from meridian_commander.filesystems import (
    FileSystemError,
    SFTPFileSystem,
    _close_ssh_client,
    _connect_resolved,
    _open_ssh_client,
    _parse_jump_spec,
    _resolve_ssh_connection,
    _split_user_host,
)


# -- paramiko-shaped fakes -----------------------------------------------------

class _Attr:
    def __init__(self, filename, mode=0o100644, size=10, mtime=1000):
        self.filename = filename
        self.st_mode = mode
        self.st_size = size
        self.st_mtime = mtime


DIR_MODE = stat_mod.S_IFDIR | 0o755
LINK_MODE = stat_mod.S_IFLNK | 0o777
FILE_MODE = stat_mod.S_IFREG | 0o644


class _FakeSftp:
    def __init__(self, listing=None, stats=None, home="/home/deploy"):
        self.listing = listing or []
        self.stats = stats or {}
        self.home = home
        self.calls: list = []
        self.closed = False
        self.files: dict = {}
        self.posix_rename_supported = True

    def normalize(self, path):
        return self.home

    def listdir_attr(self, path):
        self.calls.append(("listdir_attr", path))
        return self.listing

    def stat(self, path):
        self.calls.append(("stat", path))
        if path in self.stats:
            return self.stats[path]
        raise FileNotFoundError(path)

    def open(self, path, mode):
        self.calls.append(("open", path, mode))
        return _FakeSftpHandle(self, path)

    def utime(self, path, times):
        self.calls.append(("utime", path, times))

    def mkdir(self, path):
        self.calls.append(("mkdir", path))

    def remove(self, path):
        self.calls.append(("remove", path))

    def rmdir(self, path):
        self.calls.append(("rmdir", path))

    def posix_rename(self, src, dst):
        if not self.posix_rename_supported:
            raise OSError("extension not supported")
        self.calls.append(("posix_rename", src, dst))

    def rename(self, src, dst):
        self.calls.append(("rename", src, dst))

    def close(self):
        self.closed = True


class _FakeSftpHandle:
    def __init__(self, sftp, path, prefetch_ok=True):
        self.sftp = sftp
        self.path = path
        self.prefetched = False
        self.prefetch_ok = prefetch_ok

    def prefetch(self):
        if not self.prefetch_ok:
            raise OSError("prefetch unsupported")
        self.prefetched = True


class _FakeTransport:
    def __init__(self, username="deploy", active=True):
        self._username = username
        self._active = active
        self.channels: list = []

    def get_username(self):
        return self._username

    def is_active(self):
        return self._active

    def open_channel(self, kind, dest, src):
        self.channels.append((kind, dest, src))
        return f"channel-to-{dest[0]}:{dest[1]}"


class _FakeClient:
    def __init__(self, sftp=None, transport=None, sftp_error=None,
                 transport_error=False):
        self._sftp = sftp
        self._transport = transport if transport is not None else _FakeTransport()
        self._sftp_error = sftp_error
        self._transport_error = transport_error
        self.closed = False

    def get_transport(self):
        if self._transport_error:
            raise OSError("transport gone")
        return self._transport

    def open_sftp(self):
        if self._sftp_error is not None:
            raise self._sftp_error
        return self._sftp

    def close(self):
        self.closed = True


@pytest.fixture
def sftp_fs(monkeypatch):
    """Factory building an SFTPFileSystem over a fake client."""

    def make(sftp=None, client=None, **kwargs):
        sftp = sftp if sftp is not None else _FakeSftp()
        client = client or _FakeClient(sftp=sftp)
        monkeypatch.setattr(fsmod, "_open_ssh_client",
                            lambda *a, **k: client)
        return SFTPFileSystem("web1", username="deploy", **kwargs), sftp, client

    return make


# -- user@host splitting -------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("host", (None, "host")),
    ("user@host", ("user", "host")),
    ("a@b@host", ("a@b", "host")),
    ("@host", (None, "@host")),
    ("user@", (None, "user@")),
])
def test_split_user_host(value, expected):
    assert _split_user_host(value) == expected


# -- jump specs ----------------------------------------------------------------

@pytest.mark.parametrize("spec, expected", [
    ("gw", [(None, "gw", None)]),
    ("user@gw", [("user", "gw", None)]),
    ("gw:2222", [(None, "gw", 2222)]),
    ("user@gw:2222", [("user", "gw", 2222)]),
    ("a,b", [(None, "a", None), (None, "b", None)]),
    (" a , b ", [(None, "a", None), (None, "b", None)]),
    ("a,,b", [(None, "a", None), (None, "b", None)]),
    ("", []),
    ("gw:notaport", [(None, "gw:notaport", None)]),
])
def test_parse_jump_spec(spec, expected):
    assert _parse_jump_spec(spec) == expected


# -- connection resolution -----------------------------------------------------

def _write_config(tmp_path, body):
    path = tmp_path / "config"
    path.write_text(body)
    return str(path)


def test_resolution_without_a_config_uses_what_was_given(tmp_path):
    res = _resolve_ssh_connection("web1", "deploy", 2222,
                                  config_path=str(tmp_path / "absent"))
    assert res["hostname"] == "web1"
    assert res["username"] == "deploy"
    assert res["port"] == 2222
    assert res["proxy_command"] is None


def test_resolution_defaults_the_user_and_port(tmp_path, monkeypatch):
    monkeypatch.setattr("getpass.getuser", lambda: "someone")
    res = _resolve_ssh_connection("web1", config_path=str(tmp_path / "absent"))
    assert res["username"] == "someone"
    assert res["port"] == 22


def test_resolution_takes_the_user_from_a_user_at_host(tmp_path):
    res = _resolve_ssh_connection("deploy@web1",
                                  config_path=str(tmp_path / "absent"))
    assert res["username"] == "deploy"
    assert res["hostname"] == "web1"


def test_resolution_reads_an_alias_from_the_config(tmp_path):
    config = _write_config(tmp_path, "Host web\n"
                                     "  HostName web1.example.com\n"
                                     "  User deploy\n"
                                     "  Port 2222\n")
    res = _resolve_ssh_connection("web", config_path=config)
    assert res["hostname"] == "web1.example.com"
    assert res["username"] == "deploy"
    assert res["port"] == 2222


def test_an_explicit_value_beats_the_config(tmp_path):
    config = _write_config(tmp_path, "Host web\n  User deploy\n  Port 2222\n")
    res = _resolve_ssh_connection("web", "someone", 2020, config_path=config)
    assert res["username"] == "someone"
    assert res["port"] == 2020


def test_the_dialog_default_port_lets_the_config_win(tmp_path):
    config = _write_config(tmp_path, "Host web\n  Port 2222\n")
    # 22 is what the connect dialog pre-fills, so it counts as "unset".
    assert _resolve_ssh_connection("web", port=22, config_path=config)["port"] == 2222


def test_an_unparsable_config_port_is_ignored(tmp_path):
    config = _write_config(tmp_path, "Host web\n  Port notanumber\n")
    assert _resolve_ssh_connection("web", config_path=config)["port"] == 22


def test_an_identity_file_is_used_when_it_exists(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("key")
    config = _write_config(tmp_path, f"Host web\n  IdentityFile {key}\n")
    assert _resolve_ssh_connection("web", config_path=config)["key_filename"] == \
        [str(key)]


def test_an_identity_file_that_does_not_exist_is_skipped(tmp_path):
    config = _write_config(tmp_path, "Host web\n  IdentityFile /no/such/key\n")
    assert _resolve_ssh_connection("web", config_path=config)["key_filename"] is None


def test_an_explicit_key_beats_the_config(tmp_path):
    key = tmp_path / "id_test"
    key.write_text("key")
    config = _write_config(tmp_path, f"Host web\n  IdentityFile {key}\n")
    res = _resolve_ssh_connection("web", key_filename="/mine", config_path=config)
    assert res["key_filename"] == "/mine"


def test_a_proxy_command_is_picked_up(tmp_path):
    config = _write_config(tmp_path, "Host web\n  ProxyCommand nc %h %p\n")
    res = _resolve_ssh_connection("web", config_path=config)
    assert res["proxy_command"].startswith("nc ")
    assert res["proxy_jump"] is None


def test_a_proxy_jump_is_picked_up(tmp_path):
    config = _write_config(tmp_path, "Host web\n  ProxyJump gateway\n")
    res = _resolve_ssh_connection("web", config_path=config)
    assert res["proxy_jump"] == "gateway"


def test_a_proxy_command_wins_over_a_proxy_jump(tmp_path):
    config = _write_config(tmp_path,
                           "Host web\n  ProxyCommand nc %h %p\n  ProxyJump gw\n")
    res = _resolve_ssh_connection("web", config_path=config)
    assert res["proxy_command"] is not None
    assert res["proxy_jump"] is None


def test_a_damaged_config_does_not_stop_a_plain_connection(tmp_path):
    config = _write_config(tmp_path, "Host web\n  Port\n\x00garbage\n")
    res = _resolve_ssh_connection("web1", "deploy", config_path=config)
    assert res["hostname"] == "web1"


def test_resolution_without_paramiko_still_works(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_paramiko(name, *args, **kwargs):
        if name == "paramiko":
            raise ImportError("no paramiko")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_paramiko)
    config = _write_config(tmp_path, "Host web\n  HostName ignored\n")
    res = _resolve_ssh_connection("web", "deploy", config_path=config)
    # Without paramiko the config cannot be read; the typed host is used as-is.
    assert res["hostname"] == "web"


# -- opening a client ----------------------------------------------------------

def test_open_client_connects_to_the_resolved_destination(monkeypatch, tmp_path):
    seen = {}

    def fake_connect(res, password, sock):
        seen.update(res=res, password=password, sock=sock)
        return _FakeClient()

    monkeypatch.setattr(fsmod, "_connect_resolved", fake_connect)
    client = _open_ssh_client("web1", "deploy", "pw", 22,
                              config_path=str(tmp_path / "absent"))
    assert seen["res"]["hostname"] == "web1"
    assert seen["password"] == "pw"
    assert seen["sock"] is None
    assert client._mc_jump_clients == []


def test_a_failed_connection_becomes_a_filesystem_error(monkeypatch, tmp_path):
    def refuse(res, password, sock):
        raise OSError("connection refused")

    monkeypatch.setattr(fsmod, "_connect_resolved", refuse)
    with pytest.raises(FileSystemError, match="Could not connect to web1"):
        _open_ssh_client("web1", "deploy", None, 22,
                         config_path=str(tmp_path / "absent"))


def test_a_legacy_key_error_becomes_an_actionable_message(monkeypatch, tmp_path):
    def refuse(res, password, sock):
        raise ValueError(
            "error:0308010C:digital envelope routines::unsupported")

    monkeypatch.setattr(fsmod, "_connect_resolved", refuse)
    with pytest.raises(FileSystemError) as info:
        _open_ssh_client("web1", "deploy", None, 22,
                         config_path=str(tmp_path / "absent"))
    message = str(info.value)
    # The remedy is named, and the raw OpenSSL text is kept for the record.
    assert "legacy format" in message
    assert "ssh-keygen -p" in message
    assert "digital envelope routines" in message


def test_a_changed_host_key_becomes_an_actionable_message(monkeypatch, tmp_path):
    import paramiko

    class _K:
        def get_name(self):
            return "ssh-ed25519"

        def get_base64(self):
            return "AAAA"

    def refuse(res, password, sock):
        raise paramiko.ssh_exception.BadHostKeyException("web1", _K(), _K())

    monkeypatch.setattr(fsmod, "_connect_resolved", refuse)
    with pytest.raises(FileSystemError) as info:
        _open_ssh_client("web1", "deploy", None, 22,
                         config_path=str(tmp_path / "absent"))
    message = str(info.value)
    assert "host key has CHANGED" in message
    assert "ssh-keygen -R web1" in message


def test_known_hosts_are_loaded_and_the_user_file_is_writable(monkeypatch, tmp_path):
    import paramiko

    known = tmp_path / "known_hosts"
    known.write_text("")                       # exists -> load_host_keys path
    monkeypatch.setattr(fsmod, "_user_known_hosts", lambda: str(known))

    seen = {}

    class _Client:
        def load_system_host_keys(self):
            seen["system"] = True

        def load_host_keys(self, filename):
            seen["user"] = filename

        def set_missing_host_key_policy(self, policy):
            seen["policy"] = policy

        def connect(self, **kwargs):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", _Client)
    _connect_resolved({"hostname": "h", "port": 22, "username": "u",
                       "key_filename": None}, None, None)
    assert seen["system"] is True
    assert seen["user"] == str(known)
    assert isinstance(seen["policy"], paramiko.MissingHostKeyPolicy)


def test_an_absent_known_hosts_is_still_named_as_the_write_target(monkeypatch,
                                                                  tmp_path):
    import paramiko

    absent = tmp_path / "nope" / "known_hosts"
    monkeypatch.setattr(fsmod, "_user_known_hosts", lambda: str(absent))

    class _Client:
        def load_system_host_keys(self):
            pass

        def set_missing_host_key_policy(self, policy):
            pass

        def connect(self, **kwargs):
            pass

    monkeypatch.setattr(paramiko, "SSHClient", _Client)
    client = paramiko.SSHClient()
    fsmod._load_known_hosts(client, paramiko)
    # No file to load, but new keys have somewhere to be saved.
    assert client._host_keys_filename == str(absent)


def test_pin_policy_records_a_new_host_and_saves_it(monkeypatch):
    import paramiko

    saved = {}

    class _HostKeys:
        def add(self, hostname, keytype, key):
            saved["added"] = (hostname, keytype)

    class _Client:
        _host_keys_filename = "/somewhere/known_hosts"

        def get_host_keys(self):
            return _HostKeys()

        def save_host_keys(self, filename):
            saved["saved"] = filename

    class _Key:
        def get_name(self):
            return "ssh-ed25519"

    monkeypatch.setattr(fsmod.os, "makedirs", lambda *a, **k: None)
    policy = fsmod._pin_new_host_policy(paramiko)
    policy.missing_host_key(_Client(), "newhost", _Key())
    assert saved["added"] == ("newhost", "ssh-ed25519")
    assert saved["saved"] == "/somewhere/known_hosts"


def test_user_known_hosts_points_into_dot_ssh():
    assert fsmod._user_known_hosts().endswith("/.ssh/known_hosts")


def test_pin_policy_without_a_known_hosts_file_only_records_in_memory():
    import paramiko

    added = {}

    class _HostKeys:
        def add(self, hostname, keytype, key):
            added["key"] = (hostname, keytype)

    class _Client:
        _host_keys_filename = None   # nowhere to save

        def get_host_keys(self):
            return _HostKeys()

    class _Key:
        def get_name(self):
            return "ssh-ed25519"

    policy = fsmod._pin_new_host_policy(paramiko)
    policy.missing_host_key(_Client(), "h", _Key())   # returns without saving
    assert added["key"] == ("h", "ssh-ed25519")


def test_load_known_hosts_tolerates_a_failing_system_load(monkeypatch, tmp_path):
    import paramiko

    absent = tmp_path / "known_hosts"
    monkeypatch.setattr(fsmod, "_user_known_hosts", lambda: str(absent))

    class _Client:
        def load_system_host_keys(self):
            raise OSError("system file broken")

    client = _Client()
    fsmod._load_known_hosts(client, paramiko)
    assert client._host_keys_filename == str(absent)


def test_load_known_hosts_tolerates_a_failing_user_load(monkeypatch, tmp_path):
    import paramiko

    known = tmp_path / "known_hosts"
    known.write_text("")                       # exists -> load_host_keys tried
    monkeypatch.setattr(fsmod, "_user_known_hosts", lambda: str(known))

    class _Client:
        def load_system_host_keys(self):
            pass

        def load_host_keys(self, filename):
            raise OSError("corrupt known_hosts")

    client = _Client()
    fsmod._load_known_hosts(client, paramiko)
    # The load failed, but the file is still named as the write target.
    assert client._host_keys_filename == str(known)


def test_pin_policy_tolerates_an_unwritable_known_hosts(monkeypatch):
    import paramiko

    class _HostKeys:
        def add(self, *a):
            pass

    class _Client:
        _host_keys_filename = "/somewhere/known_hosts"

        def get_host_keys(self):
            return _HostKeys()

        def save_host_keys(self, filename):
            raise OSError("read-only file system")

    class _Key:
        def get_name(self):
            return "ssh-rsa"

    monkeypatch.setattr(fsmod.os, "makedirs", lambda *a, **k: None)
    policy = fsmod._pin_new_host_policy(paramiko)
    # A save failure must not propagate -- the connection still succeeds.
    policy.missing_host_key(_Client(), "h", _Key())


def test_a_proxy_command_is_wired_into_the_connection(monkeypatch, tmp_path):
    import paramiko

    seen = {}
    monkeypatch.setattr(paramiko, "ProxyCommand", lambda cmd: f"sock({cmd})")

    def record(res, password, sock):
        seen["sock"] = sock
        return _FakeClient()

    monkeypatch.setattr(fsmod, "_connect_resolved", record)
    config = _write_config(tmp_path, "Host web\n  ProxyCommand nc %h %p\n")
    _open_ssh_client("web", None, None, None, config_path=config)
    assert seen["sock"].startswith("sock(nc ")


def test_a_failing_proxy_command_is_reported(monkeypatch, tmp_path):
    import paramiko

    def explode(cmd):
        raise OSError("nc not found")

    monkeypatch.setattr(paramiko, "ProxyCommand", explode)
    config = _write_config(tmp_path, "Host web\n  ProxyCommand nc %h %p\n")
    with pytest.raises(FileSystemError, match="ProxyCommand failed"):
        _open_ssh_client("web", None, None, None, config_path=config)


def test_a_proxy_jump_chain_is_tunnelled(monkeypatch, tmp_path):
    gateway = _FakeClient()
    target = _FakeClient()
    made = []

    def fake_connect(res, password, sock):
        made.append((res["hostname"], res["port"], sock))
        return target

    monkeypatch.setattr(fsmod, "_connect_resolved", fake_connect)

    real_open = fsmod._open_ssh_client

    def first_hop(host, *args, **kwargs):
        if host == "gw":
            return gateway
        return real_open(host, *args, **kwargs)

    monkeypatch.setattr(fsmod, "_open_ssh_client", first_hop)
    config = _write_config(tmp_path,
                           "Host web\n  HostName web1\n  ProxyJump gw\n")
    client = real_open("web", None, None, None, config_path=config)
    assert made[-1][2] == "channel-to-web1:22"
    assert client._mc_jump_clients == [gateway]


def test_a_multi_hop_proxy_jump_chains_through_each_gateway(monkeypatch,
                                                            tmp_path):
    first = _FakeClient()
    made = []

    def fake_connect(res, password, sock):
        made.append((res["hostname"], sock))
        return _FakeClient()

    monkeypatch.setattr(fsmod, "_connect_resolved", fake_connect)
    real_open = fsmod._open_ssh_client
    monkeypatch.setattr(fsmod, "_open_ssh_client",
                        lambda host, *a, **k: first if host == "a"
                        else real_open(host, *a, **k))
    config = _write_config(tmp_path, "Host web\n  ProxyJump a,b\n")
    real_open("web", None, None, None, config_path=config)
    # The second hop is reached through a channel on the first.
    assert made[0][1] == "channel-to-b:22"


def test_a_failing_proxy_jump_is_reported_and_cleaned_up(monkeypatch, tmp_path):
    gateway = _FakeClient()
    real_open = fsmod._open_ssh_client

    def hop(host, *args, **kwargs):
        if host == "gw":
            return gateway
        return real_open(host, *args, **kwargs)

    monkeypatch.setattr(fsmod, "_open_ssh_client", hop)
    gateway._transport = None
    gateway.get_transport = lambda: (_ for _ in ()).throw(OSError("gone"))
    config = _write_config(tmp_path, "Host web\n  ProxyJump gw\n")
    with pytest.raises(FileSystemError, match="ProxyJump via 'gw' failed"):
        real_open("web", None, None, None, config_path=config)
    assert gateway.closed is True


def test_an_empty_proxy_jump_spec_leaves_the_socket_unset(monkeypatch, tmp_path):
    seen = {}

    def record(res, password, sock):
        seen["sock"] = sock
        return _FakeClient()

    monkeypatch.setattr(fsmod, "_connect_resolved", record)
    config = _write_config(tmp_path, 'Host web\n  ProxyJump ","\n')
    _open_ssh_client("web", None, None, None, config_path=config)
    assert seen["sock"] is None


def test_a_jump_loop_is_stopped(monkeypatch, tmp_path):
    config = _write_config(tmp_path,
                           "Host a\n  ProxyJump b\nHost b\n  ProxyJump a\n")
    with pytest.raises(FileSystemError, match="too deep"):
        _open_ssh_client("a", None, None, None, config_path=config)


def test_connect_resolved_passes_the_expected_arguments(monkeypatch):
    import paramiko

    captured = {}

    class _Client:
        def load_system_host_keys(self):
            captured["loaded"] = True

        def load_host_keys(self, filename):
            captured["user_hosts"] = filename

        def set_missing_host_key_policy(self, policy):
            captured["policy"] = policy

        def connect(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(paramiko, "SSHClient", _Client)
    # Point known_hosts somewhere absent so the test does not touch a real one.
    monkeypatch.setattr(fsmod, "_user_known_hosts",
                        lambda: "/nonexistent/known_hosts")
    res = {"hostname": "web1", "port": 22, "username": "deploy",
           "key_filename": None}
    _connect_resolved(res, None, None)
    assert captured["hostname"] == "web1"
    assert captured["look_for_keys"] is True     # no password: try agent/keys
    assert captured["allow_agent"] is True

    _connect_resolved(res, "secret", "sock")
    assert captured["password"] == "secret"
    assert captured["look_for_keys"] is False
    assert captured["sock"] == "sock"


# -- closing -------------------------------------------------------------------

def test_closing_a_client_closes_its_jump_chain():
    inner = _FakeClient()
    outer = _FakeClient()
    outer._mc_jump_clients = [inner]
    _close_ssh_client(outer)
    assert outer.closed and inner.closed


def test_closing_nothing_is_harmless():
    _close_ssh_client(None)


def test_a_client_that_refuses_to_close_is_ignored():
    class _Stubborn:
        def close(self):
            raise OSError("already gone")

    _close_ssh_client(_Stubborn())


# -- the SFTP backend ----------------------------------------------------------

def test_sftp_identity_and_home(sftp_fs):
    fs, sftp, _ = sftp_fs()
    assert fs.scheme == "sftp"
    assert fs.label() == "sftp://deploy@web1"
    assert fs.home() == "/home/deploy"


def test_sftp_falls_back_to_the_root_when_home_is_unknown(sftp_fs):
    sftp = _FakeSftp()
    sftp.normalize = lambda path: (_ for _ in ()).throw(OSError("no cwd"))
    fs, _, _ = sftp_fs(sftp=sftp)
    assert fs.home() == "/"


def test_sftp_reports_the_username_it_authenticated_as(sftp_fs):
    client = _FakeClient(sftp=_FakeSftp(),
                         transport=_FakeTransport(username="realuser"))
    fs, _, _ = sftp_fs(client=client)
    assert fs.username == "realuser"


def test_sftp_falls_back_to_the_typed_username(sftp_fs):
    client = _FakeClient(sftp=_FakeSftp(), transport_error=True)
    fs, _, _ = sftp_fs(client=client)
    assert fs.username == "deploy"


def test_sftp_without_a_username_shows_a_question_mark(monkeypatch):
    client = _FakeClient(sftp=_FakeSftp(), transport_error=True)
    monkeypatch.setattr(fsmod, "_open_ssh_client", lambda *a, **k: client)
    fs = SFTPFileSystem("web1")
    assert fs.username == "?"


def test_sftp_takes_the_username_from_a_user_at_host(monkeypatch):
    client = _FakeClient(sftp=_FakeSftp())
    monkeypatch.setattr(fsmod, "_open_ssh_client", lambda *a, **k: client)
    fs = SFTPFileSystem("deploy@web1")
    assert fs.typed_username == "deploy"


def test_a_server_with_sftp_disabled_says_so(monkeypatch):
    client = _FakeClient(sftp_error=OSError("subsystem request failed"))
    monkeypatch.setattr(fsmod, "_open_ssh_client", lambda *a, **k: client)
    with pytest.raises(FileSystemError, match="SSH \\(shell\\) mode instead"):
        SFTPFileSystem("web1", username="deploy")
    assert client.closed is True


def test_sftp_listing(sftp_fs):
    listing = [
        _Attr("file.txt", mode=FILE_MODE, size=42, mtime=1234),
        _Attr("subdir", mode=DIR_MODE),
    ]
    fs, _, _ = sftp_fs(sftp=_FakeSftp(listing=listing))
    by_name = {e.name: e for e in fs.listdir("/srv")}
    assert by_name["file.txt"].size == 42
    assert by_name["file.txt"].mtime == 1234.0
    assert by_name["file.txt"].is_dir is False
    assert by_name["subdir"].is_dir is True


def test_sftp_listing_follows_a_link_to_decide_if_it_is_a_directory(sftp_fs):
    sftp = _FakeSftp(listing=[_Attr("link", mode=LINK_MODE)],
                     stats={"/srv/link": _Attr("link", mode=DIR_MODE)})
    fs, _, _ = sftp_fs(sftp=sftp)
    entry = fs.listdir("/srv")[0]
    assert entry.is_symlink is True
    assert entry.is_dir is True


def test_sftp_listing_treats_a_broken_link_as_a_file(sftp_fs):
    sftp = _FakeSftp(listing=[_Attr("dangling", mode=LINK_MODE)])
    fs, _, _ = sftp_fs(sftp=sftp)
    entry = fs.listdir("/srv")[0]
    assert entry.is_symlink is True
    assert entry.is_dir is False


def test_sftp_listing_copes_with_missing_attributes(sftp_fs):
    sftp = _FakeSftp(listing=[_Attr("odd", mode=None, size=None, mtime=None)])
    fs, _, _ = sftp_fs(sftp=sftp)
    entry = fs.listdir("/srv")[0]
    assert entry.size == 0
    assert entry.mtime is None
    assert entry.mode == 0


def test_sftp_stat(sftp_fs):
    sftp = _FakeSftp(stats={"/srv/a.txt": _Attr("a.txt", size=7, mtime=99)})
    fs, _, _ = sftp_fs(sftp=sftp)
    entry = fs.stat("/srv/a.txt")
    assert entry.name == "a.txt"
    assert entry.size == 7
    assert entry.mtime == 99.0
    assert entry.is_symlink is False


def test_sftp_stat_without_a_timestamp(sftp_fs):
    sftp = _FakeSftp(stats={"/a": _Attr("a", mode=None, size=None, mtime=None)})
    fs, _, _ = sftp_fs(sftp=sftp)
    assert fs.stat("/a").mtime is None


def test_sftp_open_read_asks_for_a_prefetch(sftp_fs):
    fs, sftp, _ = sftp_fs()
    handle = fs.open_read("/srv/big.iso")
    assert handle.prefetched is True
    assert ("open", "/srv/big.iso", "rb") in sftp.calls


def test_sftp_open_read_without_prefetch_support(sftp_fs):
    fs, sftp, _ = sftp_fs()
    sftp.open = lambda path, mode: _FakeSftpHandle(sftp, path, prefetch_ok=False)
    assert fs.open_read("/srv/big.iso").prefetched is False


def test_sftp_open_write(sftp_fs):
    fs, sftp, _ = sftp_fs()
    fs.open_write("/srv/new.txt")
    assert ("open", "/srv/new.txt", "wb") in sftp.calls


# -- reading and writing must not share one channel ----------------------------
#
# One SFTP session is one channel.  open_read() prefetches, so reading and
# writing down the same session puts a flood of read responses in front of
# every write -- and both panes share one connection after "=" or a reused
# preset, which makes a remote-to-remote copy a connection copying to itself.
# Against a real server that dropped roughly one copy in three.

class _SessionHandingClient(_FakeClient):
    """A client that opens a *distinct* session each time, as a server does."""

    def __init__(self, limit=None):
        super().__init__(sftp=_FakeSftp())
        self.sessions: list = []
        self.limit = limit               # sessions this server will allow

    def open_sftp(self):
        if self.limit is not None and len(self.sessions) >= self.limit:
            raise OSError("no more channels")
        session = self._sftp if not self.sessions else _FakeSftp()
        self.sessions.append(session)
        return session


def test_writes_go_through_a_second_session(sftp_fs):
    client = _SessionHandingClient()
    fs, _sftp, _ = sftp_fs(client=client)
    fs.open_read("/srv/big.iso")
    fs.open_write("/srv/copy.iso")

    assert len(client.sessions) == 2, "the write should open its own session"
    reads, writes = client.sessions
    assert ("open", "/srv/big.iso", "rb") in reads.calls
    assert ("open", "/srv/copy.iso", "wb") in writes.calls
    # The read never touches the write session, and vice versa.
    assert ("open", "/srv/copy.iso", "wb") not in reads.calls
    assert ("open", "/srv/big.iso", "rb") not in writes.calls


def test_the_second_session_is_opened_only_when_something_is_written(sftp_fs):
    """Browsing is the common case and must not pay for a channel it never uses."""
    client = _SessionHandingClient()
    fs, _sftp, _ = sftp_fs(client=client)
    fs.listdir("/srv")
    fs.open_read("/srv/big.iso")
    assert len(client.sessions) == 1

    fs.open_write("/srv/new.txt")
    assert len(client.sessions) == 2
    # And it is reused, not reopened per file.
    fs.open_write("/srv/another.txt")
    assert len(client.sessions) == 2


def test_a_server_that_refuses_a_second_channel_still_writes(sftp_fs):
    """Losing the separation is bad; losing the ability to write is worse."""
    client = _SessionHandingClient(limit=1)
    fs, _sftp, _ = sftp_fs(client=client)
    fs.open_write("/srv/new.txt")
    assert len(client.sessions) == 1
    assert ("open", "/srv/new.txt", "wb") in client.sessions[0].calls


def test_closing_closes_the_write_session_too(sftp_fs):
    client = _SessionHandingClient()
    fs, _sftp, _ = sftp_fs(client=client)
    fs.open_write("/srv/new.txt")
    fs.close()
    assert all(session.closed for session in client.sessions)
    assert client.closed is True


def test_a_write_session_that_will_not_close_does_not_strand_the_client(sftp_fs):
    client = _SessionHandingClient()
    fs, _sftp, _ = sftp_fs(client=client)
    fs.open_write("/srv/new.txt")
    client.sessions[1].close = lambda: (_ for _ in ()).throw(OSError("gone"))
    fs.close()
    assert client.sessions[0].closed is True
    assert client.closed is True


def test_sftp_mutations(sftp_fs):
    fs, sftp, _ = sftp_fs()
    fs.utime("/a", 500.0)
    fs.mkdir("/d")
    fs.remove("/a")
    fs.rmdir("/d")
    assert ("utime", "/a", (500.0, 500.0)) in sftp.calls
    assert ("mkdir", "/d") in sftp.calls
    assert ("remove", "/a") in sftp.calls
    assert ("rmdir", "/d") in sftp.calls


def test_sftp_rename_prefers_the_posix_extension(sftp_fs):
    fs, sftp, _ = sftp_fs()
    fs.rename("/a", "/b")
    assert ("posix_rename", "/a", "/b") in sftp.calls


def test_sftp_rename_falls_back_on_older_servers(sftp_fs):
    fs, sftp, _ = sftp_fs()
    sftp.posix_rename_supported = False
    fs.rename("/a", "/b")
    assert ("rename", "/a", "/b") in sftp.calls


def test_sftp_close_tears_down_both_layers(sftp_fs):
    fs, sftp, client = sftp_fs()
    fs.close()
    assert sftp.closed is True
    assert client.closed is True


def test_sftp_close_still_closes_the_client_if_the_channel_objects(sftp_fs):
    fs, sftp, client = sftp_fs()
    sftp.close = lambda: (_ for _ in ()).throw(OSError("already gone"))
    with pytest.raises(OSError):
        fs.close()
    assert client.closed is True


def test_resolution_defaults_to_the_users_own_ssh_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host web\n  HostName from-home-config\n")
    # No config_path given: ~/.ssh/config is consulted, like ssh itself.
    assert _resolve_ssh_connection("web")["hostname"] == "from-home-config"


def test_a_failed_connection_tears_down_the_jump_chain(monkeypatch, tmp_path):
    gateway = _FakeClient()
    real_open = fsmod._open_ssh_client

    def hop(host, *args, **kwargs):
        if host == "gw":
            return gateway
        return real_open(host, *args, **kwargs)

    def refuse(res, password, sock):
        raise OSError("connection refused")

    monkeypatch.setattr(fsmod, "_open_ssh_client", hop)
    monkeypatch.setattr(fsmod, "_connect_resolved", refuse)
    config = _write_config(tmp_path, "Host web\n  ProxyJump gw\n")
    with pytest.raises(FileSystemError, match="Could not connect"):
        real_open("web", None, None, None, config_path=config)
    # The tunnel is useless without the target, so it is closed too.
    assert gateway.closed is True


def test_a_system_with_no_resolvable_user_falls_back_to_root(tmp_path,
                                                             monkeypatch):
    """Containers with no passwd entry make getuser() raise."""

    def no_user():
        raise KeyError("no matching passwd entry")

    monkeypatch.setattr("getpass.getuser", no_user)
    res = _resolve_ssh_connection("web1", config_path=str(tmp_path / "absent"))
    assert res["username"] == "root"


def test_connecting_without_paramiko_explains_what_to_install(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_paramiko(name, *args, **kwargs):
        if name == "paramiko":
            raise ImportError("No module named 'paramiko'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_paramiko)
    with pytest.raises(FileSystemError, match="pip install paramiko"):
        _open_ssh_client("web1", "deploy", None, 22)
