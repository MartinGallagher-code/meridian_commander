"""Choosing which full-screen browser opens a file.

Each browser knows one kind of content and nothing about the others; this is
the one place that maps a file to a browser.  It lives apart from them so the
dependencies stay one-way -- the document viewer builds on the text viewer,
and neither has to know that this choice exists.

The choice is made on the *name*, not the content.  That is all a remote pane
knows before downloading the file, and it is what the user is looking at when
they press F3.
"""

from __future__ import annotations

from .docx import DocxView, is_document
from .filesystems import FileSystem
from .pptx import SlideView, is_presentation
from .sheetview import SheetView
from .viewer import Viewer
from .xlsx import is_spreadsheet


def viewer_for(fs: FileSystem, path: str):
    """The browser for ``path``: a grid, a document, a deck, or plain text."""
    name = fs.basename(path)
    if is_spreadsheet(name):
        return SheetView(fs, path)
    if is_document(name):
        return DocxView(fs, path)
    if is_presentation(name):
        return SlideView(fs, path)
    return Viewer(fs, path)
