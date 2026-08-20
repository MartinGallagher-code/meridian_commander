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
