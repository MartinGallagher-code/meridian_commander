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

## Where a typed destination goes

`F5` and `F6` pre-fill the destination with the other pane's directory, so
pressing Enter copies there. A path typed **without** a leading `/` is measured
from that same directory: clearing the field and typing `backup` makes a folder
beside what the other pane is showing, which is where you are looking. The same
holds for a relative path typed into "Go to directory".

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

## When the target is already there

`F5` and `F6` ask before they replace anything. When a file of the same name
is already where a copy is going, the transfer stops and shows both — the size
and time of the one arriving and of the one already there, and which is newer —
and asks:

| Choice | Key | What it does |
| --- | --- | --- |
| Overwrite | `o` | replace this one |
| Overwrite all | `a` | replace this one and every later one without asking |
| Overwrite if older | `i` | replace this one only if it is older than the one arriving |
| Overwrite all older | `l` | the same, for this one and every later one |
| Skip | `s` | keep the one that is there |
| Skip all | `n` | keep this one and every later one without asking |
| Cancel | `c`, `Esc` | stop the transfer here; nothing after it is touched |

"Older" means older by more than two seconds — the same slack `F9` allows, for
filesystems that keep times coarsely — and a file whose age cannot be read is
not older, so the doubtful case keeps what is there. An "all" answer lasts until
the end of that one transfer, every tagged item and everything inside a
directory included, and no further.

A directory that is already there is merged into rather than asked about: what
is inside it is asked about one file at a time. Under a directory the copy made
itself nothing can be in the way, so nothing is looked up there — on a remote
pane that saves a round trip per file.

A **move** that skips anything keeps its source. Within one filesystem a move is
a rename, which every backend would otherwise carry out over an existing file
without a word; it is asked about in the same way, and Skip leaves both where
they were. `r` (rename) asks too, before giving a file a name another file
already has.

`F9` does not ask: sync decides by age, and never deletes.

## Permissions

A copy gets its source's permission bits, so a script copied to a server is
still executable there. Only the nine `rwx` bits travel: set-user-ID and
set-group-ID would hand out privileges nobody asked to give, which is why `cp`
drops them too. A directory the copy makes gets its source's mode once
everything is inside it — given a read-only mode first, it would refuse its own
contents — and a directory that was already there keeps its own.

The copy prompt has a **Keep permissions** switch under the destination field,
on by default; `Ctrl-P` turns it off for that transfer, and the copies then get
the destination's default mode.

Local, SFTP and SSH panes can set a mode. FTP (whose `SITE CHMOD` is optional)
and archives cannot, and the status line then says how many files arrived
without theirs.

## Symbolic links

A symbolic link is copied as a link, pointing exactly where the original did —
a relative target stays relative — whenever both sides can hold one: local,
SFTP and SSH panes can. A link that points nowhere is copied as it is.

Where the far side cannot hold a link (FTP, archives), a link to a **file** is
copied as the file it points at, and a link to a **directory** is left alone:
descending into it would copy its target a second time — and, for a link that
points at one of its own ancestors, for ever. Either way you are told which, by
name, and a move that left one keeps its source.

## How synchronization works

`F9` builds a plan by walking both panes' directory trees:

- a file present on only one side is copied to the other;
- a file present on both sides is compared by modification time, and the
  **newer** copy overwrites the older one (times within 2 seconds are treated as
  equal to avoid needless copies);
- two files of the same age but a **different size** are not the same file, and
  the clock cannot say which is stale: the left pane's copy wins, as it does
  when neither side reports a time at all. Backends differ in how finely they
  report times — an `ls -l` listing gives minutes — so "the same age" is a
  bucket rather than an identity;
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
