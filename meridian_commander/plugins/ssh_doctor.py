"""Built-in plugin: diagnose the SSH keys in ``~/.ssh`` and fix the legacy ones.

This closes the loop on the connect failure the SFTP/SSH backends translate for
you -- OpenSSL 3.0's opaque ``digital envelope routines::unsupported``, raised
when a private key is stored in the old PEM format whose cipher/KDF the default
provider no longer loads.  Rather than leaving you to work out which key and
what to type, this plugin:

* lists every private key in ``~/.ssh`` and names its format, flagging the ones
  OpenSSL 3.0 is liable to refuse;
* shows what the SSH agent currently holds (``ssh-add -l``);
* converts a key to the modern OpenSSH format on request -- automatically for an
  *unencrypted* legacy key (after backing the original up to ``<name>.bak``),
  and by handing you the exact ``ssh-keygen`` line to run in a terminal for an
  *encrypted* one, since that must prompt for the passphrase.

It looks only at the local machine's ``~/.ssh``; it does not touch the panes.
"""

from __future__ import annotations

import os

from ..plugin_api import Command, InputOutputPlugin

#: Files in ~/.ssh that are never private keys, so the scan skips them.
_NOT_KEYS = {"known_hosts", "known_hosts.old", "config", "authorized_keys",
             "environment", "agent.env"}

_PRIVATE_MARKER = "PRIVATE KEY-----"


def ssh_dir() -> str:
    """The directory scanned for keys (``~/.ssh``); a seam for testing."""
    return os.path.join(os.path.expanduser("~"), ".ssh")


def classify(text: str):
    """Classify a key's PEM text.

    Returns ``(format_label, encrypted, risky)`` where ``risky`` marks a key
    OpenSSL 3.0 may refuse to load, or ``None`` if the text is not a private
    key at all.
    """
    if _PRIVATE_MARKER not in text:
        return None
    if "BEGIN OPENSSH PRIVATE KEY" in text:
        # The modern format (bcrypt KDF, AES) loads whether or not it is
        # encrypted, so it is never the source of the legacy error.
        return "modern OpenSSH", None, False
    if "Proc-Type: 4,ENCRYPTED" in text:
        # Traditional PEM (PKCS#1/SEC1) encrypted with an EVP cipher + MD5 KDF:
        # the classic trigger for the OpenSSL 3.0 error.
        return "legacy PEM (encrypted)", True, True
    if "BEGIN ENCRYPTED PRIVATE KEY" in text:
        # PKCS#8 encrypted; risky when it uses a legacy PBES1 cipher.
        return "PKCS#8 (encrypted)", True, True
    if "BEGIN PRIVATE KEY" in text:
        return "PKCS#8", False, False
    # BEGIN RSA/DSA/EC PRIVATE KEY without Proc-Type: unencrypted traditional.
    return "legacy PEM", False, False


