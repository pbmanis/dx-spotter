Installation and Usage
======================

Requirements
------------

* Python 3.13 or later
* PyQt6
* pyqtgraph
* paho-mqtt >= 2.0
* pyhamtools
* colorama

Installing
----------

Clone the repository and install in editable mode:

.. code-block:: bash

   git clone https://github.com/yourname/dx-spotter.git
   cd dx-spotter
   pip install -e .

Or with `uv <https://docs.astral.sh/uv/>`_:

.. code-block:: bash

   uv sync
   uv run dxspotter

The ``dxspotter`` entry point is registered in ``pyproject.toml``.

Running the application
-----------------------

.. code-block:: bash

   dxspotter [options]

The application window opens immediately and begins connecting to the PSK
Reporter MQTT broker.  Status indicators in the bottom status bar show
connection state for both PSK Reporter and WSJT-X.

Command-line arguments
----------------------

All arguments are optional.  When a value is not provided on the command line,
the saved configuration is used (see :doc:`configuration`).

.. option:: -c CALL, --call CALL

   Your amateur radio callsign (e.g. ``W1XYZ``).  When provided, PSK Reporter
   spots where *your* call is the sender are shown as **TX** direction (orange
   label) rather than RX.  Also used as the target for the WSJT-X ``'ME'``
   decode filter.

.. option:: -b BAND, --band BAND

   Band to monitor.  Choices: ``2m``, ``6m``, ``10m``, ``15m``, ``17m``,
   ``20m``, ``30m``, ``40m``, ``80m``, ``160m``.  The PSK Reporter MQTT topic
   is built from this value.

.. option:: -m MODE, --mode MODE

   Mode filter.  Choices:

   ===== ======================================
   Value Meaning
   ===== ======================================
   FT8   FT8 only
   FT4   FT4 only
   FT2   FT2 only
   CW    CW only
   SSB   SSB only
   FC    FT8 + FT4 + FT2 + CW (default)
   FCS   FT8 + FT4 + FT2 + CW + SSB
   CS    CW + SSB
   ===== ======================================

.. option:: -r KM, --range KM

   Maximum distance in km between *your* grid square and the **reporting**
   station (PSK Reporter only).  Spots from reporters further away than this
   are discarded.  ``0`` (default) means no range limit.

.. option:: -t, --terminal

   Print each spot to the terminal in addition to the GUI table.  Useful for
   debugging or piping to a log file.

.. option:: -W, --wsjt

   Enable the WSJT-X UDP listener.  WSJT-X must be running and configured to
   send UDP packets to the same address and port (see :doc:`configuration`).

.. option:: --wsjt-filter {CQ,all,me}

   Decode filter for WSJT-X spots:

   * ``CQ`` — show only stations calling CQ (default).
   * ``all`` — show every decoded callsign.
   * ``me`` — show only decodes addressed to your callsign (requires ``--call``).

.. option:: --wsjt-port PORT

   UDP port to listen on for WSJT-X packets (default ``2237``).

.. option:: --cty-plist FILE

   Path to a local CTY plist file for callsign-to-country lookup.  If omitted,
   pyhamtools downloads the file from the internet on first run.

macOS .app bundle
-----------------

A standalone macOS application bundle can be built with PyInstaller:

.. code-block:: bash

   bash build_app.sh

The resulting ``DXSpotter.app`` is placed in ``dist/``.  When launched as an
app bundle, macOS passes a ``-psn_XXXXXXXX`` argument which DX Spotter
silently strips before argument parsing.

User interface walkthrough
--------------------------

Window layout
~~~~~~~~~~~~~

The main window is divided into two docks:

**Left dock — Settings panel**
   Contains (top to bottom):

   * **Data Filters** parameter group — Band, Mode, Decode Filter (WSJT-X),
     Max Range, and Max Spot Age controls.  Changes take effect immediately.
   * **ADIF Log** parameter group — File picker for the ADIF export file.
   * **Display** parameter group — Terminal Output toggle.
   * **Award Criteria** radio group — selects which DXCC award colors the
     QSL column.  See :doc:`display`.
   * **Display Filter** radio group — hides/shows rows based on DXCC status.
   * **Reports** panel — live counts of PSK Reporter spots, WSJT-X spots,
     and total spots received this session.
   * **Restart** button — clears the table and re-subscribes to the MQTT topic.
   * **Settings** button — opens the persistent settings dialog (log source,
     grid square, UDP address/port).
   * **Quit** button — saves configuration and closes the application.

**Right dock — Spot table**
   Displays all received spots.  See :doc:`display` for column descriptions
   and color coding.

Status bar
~~~~~~~~~~

The bottom status bar contains the following fields (left to right):

* **Left** — log source summary: file name, total QSOs, and confirmed DXCC
  count (LoTW + paper).
* **T1 / T2** — telnet DX Cluster connection state for cluster 1 and cluster 2:

  * Green ``T1: connected`` — cluster is connected.
  * Orange ``T1: connecting…`` — connection in progress or reconnecting.
  * Grey ``T1: off`` — cluster is disabled in settings.

* **PSKR** — PSK Reporter MQTT connection: green ``PSKR: connected`` or
  grey ``PSKR: connecting…``.
