"""Built-in plugin: rename many tagged files at once, by a rule.

Single-file rename lives on ``R`` in the file view; this is its bulk cousin, a
staple of the dual-pane commanders.  Tag files in the *other* pane, open this
plugin, and apply one rule to the whole set:

* ``replace OLD NEW``  -- replace every literal ``OLD`` in the name with ``NEW``
* ``prefix TEXT``      -- put ``TEXT`` in front of each name
* ``suffix TEXT``      -- insert ``TEXT`` before the final extension
* ``case lower|upper|title`` -- recase the name (the extension follows lower)
* ``number TEMPLATE``  -- rebuild the name from a template using ``{n}`` (the
  1-based position, so ``{n:03}`` gives 001, 002, ...), ``{name}`` (the stem
  without its extension) and ``{ext}`` (the extension without its dot)

Prefix any rule with ``preview`` to see the old -> new mapping without touching
a thing.  Nothing is renamed unless the whole set is safe: a rule that would
collide two names onto one, or land on a name that already exists, is refused
in full so the pane is never left half-renamed.  It renames through the pane's
own filesystem, so it works on remote (SFTP/SSH/FTP) panes as well as local.
"""

from __future__ import annotations

from ..plugin_api import Command, InputOutputPlugin

VERBS = ("replace", "prefix", "suffix", "case", "number")


def _split_ext(name: str) -> tuple[str, str]:
    """``('archive', 'tar.gz')`` style split, but only on the *last* dot.

    A leading dot (``.bashrc``) is part of the stem, not an extension.
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        return name, ""
    return stem, ext


def rename_one(name: str, verb: str, args: list[str], index: int) -> str:
    """Apply one rule to one name.  Raises ``ValueError`` on a bad rule."""
    if verb == "replace":
        if len(args) != 2:
            raise ValueError("usage: replace <old> <new>")
        return name.replace(args[0], args[1])
    if verb == "prefix":
        if len(args) != 1:
            raise ValueError("usage: prefix <text>")
        return args[0] + name
    if verb == "suffix":
        if len(args) != 1:
            raise ValueError("usage: suffix <text>")
        stem, ext = _split_ext(name)
        return f"{stem}{args[0]}.{ext}" if ext else name + args[0]
    if verb == "case":
        if len(args) != 1 or args[0] not in ("lower", "upper", "title"):
            raise ValueError("usage: case lower|upper|title")
        stem, ext = _split_ext(name)
        recased = getattr(stem, args[0])()
        return f"{recased}.{ext.lower()}" if ext else recased
    if verb == "number":
        if len(args) != 1:
            raise ValueError("usage: number <template with {n} {name} {ext}>")
        stem, ext = _split_ext(name)
        try:
            return args[0].format(n=index, name=stem, ext=ext)
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError(f"bad template: {exc}") from exc
    raise ValueError(f"unknown verb '{verb}' (try {', '.join(VERBS)})")


class MultiRename(InputOutputPlugin):
    name = "Multi-rename"
    commands = (
        Command("replace", "replace OLD with NEW in every name", arg="text"),
        Command("prefix", "put text in front of every name", arg="text"),
        Command("suffix", "put text before every extension", arg="text"),
        Command("case", "change the case of every name",
                choices=("lower", "upper", "title")),
        Command("number", "number them from a template", arg="text"),
        Command("preview", "try a rule without renaming anything",
                arg="text"),
    )
    description = "Rename the other pane's tagged files by a rule"
    prompt = "rule> "

    @property
    def greeting(self) -> str:
        names = [e.name for e in self.ctx.other_selected()]
        target = ", ".join(names) if names else "<nothing tagged>"
        return (f"Tagged (other pane): {target}\n"
                "Rules: replace OLD NEW | prefix T | suffix T | "
                "case lower|upper|title | number {n:03}-{name}.{ext}\n"
                "Prefix a rule with 'preview' to try it without renaming.")

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
        verb, args = parts[0], parts[1:]
        if verb not in VERBS:
            return f"unknown verb '{verb}' (try {', '.join(VERBS)})"

        entries = self.ctx.other_selected()
        if not entries:
            raise RuntimeError("Tag at least 1 file in the other pane.")

        # Build and validate the whole mapping before touching anything.
        plan: list[tuple[str, str]] = []
        for i, entry in enumerate(entries, start=1):
            new = rename_one(entry.name, verb, args, i)
            plan.append((entry.name, new))

        problem = self._first_conflict(plan)
        if problem:
            return problem

        changed = [(old, new) for old, new in plan if old != new]
        if not changed:
            return "Nothing to rename: the rule left every name unchanged."

        if preview:
            lines = [f"  {old} -> {new}" for old, new in changed]
            lines.append(f"({len(changed)} would be renamed; nothing written)")
            return lines

        fs = self.ctx.other_fs
        root = self.ctx.other_path
        done = 0
        for old, new in changed:
            try:
                fs.rename(fs.join(root, old), fs.join(root, new))
                self.print(f"  {old} -> {new}")
                done += 1
            except Exception as exc:
                self.print(f"  ! {old}: {exc}")
        try:
            self.ctx.refresh_other()
        except Exception:
            pass
        return f"Renamed {done} file(s)."

    def _first_conflict(self, plan: list[tuple[str, str]]) -> str | None:
        """A message if the plan is unsafe, else ``None``.

        Unsafe means an empty result, two sources mapping to one target, or a
        target that already exists in the directory but is not itself one of
        the files being renamed (renaming a set among themselves is fine).
        """
        fs = self.ctx.other_fs
        root = self.ctx.other_path
        sources = {old for old, _ in plan}
        seen: set[str] = set()
        for old, new in plan:
            if not new or "/" in new or new in (".", ".."):
                return f"Refusing to rename: '{old}' -> '{new}' is not a valid name."
            if new in seen:
                return f"Refusing to rename: two files would both become '{new}'."
            seen.add(new)
        for old, new in plan:
            if old == new or new in sources:
                continue
            if fs.exists(fs.join(root, new)):
                return f"Refusing to rename: '{new}' already exists."
        return None
