"""Meridian Commander -- a Midnight Commander style terminal file manager.

Two panes, local and networked (SFTP/FTP) browsing, cross-location copy/move,
bidirectional directory synchronization, a file viewer and a file editor.
"""

from __future__ import annotations

__version__ = "1.0.0"

from .app import main

__all__ = ["main", "__version__"]
