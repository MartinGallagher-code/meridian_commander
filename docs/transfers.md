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

**Making one:** `l` makes a link in the other pane to the entry under the
cursor, or to each tagged one, relative by default (`Ctrl-R` in the prompt
switches to absolute), named as the entry is unless you edit the name. On a
link, `l` changes where it points instead. Both panes have to be on one machine
— a link can only point at a path on its own — and an existing name is asked
about before it is replaced.

Where the far side cannot hold a link (FTP, archives), a link to a **file** is
copied as the file it points at, and a link to a **directory** is left alone:
descending into it would copy its target a second time — and, for a link that
points at one of its own ancestors, for ever. Either way you are told which, by
name, and a move that left one keeps its source.

## In the background

Switch **Background** on in the `F5`/`F6` prompt (`Ctrl-B`) and the copy or move
becomes a *job*: it runs while the panes, the viewers and every other key go on
working. The status line shows the jobs running and waiting, the bytes they
have done and how fast; when one ends, the panes it changed are reloaded and the
status line says how it went.

- **The question comes first.** A job cannot stop to ask, so before it starts
  you choose, for everything in it, what happens to a target that is already
  there: **Overwrite**, **Overwrite if older** (the same two seconds of slack
  as above), or **Skip**.
- **`J` lists the jobs** in the active pane — running, waiting, paused, done,
  failed and cancelled — with how far each has got. `p` pauses or resumes the
  one under the cursor, `c` cancels it, and `Enter` shows its errors. Errors
  stay with their job rather than popping up over whatever you are doing.
- **Cancelling** stops a job at the next chunk and removes the file it was
  writing, so nothing half-written is left behind. Whatever it had already
  finished stays where it landed.
- **Pausing** holds the job at the next chunk, with the file it is on still
  open, until you resume it.
- **One at a time between the same two places.** Jobs from one location to
  another run in the order they were started; jobs between different places
  run side by side.
- **A remote side gets a connection of its own**, opened when the job is
  queued with the details the pane was opened with — asking for a password if
  the agent and keys will not do — and closed when the job ends. Browsing that
  host is never held up behind a job.
- **Archives cannot take part**: an archive is rewritten as a whole, so a copy
  into or out of one runs in the foreground as before.
- **Quitting** with jobs still running or waiting asks whether to **wait for
  them** (Meridian quits when the last one ends; `J` shows them meanwhile) or
  **cancel them and quit**.

Background delete, background sync and resuming a job after a restart are not
done.

The jobs list also shows the commands running in the background (`&`), under
the copies and moves — see [Running commands in the
background](usage.md#running-commands-in-the-background).

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

## Auto-sync: keeping the panes in step

`F9` is a one-off. **Auto-sync** keeps going: from the moment it is turned on,
anything that changes in one pane's directory is made in the other, until you
stop it. **`Shift-F9`** (or `S`, or **Command ▸ Auto-sync panes**) turns it on,
and the same key turns it off again.

**Starting.** It refuses what `F9` refuses — a pane listing find results, and
two directories where one is inside the other — and a pane inside an archive.
A directory that looks large is asked about first, as for `F9`. Then it runs an
ordinary `F9` pass: you see the plan and confirm it, and both sides start out
the same. If you decline the plan, or cancel the copy, auto-sync does not
start. A short notice says it is on, and what it keeps in step.

**While it runs** the panes stay usable, and it is hard to miss:

- an **AUTO-SYNC** bar on the status line names the two directories, says how
  it is going (`watching`, how many copied, how many to the trash, how many
  failed) and that `F9` stops it;
- both pane frames are drawn in a colour nothing else uses;
- the function bar's `9` reads **Stop**: while auto-sync is on, `F9` stops it
  rather than starting a one-off sync.

It checks both directories, the whole tree under each as `F9` walks it, **every
2 seconds** when both panes are on this machine and **every 10 seconds** when
either is remote (SFTP, SSH or FTP). Both can be set in `config.ini` (see
[Configuration](configuration.md#autosync--how-often-auto-sync-looks)).
Changes count whoever made them: Meridian, an editor in another window, a
build, a download. The checks run on a thread of their own, with a connection
of their own to a remote pane, so a check of a big or remote tree never holds
up the keyboard, and browsing that host is not held up behind it. When a check
changes something, the pane it changed is reloaded, with the cursor kept on
the same name, and the status line says what was done.

**What a check does:**

- A file **new or changed** on one side is copied to the other, keeping its
  modification time — once it has **held still for two checks** in a row, so a
  file still being written (a download, a build, an editor saving) is not
  copied half-way. A new file therefore arrives within two checks.
- When a file has changed on **one side only**, that side's copy wins, even if
  its clock says it is older (a file put back to an earlier version).
- When the **same file changed on both sides** between checks, the newer copy
  wins, as with `F9`; the copy it replaces is **moved to the trash** first, not
  written over, and the status line says so.
- A file **deleted or renamed** on one side — one the last check saw on both
  sides — is **moved to the trash** on the other side once it has been gone
  for two checks (an editor that saves by renaming leaves a brief gap). Never
  on the first check, and never for a file only one side has had. A folder
  emptied that way, and gone from the other side, is removed too; it holds
  nothing. A renamed file arrives under its new name and the old name goes to
  the trash.
- **Nothing is ever deleted for good.** On a local pane the trash is the
  desktop's own (the same one `F8` uses). On a remote pane it is a folder of
  Meridian's own on that machine, outside the synced folder:
  `~/.meridian-trash/`, under the date and time and then the whole path the
  file had — `/srv/www/index.html` goes to
  `~/.meridian-trash/2026-11-24_153000/srv/www/index.html`. A local disk with
  no usable trash is not deleted on: the file is left, and counted as a
  failure.
- A trash inside one of the synced folders (syncing your home directory) is
  left out of the checks, so what goes into it is not copied across.
- Empty folders are not copied, as with `F9`; they arrive with their first
  file.

**When something goes wrong, it keeps going.** A copy or a move to the trash
that fails (permission denied, a full disk) is counted on the bar, and not
tried again until one of the two copies changes. `J` lists auto-sync above the
jobs; `Enter` on it shows every failure in full, then what it has done.

**A dropped connection** to a remote pane does not stop it: the bar says it is
reconnecting, it dials again at every check, and nothing is copied or deleted
until the connection is back. A password typed when it started is kept in
memory for that, never written anywhere.

**A folder that is suddenly empty is not believed.** If everything on one side
disappears at once — which is what an unmounted disk looks like — nothing is
mirrored: the bar says so, and auto-sync waits until that side has files
again. Deleting the last file of a folder that held one is mirrored as usual.

**It stops** when you press `Shift-F9` or `F9`, or choose the menu entry again,
or `c` on it in `J`; when either pane changes directory or connection (`Enter`,
`Backspace`, `<`/`>`, `Ctrl-G`, `~`, a preset, a new connection, `=`, find
results listed in the pane); and when Meridian quits. The status line says
why. Swapping the panes (`Ctrl-U`) does not stop it. A file being copied when
it stops is finished first; when quitting, it gets five seconds, and past that
it is cancelled and the part written removed, so no half-written file is left.

