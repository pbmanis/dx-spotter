Installation and Usage
======================

Requirements
------------

* Python 3.11 or later
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
   * **Award Criteria** radio group — selects which DXCC award colours the
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
   and colour coding.

Status bar
~~~~~~~~~~

The bottom status bar contains three fields:

* **Left** — log source summary: file name, total QSOs, and confirmed DXCC
  count (LoTW + paper).
* **Centre** — ``PSKR: connected`` / ``PSKR: connecting…`` (green / grey).
* **Right** — ``WSJT-X: connected`` / ``WSJT-X: waiting…`` /
  ``WSJT-X: no signal (Xs)`` / ``WSJT-X: disabled``.

Interacting with spots
~~~~~~~~~~~~~~~~~~~~~~

* **Single click** on a row — bolds the row and all other rows for the same
  callsign; click elsewhere to deselect.
* **Double click** on a row — if the spot is an FT8/FT4/FT2 mode spot and
  the WSJT-X listener is active, sends a Configure + Reply command to WSJT-X
  to point it at that station.
* **Right click** on the QSL column cell — opens a context menu showing all
  confirmed and worked QSOs for that DXCC entity under the active award
  criterion.

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
4. On double-click: sends Configure (type 15) to set the DX call and Rx DF,
   then sends Reply (type 4) to simulate a band-activity double-click.

WSJT-X must have **Accept UDP requests** enabled in
Settings → Reporting for double-click reply to work.
