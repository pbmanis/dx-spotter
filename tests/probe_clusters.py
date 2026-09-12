"""Live diagnostic: connect to the enabled DX clusters and separate parsing
failures from filtering.

This is **not** a pytest unit test — it opens real TCP connections to DX
cluster nodes and needs network access, so it is named ``probe_*`` to keep
pytest from collecting it.  Run it by hand when investigating why spots are or
are not appearing::

    python tests/probe_clusters.py

For each cluster it writes two files into a temporary directory (path printed
at the end):

* ``cluster<N>_raw.log``    — every text line received (after IAC stripping)
* ``cluster<N>_parsed.log`` — the spot dicts ``TelnetCluster._process_line`` emitted

The final summary reports, per cluster, how many lines arrived, how many
parsed into spots, and how many were left unparsed (login banners and status
lines count as unparsed — that is expected).
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

# Import the app modules from the sibling ``src`` directory.
SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from telnet_cluster import TelnetCluster  # noqa: E402

OUT = Path(tempfile.mkdtemp(prefix="dxspotter_probe_"))
DURATION = 45.0

# (index, host, port, callsign) for the clusters to probe.  Edit as needed to
# match the nodes you want to investigate.
CLUSTERS: list[tuple[int, str, int, str]] = [
    (1, "dxc.w3lpl.net", 7373, "NC3G"),
    (2, "telnet.reversebeacon.net", 7000, "NC3G"),
    (4, "K1RFI.com", 7300, "NC3G"),
]


class ProbeCluster(TelnetCluster):
    """TelnetCluster that also logs every raw line and each parse outcome."""

    def __init__(self, index: int, host: str, port: int, callsign: str) -> None:
        super().__init__(host, port, callsign, self._record_spot, index=index)
        self.raw_path = OUT / f"cluster{index}_raw.log"
        self.parsed_path = OUT / f"cluster{index}_parsed.log"
        self._raw = open(self.raw_path, "w")
        self._parsed = open(self.parsed_path, "w")
        self.n_raw = 0
        self.n_parsed = 0
        self.n_unrecognized = 0

    def _record_spot(self, spot: dict) -> None:
        self.n_parsed += 1
        self._parsed.write(repr(spot) + "\n")
        self._parsed.flush()

    def _process_line(self, line: str, host_index) -> None:
        self.n_raw += 1
        self._raw.write(line + "\n")
        self._raw.flush()
        before = self.n_parsed
        super()._process_line(line, host_index)
        if self.n_parsed == before:
            self.n_unrecognized += 1

    def close(self) -> None:
        self._raw.close()
        self._parsed.close()


def main() -> None:
    probes = [ProbeCluster(i, h, p, c) for (i, h, p, c) in CLUSTERS]
    for pr in probes:
        pr.start()
    time.sleep(DURATION)
    for pr in probes:
        pr.stop()
    time.sleep(2.0)
    print("\n==== SUMMARY ====")
    for pr in probes:
        print(
            f"cluster{pr._index} {pr._host}:{pr._port}  "
            f"lines={pr.n_raw}  parsed_spots={pr.n_parsed}  "
            f"unparsed_lines={pr.n_unrecognized}"
        )
        pr.close()
    print(f"\nRaw and parsed logs written to: {OUT}")


if __name__ == "__main__":
    main()
