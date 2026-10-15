"""Single source of truth for the DX Spotter version string.

The literal assigned to :data:`__version__` below is the one authoritative
version for the whole project.  Everything else derives from it:

* ``pyproject.toml`` declares a dynamic version read from this file by
  Hatchling (``[tool.hatch.version]``), so the package metadata always
  matches.
* ``dx-spotter.spec`` imports :data:`__version__` for the macOS bundle's
  ``CFBundleShortVersionString``.
* ``src/main_window.py`` and ``docs/conf.py`` import it for the window
  title and documentation build.

To release a new version, edit this line and nothing else.
"""

__version__: str = "0.3.5.0"
