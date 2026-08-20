"""The two SSH-backed plug-ins, against a paramiko-shaped stand-in.

No network and no paramiko: the plug-ins reach the server through
``_open_ssh_client``, so replacing that is enough to drive every path.
"""

from __future__ import annotations

import json

import pytest

from meridian_commander.plugins.json_push import JsonPush
from meridian_commander.plugins.run_remote_script import RunRemoteScript


# -- paramiko-shaped stand-ins -------------------------------------------------

class _FakeChannel:
    """A direct-tcpip channel that replays scripted receives."""

    def __init__(self, replies=(), fail_shutdown=False, fail_close=False):
        self.replies = list(replies)
        self.sent = b""
        self.timeout = None
        self.closed = False
        self._fail_shutdown = fail_shutdown
        self._fail_close = fail_close

    def settimeout(self, seconds):
        self.timeout = seconds

    def sendall(self, data):
        self.sent += data

    def shutdown_write(self):
        if self._fail_shutdown:
            raise OSError("half-close not supported")

    def recv(self, size):
        if not self.replies:
            return b""
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def close(self):
        if self._fail_close:
            raise OSError("already gone")
        self.closed = True


class _FakeTransport:
    def __init__(self, channel=None, active=True):
        self.channel = channel
        self._active = active
        self.opened = []

    def is_active(self):
        return self._active

    def open_channel(self, kind, dest, src):
        self.opened.append((kind, dest, src))
        return self.channel


class _FakeExec:
    """The (stdin, stdout, stderr) triple exec_command hands back."""

    class _Out:
        def __init__(self, data, status=0):
            self._data = data
            self.channel = self

        def read(self):
            return self._data

        def recv_exit_status(self):
            return self.status

    def __init__(self, out=b"", err=b"", status=0):
        self.stdout = self._Out(out)
        self.stdout.status = status
        self.stderr = self._Out(err)
        self.stderr.status = status


class _FakeClient:
    def __init__(self, channel=None, active=True, exec_result=None,
                 sftp=None, sftp_error=None):
        self.transport = _FakeTransport(channel, active)
        self.closed = False
        self.commands = []
        self.exec_result = exec_result or _FakeExec()
        self._sftp = sftp
        self._sftp_error = sftp_error

    def get_transport(self):
        return self.transport

    def close(self):
        self.closed = True

    def open_sftp(self):
        if self._sftp_error is not None:
            raise self._sftp_error
        return self._sftp

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        if command.startswith("cat > "):
            return _ShellUpload(self)
        result = self.exec_result
        return None, result.stdout, result.stderr


class _ShellUpload(tuple):
    """The triple returned when a script is streamed in through the shell."""

    def __new__(cls, client, status=0):
        stdin = _UploadStdin(client, status)
        stdout = stdin.channel_holder
        return super().__new__(cls, (stdin, stdout, None))


class _UploadStdin:
    def __init__(self, client, status):
        self.client = client
        self.written = b""
        self.channel = self

        class _Holder:
            channel = self

        self.channel_holder = _Holder()
        self._status = status

    def write(self, data):
        self.written = data
        self.client.uploaded = data

    def shutdown_write(self):
        pass

    def recv_exit_status(self):
        return self.client.upload_status


class _FakeSftpFile:
    def __init__(self, store, name):
        self.store = store
        self.name = name

    def write(self, data):
        self.store[self.name] = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSftp:
    def __init__(self):
        self.files: dict = {}
        self.closed = False

    def open(self, name, mode):
        return _FakeSftpFile(self.files, name)

    def close(self):
        self.closed = True


@pytest.fixture
def ssh(monkeypatch):
    """Factory installing a fake ``_open_ssh_client`` and returning the client."""
    opened = []

    def install(client):
        def fake_open(host, username, password, port, key_filename=None):
            opened.append((host, username, password, port, key_filename))
            return client

        monkeypatch.setattr("meridian_commander.filesystems._open_ssh_client",
                            fake_open)
        return opened

    return install


def _configure(plugin, attr, **values):
    getattr(plugin, attr).update(values)


# -- JSON push -----------------------------------------------------------------

def _json_push(ctx, monkeypatch, **config):
    plugin = JsonPush(ctx)
    plugin.config.update({"host": "server", "username": "deploy", **config})
    return plugin


def test_json_push_greeting_when_unconfigured(app, monkeypatch):
    from meridian_commander.plugin_api import PluginContext

    ctx = PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    plugin = JsonPush(ctx)
    plugin.config["host"] = ""
    assert "unconfigured" in plugin.greeting


def test_json_push_greeting_names_the_target(app):
    from meridian_commander.plugin_api import PluginContext

    ctx = PluginContext(app=app, own_panel=app.left, other_panel=app.right)
    plugin = JsonPush(ctx)
    plugin.config.update({"host": "server", "username": "deploy"})
    assert "deploy@server" in plugin.greeting
    assert "127.0.0.1:9000" in plugin.greeting


