# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Stand-ins for long-running programs, for the background commands' tests.

No test starts a real server.  Each stub is a few lines of ``sh``, first on
a ``PATH`` of the test's own, that behaves like the program it stands for --
says where it serves and waits to be stopped, writes a flood, fails at
once -- and every one of them ends when its process group is stopped.
Most ``exec`` their last step, so that stopping one leaves no orphan behind
for an init that may be slow to reap it.
"""

from __future__ import annotations

import os
import subprocess
import time

STUBS = {
    # python3 -m http.server: where it serves, then waits to be stopped.
    "fake-server":
        'echo "Serving HTTP on 0.0.0.0 port ${1:-8000} from $(pwd)"\n'
        "exec sleep 1000\n",
    # A server with a worker of its own; the worker's pid goes to "$1".
    "fake-workers":
        "sleep 1000 &\n"
        'echo $! > "$1"\n'
        "echo workers-up\n"
        "exec sleep 1000\n",
    # The port is taken: it says why, on stderr, and ends at once.
    "fake-port-taken":
        'echo "OSError: [Errno 98] Address already in use" >&2\n'
        "exit 1\n",
    # Runs each line typed at it, so the keys and the size can be seen.
    "fake-repl":
        "echo ready\n"
        'while read -r line; do eval "$line"; done\n',
    # Says when it is signalled, and ends.
    "fake-signals":
        "trap 'echo SIGNALLED; exit 0' HUP INT TERM\n"
        "echo ready\n"
        "while :; do sleep 0.05; done\n",
    # Will not end on SIGTERM: only SIGKILL stops it.  Its pid goes to "$1".
    # The sleep is not exec'd, since dash gives an exec'd program its
    # signals back.
    "fake-stubborn":
        "trap '' TERM\n"
        'echo $$ > "$1"\n'
        "echo ready\n"
        "sleep 1000\n"
        ":\n",
    # Megabytes of output, then its last word.
    "fake-flood":
        "head -c ${1:-4000000} /dev/zero | tr '\\0' x\n"
        "echo\n"
        "echo flood-done\n",
}


def install(directory, monkeypatch) -> str:
    """Put the stubs in ``directory`` and first on ``PATH``; ``sh`` as the
    shell.  Returns the directory."""
    os.makedirs(directory, exist_ok=True)
    for name, body in STUBS.items():
        path = os.path.join(directory, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(directory), "/usr/bin",
                                                "/bin"]))
    monkeypatch.setenv("SHELL", "/bin/sh")
    return str(directory)


def wait_until(predicate, seconds: float = 10.0) -> bool:
    """Whether ``predicate`` comes true within ``seconds``."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def process_gone(pid: int) -> bool:
    """Whether ``pid`` has ended: no such process, or only its zombie.

    A grandchild is reaped by init, not by us, and an init that reaps
    lazily leaves it a zombie for a while: dead, holding nothing.
    """
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)],
                           capture_output=True, text=True).stdout.strip()
    return state == "" or state.startswith("Z")
