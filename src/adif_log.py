"""Parse ADIF log files and the RumLogNG SQLite database for DXCC award queries.

This module provides :class:`ADIFLog`, which loads a contact log from either a
standard ADIF export file or the RumLogNG CloudKit-backed SQLite database and
answers worked/confirmed queries keyed by ``(dxcc, band, mode)`` tuples.  The
database is always opened read-only; no records are ever written or modified.

Module-level constants
----------------------
_5BD_BANDS : frozenset[str]
    Upper-case band names that count towards the 5-Band DXCC award
    (80M / 40M / 20M / 15M / 10M).
_DIGITAL_MODES : frozenset[str]
    Upper-case mode strings treated as "digital" for the DXCC Digital award.
_CF_EPOCH : int
    Core Data / CloudKit reference date (1 Jan 2001 00:00:00 UTC) as a Unix
    timestamp.  RumLogNG stores contact timestamps as seconds since this epoch.
RUMLOGNG_DB_PATH : Path
    Default filesystem path to the RumLogNG CloudKit SQLite database inside its
    macOS app-sandbox container.
"""
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_5BD_BANDS  = frozenset({'80M', '40M', '20M', '15M', '10M'})
_WARC_BANDS = frozenset({'30M', '17M', '12M'})
_DIGITAL_MODES = frozenset({
    'FT8', 'FT4', 'FT2', 'JS8', 'JT65', 'JT9', 'PSK31', 'PSK63',
    'RTTY', 'WSPR', 'MSK144', 'OLIVIA', 'CONTESTIA', 'MFSK',
})

# Core Data / CloudKit reference date (Jan 1, 2001 00:00:00 UTC) as Unix timestamp.
_CF_EPOCH = 978307200

# Default path to RumLogNG's CloudKit-backed SQLite inside its app sandbox container.
RUMLOGNG_DB_PATH = (
    Path.home()
    / 'Library/Containers/de.dl2rum.RUMlogNG/Data'
    / 'Library/Application Support/RUMlogNG/CoreQsoModel_1.sqlite'
)


