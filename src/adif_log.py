import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_5BD_BANDS = frozenset({'80M', '40M', '20M', '15M', '10M'})
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
    queries by (dxcc, band, mode).  The RumLogNG database is opened read-only
    and is never written to or modified."""

    def __init__(self, filepath: str):
        self._total_qsos:         int                                                 = 0
        self._worked:             set[tuple[int, str, str]]                         = set()
        self._worked_dxcc:        set[int]                                           = set()
        self._confirmed:          set[tuple[int, str, str]]                         = set()
        self._confirmed_dxcc:     set[int]                                           = set()
        self._confirmed_lotw_dxcc:  set[int]                                         = set()
        self._confirmed_paper_dxcc: set[int]                                         = set()
        self._confirmed_by_dxcc:  dict[int, dict[tuple[str, str], list[str]]]       = {}
        self._confirmed_details:  dict[tuple[int, str, str], list[dict[str, str]]]  = {}
        self._worked_details:     dict[tuple[int, str, str], list[dict[str, str]]]  = {}
        self._confirmed_bands:    dict[int, set[str]]                                = {}
        self._worked_bands:       dict[int, set[str]]                                = {}
        self._confirmed_modes:    dict[int, set[str]]                                = {}
        self._worked_modes:       dict[int, set[str]]                                = {}
        self._load(filepath)

    @property
    def total_qsos(self) -> int:
        return self._total_qsos

    @property
    def confirmed_dxcc_count(self) -> int:
        return len(self._confirmed_dxcc)

    @property
    def confirmed_lotw_dxcc_count(self) -> int:
        return len(self._confirmed_lotw_dxcc)

    @property
    def confirmed_paper_only_dxcc_count(self) -> int:
        return len(self._confirmed_paper_dxcc - self._confirmed_lotw_dxcc)

    @classmethod
    def from_rumlogng(cls, db_path: str | Path = RUMLOGNG_DB_PATH) -> 'ADIFLog':
        """Create an ADIFLog populated from the RumLogNG CloudKit SQLite database.
        The database is opened read-only and is never modified."""
        obj = cls.__new__(cls)
        obj._total_qsos         = 0
        obj._worked             = set()
        obj._worked_dxcc        = set()
        obj._confirmed            = set()
        obj._confirmed_dxcc       = set()
        obj._confirmed_lotw_dxcc  = set()
        obj._confirmed_paper_dxcc = set()
        obj._confirmed_by_dxcc    = {}
        obj._confirmed_details  = {}
        obj._worked_details     = {}
        obj._confirmed_bands    = {}
        obj._worked_bands       = {}
        obj._confirmed_modes    = {}
        obj._worked_modes       = {}
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
        """Return 'confirmed', 'worked', 'new', or 'n/a' for the given DXCC award criterion.

        criterion: '5bd' | 'cw' | 'mixed' | 'digital' | 'ssb' | '6m'
        band: spot's band string (e.g. '20m') — used for '5bd' and '6m'.
        Returns 'n/a' when the spot's band cannot contribute to the criterion.
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
        """Return (confirmed_list, worked_list) for the given criterion.

        Each entry: {call, band, mode, date}.
        Only called on right-click so O(n) scan is acceptable.
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
        """Return True if a spot with this mode is relevant for the award criterion."""
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
        """Return 'confirmed' | 'worked' | 'confirmed_other' | 'new' for this exact band+mode."""
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
        return self._confirmed_by_dxcc.get(dxcc, {})

    def confirmed_details(self, dxcc: int, band: str, mode: str) -> list[dict[str, str]]:
        return self._confirmed_details.get((dxcc, band.upper(), mode.upper()), [])

    def worked_details(self, dxcc: int, band: str, mode: str) -> list[dict[str, str]]:
        key = (dxcc, band.upper(), mode.upper())
        if key in self._worked and key not in self._confirmed:
            return self._worked_details.get(key, [])
        return []

    def ever_confirmed_on_band(self, dxcc: int, band: str) -> bool:
        return band.upper() in self._confirmed_bands.get(dxcc, set())