@pytest.fixture
def ctx(app):
    from meridian_commander.plugin_api import PluginContext

    return PluginContext(app=app, own_panel=app.left, other_panel=app.right)


def test_json_push_needs_configuring(ctx, monkeypatch):
    plugin = JsonPush(ctx)
    plugin.config.update({"host": "", "username": ""})
    with pytest.raises(RuntimeError, match="Not configured"):
        plugin.process("hello")


def test_json_push_sends_text_wrapped_as_json(ctx, ssh, monkeypatch):
    channel = _FakeChannel([b'{"ok": true}', b""])
    client = _FakeClient(channel)
    opened = ssh(client)
    plugin = _json_push(ctx, monkeypatch)

    result = plugin.process("hello there")
    assert json.loads(channel.sent.decode()) == {"input": "hello there"}
    assert opened == [("server", "deploy", None, 22, None)]
    assert result[0] == "<-"
    assert '"ok": true' in "\n".join(result)
    assert channel.closed is True


def test_json_push_sends_a_json_document_as_written(ctx, ssh, monkeypatch):
    channel = _FakeChannel([b"", ])
    ssh(_FakeClient(channel))
    plugin = _json_push(ctx, monkeypatch)
    plugin.process('{"a": 1}')
    assert json.loads(channel.sent.decode()) == {"a": 1}


def test_json_push_ignores_an_empty_line(ctx, monkeypatch):
    plugin = JsonPush(ctx)
    assert plugin.process("   ") is None


def test_json_push_opens_a_channel_to_the_configured_listener(ctx, ssh,
                                                              monkeypatch):
    channel = _FakeChannel([b"", ])
    client = _FakeClient(channel)
    ssh(client)
    plugin = _json_push(ctx, monkeypatch, listener_host="10.0.0.5",
                        listener_port=1234, timeout=5)
    plugin.process("ping")
    kind, dest, _src = client.transport.opened[0]
    assert kind == "direct-tcpip"
    assert dest == ("10.0.0.5", 1234)
    assert channel.timeout == 5.0


def test_json_push_reports_a_silent_listener(ctx, ssh, monkeypatch):
    ssh(_FakeClient(_FakeChannel([b""])))
    plugin = _json_push(ctx, monkeypatch)
    assert plugin.process("ping") == "<- (no reply before timeout)"


def test_json_push_stops_reading_when_the_socket_times_out(ctx, ssh, monkeypatch):
    channel = _FakeChannel([b"partial", TimeoutError("timed out")])
    ssh(_FakeClient(channel))
    plugin = _json_push(ctx, monkeypatch)
    assert plugin.process("ping") == ["<- partial"]


def test_json_push_shows_a_plain_text_reply_line_by_line(ctx, ssh, monkeypatch):
    ssh(_FakeClient(_FakeChannel([b"one\ntwo\n", b""])))
    plugin = _json_push(ctx, monkeypatch)
    assert plugin.process("ping") == ["<- one", "<- two"]


def test_json_push_copes_with_a_channel_that_cannot_half_close(ctx, ssh,
                                                               monkeypatch):
    channel = _FakeChannel([b"ok", b""], fail_shutdown=True, fail_close=True)
    ssh(_FakeClient(channel))
    plugin = _json_push(ctx, monkeypatch)
    assert plugin.process("ping") == ["<- ok"]


def test_json_push_reuses_a_live_connection(ctx, ssh, monkeypatch):
    client = _FakeClient(_FakeChannel([b"", ]))
    opened = ssh(client)
    plugin = _json_push(ctx, monkeypatch)
    plugin.process("one")
    client.transport.channel = _FakeChannel([b"", ])
    plugin.process("two")
    assert len(opened) == 1              # dialled once, used twice


def test_json_push_redials_a_dead_connection(ctx, ssh, monkeypatch):
    dead = _FakeClient(_FakeChannel([b"", ]), active=False)
    opened = ssh(dead)
    plugin = _json_push(ctx, monkeypatch)
    plugin.process("one")
    plugin.process("two")
    assert len(opened) == 2
    assert dead.closed is True


def test_json_push_redials_when_the_transport_has_gone(ctx, ssh, monkeypatch):
    client = _FakeClient(_FakeChannel([b"", ]))
    opened = ssh(client)
    plugin = _json_push(ctx, monkeypatch)
    plugin.process("one")
    client.transport = None
    client.get_transport = lambda: None
    # A dropped transport reaches the caller as the raw AttributeError
    # from get_transport() answering None. Named here so that if the
    # plug-in ever learns to report it properly, this test says so.
    with pytest.raises(AttributeError):
        plugin.process("two")
    assert len(opened) == 2


def test_json_push_closes_its_connection_on_exit(ctx, ssh, monkeypatch):
    client = _FakeClient(_FakeChannel([b"", ]))
    ssh(client)
    plugin = _json_push(ctx, monkeypatch)
    plugin.process("one")
    plugin.on_exit()
    assert client.closed is True
    assert plugin._client is None
    plugin.on_exit()                     # a second close is harmless


