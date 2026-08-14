"""Built-in plugin: tidy whitespace in the other pane's tagged text files.

The batch-edit cousin of the CSV cleaners, for plain text: tag files in the
other pane and apply one rule to the set, in place --

* ``lf``          -- convert CRLF (and lone CR) line endings to LF
* ``crlf``        -- convert line endings to CRLF
* ``untabs [n]``  -- expand tabs to ``n`` spaces (default 4)
* ``trim``        -- strip trailing spaces and tabs from every line
* ``finalnl``     -- ensure the file ends with exactly one newline

Prefix any rule with ``preview`` to see which files *would* change without
writing.  A file whose bytes the rule leaves unchanged is skipped, and a file
that looks binary (a NUL byte in its first block) is never touched.  It reads
and writes through the pane's filesystem, so it normalises remote files too.
"""

from __future__ import annotations

from . import _io
from ..plugin_api import InputOutputPlugin

VERBS = ("lf", "crlf", "untabs", "trim", "finalnl")
SNIFF = 8192      # bytes examined for a NUL before deciding a file is binary


def transform(data: bytes, verb: str, tab_width: int) -> bytes:
    """Apply one normalisation rule to a file's bytes."""
    if verb in ("lf", "crlf", "trim", "finalnl"):
        text = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        if verb == "trim":
            text = b"\n".join(line.rstrip(b" \t") for line in text.split(b"\n"))
        elif verb == "finalnl":
            text = text.rstrip(b"\n") + b"\n" if text else text
        if verb == "crlf":
            text = text.replace(b"\n", b"\r\n")
        return text
    # untabs
    return data.replace(b"\t", b" " * tab_width)


class NormalizeText(InputOutputPlugin):
    name = "Normalise text"
    description = "Fix line endings, tabs and trailing space in tagged files"
    prompt = "lf|crlf|untabs|trim|finalnl> "

    @property
    def greeting(self) -> str:
        names = [e.name for e in self.ctx.other_selected()]
        target = ", ".join(names) if names else "<nothing tagged>"
        return (f"Tagged (other pane): {target}\n"
                "Rules: lf | crlf | untabs [n] | trim | finalnl. "
                "Prefix with 'preview' to try without writing.")

    def process(self, line: str):
        line = line.strip()
        if not line:
            return None

        preview = False
        if line.split(None, 1)[0] == "preview":
            preview = True
            line = line[len("preview"):].strip()
            if not line:
                return "usage: preview <rule> ..."

        parts = line.split()
        verb = parts[0].lower()
        if verb not in VERBS:
            return f"unknown rule '{verb}' (try {', '.join(VERBS)})"
        tab_width = 4
        if verb == "untabs" and len(parts) > 1:
            if not parts[1].isdigit() or int(parts[1]) < 1:
                return "usage: untabs [positive number]"
            tab_width = int(parts[1])

        entries = [e for e in self.ctx.other_selected() if not e.is_dir]
        if not entries:
            raise RuntimeError("Tag at least 1 file in the other pane.")

        fs, root = self.ctx.other_fs, self.ctx.other_path
        changed = skipped = 0
        for entry in entries:
            path = fs.join(root, entry.name)
            try:
                data, _ = _io.read_bytes(fs, path)
            except Exception as exc:
                self.print(f"  ! {entry.name}: {exc}")
                continue
            if b"\x00" in data[:SNIFF]:
                self.print(f"  ~ {entry.name}: looks binary, skipped")
                skipped += 1
                continue
            new = transform(data, verb, tab_width)
            if new == data:
                skipped += 1
                continue
            if preview:
                self.print(f"  would change {entry.name}")
                changed += 1
                continue
            try:
                _io.write_bytes(fs, path, new)
            except Exception as exc:
                self.print(f"  ! {entry.name}: {exc}")
                continue
            self.print(f"  changed {entry.name}")
            changed += 1

        if not preview:
            try:
                self.ctx.refresh_other()
            except Exception:
                pass
        verb_done = "would change" if preview else "changed"
        return f"{verb_done} {changed} file(s), {skipped} already clean/skipped."
