"""Built-in plugin: push JSON to a listener on a remote server over SSH.

The workflow this implements:

1. the user types input into the bottom input line and presses Enter;
2. the plugin logs into a remote server over SSH (once; the connection is
   reused for subsequent requests);
3. the input is sent as a JSON document to a TCP listener running on that
   server (through an SSH direct-tcpip channel, so the listener only needs to
   listen on the server's loopback -- nothing is exposed to the network);
4. the plugin waits for the listener's reply and displays it in the output
   area.

If the input parses as JSON it is sent as-is; otherwise it is wrapped as
``{"input": "<text>"}``.  A newline terminates each request, and the reply is
read until the listener closes the stream (or the timeout passes).

Configuration
-------------
Set the connection details in ``~/.config/meridian-commander/config.ini`` under
``[plugin:json_push]`` (press ``C`` inside Meridian Commander and choose "Edit
configuration").  To keep one variant per target system, copy this file to
``~/.config/meridian-commander/plugins/`` under a new name, change the class
``name`` and ``config_section``, and add a matching ``[plugin:<section>]``
block to the configuration file.
"""

from __future__ import annotations

import json

from ..config import plugin_settings
from ..plugin_api import InputOutputPlugin

DEFAULTS = {
    "host": "",              # SSH server, e.g. "server.example.com"
    "port": 22,              # SSH port
    "username": "",          # SSH user
    "password": "",          # empty -> use SSH agent / default keys
    "key_filename": "",      # or a path to a private key
    "listener_host": "127.0.0.1",  # listener address *as seen from the server*
    "listener_port": 9000,         # listener TCP port
    "timeout": 15,           # seconds to wait for the reply
}


class JsonPush(InputOutputPlugin):
    name = "JSON push"
    description = "Send JSON to a listener on a remote host via SSH, show reply"
    prompt = "json> "
    config_section = "json_push"

    @property
    def greeting(self) -> str:
        cfg = self.config
        target = cfg["host"] or "<unconfigured -- press C, Edit configuration>"
        return (f"Target: ssh {cfg['username'] or '<user>'}@{target} -> "
                f"{cfg['listener_host']}:{cfg['listener_port']}\n"
                f"Type text or a JSON document and press Enter.")

    def on_start(self) -> None:
        self.config = plugin_settings(self.config_section, DEFAULTS)
        super().on_start()
        self._client = None

    def on_exit(self) -> None:
        if self._client is not None:
            from ..filesystems import _close_ssh_client

            _close_ssh_client(self._client)
            self._client = None

    # -- connection ---------------------------------------------------------
    def _connect(self):
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
            self.on_exit()
        cfg = self.config
        if not cfg["host"] or not cfg["username"]:
            raise RuntimeError(
                "Not configured: set host/username in [plugin:json_push] "
                "(press C -> Edit configuration)."
            )
        from ..filesystems import _open_ssh_client

        self.print(f"connecting to {cfg['username']}@{cfg['host']} ...")
        self._client = _open_ssh_client(
            cfg["host"], cfg["username"], cfg.get("password") or None,
            int(cfg.get("port", 22)), cfg.get("key_filename") or None,
        )
        return self._client

    # -- request/response ------------------------------------------------------
    def process(self, line: str):
        text = line.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            payload = {"input": text}
        data = json.dumps(payload)

        client = self._connect()
        cfg = self.config
        transport = client.get_transport()
        chan = transport.open_channel(
            "direct-tcpip",
            (cfg["listener_host"], int(cfg["listener_port"])),
            ("127.0.0.1", 0),
        )
        try:
            chan.settimeout(float(cfg.get("timeout", 15)))
            chan.sendall(data.encode("utf-8") + b"\n")
            try:
                chan.shutdown_write()
            except Exception:
                pass
            self.print(f"-> {data}")

            reply = b""
            while True:
                try:
                    chunk = chan.recv(65536)
                except Exception:
                    break  # timeout
                if not chunk:
                    break
                reply += chunk
        finally:
            try:
                chan.close()
            except Exception:
                pass

        if not reply:
            return "<- (no reply before timeout)"
        text_reply = reply.decode("utf-8", errors="replace").rstrip("\n")
        # Pretty-print JSON replies when possible.
        try:
            pretty = json.dumps(json.loads(text_reply), indent=2)
            return ["<-"] + pretty.splitlines()
        except ValueError:
            return [f"<- {l}" for l in text_reply.splitlines() or [""]]