# -- run remote script ---------------------------------------------------------

@pytest.fixture
def script(tmp_path):
    path = tmp_path / "deploy.sh"
    path.write_text("#!/bin/sh\necho hello\n")
    return path


def _runner(ctx, script, **config):
    plugin = RunRemoteScript(ctx)
    plugin.cfg.update({"host": "server", "username": "deploy",
                       "script": str(script), **config})
    return plugin


def test_run_script_greeting_when_unconfigured(ctx):
    plugin = RunRemoteScript(ctx)
    plugin.cfg.update({"host": "", "script": ""})
    assert "Not configured yet" in plugin.greeting


def test_run_script_greeting_names_the_script_and_target(ctx, script):
    plugin = _runner(ctx, script)
    greeting = plugin.greeting
    assert "deploy.sh" in greeting
    assert "deploy@server:/tmp" in greeting


def test_run_script_needs_a_script(ctx):
    plugin = RunRemoteScript(ctx)
    plugin.cfg.update({"host": "server", "username": "deploy", "script": ""})
    assert "No script configured" in plugin.process("")


def test_run_script_reports_a_missing_script(ctx, tmp_path):
    plugin = RunRemoteScript(ctx)
    plugin.cfg.update({"host": "server", "username": "deploy",
                       "script": str(tmp_path / "absent.sh")})
    assert "Script not found" in plugin.process("")


def test_run_script_needs_configuring(ctx, script):
    plugin = RunRemoteScript(ctx)
    plugin.cfg.update({"host": "", "username": "", "script": str(script)})
    with pytest.raises(RuntimeError, match="Not configured"):
        plugin.process("")


def test_run_script_uploads_over_sftp_and_runs_it(ctx, ssh, script):
    sftp = _FakeSftp()
    client = _FakeClient(sftp=sftp,
                         exec_result=_FakeExec(out=b"hello\n", status=0))
    ssh(client)
    result = _runner(ctx, script).process("")
    assert sftp.files["/tmp/deploy.sh"] == script.read_bytes()
    assert sftp.closed is True
    assert result == ["hello", "(exit 0)"]
    assert "sh /tmp/deploy.sh" in client.commands[-1]


def test_run_script_quotes_the_arguments_it_is_given(ctx, ssh, script):
    client = _FakeClient(sftp=_FakeSftp(), exec_result=_FakeExec())
    ssh(client)
    _runner(ctx, script).process("one 'two three'")
    assert "'two three'" in client.commands[-1]


def test_run_script_reports_stderr_and_a_failing_exit_status(ctx, ssh, script):
    client = _FakeClient(sftp=_FakeSftp(),
                         exec_result=_FakeExec(out=b"partial\n",
                                               err=b"boom\n", status=3))
    ssh(client)
    assert _runner(ctx, script).process("") == ["partial", "! boom", "(exit 3)"]


def test_run_script_falls_back_to_the_shell_when_sftp_is_unavailable(ctx, ssh,
                                                                     script):
    client = _FakeClient(sftp_error=OSError("sftp disabled"),
                         exec_result=_FakeExec(out=b"ran\n"))
    client.upload_status = 0
    ssh(client)
    result = _runner(ctx, script).process("")
    assert client.uploaded == script.read_bytes()
    assert any(c.startswith("cat > ") for c in client.commands)
    assert result == ["ran", "(exit 0)"]


def test_run_script_reports_a_failed_shell_upload(ctx, ssh, script):
    client = _FakeClient(sftp_error=OSError("sftp disabled"))
    client.upload_status = 1
    ssh(client)
    with pytest.raises(RuntimeError, match="could not upload"):
        _runner(ctx, script).process("")


def test_run_script_honours_the_configured_interpreter_and_directory(ctx, ssh,
                                                                     script):
    client = _FakeClient(sftp=_FakeSftp(), exec_result=_FakeExec())
    ssh(client)
    _runner(ctx, script, interpreter="bash", remote_dir="/opt/run/").process("")
    command = client.commands[-1]
    assert "cd /opt/run/" in command
    assert "bash /opt/run/deploy.sh" in command


def test_run_script_reuses_and_redials_its_connection(ctx, ssh, script):
    client = _FakeClient(sftp=_FakeSftp(), exec_result=_FakeExec())
    opened = ssh(client)
    plugin = _runner(ctx, script)
    plugin.process("")
    plugin.process("")
    assert len(opened) == 1

    client.transport = _FakeTransport(active=False)
    plugin.process("")
    assert len(opened) == 2


def test_run_script_closes_its_connection_on_exit(ctx, ssh, script):
    client = _FakeClient(sftp=_FakeSftp(), exec_result=_FakeExec())
    ssh(client)
    plugin = _runner(ctx, script)
    plugin.process("")
    plugin.on_exit()
    assert client.closed is True
    plugin.on_exit()
