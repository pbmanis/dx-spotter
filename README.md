# DX Spotter

Based on pskspotter from:

https://github.com/petrkr/pskspotter.git

Utility to show MQTT data from PSK Repoter in table, filter what you hear and where you are heard. This
program can also read from an running instance of WSJT-X get received stations. 

The use case for this version is to monitor spots on a band, as provided by PSK reporter and a local wsjt-x monitoring a band. New DXCC on the band or overall are identified and shown. A radius around the home grid is used to filter spots that are local, rather than showing all spots. 

This is currently designed to work with RumLogNG on a Mac. The GUI use PyQt6, and was mostly coded by
Claude (Sonnett).

Since it using MQTT it is very fast to show new data. And because it uses python, it will works on any platform, which supports python.



Much of the updating was done as "pair coding" with Claude Code. 

The changes from the original: 
 - Uses pyqtgraph to create a window with a scrollable table, and a command panel on the left. 
 - Reads an adif log file to look for prior contacts/dxcc's.
 - Has filtering on spots as well as a comparision with the current log file to help identify new entities, or 5B-DXCC entities. A special mode also filters just for 6M DXCC.
 - Uses uv to build a local environment.
 - A shell script allows the program to be built as an application.
 - A settings file (which lives in ~/Library/Application Support/DXSpotter) holds the current program state, as well as some parameters that do not often change (WSJT-X address/port; location, where to find the log file).


## Build the virtual environment.

`uv sync`

## run:

python src/pskspotter.py <optional command arguments>

You can download cty plist from https://www.country-files.com/cty/cty.plist and then select it by parameter `--cty-plist`

## Build an app:
./build_app.sh

