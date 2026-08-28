"""Shell integration: leave the shell in the directory you browsed to.

A program cannot change its parent shell's directory -- nothing on Unix can
reach into another process and chdir it.  What every file manager does instead
is have the *shell* start it: a function runs ``meridian --printwd=FILE``,
and when it exits the function reads that file and cds there itself.  Bound to
a key, that gives the DOS TSR feel -- one keystroke away to browse, one back,
and the prompt has followed you.

The functions are printed rather than documented so nobody has to copy a dozen
lines out of a manual and keep them in step::

    eval "$(meridian --shell-init bash)"        # in ~/.bashrc
    meridian --shell-init zsh >> ~/.zshrc
    meridian --shell-init fish > ~/.config/fish/conf.d/meridian.fish

Each snippet does the same four things: make a temporary file, run Meridian
with it, cd if it came back non-empty, and delete it.  Empty means "nowhere a
shell could go" -- the pane you left from was SFTP, FTP or an archive -- and
the shell then stays where it was, which is better than being moved somewhere
you did not ask for.

``Ctrl-O`` is the key, the one Midnight Commander uses for the same idea. It
is bound to ``operate-and-get-next`` in bash by default, which few people use
and which the comment in the snippet points out, since replacing a binding
silently would be rude.
"""

from __future__ import annotations

BASH = """\
# Meridian Commander shell integration -- eval "$(meridian --shell-init bash)"
meridian_cd() {
    local wd rc
    wd="$(mktemp "${TMPDIR:-/tmp}/meridian-wd.XXXXXX")" || return 1
    command meridian --printwd="$wd" "$@"
    rc=$?
    # Empty means the pane was not local, so there is nowhere to follow to.
    if [ -s "$wd" ]; then
        cd -- "$(cat "$wd")" || true
    fi
    rm -f "$wd"
    return $rc
}
# Ctrl-O, as in Midnight Commander. This replaces readline's
# operate-and-get-next; bind another key here if you use it.
bind -x '"\\C-o": meridian_cd' 2>/dev/null
"""

ZSH = """\
# Meridian Commander shell integration -- eval "$(meridian --shell-init zsh)"
meridian_cd() {
    local wd rc
    wd="$(mktemp "${TMPDIR:-/tmp}/meridian-wd.XXXXXX")" || return 1
    command meridian --printwd="$wd" "$@"
    rc=$?
    # Empty means the pane was not local, so there is nowhere to follow to.
    if [[ -s "$wd" ]]; then
        cd -- "$(cat "$wd")"
    fi
    rm -f "$wd"
    return $rc
}
# Ctrl-O types the command and runs it, rather than calling it from a zle
# widget. A widget keeps hold of stdin while it runs, and a full-screen
# program started from inside one draws its first frame and then never sees a
# keystroke. Typing the line hands the terminal over the ordinary way, at the
# cost of replacing whatever was half-typed and leaving an entry in history.
bindkey -s '^O' 'meridian_cd\n'
"""

FISH = """\
# Meridian Commander shell integration
# meridian --shell-init fish > ~/.config/fish/conf.d/meridian.fish
function meridian_cd --description 'Browse with Meridian, then follow it'
    set -l wd (mktemp (test -n "$TMPDIR"; and echo $TMPDIR; or echo /tmp)/meridian-wd.XXXXXX)
    command meridian --printwd=$wd $argv
    set -l status_code $status
    # Empty means the pane was not local, so there is nowhere to follow to.
    if test -s $wd
        cd (cat $wd)
    end
    rm -f $wd
    return $status_code
end
function meridian_cd_widget
    meridian_cd
    commandline -f repaint
end
# Ctrl-O, as in Midnight Commander.
bind \\co meridian_cd_widget
"""

#: The shells ``--shell-init`` knows, in the order ``--help`` should list them.
SNIPPETS = {"bash": BASH, "zsh": ZSH, "fish": FISH}


def snippet(shell: str) -> str:
    """The integration snippet for ``shell``.

    ``argparse`` has already rejected anything not in :data:`SNIPPETS`, so a
    missing key here would be a typo in the choices rather than user error.
    """
    return SNIPPETS[shell]
