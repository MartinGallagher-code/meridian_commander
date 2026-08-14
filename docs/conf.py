# Sphinx configuration for the Read the Docs manual.
#
# This directory is also the GitHub Pages site (index.html, style.css,
# assets/), which .github/workflows/pages.yml uploads verbatim. The two
# coexist because Sphinx only treats files with a source suffix (.md, .rst)
# as documents: the site's index.html and style.css are ignored by the docs
# build, and the Markdown sources ride along in the Pages artifact without
# being linked from the site.

import os
import sys

# The API reference imports the package. Read the Docs installs it with pip,
# but this lets a plain local `sphinx-build` work from a checkout too.
sys.path.insert(0, os.path.abspath(".."))

from meridian_commander import __version__

project = "Meridian Commander"
author = "Martin Gallagher"
copyright = "Martin Gallagher"
version = __version__
release = __version__

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.viewcode",
    "sphinx_rtd_theme",
]

exclude_patterns = ["_build"]

# The tutorial links to section anchors GitHub-style; generate matching ones.
myst_heading_anchors = 3

autodoc_member_order = "bysource"

html_theme = "sphinx_rtd_theme"
html_theme_options = {
    "collapse_navigation": False,
}
