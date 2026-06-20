# DX Spotter — Claude Code guide

## Project overview

Ham radio spot aggregator for DX hunting. Subscribes to PSK Reporter via MQTT and listens to WSJT-X UDP decodes, displays spots in a PyQt6 table coloured by DXCC award status, and compares against an ADIF log or RumLogNG CloudKit database.

##
- format code with black, and use pep-8 style when generating new code blocks. 
- Never perform git actions.
- Never delete a file; ask me to delete it manually.
- Create docstrings using the numpy format, in prepratation for sphinx to generate the documentation.
- Only generate docstrings for "public" functions, not for "private" functions. A short description comment is sufficient for the private functions, unless they do something complex or unusual, in which case a longer description is needed.
- Use clear variable and function names, consistent with the names already in the project.
- Always type variables and return values. 
- If the logic of a suggestion/request is not clear, or a conflicting set of states appears in the analysis, do not try to solve the problem, but instead ask for clarification using a succinct description of the issue. 

## Running the app

```bash
cd src
python dxspotter.py          # uses saved config
python dxspotter.py -b 20m -m FT8 -t   # band, mode, terminal output
```

## building the app

```
./build_app.sh
```
- do not build the app unless it is specifically requested.
  
## Architecture

| File | Role |
|---|---|
| `src/dxspotter.py` | Main controller: MQTT client, WSJT-X wiring, Qt app lifecycle |
| `src/main_window.py` | `MainWindow` — ParameterTree settings dock + spot table dock + status bar |
| `src/spot_window.py` | `SpotTable` — QTableWidget with award colouring, age expiry, context menu |
| `src/adif_log.py` | `ADIFLog` — parses ADIF files or RumLogNG SQLite (read-only) |
| `src/wsjtx_listener.py` | `WsjtxListener` — UDP thread, WSJT-X protocol encode/decode |
| `src/settings_dialog.py` | Settings dialog (log source, station grid, WSJT-X network) |
| `src/appconfig.py` | `AppConfig` dataclass + TOML load/save (`~/Library/Application Support/DXSpotter/config.toml`) |

## Critical constraints

- **RumLogNG database is strictly read-only.** Always open with `sqlite3.connect("file:path?mode=ro", uri=True)`. Never write to it.
- MQTT callbacks and WSJT-X listener run in background threads. Use PyQt6 signals to pass data to the main thread — never update Qt widgets directly from threads.
- GIL-safe scalar writes (`bool`, `float`) from background threads are polled by a 250 ms `QTimer` in the main thread for status indicators.

## Award status values

`'confirmed'` · `'worked'` · `'new'` · `'n/a'` · `'over100'` (5BD only — band ≥ 100 confirmed entities, cyan)

## Config file

`~/Library/Application Support/DXSpotter/config.toml` — written on quit, never during runtime.

## Dependencies

Managed with `uv`. Key packages: `PyQt6`, `pyqtgraph`, `paho-mqtt`, `pyhamtools`, `colorama`.

```bash
uv sync          # install
uv run python src/dxspotter.py
```
