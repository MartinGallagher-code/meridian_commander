# How copying and synchronizing work

## Copying on one connection

Both panes share a single live connection after `=` (mirror) or after opening a
preset that reuses one — that is the point of both features, since it avoids
authenticating twice and makes a move between the panes a cheap same-server
rename. It also means a remote→remote copy is frequently **a connection copying
to itself**.

That used to be unsafe. One SFTP session is one channel, and every file handle
it opens shares that channel. Reading calls paramiko's `prefetch()`, which fills
the channel with read responses; the writes then have to get out past them. The
two directions contend, and the session either stalls or drops — measured
against a real server on loopback, roughly **one copy in three** died. Over a
slow link it hangs instead, because the window to lose the race in is wider.

So writes now go through **a second SFTP session** on the same SSH transport.
It costs one channel and no authentication — the transport is already up — and
the two directions stop meeting. The session is opened on first write, so
browsing never pays for it, and a server that refuses a second channel falls
back to the old single-session behaviour rather than losing the ability to write.

This does not affect **SSH (shell)** panes, which already run each `cat` in its
own channel, or FTP, or any copy between two different connections.

## What a transfer refuses

Two shapes are refused before anything is written, because neither can end
well:

- **A file onto itself.** The destination is opened for writing, which
  truncates it, so the copy would empty the file it was asked to copy.
- **A directory into its own subtree** — `project` into `project/backups` —
  where the walk would keep finding the copy it was making, one level deeper
  each time, until the disk filled.

`F9` refuses the same overlap: syncing a directory with one inside it would put
every file under the inner one into both indexes under two different names and
"merge" the tree into itself.

Sameness is decided by where the paths *are*, not by which object they arrived
through: two panes on this machine hold two `LocalFileSystem` objects for one
disk, and a check on object identity (which is the right question for "can this
move be a rename?") misses exactly the case that matters. Paths are compared as
written, so a symlink pointing back into the source is not caught — resolving
one would mean asking a backend that may have no way to answer.

## Symlinked directories are left where they are

A symbolic link to a directory is neither a directory nor a file, and a
transfer can do nothing useful with one. Descending into it would copy the
target a second time — and, for a link that points at one of its own
ancestors, for ever. Opening it as a file is an error. Recreating it at the far
end would leave an empty directory pretending to be a link, and there is no way
to *make* a symlink through the filesystem interface: local disk, SFTP, FTP and
archives share one small set of operations, and creating links is not among
them.

So `F5` and `F6` copy everything else and tell you which links they left, by
name. An `F6` move between two different connections keeps its source in that
case rather than deleting a link it could not reproduce; a move *within* one
filesystem is a rename, which moves links intact and has nothing to skip. `F9`
leaves them out of the sync plan for the same reason.

A link to a *file* is followed as it always was: copying one gives you the
file's contents.

## How synchronization works

`F9` builds a plan by walking both panes' directory trees:

- a file present on only one side is copied to the other;
- a file present on both sides is compared by modification time, and the
  **newer** copy overwrites the older one (times within 2 seconds are treated as
  equal to avoid needless copies);
- the copied file is stamped with the **source file's modification time**, so
  both sides stay identical in age — a second sync finds nothing to do instead
  of copying the file back the other way;
- nothing is ever deleted.

You see the full list of planned copies and the total byte count before
confirming, and the operation can be cancelled mid-way.

**A directory that looks too big to sync is queried first.** `F9` is one key
along from Delete, and the pane you left it on may be your home directory or the
root of a remote account — a two-way sync of which is almost never what you
meant. Before the scan starts, both panes' listings are counted, and if either
holds **200 files or more** or **25 subdirectories or more** you are shown what
is in them and asked whether to go ahead, defaulting to *No*. The check reads
the listings the panes have already loaded, so it costs nothing and adds no
delay: the point is to be asked before the wait, not after it. It is a
deliberately shallow look — three subdirectories hiding a hundred thousand files
will not trip it, because measuring that would mean doing the very walk the
question is trying to save you from.

**Both halves are interruptible.** The scan is the slow one on a large tree —
it produces nothing until it has walked both sides to the bottom, and on a
remote pane every directory is a network round trip — so it shows a running
file count and takes **Esc** or **q** to abandon it. Nothing has been copied at
that point, so cancelling a scan costs you nothing but the wait. The walk is
iterative rather than recursive, so tree depth is bounded by the filesystem
rather than by Python's recursion limit.
