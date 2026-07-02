Source Layout
=============

.. code-block:: text

   src/
     adif_log.py          — ADIF / RumLogNG log parser; award status queries
     appconfig.py         — TOML config load/save; AppConfig dataclass
     band_map.py          — Band map widget; spot plotting; click-to-tune
     commander_client.py  — DX Lab Commander TCP/IP rig control client
     dxspotter.py         — Main controller; MQTT + WSJT-X wiring; entry point
     main_window.py       — Qt main window (dock layout, parameter tree, signals)
     mqtt_listener.py     — PSK Reporter MQTT listener and spot parser
     settings_dialog.py   — Modal settings dialog (log source, grid, UDP)
     spot_window.py       — Spot table widget; row coloring; age expiry
     telnet_cluster.py    — Telnet DX Cluster listener and spot parser
     version.py           — Version string
     wsjtx_listener.py    — WSJT-X UDP listener and command sender
