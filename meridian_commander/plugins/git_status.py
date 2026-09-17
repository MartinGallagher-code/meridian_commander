# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Built-in plugin: a small git client for the other pane's working tree.

Point the other pane at a git repository and drive the everyday workflow
without leaving Meridian Commander.  The pane opens on a **summary** rather
than on a blank prompt -- branch, upstream, how far ahead or behind, what is
staged and what is not, the last commit, the stash -- because that is what
you came to the pane to find out, and it is redrawn after every command that
can change it:

* ``status`` (the default) -- the summary, with the changed files under it
* ``log [n]``              -- the last ``n`` commits, one line each (default 15)
* ``diff [path]``          -- the working-tree diff, optionally for one path
* ``add <path>...``        -- stage files (``add .`` stages everything)
* ``unstage [path]...``    -- unstage (defaults to everything)
* ``branch``               -- list branches
* ``switch <name>``        -- check out another branch
* ``commit <message>``     -- commit the staged changes
* ``fetch`` / ``pull`` / ``push`` -- the network commands
* ``stash`` / ``pop``      -- shelve and restore the working tree

It runs ``git`` in the other pane's directory, so it only works on a **local**
pane.

Why the network commands are here now
-------------------------------------
They used to be left out, because ``git`` can stop dead waiting for a
credential or a passphrase on a terminal a line-oriented plug-in has no way to
answer, and a pane that never comes back is worse than a missing feature.
That is a fixable problem rather than a reason to do without ``push``:

* the prompts are switched off (:func:`_env`), so git fails and says why
  instead of waiting for an answer that cannot arrive; and
* every network command runs under a timeout, so even a hang nothing
  anticipated ends in a message rather than a dead pane.

