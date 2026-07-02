# Contributing to DX Spotter

Thank you for your interest in improving DX Spotter.  This document covers
everything you need to get a working development environment, understand the
project conventions, and submit a change.

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Getting started](#getting-started)
3. [Project layout](#project-layout)
4. [Running the app](#running-the-app)
5. [Code style](#code-style)
6. [Docstrings](#docstrings)
7. [Tests](#tests)
8. [Building the docs](#building-the-docs)
9. [Building the macOS app bundle](#building-the-macos-app-bundle)
10. [Submitting a change](#submitting-a-change)
11. [Architecture constraints](#architecture-constraints)
12. [Licence](#licence)

---

## Prerequisites

| Tool | Minimum version | Purpose |
|---|---|---|
| Python | 3.13 | Runtime |
| [uv](https://docs.astral.sh/uv/) | latest | Dependency management and virtual-env |
| Git | any | Version control |
| macOS | 13+ | Required only for RumLogNG / `.app` bundle features |

---

## Getting started

```bash
# Clone
git clone https://github.com/pbmanis/dx-spotter.git
cd dx-spotter

# Create the virtual environment and install all dependencies
# (runtime + dev: furo, pyinstaller, pytest, sphinx, …)
uv sync --all-groups

# Verify the environment
uv run python -c "import PyQt6; print('PyQt6 OK')"
```

---

## Project layout

```
src/
  adif_log.py          ADIF / RumLogNG log parser; award-status queries
  appconfig.py         TOML config load/save; AppConfig dataclass
  band_map.py          Band map widget (pyqtgraph); spot plotting; click-to-tune
  commander_client.py  DX Lab Commander TCP/IP rig control
  dxspotter.py         Main controller: MQTT + WSJT-X wiring; Qt app lifecycle
  main_window.py       MainWindow — ParameterTree dock + spot table dock + status bar
  mqtt_listener.py     PSK Reporter MQTT listener and spot parser
  settings_dialog.py   Modal settings dialog
  spot_window.py       SpotTable — QTableWidget with award colouring, age expiry
  telnet_cluster.py    Telnet DX Cluster listener and spot parser
  version.py           Version string (single source of truth)
  wsjtx_listener.py    WSJT-X UDP listener and command encoder/decoder

docs/                  Sphinx source (Furo theme, dark mode)
tests/                 pytest test suite
build_app.sh           PyInstaller macOS app bundle script
build_docs.sh          Sphinx HTML + optional LaTeX/PDF
```

---

## Running the app

```bash
# Using saved configuration
uv run python src/dxspotter.py

# Override band, mode, and enable terminal output
uv run python src/dxspotter.py -b 20m -m FT8 -t

# Full option list
uv run python src/dxspotter.py --help
```

The config file lives at `~/Library/Application Support/DXSpotter/config.toml`
on macOS and is written automatically on quit.

---

## Code style

- **Formatter**: [black](https://black.readthedocs.io/).  Run before committing:
  ```bash
  uv run black src/
  ```
- **Style guide**: PEP 8 for everything black does not handle (naming, imports,
  line length already managed by black).
- **Type annotations**: all function parameters and return values must be typed.
  Use `from __future__ import annotations` at the top of every module so
  forward references work without quotes.
- **Comments**: write *why*, not *what*.  Omit comments entirely when the code
  is self-explanatory.  A one-line comment is almost always enough; never use
  multi-line comment blocks.
- **New features**: match the naming conventions already in the file you are
  editing.  Do not add abstractions beyond what the task requires.

---

## Docstrings

Public functions and classes use **NumPy-style** docstrings for Sphinx
autodoc generation:

```python
def award_status(self, dxcc: int, band: str, criterion: str) -> str:
    """Return the DXCC award status for a given entity.

    Parameters
    ----------
    dxcc : int
        ADIF DXCC entity number.
    band : str
        Amateur band string, e.g. ``"20m"``.
    criterion : str
        Award criterion: ``"mixed"``, ``"cw"``, ``"ssb"``, ``"digital"``,
        ``"5bd"``, or ``"6m"``.

    Returns
    -------
    str
        One of ``"confirmed"``, ``"worked"``, ``"new"``, ``"n/a"``,
        or ``"over100"`` (5BD only).
    """
```

Private functions (`_name`) need only a short description comment unless the
logic is non-obvious.

PyQt6 signals are documented with `#:` comments immediately above the
assignment so Sphinx picks up the description rather than PyQt6's internal
docstring:

```python
#: Emitted when the user double-clicks a spot row.  Payload is a spot dict.
spot_activated = pyqtSignal(dict)
```

---

## Tests

```bash
# Run the full suite
uv run pytest

# Run a specific file
uv run pytest tests/test_adif_log.py -v
```

Tests live under `tests/` and are discovered automatically by pytest
(configured in `pyproject.toml`).  The `src/` directory is on the Python path
so imports work without installation.

---

## Building the docs

```bash
# Incremental HTML build
./build_docs.sh

# Full rebuild (clears cached environment first)
./build_docs.sh -E
```

Output lands in `docs/_build/html/`.  Open `docs/_build/html/index.html` in a
browser to preview.

When adding a new source module, create the matching stub under `docs/api/`:

```rst
.. automodule:: my_module
   :members:
   :show-inheritance:
```

Then add the stub to the API Reference toctree in `docs/index.rst`.

---

## Building the macOS app bundle

```bash
bash build_app.sh
```

The resulting `DXSpotter.app` is placed in `dist/`.  Do not commit the
`dist/` or `build/` directories.

---

## Submitting a change

1. **Fork** the repository and create a branch:
   ```bash
   git checkout -b fix/my-descriptive-name
   ```
2. **Make your changes** and run black + pytest:
   ```bash
   uv run black src/
   uv run pytest
   ```
3. **Build the docs** and confirm zero warnings:
   ```bash
   ./build_docs.sh -E 2>&1 | grep -E "WARNING|ERROR"
   ```
4. **Commit** with a concise message describing *why* the change is needed,
   not just *what* changed.
5. **Open a pull request** against `main`.  Describe the problem, your
   approach, and any trade-offs.

### What makes a good PR

- Focused: one logical change per PR.
- No unrelated cleanup bundled in.
- New public functions have NumPy docstrings and type annotations.
- If you add a new source module, add the `docs/api/` stub and toctree entry.

---

## Architecture constraints

These are hard rules enforced throughout the codebase:

| Constraint | Reason |
|---|---|
| **RumLogNG database is read-only.** Always open with `sqlite3.connect("file:path?mode=ro", uri=True)`. Never write to it. | The database belongs to RumLogNG; writes would corrupt the user's log. |
| **Never update Qt widgets from background threads.** MQTT, WSJT-X, and telnet listeners run on daemon threads.  Use `pyqtSignal` to pass data to the main thread. | Qt's widget system is not thread-safe. |
| **Scalar GIL-safe writes only from threads.** `bool` and `float` instance variables (e.g. `_last_wsjt_heartbeat`) may be written from background threads and read by the main-thread 250 ms QTimer. | CPython's GIL makes these atomic for simple types. |
| **Config is written on quit only.** `AppConfig.save_config()` is called from `_save_and_cleanup()`, never at runtime. | Prevents partial writes if the app crashes mid-session. |

---

## Licence

DX Spotter is released under the **MIT Licence**.  By contributing you agree
that your changes will be distributed under the same terms.
