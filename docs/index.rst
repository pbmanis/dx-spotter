DX Spotter Documentation
========================

DX Spotter is a desktop application for ham radio operators that monitors
`PSK Reporter <https://pskreporter.info>`_ (via MQTT) and optionally WSJT-X
(via UDP) for DX spots, and colours each spot by its DXCC award status
relative to the operator's contact log.

.. toctree::
   :maxdepth: 2
   :caption: User Guide

   usage
   configuration
   display

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/adif_log
   api/appconfig
   api/dxspotter
   api/main_window
   api/spot_window
   api/settings_dialog
   api/wsjtx_listener

Overview
--------

DX Spotter connects to the PSK Reporter MQTT broker
(``mqtt.pskreporter.info:1883``) and subscribes to a band/mode/callsign
filter topic.  Each incoming spot is:

1. Looked up in a callsign database (pyhamtools / cty.plist) to resolve the
   country name and ADIF DXCC entity number.
2. Compared against the operator's ADIF or RumLogNG contact log to determine
   the award status for the active criterion.
3. Inserted into the spot table with a background colour reflecting that
   status.

Optionally, DX Spotter also listens on a UDP port for WSJT-X decode packets.
WSJT-X spots are displayed in the same table (in italic font) and support
double-click reply via the WSJT-X UDP command protocol.

Quick start
-----------

.. code-block:: bash

   # Install
   pip install -e .

   # Run (macOS / Linux)
   dxspotter --call W1XYZ --band 20m --mode FT8

   # Run with WSJT-X listener
   dxspotter --call W1XYZ --band 20m --mode FC --wsjt

See :doc:`usage` for full installation instructions and command-line options.

Source layout
-------------

.. code-block:: text

   src/
     adif_log.py        — ADIF / RumLogNG log parser; award status queries
     appconfig.py       — TOML config load/save; AppConfig dataclass
     dxspotter.py       — Main controller; MQTT + WSJT-X wiring; entry point
     main_window.py     — Qt main window (dock layout, parameter tree, signals)
     spot_window.py     — Spot table widget; row colouring; age expiry
     settings_dialog.py — Modal settings dialog (log source, grid, UDP)
     wsjtx_listener.py  — WSJT-X UDP listener and command sender

Indices and tables
------------------

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