The first is what makes the common setup -- an ssh agent, or a credential
helper -- work normally, and the second is the backstop for everything else.
A repository whose credentials really do need typing is the one case this
cannot serve: use the full-screen shell (``!``) or the Terminal plug-in.
"""

from __future__ import annotations

import os
import subprocess

from ..plugin_api import Command, InputOutputPlugin

DEFAULT_LOG = 15
#: Seconds a network command may run before it is killed.  Generous: a big
#: first push over a slow link is slow but not stuck, and the point of the
#: limit is only that the pane always comes back.
NETWORK_TIMEOUT = 120
#: Changed files listed under the summary before it says how many more there
#: are.  A pane is a few dozen lines; a thousand-file status would bury the
#: summary it is printed under.
MAX_FILES = 20

#: The verbs that talk to a remote -- the ones that get the timeout.
NETWORK = frozenset({"fetch", "pull", "push"})
#: The verbs after which the working tree, or where HEAD points, may have
#: changed: the other pane is reloaded and the summary reprinted.
CHANGES_TREE = frozenset({"add", "unstage", "commit", "switch", "pull",
                          "stash", "pop"})

#: ``.git`` entries that mean an operation is half-finished, and what to call
#: it.  Checked in this order: a rebase that hit a conflict has MERGE_HEAD as
#: well, and "rebase" is the more useful of the two answers.
IN_PROGRESS = (
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("MERGE_HEAD", "merge"),
    ("BISECT_LOG", "bisect"),
)


class GitStatus(InputOutputPlugin):
    name = "Git"
    description = "Status, stage, commit, push and pull in the other pane"
    prompt = "F2 for commands, or type one> "
    commands = (
        Command("status", "summary, and what has changed"),
        Command("log", f"the last {DEFAULT_LOG} commits, one line each"),
        Command("diff", "working-tree diff, whole tree or one path",
                arg="path"),
        Command("add", "stage one file", arg="path", allow_bare=False),
        Command("add .", "stage everything"),
        Command("unstage", "unstage everything"),
        Command("commit", "commit the staged changes", arg="text"),
        Command("branch", "list branches"),
        Command("switch", "check out another branch", arg="options"),
        Command("fetch", "fetch from the remote"),
        Command("pull", "fetch and merge the upstream branch"),
        Command("push", "push this branch to its remote"),
        Command("stash", "shelve the working tree"),
        Command("pop", "restore the newest stash"),
    )

    # -- opening ---------------------------------------------------------
    @property
    def greeting(self) -> str:
        if not self._is_local():
            return ("Git works on a local pane only. Point the other pane at "
                    "a repository on this machine.")
        head = ["Press F2 for the commands -- one keystroke each."]
        return "\n".join(self._report(self.ctx.other_path) + [""] + head)

    def command_options(self, command):
        """Branch names for ``switch``, newest checkout first.

        Discovered rather than fixed, which is the whole reason the menu can
        offer them: nobody can list a repository's branches up front.
        """
        if command.verb != "switch" or not self._is_local():
            return None
        rc, out, _ = _git(["for-each-ref", "--sort=-committerdate",
                           "--format=%(refname:short)", "refs/heads/"],
                          self.ctx.other_path)
        if rc != 0:
            return None
        return [b for b in out.split() if b] or None

    # -- one submitted line ----------------------------------------------
    def process(self, line: str):
        parts = line.split()
        verb = parts[0].lower() if parts else "status"
        args = parts[1:]

        if not self._is_local():
            return "Git works on a local pane only (this pane is remote)."
        cwd = self.ctx.other_path

        if verb == "status":
            return self._report(cwd)

        note = None
        if verb == "log":
            count = str(DEFAULT_LOG)
            if args:
                if not args[0].isdigit() or int(args[0]) < 1:
                    return "usage: log [positive number]"
                count = args[0]
            argv = ["log", "--oneline", "-n", count]
        elif verb == "diff":
            argv = ["diff"] + args
        elif verb == "add":
            if not args:
                return "usage: add <path> (or 'add .' for everything)"
            argv = ["add"] + args
        elif verb == "unstage":
            argv = ["restore", "--staged"] + (args or ["."])
        elif verb == "branch":
            argv = ["branch"]
        elif verb == "switch":
            if not args:
                return "usage: switch <branch>"
            argv = ["switch"] + args
        elif verb == "commit":
            if not args:
                return "usage: commit <message>"
            argv = ["commit", "-m", line.split(None, 1)[1]]
        elif verb == "fetch":
            argv = ["fetch"] + args
        elif verb == "pull":
            # --no-edit: a merge commit would otherwise open an editor this
            # pane cannot show, which is the hang the timeout exists to catch.
            argv = ["pull", "--no-edit"] + args
        elif verb == "push":
            argv, note = self._push_argv(cwd, args)
            if argv is None:
                return note
        elif verb == "stash":
            argv = ["stash", "push"] + args
        elif verb == "pop":
            argv = ["stash", "pop"] + args
        else:
            return (f"unknown command '{verb}' (try status, log, diff, add, "
                    "unstage, commit, branch, switch, fetch, pull, push, "
                    "stash or pop)")

        timeout = NETWORK_TIMEOUT if verb in NETWORK else None
        if verb in NETWORK:
            self.ctx.set_status(f"git {verb}...")
        rc, out, err = _git(argv, cwd, timeout=timeout)
        if rc is None:
            return f"Could not run git: {err}"

        lines: list[str] = [f"  {note}"] if note else []
        # git prints plenty of ordinary information (commit summaries, hints,
        # push progress) to stderr, so show it too rather than hiding it.
        lines += [f"  {text}" for text in out.splitlines()]
        lines += [f"  {text}" for text in err.splitlines()]
        if not lines:
            lines.append(f"(git {verb}: nothing to show)")
        if rc != 0:
            lines.append(f"(git exited {rc})")
            if verb in NETWORK:
                lines.append(_network_hint(err))

        if verb in CHANGES_TREE and rc == 0:
            # The listing next door is now stale -- a switch or a pull can
            # have replaced every file in it.
            self.ctx.refresh_other()
            lines += [""] + self._report(cwd)
        return lines

    # -- the summary ------------------------------------------------------
    def _report(self, cwd: str) -> list[str]:
        """The summary block, with the changed files under it."""
        info, why = _read_status(cwd)
        if info is None:
            return [f"  {why}"]

        rows = [("Repository", cwd)]
        rows.append(("Branch", _branch_text(info)))
        remote = _remote_url(cwd)
        if remote:
            rows.append(("Remote", remote))
        rows.append(("Working tree", _tree_text(info)))
        last = _last_commit(cwd)
        if last:
            rows.append(("Last commit", last))
        stashes = _stash_count(cwd)
        if stashes:
            rows.append(("Stash", f"{stashes} "
                                  f"{'entry' if stashes == 1 else 'entries'}"))
        busy = _in_progress(cwd)
        if busy:
            rows.append(("In progress", f"{busy} -- finish it or abort it "
                                        f"({busy} --abort)"))

        width = max(len(label) for label, _ in rows)
        lines = [f"  {label.ljust(width)}  {value}" for label, value in rows]

        files = info["files"]
        if files:
            lines.append("")
            for xy, path in files[:MAX_FILES]:
                # Porcelain v2 writes "nothing in this column" as a dot; the
                # short format everyone reads writes it as a space, and this
                # list is for reading.
                lines.append(f"  {xy.replace('.', ' ')} {path}")
            if len(files) > MAX_FILES:
                lines.append(f"  ... and {len(files) - MAX_FILES} more")
        return lines

    # -- helpers ----------------------------------------------------------
    def _is_local(self) -> bool:
        return getattr(self.ctx.other_fs, "scheme", "") == "local"

    def _push_argv(self, cwd: str, args: list[str]):
        """``(argv, note)`` for ``push``, or ``(None, message)`` to refuse.

        A branch with no upstream is the ordinary state of a branch pushed for
        the first time, and plain ``git push`` answers it with an instruction
        to type a longer command.  The longer command is the only thing that
        was ever going to happen next, so the plug-in runs it -- and says so,
        because setting an upstream is a change to the repository's
        configuration and should not be silent.
        """
        if args:
            return ["push"] + args, None
        info, why = _read_status(cwd)
        if info is None:
            return None, f"  {why}"
        if info["upstream"]:
            return ["push"], None
        if info["detached"]:
            return None, ("  HEAD is detached -- switch to a branch before "
                          "pushing.")

        rc, out, _ = _git(["remote"], cwd)
        remotes = out.split() if rc == 0 else []
        if not remotes:
            return None, "  No remote is configured for this repository."
        remote = "origin" if "origin" in remotes else remotes[0]
        if len(remotes) > 1 and "origin" not in remotes:
            return None, ("  Several remotes and no 'origin' -- say which: "
                          f"push {remotes[0]} {info['head']}")
        head = info["head"]
        return (["push", "--set-upstream", remote, head],
                f"no upstream yet -- setting {remote}/{head}")


# -- running git ----------------------------------------------------------

def _env() -> dict:
    """The environment git is run in: one that can never stop and ask.

    Every one of these turns a question git might put to a terminal into an
    error it reports instead.  A plug-in with an output area and an input line
    has no way to answer a password prompt, so a prompt means a pane wedged
    until the application is killed.
    """
    env = dict(os.environ)
    # No username/password prompt on the terminal.
    env["GIT_TERMINAL_PROMPT"] = "0"
    # No graphical askpass helper either, on any platform.
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS_REQUIRE"] = "never"
    # No editor for a merge or commit message.  ``true`` exits 0 having
    # written nothing, which git reads as "the message is fine as it is".
    env["GIT_EDITOR"] = "true"
    # BatchMode stops ssh asking for a key passphrase or about an unknown
    # host key.  setdefault: a user who has set GIT_SSH_COMMAND themselves
    # has said how ssh should run, and that is not ours to overrule.
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return env


def _git(argv, cwd, timeout: float | None = None):
    """Run ``git argv`` in ``cwd``; ``(rc, stdout, stderr)`` or ``(None, '', why)``."""
    try:
        done = subprocess.run(["git"] + argv, cwd=cwd, capture_output=True,
                              text=True, env=_env(), timeout=timeout)
    except FileNotFoundError:
        return None, "", "git not found"
    except subprocess.TimeoutExpired:
        return None, "", (f"git {argv[0]} gave up after {timeout:g}s -- the "
                          "remote did not answer")
    except OSError as exc:
        return None, "", str(exc)
    return done.returncode, done.stdout, done.stderr


def _network_hint(err: str) -> str:
    """A line of advice for the network failures that have a known cause."""
    low = err.lower()
    if "could not read username" in low or "terminal prompts disabled" in low:
        return ("(this repository wants a typed credential, which this pane "
                "cannot offer -- use the shell '!' or a credential helper)")
    if "permission denied (publickey)" in low or "batch mode" in low:
        return ("(ssh could not authenticate without asking -- start an "
                "ssh-agent and add your key)")
    if "host key verification failed" in low:
        return "(the host is not in known_hosts -- connect once from a shell)"
    return ""


# -- reading the repository ------------------------------------------------

def _read_status(cwd: str):
    """``(info, '')`` from ``status --porcelain=v2``, or ``(None, why)``.

    Porcelain v2 rather than the short format because it is the one designed
    to be read by a program: the branch, its upstream and the ahead/behind
    counts arrive as their own labelled lines instead of having to be picked
    back out of a ``## main...origin/main [ahead 2]`` header.
    """
    rc, out, err = _git(["status", "--porcelain=v2", "--branch"], cwd)
    if rc is None:
        return None, f"Could not run git: {err}"
    if rc != 0:
        first = err.strip().splitlines()
        return None, (first[0] if first else f"git exited {rc}")
    return _parse_status(out), ""


def _parse_status(text: str) -> dict:
    """Branch, counts and changed files from porcelain v2 output."""
    info: dict = {"head": "", "upstream": "", "ahead": 0, "behind": 0,
                  "initial": False, "detached": False, "staged": 0,
                  "modified": 0, "untracked": 0, "conflicts": 0,
                  "files": []}
    for line in text.splitlines():
        if line.startswith("# branch.oid "):
            info["initial"] = line[13:].strip() == "(initial)"
        elif line.startswith("# branch.head "):
            head = line[14:].strip()
            info["detached"] = head == "(detached)"
            info["head"] = "" if info["detached"] else head
        elif line.startswith("# branch.upstream "):
            info["upstream"] = line[18:].strip()
        elif line.startswith("# branch.ab "):
            for field in line[12:].split():
                if field.startswith("+"):
                    info["ahead"] = int(field[1:])
                elif field.startswith("-"):
                    info["behind"] = int(field[1:])
        elif line[:2] in ("1 ", "2 "):
            # "1 XY sub mH mI mW hH hI path".  A rename ("2") is the same up
            # to a similarity score wedged in before the path, and carries
            # both names after it, tab-separated -- the first is the name the
            # file has now.  One field's difference, and reading the "2" line
            # as a "1" line yields "R100 new.txt" as the filename.
            xy = line[2:4]
            path = line.split(" ", 9 if line[0] == "2" else 8)[-1]
            path = path.split("\t")[0]
            info["staged"] += xy[0] != "."
            info["modified"] += xy[1] != "."
            info["files"].append((xy, path))
        elif line.startswith("u "):
            info["conflicts"] += 1
            info["files"].append((line[2:4], line.split(" ", 10)[-1]))
        elif line.startswith("? "):
            info["untracked"] += 1
            info["files"].append(("??", line[2:]))
    return info


def _branch_text(info: dict) -> str:
    """The branch line: where HEAD is, and how it stands against upstream."""
    if info["detached"]:
        return "(detached HEAD)"
    head = info["head"] or "(unknown)"
    if info["initial"]:
        return f"{head}  (no commits yet)"
    if not info["upstream"]:
        return f"{head}  (no upstream)"
    text = f"{head} -> {info['upstream']}"
    if info["ahead"] and info["behind"]:
        return (f"{text}  diverged: {info['ahead']} ahead, "
                f"{info['behind']} behind")
    if info["ahead"]:
        return f"{text}  {info['ahead']} ahead"
    if info["behind"]:
        return f"{text}  {info['behind']} behind"
    return f"{text}  up to date"


def _tree_text(info: dict) -> str:
    """"clean", or what is staged, changed, untracked and conflicted."""
    bits = []
    for count, label in ((info["conflicts"], "conflicted"),
                         (info["staged"], "staged"),
                         (info["modified"], "modified"),
                         (info["untracked"], "untracked")):
        if count:
            bits.append(f"{count} {label}")
    return ", ".join(bits) if bits else "clean"


def _last_commit(cwd: str) -> str:
    """``sha  when  subject`` for HEAD, or "" when there is no commit yet."""
    rc, out, _ = _git(["log", "-1", "--format=%h%x00%cr%x00%s"], cwd)
    if rc != 0:
        return ""
    parts = out.strip().split("\0")
    if len(parts) != 3:
        return ""
    sha, when, subject = parts
    return f"{sha}  {when}  {subject}"


def _remote_url(cwd: str) -> str:
    """The push URL of ``origin`` (or of the only remote), or "".

    Named rather than assumed: a pane that says it is about to push should
    say where to, and "origin" alone does not answer that.
    """
    rc, out, _ = _git(["remote"], cwd)
    if rc != 0:
        return ""
    remotes = out.split()
    if not remotes:
        return ""
    name = "origin" if "origin" in remotes else remotes[0]
    rc, out, _ = _git(["remote", "get-url", "--push", name], cwd)
    if rc != 0 or not out.strip():
        return ""
    return f"{name}  {out.strip()}"


def _stash_count(cwd: str) -> int:
    rc, out, _ = _git(["stash", "list"], cwd)
    if rc != 0:
        return 0
    return len([line for line in out.splitlines() if line.strip()])


def _in_progress(cwd: str) -> str:
    """"merge", "rebase", ... when one is half-finished, else "".

    The state a user most needs the pane to volunteer: a conflicted merge
    looks like an ordinary dirty tree in a file listing, and every command
    behaves oddly until it is finished or abandoned.
    """
    rc, out, _ = _git(["rev-parse", "--git-dir"], cwd)
    if rc != 0:
        return ""
    git_dir = out.strip()
    if not git_dir:
        return ""
    if not os.path.isabs(git_dir):
        git_dir = os.path.join(cwd, git_dir)
    for entry, label in IN_PROGRESS:
        if os.path.exists(os.path.join(git_dir, entry)):
            return label
    return ""
