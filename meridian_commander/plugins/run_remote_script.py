"""Built-in plugin: upload a script to a remote server, run it, show output.

On every submitted input line the plugin:

1. logs into the configured SSH server (once; the connection is reused);
2. copies the configured local script into the configured remote directory
   (SFTP when the server offers it, falling back to ``cat >`` over the shell);
3. runs it with the configured interpreter, passing the user's input as
   arguments;
4. displays the script's stdout/stderr in the output area.

Configure it in ``~/.config/meridian-commander/config.ini`` under
``[plugin:run_remote_script]`` (press ``C`` inside Meridian Commander and choose
"Edit configuration")::

    [plugin:run_remote_script]
    host = server.example.com
    username = me
    script = ~/bin/report.sh
    remote_dir = /tmp
    interpreter = sh

To make a variant for another server or script, copy this file into
``~/.config/meridian-commander/plugins/`` under a new name, change the class
``name`` and ``config_section``, and add a matching ``[plugin:<section>]``
block to the configuration file.
"""

from __future__ import annotations

import os
import shlex

from ..config import plugin_settings
from ..plugin_api import InputOutputPlugin

DEFAULTS = {
    "host": "",
    "port": 22,
    "username": "",
    "password": "",
    "key_filename": "",
    "script": "",           # local script to upload
    "remote_dir": "/tmp",   # where to place and run it
    "interpreter": "sh",    # how to run it
    "timeout": 30,
}


class RunRemoteScript(InputOutputPlugin):
    name = "Run remote script"
    description = "Upload the configured script over SSH, run it, show output"
    prompt = "args> "
    config_section = "run_remote_script"

    @property
    def greeting(self) -> str:
        cfg = self.cfg
        if not cfg["host"] or not cfg["script"]:
            return ("Not configured yet: press C in the file view and choose\n"
                    "'Edit configuration', then fill in [plugin:run_remote_script].")
        return (f"Script: {cfg['script']}\n"
                f"Target: {cfg['username']}@{cfg['host']}:{cfg['remote_dir']} "
                f"(run with '{cfg['interpreter']}')\n"
                f"Type arguments (or nothing) and press Enter to run.")

    def on_start(self) -> None:
        self.cfg = plugin_settings(self.config_section, DEFAULTS)
        super().on_start()
        self._client = None

    def on_exit(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # -- helpers ------------------------------------------------------------
    def _connect(self):
        if self._client is not None:
            transport = self._client.get_transport()
            if transport is not None and transport.is_active():
                return self._client
            self.on_exit()
        cfg = self.cfg
        if not cfg["host"] or not cfg["username"]:
            raise RuntimeError(
                "Not configured: set host/username in "
                "[plugin:run_remote_script] (press C -> Edit configuration)."
            )
        from ..filesystems import _open_ssh_client

        self.print(f"connecting to {cfg['username']}@{cfg['host']} ...")
        self._client = _open_ssh_client(
            cfg["host"], cfg["username"], cfg.get("password") or None,
            int(cfg.get("port", 22)), cfg.get("key_filename") or None,
        )
        return self._client

    def _upload(self, client, local: str, remote: str) -> None:
        with open(local, "rb") as f:
            data = f.read()
        try:
            sftp = client.open_sftp()
            try:
                with sftp.open(remote, "wb") as rf:
                    rf.write(data)
            finally:
                sftp.close()
            return
        except Exception:
            pass
        # SFTP unavailable: stream through the shell instead.
        stdin, stdout, _err = client.exec_command(
            f"cat > {shlex.quote(remote)}", timeout=None)
        stdin.write(data)
        stdin.channel.shutdown_write()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(f"could not upload to {remote}")

    # -- the actual work --------------------------------------------------------
    def process(self, line: str):
        cfg = self.cfg
        script = os.path.expanduser(cfg["script"])
        if not script:
            return ("No script configured -- set 'script' in "
                    "[plugin:run_remote_script].")
        if not os.path.isfile(script):
            return f"Script not found: {script}"

        client = self._connect()
        remote = f"{cfg['remote_dir'].rstrip('/')}/{os.path.basename(script)}"

        self.print(f"uploading {os.path.basename(script)} -> "
                   f"{cfg['host']}:{remote}")
        self._upload(client, script, remote)

        args = ""
        if line.strip():
            args = " " + " ".join(shlex.quote(a) for a in shlex.split(line))
        command = (f"cd {shlex.quote(cfg['remote_dir'])} && "
                   f"{cfg['interpreter']} {shlex.quote(remote)}{args}")
        self.print(f"$ {cfg['interpreter']} {os.path.basename(script)}{args}")

        _in, stdout, stderr = client.exec_command(
            command, timeout=float(cfg.get("timeout", 30)))
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        status = stdout.channel.recv_exit_status()

        result: list[str] = []
        for l in out.splitlines():
            result.append(l)
        for l in err.splitlines():
            result.append(f"! {l}")
        result.append(f"(exit {status})")
        return result