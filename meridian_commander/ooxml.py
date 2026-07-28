"""The Office Open XML package layer, shared by the .xlsx/.docx/.pptx readers.

Every OOXML file -- a workbook, a document, a presentation -- is the same
thing underneath: a zip archive of XML parts wired together by relationship
files.  Finding the main part, following an ``r:id`` to the part it names, and
reading a part safely is identical for all three, so it lives here once and
:mod:`meridian_commander.xlsx`, :mod:`meridian_commander.docx` and
:mod:`meridian_commander.pptx` are left with only the format-specific work.

Two rules the whole layer follows:

* **Names are matched on their local part**, ignoring the namespace.  No two
  parts of the format reuse a name with a different meaning, so this costs
  nothing and a producer that declares the schema oddly still parses.
* **Nothing is read in part.**  A zip's central directory lives at the *end*
  of the archive, so a truncated file does not parse at all; the size cap is a
  refusal rather than the silent truncation a text file would get.
"""

from __future__ import annotations

import io
import posixpath
import zipfile
import xml.etree.ElementTree as ET

# The archive is read whole (see above), so this bounds the memory a stray
# "view" on a huge file can cost.
MAX_BYTES = 64 * 1024 * 1024
# A part that inflates beyond this is refused unread: the compressed size in
# the central directory says nothing about what a decompressor will produce.
MAX_PART_BYTES = 256 * 1024 * 1024


class OoxmlError(Exception):
    """An OOXML file could not be read."""


def local(tag: str) -> str:
    """Local name of an element tag, discarding the namespace."""
    return tag.rpartition("}")[2]


def attr(elem, name: str) -> str | None:
    """An attribute by local name, whichever namespace it was written in.

    Producers differ over whether they qualify an attribute, and the local
    name is unambiguous in practice -- no element here carries two attributes
    that differ only by namespace.
    """
    for key, value in elem.attrib.items():
        if key.rpartition("}")[2] == name:
            return value
    return None


def rel_id(elem) -> str | None:
    """The ``r:id`` of an element: what it points at lives in the .rels file."""
    return attr(elem, "id")


# -- parts ------------------------------------------------------------------
def read_part(zf: zipfile.ZipFile, name: str) -> bytes | None:
    """Read one archive member, or ``None`` if absent or implausibly large."""
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_PART_BYTES:
        raise OoxmlError(f"{name} is too large to read "
                         f"({info.file_size} bytes)")
    try:
        return zf.read(name)
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise OoxmlError(f"cannot read {name}: {exc}") from exc


def parse_xml(data: bytes, name: str):
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise OoxmlError(f"{name} is not valid XML: {exc}") from exc


def parse_part(zf: zipfile.ZipFile, name: str):
    """Read and parse a member, or ``None`` when it is not in the archive."""
    data = read_part(zf, name)
    return None if data is None else parse_xml(data, name)


def part_size(zf: zipfile.ZipFile, name: str) -> int | None:
    """Uncompressed size of a member, or ``None`` if it is not there."""
    try:
        return zf.getinfo(name).file_size
    except KeyError:
        return None


# -- relationships ----------------------------------------------------------
def resolve(base_part: str, target: str) -> str:
    """Turn a relationship target into an archive member name."""
    if target.startswith("/"):
        return target[1:]
    base_dir = posixpath.dirname(base_part)
    if not base_dir:
        return target
    return posixpath.normpath(posixpath.join(base_dir, target))


def relationships(zf: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """``{rId: (type, member)}`` for the part's ``.rels`` companion.

    Passing the empty part name gives the package's own relationships, which
    is where the main part is named -- the naming rule below turns "" into
    "_rels/.rels" on its own.
    """
    base_dir = posixpath.dirname(part)
    rels_name = posixpath.join(base_dir, "_rels",
                               posixpath.basename(part) + ".rels")
    root = parse_part(zf, rels_name)
    if root is None:
        return {}
    out = {}
    for node in root:
        if local(node.tag) != "Relationship":
            continue
        rid = node.get("Id")
        target = node.get("Target")
        if not rid or not target:
            continue
        # An external relationship (a link to another file) has no member in
        # this archive, so there is nothing here to resolve it against.
        if node.get("TargetMode") == "External":
            continue
        out[rid] = (node.get("Type", ""), resolve(part, target))
    return out


def related(rels: dict, suffix: str) -> str | None:
    """The member of the first relationship whose type ends with ``suffix``."""
    for _rid, (rtype, member) in rels.items():
        if rtype.rsplit("/", 1)[-1] == suffix:
            return member
    return None


# -- packages ---------------------------------------------------------------
def open_package(data: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise OoxmlError(f"not a readable Office file: {exc}") from exc


def main_part(zf: zipfile.ZipFile, fallback: str) -> str:
    """The package's main document part, by relationship or by convention."""
    return related(relationships(zf, ""), "officeDocument") or fallback


def load_bytes(fs, path: str) -> bytes:
    """Read a whole Office file through any backend, refusing oversized ones."""
    stream = fs.open_read(path)
    try:
        data = stream.read(MAX_BYTES + 1)
    finally:
        stream.close()
    if len(data) > MAX_BYTES:
        raise OoxmlError(
            f"file is larger than {MAX_BYTES // (1024 * 1024)} MiB; an "
            "Office file cannot be read in part")
    return data
