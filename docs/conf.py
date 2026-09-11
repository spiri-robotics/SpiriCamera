"""Sphinx configuration for SpiriCamera."""

from __future__ import annotations
import sys
from pathlib import Path

# Add the project root to the path so autodoc can find the package
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

project = "SpiriCamera"
copyright = "2026, Spiri"  # noqa: A001
author = "Alex Davies"
release = "0.1.0"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

html_theme = "furo"
html_static_path: list[str] = ["static"]
html_css_files = ["custom.css"]

html_theme_options = {
    "light_logo": "spiri-logo-light.svg",
    "dark_logo": "spiri-logo-dark.svg",
    "sidebar_hide_name": True,
}

# Autodoc settings
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
}
autodoc_member_order = "bysource"
autodoc_typehints = "description"

# Napoleon settings for Google-style docstrings
napoleon_google_docstring = True
napoleon_numpy_docstring = False

# MyST extensions
myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

# Intersphinx mapping
intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pydantic": ("https://docs.pydantic.dev/latest/", None),
    "loguru": ("https://loguru.readthedocs.io/en/latest/", None),
    "typer": ("https://typer.tiangolo.com/", None),
}
