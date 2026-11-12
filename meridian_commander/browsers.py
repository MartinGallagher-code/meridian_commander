# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Martin J. Gallagher
"""Choosing which full-screen browser opens a file.

Each browser knows one kind of content and nothing about the others; this is
the one place that maps a file to a browser.  It lives apart from them so the
dependencies stay one-way -- the document viewer builds on the text viewer,
and neither has to know that this choice exists.

The choice is made on the *name*, not the content.  That is all a remote pane
knows before downloading the file, and it is what the user is looking at when
they press F3.  The one exception is a name two kinds of file share: ``.dc``
is a datacenter layout and also a ``dc`` calculator script, so a ``.dc`` is
claimed only once the start of the file says it is a layout.  That claim reads
a few kilobytes, and only of a file whose name already matched.
"""

from __future__ import annotations

from .dcview import FloorView, is_layout_file, is_layout_name
from .docx import DocxView, is_document
from .filesystems import FileSystem
from .image import is_image
from .imageview import ImageView
from .markdown import MarkdownView, is_markdown
from .pdf import PdfView, is_pdf
from .pptx import SlideView, is_presentation
from .sheetview import SheetView
from .table import is_table
from .tableview import TableView
from .viewer import Viewer
from .xlsx import is_spreadsheet


#: What claims a file, and what opens it, in the order the claims are tried:
#: (claims the name, confirms it by the content or ``None``, the browser).
#: A table rather than a chain of ``if``s so that :func:`has_own_browser` --
#: which decides whether an outside pager may have the file instead -- cannot
#: drift out of step with what :func:`viewer_for` would actually open.
BROWSERS = (
    (is_spreadsheet, None, SheetView),
    (is_table, None, TableView),
    (is_document, None, DocxView),
    (is_presentation, None, SlideView),
    (is_markdown, None, MarkdownView),
    (is_image, None, ImageView),
    (is_pdf, None, PdfView),
    (is_layout_name, is_layout_file, FloorView),
)


def browser_for(fs: FileSystem, path: str):
    """The browser class that claims ``path``, or ``None`` for text."""
    name = fs.basename(path)
    for claims, confirms, browser in BROWSERS:
        if claims(name) and (confirms is None or confirms(fs, path)):
            return browser
    return None


def viewer_for(fs: FileSystem, path: str):
    """The browser for ``path``: a grid, table, document, deck, page, image,
    floor plan or text."""
    return (browser_for(fs, path) or Viewer)(fs, path)


def has_own_browser(fs: FileSystem, path: str) -> bool:
    """Whether ``path`` has a browser of its own rather than being text.

    Asked before handing a file to a configured pager: ``less`` on a ``.xlsx``
    is a screen of zip bytes, and choosing a pager is an answer about text
    files rather than a request to give up the other browsers.  It is asked
    the same question :func:`viewer_for` is, so a ``.dc`` that is a layout
    keeps the floor view and one that is a calculator script goes to the
    pager.
    """
    return browser_for(fs, path) is not None
