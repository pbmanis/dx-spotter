import re


class ADIFLog:
    """Parse an ADIF log file and answer worked/confirmed queries by (dxcc, band, mode)."""

    def __init__(self, filepath: str):
        self._worked:             set[tuple[int, str, str]]                        = set()
        self._worked_dxcc:        set[int]                                         = set()
        self._confirmed:          set[tuple[int, str, str]]                        = set()
        self._confirmed_dxcc:     set[int]                                         = set()
        self._confirmed_by_dxcc:  dict[int, dict[tuple[str, str], list[str]]]     = {}
        self._confirmed_details:  dict[tuple[int, str, str], list[dict[str, str]]] = {}
        self._load(filepath)

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

            key = (dxcc, band, mode)
            self._worked.add(key)
            self._worked_dxcc.add(dxcc)

            if rec.get('LOTW_QSL_RCVD', '').upper() == 'Y' or \
               rec.get('QSL_RCVD', '').upper() == 'Y':
                self._confirmed.add(key)
                self._confirmed_dxcc.add(dxcc)
                call = rec.get('CALL', '')
                self._confirmed_by_dxcc.setdefault(dxcc, {}).setdefault((band, mode), []).append(call)
                self._confirmed_details.setdefault(key, []).append({
                    'call': call,
                    'date': rec.get('QSO_DATE', ''),
                })

        print(f"ADIF: {len(self._worked)} QSOs loaded, "
              f"{len(self._confirmed)} confirmed across "
              f"{len(self._confirmed_dxcc)} DXCC entities")

    # -- query ----------------------------------------------------------------

    def ever_worked(self, dxcc: int) -> bool:
        """Return True if this DXCC entity has been worked on any band or mode."""
        return dxcc >= 0 and dxcc in self._worked_dxcc

    def ever_confirmed_on_band(self, dxcc: int, band: str) -> bool:
        """Return True if this DXCC has been confirmed on any mode of the given band."""
        band_up = band.upper()
        return any(b == band_up for (b, _) in self._confirmed_by_dxcc.get(dxcc, {}))

    def status(self, dxcc: int, band: str, mode: str) -> str:
        """
        Returns one of:
          'confirmed'       – confirmed on this exact band + mode
          'confirmed_other' – confirmed on some other band or mode
          'worked'          – QSO exists on this band + mode but not confirmed
          'new'             – never worked on this band + mode
        """
        if dxcc < 0:
            return 'new'
        key = (dxcc, band.upper(), mode.upper())
        if key in self._confirmed:
            return 'confirmed'
        if dxcc in self._confirmed_dxcc:
            return 'confirmed_other'
        if key in self._worked:
            return 'worked'
        return 'new'

    def confirmed_band_modes(self, dxcc: int) -> dict[tuple[str, str], list[str]]:
        """Return confirmed {(band, mode): [calls]} for this DXCC entity."""
        return self._confirmed_by_dxcc.get(dxcc, {})

    def confirmed_details(self, dxcc: int, band: str, mode: str) -> list[dict[str, str]]:
        """Return [{call, date}] for confirmed QSOs on this exact band+mode."""
        return self._confirmed_details.get((dxcc, band.upper(), mode.upper()), [])