class SshDoctor(InputOutputPlugin):
    name = "SSH doctor"
    commands = (
        Command("list", "every private key in ~/.ssh, with its format"),
        Command("agent", "what ssh-add -l reports"),
        Command("convert", "convert a key to the modern format", arg="text"),
    )
    description = "Inspect ~/.ssh keys and convert legacy ones to modern format"
    prompt = "F2 for commands, or type one> "
    greeting = ("SSH key doctor. Commands:\n"
                "  list           -- scan ~/.ssh and classify each private key\n"
                "  agent          -- what ssh-add -l reports\n"
                "  convert <key>  -- re-save a key in the modern OpenSSH format")

    def on_start(self) -> None:
        super().on_start()
        # Greet with the scan straight away (process() prints results, but this
        # runs outside that path, so print it here).
        result = self._list()
        for line in result if isinstance(result, list) else [result]:
            self.print(line)

    def process(self, line: str):
        parts = line.split()
        verb = parts[0].lower() if parts else "list"
        if verb == "list":
            return self._list()
        if verb == "agent":
            return self._agent()
        if verb == "convert":
            if len(parts) < 2:
                return "usage: convert <key-name>"
            return self._convert(" ".join(parts[1:]))
        return f"unknown command '{verb}' (try list, agent or convert <key>)"

    # -- scanning ------------------------------------------------------------
    def _keys(self):
        """Yield ``(name, path, (fmt, encrypted, risky))`` for each private key."""
        directory = ssh_dir()
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return
        for name in names:
            if name in _NOT_KEYS or name.endswith(".pub"):
                continue
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", errors="replace") as f:
                    head = f.read(8192)
            except OSError:
                continue
            info = classify(head)
            if info is not None:
                yield name, path, info

    def _list(self):
        directory = ssh_dir()
        if not os.path.isdir(directory):
            return f"No {directory} directory found."
        found = list(self._keys())
        if not found:
            return f"No private keys found in {directory}."
        lines = [f"Keys in {directory}:"]
        risky = 0
        for name, _path, (fmt, _enc, is_risky) in found:
            flag = "  <-- OpenSSL 3.0 may refuse this" if is_risky else ""
            if is_risky:
                risky += 1
            lines.append(f"  {name}: {fmt}{flag}")
        if risky:
            lines.append(f"{risky} key(s) may need 'convert'. See 'convert <key>'.")
        else:
            lines.append("All keys are in a format OpenSSL 3.0 loads.")
        return lines

    # -- agent ---------------------------------------------------------------
    def _agent(self):
        rc, out, err = _run(["ssh-add", "-l"])
        if rc is None:
            return f"Could not run ssh-add: {err}"
        text = (out or err).strip() or "(no output)"
        return [f"ssh-add -l (exit {rc}):"] + [f"  {l}" for l in text.splitlines()]

    # -- conversion ----------------------------------------------------------
    def _convert(self, name: str):
        directory = ssh_dir()
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            return f"No such key: {path}"
        try:
            with open(path, "r", errors="replace") as f:
                info = classify(f.read(8192))
        except OSError as exc:
            return f"Could not read {name}: {exc}"
        if info is None:
            return f"{name} does not look like a private key."

        fmt, encrypted, risky = info
        if fmt == "modern OpenSSH":
            return f"{name} is already in the modern OpenSSH format; nothing to do."

        cmd = f"ssh-keygen -p -o -f {_shquote(path)}"
        if encrypted:
            # We cannot supply the passphrase safely, so hand over the command.
            return [
                f"{name} is {fmt} and is passphrase-protected.",
                "Run this in a terminal (it will prompt for the old and new "
                "passphrase):",
                f"  {cmd}",
                "The built-in Terminal plug-in works too.",
            ]

        # Unencrypted: safe to do here.  Back up the original first, then
        # rewrite it in place with an empty passphrase in the modern format.
        backup = path + ".bak"
        if os.path.exists(backup):
            return (f"A backup {name}.bak already exists; move it aside first "
                    f"(or convert by hand with '{cmd}').")
        try:
            import shutil
            shutil.copy2(path, backup)
        except OSError as exc:
            return f"Could not back up {name}: {exc}"

        rc, out, err = _run(["ssh-keygen", "-p", "-o", "-N", "", "-P", "",
                             "-f", path])
        if rc is None:
            _restore(backup, path)
            return f"Could not run ssh-keygen: {err}"
        if rc != 0:
            _restore(backup, path)
            detail = (err or out).strip()
            return f"ssh-keygen failed (exit {rc}): {detail}"
        return [f"Converted {name} to the modern OpenSSH format.",
                f"The original is saved as {name}.bak."]


def _run(argv):
    """Run ``argv``; return ``(rc, stdout, stderr)`` or ``(None, '', reason)``."""
    import subprocess

    try:
        done = subprocess.run(argv, capture_output=True, text=True)
    except FileNotFoundError:
        return None, "", f"{argv[0]} not found"
    except OSError as exc:
        return None, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def _restore(backup: str, path: str) -> None:
    """Put a backup back over ``path`` after a failed conversion."""
    try:
        import shutil
        shutil.move(backup, path)
    except OSError:
        pass


def _shquote(path: str) -> str:
    import shlex

    return shlex.quote(path)
