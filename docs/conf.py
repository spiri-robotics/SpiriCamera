"""Sphinx configuration for SpiriCamera.

The docs are a controlled document, like every Spiri manual: the spiri_docs
extension reads revisions.yaml on every build, validates it, sets the revision,
stamps it into the PDF footer, and names the PDF after it. Theme and structure
live here, because they are decided once per project.

Build with `make docs` (HTML) and `make pdf`.
"""

from __future__ import annotations

project = "SpiriCamera"
author = "Alex Davies"
copyright = "%Y, Alex Davies"  # noqa: A001 - the name Sphinx expects

# `version` and `release` are deliberately not set here. spiri_docs sets both
# from the current entry in revisions.yaml.

extensions = [
    "myst_parser",
    "spiri_docs",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Prose ------------------------------------------------------------------

# `substitution` is what makes {{ document_number }} and {{ revision }} work in
# Markdown; the values come from revisions.yaml via spiri_docs.
myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "substitution",
]
myst_heading_anchors = 3

# -- API reference ----------------------------------------------------------

autodoc_default_options = {
    "members": True,
    "show-inheritance": True,
}
autodoc_member_order = "bysource"
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
# fastapi/nicegui are mocked because importing them for real during a docs
# build triggers a pydantic bug on Python 3.14 where an unrelated model's
# schema generation corrupts the schema-ref cache for fastapi's own model
# (id() reuse in a pydantic internal cache — pydantic/pydantic#8647), causing
# ImportError while autodocing SpiriCamera.ui / SpiriCamera.whep.
autodoc_mock_imports = ["fastapi", "nicegui"]

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
    "loguru": ("https://loguru.readthedocs.io/en/latest/", None),
    "typer": ("https://typer.tiangolo.com/", None),
}

# -- HTML -------------------------------------------------------------------

html_theme = "furo"
html_static_path = ["_static"]
html_css_files = ["custom.css"]
html_title = "SpiriCamera"
html_baseurl = "https://spiri-robotics.github.io/SpiriCamera/"

html_theme_options = {
    "navigation_with_keys": True,
    # Two files, not one tinted by CSS: the wordmark is black in one and white in
    # the other, and `filter: invert()` on a multi-coloured mark would take the
    # brand colours with it. Furo swaps these on the theme toggle.
    "light_logo": "spiri-logo-light.svg",
    "dark_logo": "spiri-logo-dark.svg",
}

# -- PDF --------------------------------------------------------------------
# The cover logo. pdflatex cannot read SVG, so spiri_docs converts this one to
# PDF during a LaTeX build. The light variant is correct: a cover is printed.
spiri_docs_logo = "_static/spiri-logo-light.svg"
# spiri_docs sets latex_documents (so the file is named for the document number
# and revision) and appends the document-control preamble. What is left here is
# page setup.
latex_elements = {
    "papersize": "letterpaper",
    "pointsize": "11pt",
    "figure_align": "htbp",
}
