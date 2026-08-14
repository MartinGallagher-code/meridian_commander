"""Built-in plugin: pack the *other* pane's tagged files into an archive.

``archive.py`` lets a pane step *into* a ``.zip`` or ``.tar`` and browse it;
this is the other half -- making one.  Tag files and directories in the other
pane, open this plugin, and write them into a single archive that lands beside
them:

* ``zip [name]``  -- a ``.zip`` (deflated)
* ``tar [name]``  -- an uncompressed ``.tar``
* ``tgz [name]``  -- a gzip-compressed ``.tar.gz``

With no name the archive is named after the source directory (or ``archive``).
Directories are added recursively.  Everything goes through the pane's own
filesystem -- reads *and* the final write -- so it packs a remote (SFTP/SSH/FTP)
pane as readily as a local one; the bytes stream through this machine only long
enough to be compressed.
"""

from __future__ import annotations

import io
import tarfile
import time
import zipfile

from . import _io
from ..plugin_api import InputOutputPlugin
from ..util import human_size

# arcname -> (payload, mtime); directories are implied by their members.
VERBS = {
    "zip": (".zip", "zip"),
    "tar": (".tar", "tar"),
    "tgz": (".tar.gz", "tgz"),
}


class MakeArchive(InputOutputPlugin):
    name = "Make archive"
    description = "Pack the other pane's tagged files into a zip or tar"
    prompt = "zip|tar|tgz [name]> "

    @property
    def greeting(self) -> str:
        names = [e.name for e in self.ctx.other_selected()]
        target = ", ".join(names) if names else "<nothing tagged>"
        return (f"Tagged (other pane): {target}\n"
                "Type 'zip', 'tar' or 'tgz' (optionally with a name) to pack them.")

    def process(self, line: str):
        line = line.strip()
        if not line:
            return None
        parts = line.split(None, 1)
        verb = parts[0].lower()
        if verb not in VERBS:
            return f"unknown format '{verb}' (try zip, tar or tgz)"

        entries = self.ctx.other_selected()
        if not entries:
            raise RuntimeError("Tag at least 1 file in the other pane.")

        fs = self.ctx.other_fs
        root = self.ctx.other_path
        default_stem = fs.basename(root.rstrip("/")) or "archive"
        ext, kind = VERBS[verb]
        given = parts[1].strip() if len(parts) > 1 else ""
        stem = given[: -len(ext)] if given.lower().endswith(ext) else given
        out_path = self._unique(fs, root, (stem or default_stem) + ext)

        # Gather every member first, so a read error aborts before we write a
        # half-built archive over the wire.
        members: list[tuple[str, bytes, float | None]] = []
        for entry in entries:
            self._gather(fs, fs.join(root, entry.name), entry.name,
                         entry, members)

        data = self._pack(kind, members)
        _io.write_bytes(fs, out_path, data)

        try:
            self.ctx.refresh_other()
        except Exception:
            pass
        return (f"Wrote {fs.basename(out_path)} "
                f"({len(members)} file(s), {human_size(len(data))}).")

    def _gather(self, fs, path, arcname, entry, members) -> None:
        """Collect ``(arcname, bytes, mtime)`` for a file or a whole subtree."""
        if entry.is_dir and not entry.is_symlink:
            try:
                children = fs.listdir(path)
            except Exception as exc:
                self.print(f"  ! {arcname}: {exc}")
                return
            for child in sorted(children, key=lambda e: e.name.lower()):
                self._gather(fs, fs.join(path, child.name),
                             f"{arcname}/{child.name}", child, members)
            return
        try:
            payload, _ = _io.read_bytes(fs, path)
        except Exception as exc:
            self.print(f"  ! {arcname}: {exc}")
            return
        members.append((arcname, payload, entry.mtime))
        self.print(f"  + {arcname}")

    @staticmethod
    def _pack(kind: str, members) -> bytes:
        buf = io.BytesIO()
        if kind == "zip":
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for arcname, payload, mtime in members:
                    info = zipfile.ZipInfo(arcname)
                    if mtime:
                        info.date_time = time.localtime(mtime)[:6]
                    info.compress_type = zipfile.ZIP_DEFLATED
                    zf.writestr(info, payload)
        else:
            mode = "w:gz" if kind == "tgz" else "w"
            with tarfile.open(fileobj=buf, mode=mode) as tf:
                for arcname, payload, mtime in members:
                    info = tarfile.TarInfo(arcname)
                    info.size = len(payload)
                    if mtime:
                        info.mtime = int(mtime)
                    tf.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    @staticmethod
    def _unique(fs, directory: str, name: str) -> str:
        """A path in ``directory`` that does not yet exist, numbered if needed."""
        candidate = fs.join(directory, name)
        if not fs.exists(candidate):
            return candidate
        stem, dot, ext = name.partition(".")
        n = 2
        while True:
            numbered = f"{stem}{n}{dot}{ext}" if dot else f"{stem}{n}"
            candidate = fs.join(directory, numbered)
            if not fs.exists(candidate):
                return candidate
            n += 1
