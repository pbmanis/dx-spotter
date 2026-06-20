Spot Table: Columns, Colours, and Filters
==========================================

Spot table columns
------------------

The spot table has the following columns (left to right):

.. list-table::
   :header-rows: 1
   :widths: 12 88

   * - Column
     - Description
   * - **DX Call**
     - Callsign of the DX station being spotted.  For a PSK Reporter spot where
       your own callsign was heard (TX direction), this shows the callsign of
       the *reporting* station instead.
   * - **Dir**
     - Direction: ``RX`` = DX station heard by someone else; ``TX`` = your
       callsign was heard by the reporting station (PSK Reporter only).
   * - **Country**
     - Country or territory name resolved from the DX callsign via the
       pyhamtools cty.plist database.
   * - **DX Grid**
     - Maidenhead grid square of the DX station (up to 6 characters, e.g.
       ``FN31pr``).  Blank when unknown.
   * - **Time**
     - UTC timestamp when the spot was received, formatted
       ``YYYY-MM-DD HH:MM:SS``.
   * - **Age**
     - Time elapsed since the spot was first received, displayed as
       ``Xs``, ``Xm YYs``, or ``Xh YYm``.  Updated every 15 seconds.
       Rows older than **Max Spot Age** are automatically removed.
   * - **dHz**
     - Audio frequency offset (DF) of the signal in Hz, relative to the
       standard dial frequency for the band and mode.  This is the position
       of the signal in the WSJT-X waterfall / audio passband.
   * - **Dist**
     - Path distance in km between the DX station (sender) and the PSK
       Reporter reporting station.  For WSJT-X spots this is the distance
       from your grid to the DX station.
   * - **Mode**
     - Operating mode: ``FT8``, ``FT4``, ``FT2``, ``CW``, ``SSB``, etc.
   * - **SNR**
     - Signal-to-noise ratio in dB as reported by the receiving station.
       Negative values are common for FT8/FT4 (e.g. ``-10 dB``).
   * - **Band**
     - Amateur band (e.g. ``20m``, ``40m``).
   * - **Src**
     - Spot source: ``psk`` = PSK Reporter; ``wsjt`` = WSJT-X local decode
       (shown in italic font).
   * - **Reporter**
     - Callsign of the PSK Reporter station that heard the DX station.  For
       WSJT-X spots this is your own callsign (or ``WSJT-X`` if no callsign
       was configured).
   * - **Rptr Grid**
     - Maidenhead grid square of the reporting station (up to 6 characters).
   * - **Range**
     - Distance in km from *your* grid square (``my_grid``) to the PSK
       Reporter reporting station.  For WSJT-X spots this is ``0`` (the
       range filter does not apply to locally decoded signals).
   * - **QSL**
     - Award status cell — see `Award colour scheme`_ below.

Award colour scheme
-------------------

Every row is coloured according to the active **Award Criterion** (selected in
the left panel) and the DXCC award status of the spotted entity.

.. list-table::
   :header-rows: 1
   :widths: 15 20 65

   * - Colour
     - Status
     - Meaning
   * - Dark red background, white text
     - **New**
     - Entity has never been worked under this criterion.  Worth pursuing.
   * - Dark orange background, white text
     - **Worked**
     - Entity has been worked (QSO in log) but not yet confirmed with a QSL
       (LoTW or paper card).
   * - Dark grey background, light grey text
     - **Confirmed**
     - Entity is already confirmed for this criterion.  No action needed.
   * - Same grey background, dim text
     - **n/a**
     - The spot's mode cannot contribute to the active criterion (e.g. an
       FT8 spot against the CW criterion).  Row is de-emphasised.
   * - Dark teal background, cyan text
     - **Over 100** (5BD only)
     - The band already has ≥ 100 confirmed DXCC entities, so the 5-Band DXCC
       award requirement for this band is already met.  The spot is still
       shown but de-prioritised relative to bands with fewer than 100.

The **QSL column** also displays a brief text label:

* ``New [Mix]`` — never worked, Mixed criterion.
* ``Wkd [CW]: W1AW 20m/CW 2023-04-01`` — worked but unconfirmed; shows the
  most recent matching QSO.  ``(n)`` appears when there are multiple QSOs.
* ``Conf [Mix]: W1AW 20m/FT8 2023-01-15`` — confirmed; shows the most recent
  confirmed QSO.
* ``New &  band>100 [5BD/20m]`` — 5BD over-100 status.
* ``— [6M]`` — n/a (e.g. a non-6M spot against the 6M criterion).

Right-click context menu
~~~~~~~~~~~~~~~~~~~~~~~~

Right-clicking on the **QSL column cell** of any row opens a context menu
listing all confirmed and worked QSOs for that DXCC entity under the active
criterion.  Entries are sorted by band (low to high) then by mode.

Award criteria
--------------

The award criterion is selected with the **Award Criteria** radio buttons in
the left panel.

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Criterion
     - Description
   * - **5 Band DXCC**
     - Requires 100 confirmed entities on each of the five classic HF bands:
       80 m, 40 m, 20 m, 15 m, 10 m.  The colour is per-band: a spot on 20 m
       is compared only against 20 m confirmed/worked entities.  Spots on
       other bands (e.g. 17 m) show as **New** since they do not contribute.
       When a band reaches ≥ 100 confirmed entities the ``over100`` (cyan)
       colour is used.
   * - **DXCC CW**
     - Considers only CW QSOs.  FT8 / SSB spots are marked **n/a**.
   * - **DXCC Mixed**
     - Considers QSOs on any mode and any band (default criterion).
   * - **DXCC Digital**
     - Considers QSOs using digital modes: FT8, FT4, FT2, JS8, JT65, JT9,
       PSK31, PSK63, RTTY, WSPR, MSK144, OLIVIA, CONTESTIA, MFSK.
   * - **DXCC SSB**
     - Considers only SSB QSOs.
   * - **DXCC 6M**
     - Considers only 6 m QSOs.  Spots on other bands are marked **n/a**.

Display filters
---------------

The **Display Filter** radio group controls which rows are visible.

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Filter
     - Effect
   * - **All**
     - No rows are hidden (default).
   * - **DXCC only (no US, Canada)**
     - Hides rows for DXCC entity 291 (mainland United States) and entity 1
       (Canada), leaving only non-North-American DX spots visible.
   * - **Unconfirmed or New**
     - Hides rows for entities that are already **confirmed** under the active
       award criterion.  Shows only **New** and **Worked** spots — i.e., spots
       that still have award value.

Row font styles
---------------

* **Normal (upright)** — PSK Reporter spot, not selected.
* **Italic** — WSJT-X locally decoded spot.
* **Bold** — currently selected callsign (single-click to select).
* **Bold italic** — selected WSJT-X spot.
* **Dimmed colours** — the spotted station has entered a QSO (moved from CQ
  to working another station); detected from WSJT-X decode traffic.

Spot deduplication and update
------------------------------

Incoming spots are batched for 250 ms and then flushed to the table.  Within
each batch, only the **most recent** spot per callsign is kept: if the same
station is heard multiple times in a 250 ms window, earlier spots are
discarded.

When a callsign already has a row in the table, the old row is **removed**
and a new row is inserted at the **top** of the table with the updated
information.  This keeps the most recently active stations at the top.

Spot expiry
-----------

Rows older than **Max Spot Age** (configurable in the Data Filters panel,
default 30 minutes) are automatically removed during the 15-second age-update
pass.  Setting Max Spot Age to ``0`` disables expiry.
