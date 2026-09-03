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
from .image import is_image
from .imageview import ImageView
from .markdown import MarkdownView, is_markdown
from .pdf import PdfView, is_pdf
from .pptx import SlideView, is_presentation
from .sheetview import SheetView
from .viewer import Viewer
from .xlsx import is_spreadsheet


#: What claims a name, and what opens it, in the order the claims are tried.
#: A table rather than a chain of ``if``s so that :func:`has_own_browser` --
#: which decides whether an outside pager may have the file instead -- cannot
#: drift out of step with what :func:`viewer_for` would actually open.
BROWSERS = (
    (is_spreadsheet, SheetView),
    (is_document, DocxView),
    (is_presentation, SlideView),
    (is_markdown, MarkdownView),
    (is_image, ImageView),
    (is_pdf, PdfView),
)


def viewer_for(fs: FileSystem, path: str):
    """The browser for ``path``: a grid, document, deck, page, image or text."""
    name = fs.basename(path)
    for claims, browser in BROWSERS:
        if claims(name):
            return browser(fs, path)
    return Viewer(fs, path)


def has_own_browser(name: str) -> bool:
    """Whether ``name`` has a browser of its own rather than being text.

    Asked before handing a file to a configured pager: ``less`` on a ``.xlsx``
    is a screen of zip bytes, and choosing a pager is an answer about text
    files rather than a request to give up the other browsers.
    """
    return any(claims(name) for claims, _ in BROWSERS)
