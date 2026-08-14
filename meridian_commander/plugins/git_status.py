"""Built-in plugin: a small git client for the other pane's working tree.

Point the other pane at a git repository and drive the everyday local workflow
without leaving Meridian Commander:

* ``status`` (the default) -- short status with the branch line
* ``log [n]``              -- the last ``n`` commits, one line each (default 15)
* ``diff [path]``          -- the working-tree diff, optionally for one path
* ``add <path>...``        -- stage files (``add .`` stages everything)
* ``unstage [path]...``    -- unstage (defaults to everything)
* ``branch``               -- list branches
* ``commit <message>``     -- commit the staged changes

It runs ``git`` in the other pane's directory, so it only works on a **local**
pane.  Network commands (``push``/``pull``) are deliberately left out -- they can
block on a credential prompt a line-oriented plug-in cannot answer; use the
full-screen shell (``!``) or the Terminal plug-in for those.
"""

from __future__ import annotations

from ..plugin_api import InputOutputPlugin

DEFAULT_LOG = 15


class GitStatus(InputOutputPlugin):
    name = "Git"
    description = "Status, stage and commit in the other pane's repository"
    prompt = "status|log|diff|add|unstage|branch|commit> "

    @property
    def greeting(self) -> str:
        fs = self.ctx.other_fs
        if getattr(fs, "scheme", "") != "local":
            return ("Git works on a local pane only. Point the other pane at a "
                    "repository on this machine.")
        return (f"Repository: {self.ctx.other_path}\n"
                "Commands: status | log [n] | diff [path] | add <path> | "
                "unstage [path] | branch | commit <message>")

    def process(self, line: str):
        parts = line.split()
        verb = parts[0].lower() if parts else "status"

        fs = self.ctx.other_fs
        if getattr(fs, "scheme", "") != "local":
            return "Git works on a local pane only (this pane is remote)."
        cwd = self.ctx.other_path

        if verb == "status":
            argv = ["status", "--short", "--branch"]
        elif verb == "log":
            count = str(DEFAULT_LOG)
            if len(parts) > 1:
                if not parts[1].isdigit() or int(parts[1]) < 1:
                    return "usage: log [positive number]"
                count = parts[1]
            argv = ["log", "--oneline", "-n", count]
        elif verb == "diff":
            argv = ["diff"] + parts[1:]
        elif verb == "add":
            if len(parts) < 2:
                return "usage: add <path> (or 'add .' for everything)"
            argv = ["add"] + parts[1:]
        elif verb == "unstage":
            argv = ["restore", "--staged"] + (parts[1:] or ["."])
        elif verb == "branch":
            argv = ["branch"]
        elif verb == "commit":
            if len(parts) < 2:
                return "usage: commit <message>"
            argv = ["commit", "-m", line.split(None, 1)[1]]
        else:
            return (f"unknown command '{verb}' (try status, log, diff, add, "
                    "unstage, branch or commit)")

        rc, out, err = _git(argv, cwd)
        if rc is None:
            return f"Could not run git: {err}"

        lines: list[str] = [f"  {l}" for l in out.splitlines()]
        # git prints plenty of ordinary information (commit summaries, hints) to
        # stderr, so show it too rather than hiding it.
        lines += [f"  {l}" for l in err.splitlines()]
        if not lines:
            lines.append(f"(git {verb}: nothing to show)")
        if rc != 0:
            lines.append(f"(git exited {rc})")
        return lines


def _git(argv, cwd):
    """Run ``git argv`` in ``cwd``; ``(rc, stdout, stderr)`` or ``(None, '', why)``."""
    import subprocess

    try:
        done = subprocess.run(["git"] + argv, cwd=cwd,
                              capture_output=True, text=True)
    except FileNotFoundError:
        return None, "", "git not found"
    except OSError as exc:
        return None, "", str(exc)
    return done.returncode, done.stdout, done.stderr
