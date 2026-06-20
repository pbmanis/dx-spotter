"""Sphinx configuration for DX Spotter documentation."""
import sys
from pathlib import Path

# Make the src/ package importable during autodoc
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# -- Project information -------------------------------------------------------
from version import __version__  # noqa: E402

project = "DX Spotter"
author = r"\normalfont\small \hfill \begin{tabular}{r} Concept, Design, Debugging, Testing: Paul B. Manis (NC3G)"
author += r" \\ \small Implementation: Claude Code, powered by Claude Sonnet 4.6 (model ID: claude-sonnet-4-6)."
author += r" \\ \small PSK Reporter MQTT reading code: Petr Kracik (OK1RP)  \end{tabular}"
copyright = "2024-2026, Paul Manis"
license = "MIT License"
release = __version__
version = __version__
show_authors = True

# -- General configuration -----------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
]

napoleon_google_docstring = False
napoleon_numpy_docstring = True
napoleon_use_param = True
napoleon_use_rtype = True
napoleon_preprocess_types = True
napoleon_use_ivar = True

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "special-members": "__init__",
}
autodoc_typehints = "description"
autodoc_typehints_description_target = "documented"
always_document_param_types = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- HTML output ---------------------------------------------------------------
html_theme = "furo"
html_static_path = ["_static"]
html_title = "DX Spotter"

# -- LaTeX / PDF output --------------------------------------------------------
latex_engine = "xelatex"
latex_documents = [
    (
        "index",                   # toctree root
        "dxspotter.tex",           # output .tex filename
        f"DX Spotter V{release}",  # document title
        author,
        "manual",                  # document class
    ),
]
latex_elements = {
    "papersize": "letterpaper",
    "pointsize": "11pt",
    # Keep the default Sphinx preamble; add nothing extra.
    "preamble": "",
}
