"""Built-in plugin: hash the *other* pane's files, or verify them against sums.

Tag files in the other pane and:

* ``sha256`` (the default -- a bare Enter does the same), ``sha1``, ``md5`` or
  ``sha512`` -- print ``<hash>  <name>`` for each tagged file;
* ``write [algo] [name]`` -- save those lines to a sums file (default
  ``SHA256SUMS``), in the usual ``sha256sum -c`` layout;
* ``verify [name]`` -- read a sums file from the other pane, re-hash each listed
  file, and report ``OK`` / ``FAILED`` / ``missing``.

Hashing streams each file in chunks through the pane's own filesystem, so it
covers remote (SFTP/SSH/FTP) panes without pulling whole files into memory, and
verifying is exactly the integrity check the ``shared_tools`` bundle workflow
does by hand.
"""

from __future__ import annotations

from . import _io
from ..plugin_api import InputOutputPlugin

ALGORITHMS = ("md5", "sha1", "sha256", "sha512")
DEFAULT_ALGO = "sha256"
#: A sums file may be large-ish, but a runaway is still capped.
MAX_SUMS_BYTES = 16 * 1024 * 1024

#: Hash a file through the shared, backend-agnostic helper.
hash_stream = _io.hash_file


class Checksum(InputOutputPlugin):
    name = "Checksum / verify"
    description = "Hash the other pane's tagged files, or verify a sums file"
    prompt = "sha256|write|verify> "

    @property
    def greeting(self) -> str:
        names = [e.name for e in self.ctx.other_selected()]
        target = ", ".join(names) if names else "<nothing tagged>"
        return (f"Tagged (other pane): {target}\n"
                "Type an algorithm (sha256, sha1, md5, sha512), "
                "'write [algo] [name]', or 'verify [name]'.")

    def process(self, line: str):
        parts = line.split()
        verb = parts[0].lower() if parts else DEFAULT_ALGO

        if verb == "write":
            return self._write(parts[1:])
        if verb == "verify":
            return self._verify(parts[1:])
        if verb in ALGORITHMS:
            return self._hash(verb)
        return (f"unknown command '{verb}' "
                f"(algorithms: {', '.join(ALGORITHMS)}; or write / verify)")

    # -- hashing tagged files ------------------------------------------------
    def _tagged(self):
        entries = [e for e in self.ctx.other_selected()
                   if not e.is_dir]
        if not entries:
            raise RuntimeError("Tag at least 1 file in the other pane.")
        return entries

    def _hash(self, algo: str):
        fs, root = self.ctx.other_fs, self.ctx.other_path
        lines = []
        for entry in self._tagged():
            try:
                digest = hash_stream(fs, fs.join(root, entry.name), algo)
                lines.append(f"{digest}  {entry.name}")
            except Exception as exc:
                lines.append(f"! {entry.name}: {exc}")
        return lines

    def _write(self, args: list[str]):
        algo = DEFAULT_ALGO
        name = ""
        for arg in args:
            if arg.lower() in ALGORITHMS:
                algo = arg.lower()
            else:
                name = arg
        name = name or f"{algo.upper()}SUMS"

        fs, root = self.ctx.other_fs, self.ctx.other_path
        body = []
        for entry in self._tagged():
            try:
                digest = hash_stream(fs, fs.join(root, entry.name), algo)
                body.append(f"{digest}  {entry.name}")
            except Exception as exc:
                self.print(f"  ! {entry.name}: {exc}")
        if not body:
            return "Nothing hashed; wrote no sums file."

        out_path = fs.join(root, name)
        data = ("\n".join(body) + "\n").encode("utf-8")
        _io.write_bytes(fs, out_path, data)
        try:
            self.ctx.refresh_other()
        except Exception:
            pass
        return f"Wrote {name} ({len(body)} file(s), {algo})."

    # -- verifying against a sums file ---------------------------------------
    def _verify(self, args: list[str]):
        name = args[0] if args else f"{DEFAULT_ALGO.upper()}SUMS"
        fs, root = self.ctx.other_fs, self.ctx.other_path
        sums_path = fs.join(root, name)
        try:
            raw_bytes, _ = _io.read_bytes(fs, sums_path, max_bytes=MAX_SUMS_BYTES)
            text = raw_bytes.decode("utf-8", errors="replace")
        except Exception as exc:
            return f"Could not read {name}: {exc}"

        ok = failed = missing = 0
        for raw in text.splitlines():
            entry = _parse_sums_line(raw)
            if entry is None:
                continue
            expected, filename = entry
            algo = _algo_for_digest(expected)
            if algo is None:
                self.print(f"  ? {filename}: unrecognised hash length")
                continue
            target = fs.join(root, filename)
            if not fs.exists(target):
                self.print(f"  missing  {filename}")
                missing += 1
                continue
            try:
                actual = hash_stream(fs, target, algo)
            except Exception as exc:
                self.print(f"  ! {filename}: {exc}")
                failed += 1
                continue
            if actual.lower() == expected.lower():
                self.print(f"  OK       {filename}")
                ok += 1
            else:
                self.print(f"  FAILED   {filename}")
                failed += 1

        summary = f"{ok} OK"
        if failed:
            summary += f", {failed} FAILED"
        if missing:
            summary += f", {missing} missing"
        return summary


#: Digest hex length -> algorithm, for reading a sums file back.
_DIGEST_LENGTHS = {32: "md5", 40: "sha1", 64: "sha256", 128: "sha512"}


def _algo_for_digest(digest: str) -> str | None:
    return _DIGEST_LENGTHS.get(len(digest))


def _parse_sums_line(line: str):
    """Parse ``<hex>  <name>`` (coreutils style); ``None`` for blank/comment.

    The separator is two spaces (or ``' *'`` for binary mode) in the usual
    format, but a single run of whitespace is accepted too.
    """
    line = line.rstrip("\n")
    if not line.strip() or line.lstrip().startswith("#"):
        return None
    parts = line.split(None, 1)
    if len(parts) != 2:
        return None
    digest, filename = parts[0], parts[1]
    # Binary-mode marker: "<hex> *name".
    if filename.startswith("*"):
        filename = filename[1:]
    if not all(c in "0123456789abcdefABCDEF" for c in digest):
        return None
    return digest, filename
