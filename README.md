# PSK Spotter

Utility to show MQTT data from PSK Repoter in table, filter what you hear and where you are heard.

The use case for this version is to monitor spots on a band, as provided by PSK reporter and a local wsjt-x monitoring a band. New DXCC on the band or overall are identified and shown. A radius around the home grid is used to filter spots that are local, rather than showing all spots. 

Since it using MQTT it is very fast to show new data. And because it uses python, it will works on any platform, which supports python.

Based on pskspotter from:

https://github.com/petrkr/pskspotter.git

Much of the updating was done as "pair coding" with Claude Code. 

The changes from the original: 
 - Uses pyqtgraph to create a window with a scrollable table, and a command panel on the left. 
 - Reads an adif log file to look for prior contacts/dxcc's.
 - Uses uv to build a local environment.


## Build the virtual environment.

`uv sync`

## run:

python src/pskspotter.py <optional command arguments>

You can download cty plist from https://www.country-files.com/cty/cty.plist and then select it by parameter `--cty-plist`
