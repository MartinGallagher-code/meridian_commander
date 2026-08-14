"""The SSH doctor built-in plug-in."""

from __future__ import annotations

import os

import pytest

from meridian_commander.plugins import ssh_doctor
from meridian_commander.plugins.ssh_doctor import SshDoctor, classify

MODERN = ("-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1r\n"
          "-----END OPENSSH PRIVATE KEY-----\n")
LEGACY_ENC = ("-----BEGIN RSA PRIVATE KEY-----\nProc-Type: 4,ENCRYPTED\n"
              "DEK-Info: DES-EDE3-CBC,0011\n\nabc\n-----END RSA PRIVATE KEY-----\n")
LEGACY_PLAIN = "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----\n"
PKCS8_ENC = ("-----BEGIN ENCRYPTED PRIVATE KEY-----\nabc\n"
             "-----END ENCRYPTED PRIVATE KEY-----\n")
PKCS8 = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"


@pytest.fixture
def ssh_home(tmp_path, monkeypatch):
    """A fake ~/.ssh the plug-in scans, plus a captured ``_run``."""
    d = tmp_path / ".ssh"
    d.mkdir()
    monkeypatch.setattr(ssh_doctor, "ssh_dir", lambda: str(d))
    calls = []

    def fake_run(argv):
        calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(ssh_doctor, "_run", fake_run)
    return d, calls


def _key(d, name, text):
    (d / name).write_text(text)
    (d / (name + ".pub")).write_text("ssh-rsa AAAA test\n")


def _ctx(data_ctx):
    return data_ctx({"x": "1"})


# -- classify ------------------------------------------------------------------

@pytest.mark.parametrize("text, fmt, encrypted, risky", [
    (MODERN, "modern OpenSSH", None, False),
    (LEGACY_ENC, "legacy PEM (encrypted)", True, True),
    (PKCS8_ENC, "PKCS#8 (encrypted)", True, True),
    (PKCS8, "PKCS#8", False, False),
    (LEGACY_PLAIN, "legacy PEM", False, False),
])
def test_classify(text, fmt, encrypted, risky):
    assert classify(text) == (fmt, encrypted, risky)


def test_classify_ignores_non_keys():
    assert classify("just some text\n") is None
    assert classify("ssh-rsa AAAA a comment\n") is None


# -- listing -------------------------------------------------------------------

def test_list_classifies_and_flags_risky_keys(ssh_home, data_ctx):
    d, _ = ssh_home
    _key(d, "id_ed25519", MODERN)
    _key(d, "id_rsa", LEGACY_ENC)
    (d / "known_hosts").write_text("host ssh-rsa AAAA\n")
    (d / "config").write_text("Host *\n")

    plugin = SshDoctor(_ctx(data_ctx))
    printed = "\n".join(plugin.output)
    assert "id_ed25519: modern OpenSSH" in printed
    assert "id_rsa: legacy PEM (encrypted)  <-- OpenSSL 3.0 may refuse this" in printed
    assert "known_hosts" not in printed and "config" not in printed
    assert "1 key(s) may need 'convert'" in printed


def test_list_when_all_keys_are_modern(ssh_home, data_ctx):
    d, _ = ssh_home
    _key(d, "id_ed25519", MODERN)
    plugin = SshDoctor(_ctx(data_ctx))
    assert "All keys are in a format OpenSSL 3.0 loads." in "\n".join(plugin.output)


def test_list_with_no_keys(ssh_home, data_ctx):
    d, _ = ssh_home
    out = "\n".join(SshDoctor(_ctx(data_ctx)).output)
    assert "No private keys found" in out


def test_list_with_no_ssh_dir(tmp_path, monkeypatch, data_ctx):
    monkeypatch.setattr(ssh_doctor, "ssh_dir",
                        lambda: str(tmp_path / "nowhere" / ".ssh"))
    monkeypatch.setattr(ssh_doctor, "_run", lambda argv: (0, "", ""))
    out = "\n".join(SshDoctor(_ctx(data_ctx)).output)
    assert "No" in out and ".ssh" in out


# -- agent ---------------------------------------------------------------------

def test_agent_reports_ssh_add_output(ssh_home, data_ctx, monkeypatch):
    monkeypatch.setattr(ssh_doctor, "_run",
                        lambda argv: (0, "256 SHA256:abc me (ED25519)\n", ""))
    plugin = SshDoctor(_ctx(data_ctx))
    out = "\n".join(plugin.process("agent"))
    assert "ssh-add -l (exit 0)" in out
    assert "me (ED25519)" in out


def test_agent_when_ssh_add_is_missing(ssh_home, data_ctx, monkeypatch):
    monkeypatch.setattr(ssh_doctor, "_run",
                        lambda argv: (None, "", "ssh-add not found"))
    plugin = SshDoctor(_ctx(data_ctx))
    assert "Could not run ssh-add" in plugin.process("agent")


# -- convert -------------------------------------------------------------------

def test_convert_needs_a_name(ssh_home, data_ctx):
    assert SshDoctor(_ctx(data_ctx)).process("convert") == "usage: convert <key-name>"


def test_convert_missing_key(ssh_home, data_ctx):
    assert "No such key" in SshDoctor(_ctx(data_ctx)).process("convert nope")


def test_convert_modern_key_is_a_noop(ssh_home, data_ctx):
    d, calls = ssh_home
    _key(d, "id_ed25519", MODERN)
    out = SshDoctor(_ctx(data_ctx)).process("convert id_ed25519")
    assert "already in the modern OpenSSH format" in out
    assert calls == []   # nothing was run


def test_convert_encrypted_key_hands_over_the_command(ssh_home, data_ctx):
    d, calls = ssh_home
    _key(d, "id_rsa", LEGACY_ENC)
    out = "\n".join(SshDoctor(_ctx(data_ctx)).process("convert id_rsa"))
    assert "passphrase-protected" in out
    assert "ssh-keygen -p -o -f" in out
    assert calls == []   # we did not try to run it ourselves


def test_convert_unencrypted_key_runs_and_backs_up(ssh_home, data_ctx):
    d, calls = ssh_home
    _key(d, "id_rsa", LEGACY_PLAIN)
    out = "\n".join(SshDoctor(_ctx(data_ctx)).process("convert id_rsa"))
    assert "Converted id_rsa to the modern OpenSSH format." in out
    assert (d / "id_rsa.bak").read_text() == LEGACY_PLAIN
    assert any(argv[0] == "ssh-keygen" for argv in calls)


def test_convert_refuses_when_a_backup_already_exists(ssh_home, data_ctx):
    d, calls = ssh_home
    _key(d, "id_rsa", LEGACY_PLAIN)
    (d / "id_rsa.bak").write_text("old backup")
    out = SshDoctor(_ctx(data_ctx)).process("convert id_rsa")
    assert "backup id_rsa.bak already exists" in out
    assert calls == []


def test_convert_restores_the_backup_when_ssh_keygen_fails(ssh_home, data_ctx,
                                                           monkeypatch):
    d, _ = ssh_home
    _key(d, "id_rsa", LEGACY_PLAIN)
    monkeypatch.setattr(ssh_doctor, "_run",
                        lambda argv: (1, "", "load failed"))
    out = SshDoctor(_ctx(data_ctx)).process("convert id_rsa")
    assert "ssh-keygen failed (exit 1): load failed" in out
    # The original is back in place and the stray backup is gone.
    assert (d / "id_rsa").read_text() == LEGACY_PLAIN
    assert not (d / "id_rsa.bak").exists()


def test_unknown_command(ssh_home, data_ctx):
    assert "unknown command" in SshDoctor(_ctx(data_ctx)).process("wobble")