* **WSJT-X** — WSJT-X listener state (three colors):

  * Green ``WSJT-X: connected`` — heartbeat active and decodes are arriving.
  * Yellow ``WSJT-X: no decodes`` — heartbeat active but no decode packets
    received in the last *N* minutes (configurable; see
    :option:`wsjt_no_spot_mins`).
  * Red ``WSJT-X: no signal (Xs)`` — no heartbeat received in the last 45 s.
  * Grey ``WSJT-X: waiting…`` — listener active but no heartbeat received yet.

Interacting with spots
~~~~~~~~~~~~~~~~~~~~~~

* **Single click** on a row — bolds the row and all other rows for the same
  callsign; click elsewhere to deselect.
* **Double click** on a digital (FT8/FT4/FT2) row — if the WSJT-X listener
  is active:

  1. Sends a Configure + Reply command to WSJT-X to point it at that station.
  2. If DX Lab Commander is enabled, simultaneously QSYs the radio to the
     standard FT8/FT4/FT2 dial frequency for the spot's band in ``DATA-U``
     mode.

  If the station is not currently visible in WSJT-X's band-activity window,
  a confirmation dialog asks whether to send the spot anyway.

* **Double click** on a CW or SSB row — if DX Lab Commander is enabled, QSYs
  the radio to the spot's frequency and sets the appropriate mode (``CW``,
  ``USB``, or ``LSB``).
* **Right click** on the QSL column cell — opens a context menu showing all
  confirmed and worked QSOs for that DXCC entity under the active award
  criterion.

The same double-click actions apply when clicking a spot line in the
**Band Map** (see `Band map`_ below).

WSJT-X integration
-------------------

When ``--wsjt`` is active, DX Spotter:

1. Joins the WSJT-X multicast group (``224.0.0.1``) so it receives its own
   independent copy of every UDP packet, even when RUMlogNG, GridTracker, or
   JTAlert are also bound to the same port.
2. Sends a Heartbeat (type 0) to WSJT-X every 15 seconds to keep the client
   registered.
3. Receives Decode (type 2) packets and forwards CQ spots to the spot table
   (subject to the decode filter and the 5-minute rate-limiting gate).
4. Tracks the most recent decode per callsign for use in double-click Reply,
   regardless of the rate-limiting gate.
5. On double-click: sends Configure (type 15) to set the DX call and Rx DF,
   then sends Reply (type 4) to simulate a band-activity double-click.

WSJT-X must have **Accept UDP requests** enabled in
Settings → Reporting for double-click reply to work.

The WSJT-X status indicator in the status bar uses three colors:

* **Green** — heartbeat received and decodes arriving within the last
  *N* minutes (see :option:`wsjt_no_spot_mins`).
* **Yellow** — heartbeat received but no decodes recently.  WSJT-X is
  running and connected but nothing is being heard on the band.
* **Red** — no heartbeat in the last 45 s; WSJT-X is likely not running
  or the UDP address/port is misconfigured.

DX Lab Commander rig control
-----------------------------

When **Commander** is enabled in the Rig Control section of the Settings
dialog, double-clicking a spot also commands the radio via DX Lab Suite's
Commander application:

* **Digital spots (FT8/FT4/FT2)** — radio is set to the standard dial
  frequency for the band/mode and switched to ``DATA-U`` (USB digital)
  mode.
* **CW spots** — radio is set to the spot frequency in ``CW`` mode.
* **SSB spots** — radio is set to the spot frequency in ``USB`` or
  ``LSB`` mode (automatic selection based on whether the frequency is
  above or below 10 MHz).

Commander must be running on the same machine (or reachable at the
configured host/port) for rig control to work.  If Commander is not
reachable, the double-click still sends the spot to WSJT-X for digital
modes but the radio is not moved.  See :doc:`configuration` for Commander
network settings.

Band map
--------

The band map is displayed in the right portion of the window alongside the
spot table.  It shows all visible spots for the current band as vertical
lines on a frequency-vs-SNR plot.

* **X-axis** — absolute frequency in kHz.
* **Y-axis** — SNR in dB (positive values extend upward; FT8 values are
  typically −10 to +10 dB).
* **Line color** — matches the mode: blue = FT8, light blue = FT4, cyan = FT2,
  green = CW, magenta = SSB.
* **Line width** — thin (1 px) for ``n/a`` award status; thick (3 px) when the
  spot has award value (new, worked, or confirmed).
* **Label** — callsign printed vertically at the top of each line.

Mode zoom buttons
~~~~~~~~~~~~~~~~~

Four buttons above the plot control the x-axis zoom:

* **All** — restores auto-range to show the full band.
* **CW** — zooms to the CW sub-band for the current band.
* **FT** — zooms to the FT8 sub-band (dial frequency ± ~4 kHz for 10 m and 15 m;
  wider on other bands to include FT4 and FT2 segments).
* **SSB** — zooms to the SSB segment.  Disabled on WARC bands (30 m, 17 m, 12 m)
  which have no SSB allocation in the band plan.

Double-clicking in the band map
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Double-clicking near a spot line fires the same routing logic as double-clicking
the corresponding row in the spot table: digital spots are sent to WSJT-X and
(if Commander is enabled) the radio is QSYed; CW/SSB spots QSY via Commander
only.  A 10-pixel click tolerance is applied so you do not need to hit the line
exactly.