class ADIFLog:
    """Parse an ADIF log file (or RumLogNG SQLite) and answer worked/confirmed
    queries by (dxcc, band, mode).

    Two construction paths are available:

    * ``ADIFLog(filepath)`` — load from a plain-text ADIF export file.
    * :meth:`from_rumlogng` — load from the RumLogNG CloudKit SQLite database
      (macOS only; the file is opened read-only and never modified).

    Internally, QSOs are indexed in several overlapping data structures so that
    all award-status queries run in O(1) or O(k) time where *k* is the number
    of matching records for a given DXCC entity.

    Attributes
    ----------
    _total_qsos : int
        Total number of QSO records successfully parsed from the log source.
    _worked : set[tuple[int, str, str]]
        ``(dxcc, band, mode)`` keys for all worked (QSO in log) contacts.
    _worked_dxcc : set[int]
        DXCC entity numbers seen at least once in the worked set.
    _confirmed : set[tuple[int, str, str]]
        ``(dxcc, band, mode)`` keys for contacts with a QSL confirmed (LoTW or
        paper card).
    _confirmed_dxcc : set[int]
        DXCC entity numbers with at least one confirmed QSO on any band/mode.
    _confirmed_lotw_dxcc : set[int]
        DXCC entity numbers confirmed via LoTW.
    _confirmed_paper_dxcc : set[int]
        DXCC entity numbers confirmed via paper QSL card.
    _confirmed_by_dxcc : dict[int, dict[tuple[str, str], list[str]]]
        Maps ``dxcc → {(band, mode): [callsigns]}`` for confirmed QSOs.
    _confirmed_details : dict[tuple[int, str, str], list[dict[str, str]]]
        Maps ``(dxcc, band, mode)`` → list of ``{call, date}`` dicts for
        confirmed QSOs.
    _worked_details : dict[tuple[int, str, str], list[dict[str, str]]]
        Maps ``(dxcc, band, mode)`` → list of ``{call, date}`` dicts for
        worked-but-unconfirmed QSOs.
    _confirmed_bands : dict[int, set[str]]
        Maps ``dxcc → set of upper-case band strings`` with confirmed QSOs.
    _worked_bands : dict[int, set[str]]
        Maps ``dxcc → set of upper-case band strings`` with worked QSOs.
    _confirmed_modes : dict[int, set[str]]
        Maps ``dxcc → set of upper-case mode strings`` with confirmed QSOs.
    _worked_modes : dict[int, set[str]]
        Maps ``dxcc → set of upper-case mode strings`` with worked QSOs.
    """

    def __init__(self, filepath: str) -> None:
        """Load QSO records from an ADIF file.

        Parameters
        ----------
        filepath : str
            Path to an ADIF log export file (``*.adif`` or ``*.adi``).
            If the file cannot be read a warning is printed and the object is
            left in an empty state (zero QSOs).
        """
        self._total_qsos: int = 0
        self._worked: set[tuple[int, str, str]] = set()
        self._worked_dxcc: set[int] = set()
        self._confirmed: set[tuple[int, str, str]] = set()
        self._confirmed_dxcc: set[int] = set()
        self._confirmed_lotw_dxcc: set[int] = set()
        self._confirmed_paper_dxcc: set[int] = set()
        self._confirmed_by_dxcc: dict[int, dict[tuple[str, str], list[str]]] = {}
        self._confirmed_details: dict[tuple[int, str, str], list[dict[str, str]]] = {}
        self._worked_details: dict[tuple[int, str, str], list[dict[str, str]]] = {}
        self._confirmed_bands: dict[int, set[str]] = {}
        self._worked_bands: dict[int, set[str]] = {}
        self._confirmed_modes: dict[int, set[str]] = {}
        self._worked_modes: dict[int, set[str]] = {}
        self._load(filepath)

    @property
    def total_qsos(self) -> int:
        """Total number of QSO records successfully loaded from the log source."""
        return self._total_qsos

    @property
    def confirmed_dxcc_count(self) -> int:
        """Number of distinct DXCC entities with at least one confirmed QSO."""
        return len(self._confirmed_dxcc)

    @property
    def confirmed_lotw_dxcc_count(self) -> int:
        """Number of distinct DXCC entities confirmed via LoTW."""
        return len(self._confirmed_lotw_dxcc)

    @property
    def confirmed_paper_only_dxcc_count(self) -> int:
        """Number of distinct DXCC entities confirmed by paper QSL only (not LoTW)."""
        return len(self._confirmed_paper_dxcc - self._confirmed_lotw_dxcc)

    def confirmed_5bd_count(self, band: str) -> int:
        """Count distinct DXCC entities confirmed on a given band.

        Used by the spot table to detect when a 5BD band already has ≥ 100
        confirmed entities, at which point new spots for that band are
        coloured ``'over100'`` (cyan) instead of the standard ``'new'`` (red).

        Parameters
        ----------
        band : str
            Band string (case-insensitive, e.g. ``'20m'`` or ``'20M'``).

        Returns
        -------
        int
            Number of distinct DXCC entity numbers that have at least one
            confirmed QSO on this band.
        """
        band_up = band.upper()
        return sum(1 for bands in self._confirmed_bands.values() if band_up in bands)

    @classmethod
    def from_rumlogng(cls, db_path: str | Path = RUMLOGNG_DB_PATH) -> 'ADIFLog':
        """Create an :class:`ADIFLog` populated from the RumLogNG CloudKit SQLite database.

        The database is opened in read-only URI mode (``?mode=ro``) and is never
        written to or modified.  If the database file does not exist or cannot be
        opened, a warning is printed and an empty :class:`ADIFLog` is returned.

        Parameters
        ----------
        db_path : str or Path, optional
            Filesystem path to the RumLogNG ``CoreQsoModel_1.sqlite`` file.
            Defaults to :data:`RUMLOGNG_DB_PATH` (the standard macOS location
            inside the app-sandbox container).

        Returns
        -------
        ADIFLog
            Populated instance; may be empty if the database could not be read.
        """
        obj = cls.__new__(cls)
        obj._total_qsos = 0
        obj._worked = set()
        obj._worked_dxcc = set()
        obj._confirmed = set()
        obj._confirmed_dxcc = set()
        obj._confirmed_lotw_dxcc = set()
        obj._confirmed_paper_dxcc = set()
        obj._confirmed_by_dxcc = {}
        obj._confirmed_details = {}
        obj._worked_details = {}
        obj._confirmed_bands = {}
        obj._worked_bands = {}
        obj._confirmed_modes = {}
        obj._worked_modes = {}
        obj._load_rumlogng(Path(db_path))
        return obj

    # -- RumLogNG SQLite loader (read-only) -----------------------------------

    def _load_rumlogng(self, db_path: Path) -> None:
        if not db_path.exists():
            print(f"Warning: RumLogNG database not found: {db_path}")
            return
        try:
            # uri=True + ?mode=ro ensures the file is never written to
            uri = f"file:{db_path}?mode=ro"
            con = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as e:
            print(f"Warning: could not open RumLogNG database: {e}")
            return

        try:
            cur = con.execute(
                "SELECT ZCALLSIGN, ZBAND, ZMODE, ZDXCCADIF, ZQSL, ZLOTWQSL, ZDATETIME "
                "FROM ZCORE_QSO "
                "WHERE ZCALLSIGN IS NOT NULL AND ZBAND IS NOT NULL AND ZMODE IS NOT NULL"
            )
            rows = cur.fetchall()
        except sqlite3.Error as e:
            print(f"Warning: could not query RumLogNG database: {e}")
            con.close()
            return
        finally:
            con.close()

        for call, band_raw, mode_raw, dxcc_raw, qsl, lotw, cf_ts in rows:
            try:
                dxcc = int(dxcc_raw)
            except (TypeError, ValueError):
                continue
            band = (band_raw or '').upper()
            mode = (mode_raw or '').upper()
            if not band or not mode:
                continue

            try:
                unix_ts = float(cf_ts) + _CF_EPOCH
                date_str = datetime.fromtimestamp(unix_ts, tz=timezone.utc).strftime('%Y%m%d')
            except (TypeError, ValueError, OSError):
                date_str = ''

            key = (dxcc, band, mode)
            self._worked.add(key)
            self._worked_dxcc.add(dxcc)
            self._worked_bands.setdefault(dxcc, set()).add(band)
            self._worked_modes.setdefault(dxcc, set()).add(mode)

            # confirmed = LoTW confirmed (X) or paper QSL confirmed (X); both use 'X' in RumLogNG
            lotw_ok  = (lotw == 'X')
            paper_ok = (qsl  == 'X')
            confirmed = lotw_ok or paper_ok
            if confirmed:
                self._confirmed.add(key)
                self._confirmed_dxcc.add(dxcc)
                if lotw_ok:
                    self._confirmed_lotw_dxcc.add(dxcc)
                if paper_ok:
                    self._confirmed_paper_dxcc.add(dxcc)
                self._confirmed_bands.setdefault(dxcc, set()).add(band)
                self._confirmed_modes.setdefault(dxcc, set()).add(mode)
                self._confirmed_by_dxcc.setdefault(dxcc, {}).setdefault((band, mode), []).append(call)
                self._confirmed_details.setdefault(key, []).append(
                    {'call': call, 'date': date_str}
                )
            else:
                self._worked_details.setdefault(key, []).append(
                    {'call': call, 'date': date_str}
                )
            self._total_qsos += 1

        print(f"RumLogNG: {self._total_qsos} QSOs loaded, "
              f"{len(self._confirmed)} confirmed across "
              f"{len(self._confirmed_dxcc)} DXCC entities")

    # -- parsing --------------------------------------------------------------

    @staticmethod
    def _parse(content: str) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        current: dict[str, str] = {}
        tag_re = re.compile(r'<([^:>]+)(?::(\d+)(?::[^>]*)?)?>',  re.IGNORECASE)
        pos = 0
        while pos < len(content):
            m = tag_re.search(content, pos)
            if not m:
                break
            tag = m.group(1).upper()
            pos = m.end()
            if tag == 'EOR':
                if current:
                    records.append(current)
                current = {}
            elif tag != 'EOH' and m.group(2):
                length = int(m.group(2))
                current[tag] = content[pos:pos + length].strip()
                pos += length
        return records

    def _load(self, filepath: str) -> None:
        try:
            with open(filepath, encoding='utf-8', errors='replace') as f:
                content = f.read()
        except OSError as e:
            print(f"Warning: could not read ADIF file: {e}")
            return

        for rec in self._parse(content):
            dxcc_str = rec.get('DXCC', '')
            band     = rec.get('BAND', '').upper()
            mode     = rec.get('MODE', '').upper()
            if not dxcc_str or not band or not mode:
                continue
            try:
                dxcc = int(dxcc_str)
            except ValueError:
                continue

            call = rec.get('CALL', '')
            key  = (dxcc, band, mode)
            self._worked.add(key)
            self._worked_dxcc.add(dxcc)
            self._worked_bands.setdefault(dxcc, set()).add(band)
            self._worked_modes.setdefault(dxcc, set()).add(mode)

            lotw_ok  = rec.get('LOTW_QSL_RCVD', '').upper() == 'Y'
            paper_ok = rec.get('QSL_RCVD',      '').upper() == 'Y'
            if lotw_ok or paper_ok:
                self._confirmed.add(key)
                self._confirmed_dxcc.add(dxcc)
                if lotw_ok:
                    self._confirmed_lotw_dxcc.add(dxcc)
                if paper_ok:
                    self._confirmed_paper_dxcc.add(dxcc)
                self._confirmed_bands.setdefault(dxcc, set()).add(band)
                self._confirmed_modes.setdefault(dxcc, set()).add(mode)
                self._confirmed_by_dxcc.setdefault(dxcc, {}).setdefault((band, mode), []).append(call)
                self._confirmed_details.setdefault(key, []).append({
                    'call': call,
                    'date': rec.get('QSO_DATE', ''),
                })
            else:
                self._worked_details.setdefault(key, []).append({
                    'call': call,
                    'date': rec.get('QSO_DATE', ''),
                })
            self._total_qsos += 1

        print(f"ADIF: {self._total_qsos} QSOs loaded, "
              f"{len(self._confirmed)} confirmed across "
              f"{len(self._confirmed_dxcc)} DXCC entities")

    # -- award queries --------------------------------------------------------

    def award_status(self, dxcc: int, band: str, criterion: str) -> str:
        """Return the award status of a DXCC entity for the given criterion.

        Parameters
        ----------
        dxcc : int
            ADIF DXCC entity number.  Negative values indicate an unknown entity
            (callsign lookup failed) and always return ``'new'``.
        band : str
            Band string as used in the spot table (e.g. ``'20m'``).  Only
            relevant for ``'5bd'`` and ``'6m'`` criteria; ignored by the others.
        criterion : str
            Award criterion key — one of ``'5bd'``, ``'cw'``, ``'mixed'``,
            ``'digital'``, ``'ssb'``, or ``'6m'``.

        Returns
        -------
        str
            One of:

            * ``'confirmed'`` — at least one QSL-confirmed QSO satisfies the
              criterion.
            * ``'worked'``    — QSO in log but no confirmed QSL for this
              criterion.
            * ``'new'``       — entity never worked under this criterion.
            * ``'n/a'``       — the spot's band cannot contribute to the
              selected criterion (e.g. a 40 m spot against the ``'6m'``
              criterion).
        """
        if dxcc < 0:
            return 'new'
        band_up = band.upper()

        if criterion == '5bd':
            if band_up not in _5BD_BANDS:
                return 'new'
            if band_up in self._confirmed_bands.get(dxcc, set()):
                return 'confirmed'
            if band_up in self._worked_bands.get(dxcc, set()):
                return 'worked'

        elif criterion == 'warc':
            if band_up not in _WARC_BANDS:
                return 'n/a'
            if band_up in self._confirmed_bands.get(dxcc, set()):
                return 'confirmed'
            if band_up in self._worked_bands.get(dxcc, set()):
                return 'worked'

        elif criterion == '6m':
            if band_up != '6M':
                return 'n/a'
            if '6M' in self._confirmed_bands.get(dxcc, set()):
                return 'confirmed'
            if '6M' in self._worked_bands.get(dxcc, set()):
                return 'worked'

        elif criterion == 'cw':
            if 'CW' in self._confirmed_modes.get(dxcc, set()):
                return 'confirmed'
            if 'CW' in self._worked_modes.get(dxcc, set()):
                return 'worked'

        elif criterion == 'mixed':
            if dxcc in self._confirmed_dxcc:
                return 'confirmed'
            if dxcc in self._worked_dxcc:
                return 'worked'

        elif criterion == 'digital':
            if self._confirmed_modes.get(dxcc, set()) & _DIGITAL_MODES:
                return 'confirmed'
            if self._worked_modes.get(dxcc, set()) & _DIGITAL_MODES:
                return 'worked'

        elif criterion == 'ssb':
            if 'SSB' in self._confirmed_modes.get(dxcc, set()):
                return 'confirmed'
            if 'SSB' in self._worked_modes.get(dxcc, set()):
                return 'worked'

        return 'new'

    def criterion_qso_details(
        self, dxcc: int, band: str, criterion: str
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        """Return per-QSO details for a DXCC entity under a given award criterion.

        Performs an O(n) scan over the confirmed and worked detail dicts.  This
        is acceptable because the method is only called on a right-click context
        menu action, not on every incoming spot.

        Parameters
        ----------
        dxcc : int
            ADIF DXCC entity number.
        band : str
            Spot's band string (case-insensitive).  Used by :meth:`_matches_criterion`
            to filter ``'5bd'`` entries to the monitored band only.
        criterion : str
            Award criterion key (see :meth:`award_status`).

        Returns
        -------
        confirmed_list : list[dict[str, str]]
            QSOs for this entity that satisfy the criterion and have a confirmed
            QSL.  Each dict has keys ``call``, ``band``, ``mode``, ``date``.
        worked_list : list[dict[str, str]]
            QSOs for this entity that satisfy the criterion but have no confirmed
            QSL.  Same dict structure as ``confirmed_list``.
        """
        confirmed: list[dict[str, str]] = []
        worked:    list[dict[str, str]] = []
        band_up = band.upper()

        for (d, b, m), details in self._confirmed_details.items():
            if d != dxcc:
                continue
            if not self._matches_criterion(b, m, band_up, criterion):
                continue
            for entry in details:
                confirmed.append({'call': entry['call'], 'band': b,
                                  'mode': m, 'date': entry['date']})

        for (d, b, m), details in self._worked_details.items():
            if d != dxcc:
                continue
            if not self._matches_criterion(b, m, band_up, criterion):
                continue
            for entry in details:
                worked.append({'call': entry['call'], 'band': b,
                               'mode': m, 'date': entry['date']})

        return confirmed, worked

    @staticmethod
    def _matches_criterion(band: str, mode: str, monitored_band: str, criterion: str) -> bool:
        if criterion == '5bd':
            return band == monitored_band and band in _5BD_BANDS
        if criterion == 'warc':
            return band == monitored_band and band in _WARC_BANDS
        if criterion == '6m':
            return band == '6M'
        if criterion == 'cw':
            return mode == 'CW'
        if criterion == 'mixed':
            return True
        if criterion == 'digital':
            return mode in _DIGITAL_MODES
        if criterion == 'ssb':
            return mode == 'SSB'
        return False

    @staticmethod
    def mode_matches_criterion(mode: str, criterion: str) -> bool:
        """Return ``True`` if a spot with this mode is relevant for the award criterion.

        Used by the spot table to decide whether to colour a row with award
        status colours or dim it as ``'n/a'`` before calling
        :meth:`award_status`.

        Parameters
        ----------
        mode : str
            Mode string from the spot payload (case-insensitive, e.g. ``'FT8'``).
        criterion : str
            Award criterion key (see :meth:`award_status`).

        Returns
        -------
        bool
            ``False`` when the mode can never contribute to the criterion
            (e.g. ``'FT8'`` against ``'cw'``); ``True`` otherwise.  The
            ``'mixed'`` and ``'5bd'`` criteria accept any mode and always
            return ``True``.
        """
        m = mode.upper()
        if criterion == 'ssb':
            return m == 'SSB'
        if criterion == 'cw':
            return m == 'CW'
        if criterion == 'digital':
            return m in _DIGITAL_MODES
        return True  # 'mixed' and '5bd' accept any mode

    # -- legacy per-band/mode queries (kept for backward compat) --------------

    def status(self, dxcc: int, band: str, mode: str) -> str:
        """Return the QSO status for an exact ``(dxcc, band, mode)`` combination.

        This is a lower-level, criterion-agnostic query kept for backward
        compatibility.  Prefer :meth:`award_status` for new code.

        Parameters
        ----------
        dxcc : int
            ADIF DXCC entity number.  Negative values always return ``'new'``.
        band : str
            Band string (case-insensitive).
        mode : str
            Mode string (case-insensitive).

        Returns
        -------
        str
            One of:

            * ``'confirmed'``       — confirmed QSO on this exact band + mode.
            * ``'worked'``          — worked but unconfirmed on this band + mode.
            * ``'confirmed_other'`` — confirmed on a *different* band or mode.
            * ``'new'``             — never worked on any band/mode.
        """
        if dxcc < 0:
            return 'new'
        key = (dxcc, band.upper(), mode.upper())
        if key in self._confirmed:
            return 'confirmed'
        if key in self._worked:
            return 'worked'
        if dxcc in self._confirmed_dxcc:
            return 'confirmed_other'
        return 'new'

    def confirmed_band_modes(self, dxcc: int) -> dict[tuple[str, str], list[str]]:
        """Return a mapping of ``(band, mode)`` → ``[callsigns]`` for confirmed QSOs.

        Parameters
        ----------
        dxcc : int
            ADIF DXCC entity number.

        Returns
        -------
        dict[tuple[str, str], list[str]]
            Dict mapping ``(band, mode)`` tuples to lists of confirmed
            callsigns worked on that combination.  Returns an empty dict when
            no confirmed QSOs exist for this entity.
        """
        return self._confirmed_by_dxcc.get(dxcc, {})

    def confirmed_details(self, dxcc: int, band: str, mode: str) -> list[dict[str, str]]:
        """Return per-QSO detail records for confirmed contacts on an exact band/mode.

        Parameters
        ----------
        dxcc : int
            ADIF DXCC entity number.
        band : str
            Band string (case-insensitive).
        mode : str
            Mode string (case-insensitive).

        Returns
        -------
        list[dict[str, str]]
            List of ``{call, date}`` dicts; empty when no matching confirmed
            QSOs exist.
        """
        return self._confirmed_details.get((dxcc, band.upper(), mode.upper()), [])

    def worked_details(self, dxcc: int, band: str, mode: str) -> list[dict[str, str]]:
        """Return per-QSO detail records for worked-but-unconfirmed contacts.

        Only returns records when the ``(dxcc, band, mode)`` key is in the
        worked set but *not* in the confirmed set.

        Parameters
        ----------
        dxcc : int
            ADIF DXCC entity number.
        band : str
            Band string (case-insensitive).
        mode : str
            Mode string (case-insensitive).

        Returns
        -------
        list[dict[str, str]]
            List of ``{call, date}`` dicts; empty when the key is confirmed or
            not worked at all.
        """
        key = (dxcc, band.upper(), mode.upper())
        if key in self._worked and key not in self._confirmed:
            return self._worked_details.get(key, [])
        return []

    def ever_confirmed_on_band(self, dxcc: int, band: str) -> bool:
        """Return ``True`` if any QSO with this DXCC entity on this band is confirmed.

        Parameters
        ----------
        dxcc : int
            ADIF DXCC entity number.
        band : str
            Band string (case-insensitive).

        Returns
        -------
        bool
            ``True`` if at least one confirmed QSO exists for this entity on
            the given band, regardless of mode.
        """
        return band.upper() in self._confirmed_bands.get(dxcc, set())
