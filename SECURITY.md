# Security Policy

## Supported versions

Meridian Commander is developed on `main` and released from it. Fixes go into
the next release; there are no maintenance branches for older versions.

| Version | Supported |
| ------- | --------- |
| 1.3.x   | Yes       |
| < 1.3   | No — upgrade |

## Reporting a vulnerability

Please report privately rather than opening a public issue.

Use GitHub's [private vulnerability
reporting](https://github.com/MartinGallagher-code/meridian_commander/security/advisories/new)
on this repository. If that is unavailable to you, open an issue that says
only that you have a security report and asks for a contact — no details.

Please include what an attacker gains, the steps to reproduce, and the
version, Python version and platform you saw it on. A reply should come within
a week; if one does not, assume it was missed and chase it.

## What this program touches

Meridian Commander is a file manager, so most of its risk is in what it reads,
writes and connects to. Worth knowing when judging whether something is a
vulnerability:

**Remote connections.** SSH host keys are checked. A host is trusted the first
time it is seen and its key recorded in `~/.ssh/known_hosts`, so a *changed*
key is refused afterwards — paramiko raises before the program's own policy is
consulted. An unwritable `known_hosts` means the key is pinned for that session
only, never that checking is skipped. FTP is plaintext by nature; that is the
protocol, not a defect here.

**Credentials.** Passwords typed into the connection dialog are held for the
session and are not written anywhere. Presets store a connection and a
directory, never a password.

**The configuration file.** `~/.config/meridian-commander/config.ini` may hold
a plug-in's SSH password, because some plug-ins connect on their own account.
It is created mode `0600` in a `0700` directory. An existing file keeps
whatever permissions it already has, so a file created by an older version may
still be world-readable — check it with `ls -l` and `chmod 600` it if so.

**Files it opens.** The viewer, the editor, the archive browser and the
`.xlsx`, `.docx`, `.pptx`, image and PDF readers all parse untrusted input,
each written against the format rather than a library. They have explicit
caps on size, member count and block count, and a malformed file should
produce an error rather than a crash, a hang, or unbounded memory. A file that
defeats one of those is a bug worth reporting.

**Running files.** `Enter` on a script or an executable runs it, and the
in-pane terminal runs a shell. Both are the point of the feature.

## Scope

In scope: anything that lets a remote server, a crafted file, or another local
user read or write what they should not, execute code unexpectedly, or hang or
exhaust the machine.

Out of scope: findings that require the user to already be running arbitrary
code as themselves, the plaintext nature of FTP, and reports from automated
scanners without a demonstrated impact on this program.
