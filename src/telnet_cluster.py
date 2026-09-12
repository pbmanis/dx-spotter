"""Telnet DX cluster client for DX Spotter.

Connects to a DX cluster node via TCP, logs in with the operator's callsign,
and forwards parsed DX spot lines to a caller-supplied callback.

Three spot line formats are recognized:

Standard (AR-Cluster, DXSpider)::

    DX de <spotter>:    <freq_kHz>  <dx_call>    <comment>

e.g.::

    DX de W9XYZ:     14074.0  VK2ABC       FT8 -10 dB 2314Z JO22

Alternate (used by some nodes)::

    <freq_kHz>  <dx_call>   <DD-Mon-YYYY>  <HHMMZ>   <comment>  <<spotter>>

e.g.::

    14292.0  EA3KT       01-Jul-2026 0221Z                            EA <N5VBP>

Self-spot / activation announcement (seen on WWFF/POTA self-spots; missing
the colon after the spotter call)::

    DX de <spotter>     <freq_kHz>  <dx_call>    <comment>

e.g.::

    DX de JI1IZS/     3500.0  JI1IZS/2     WWFF JAFF-0262    1201Z

When no SNR is present in the comment, ``rp`` is set to ``'0'``.
Mode is extracted from the comment first; if not found, it is inferred from
the frequency using IARU band plans.  Trailing control characters (e.g. the
terminal-bell ``\\x07`` some nodes append to alert spots) are stripped from
the comment.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from typing import Callable

# Standard format: "DX de <spotter>: <freq> <dx_call> <comment>"
# Groups: (spotter_call, freq_kHz, dx_call, comment)
_SPOT_RE = re.compile(
    r"^DX\s+de\s+([A-Z0-9/\-#]+)\s*:\s+([\d.]+)\s+([A-Z0-9/]+)\s*(.*)",
    re.IGNORECASE,
)

# Alternate format: "<freq>  <dx_call>  <DD-Mon-YYYY>  <HHMMZ>  <comment>  <<spotter>>"
# Groups: (freq_kHz, dx_call, comment, spotter_call)
_SPOT_RE_ALT = re.compile(
    r"^\s*([\d.]+)\s+([A-Z0-9/]+)\s+"
    r"\d{2}-[A-Za-z]{3}-\d{4}\s+\d{4}[Zz]\s*(.*?)\s*<([A-Z0-9/\-#]+)>\s*$",
    re.IGNORECASE,
)

# WB5VZL format (RBN):
# DX de OE3KLU-#: 28187.90  OE3XAC         CW     5 dB  14 WPM  BEACON  0032Z
# Groups: (spotter_call, freq_kHz, dx_call) followed by mode, snr, speed, cq, time comment)
_SPOT_RE_RBN = re.compile(
    r"^DX\s+de\s+([A-Z0-9/\-#]+)\s*:\s+([\d.]+)\s+([A-Z0-9/]+)\s*(.*)",
    re.IGNORECASE,
)

# Self-spot / activation announcement format: same layout as the standard
# format but missing the colon after the spotter call.
# DX de JI1IZS/     3500.0  JI1IZS/2     WWFF JAFF-0262    1201Z
# Groups: (spotter_call, freq_kHz, dx_call, comment)
# Tried only after the colon-requiring formats above, since a colon right
# after the spotter call already prevents this pattern from matching.
_SPOT_RE_NO_COLON = re.compile(
    r"^DX\s+de\s+([A-Z0-9/\-#]+)\s+([\d.]+)\s+([A-Z0-9/]+)\s*(.*)",
    re.IGNORECASE,
)

# Matches SNR values like "-10 dB", "+5dB", "-8 dB" in comments
_SNR_RE = re.compile(r"([+-]?\d+)\s*dB", re.IGNORECASE)

# A line "looks like a spot" if it starts with "DX de" (standard/RBN/no-colon
# formats) or with a bare frequency (alternate format).  Only such lines are
# logged when they fail to parse; login banners, help text, status lines, and
# command prompts are silently ignored to avoid flooding the console.
_LOOKS_LIKE_SPOT_RE = re.compile(r"^\s*(?:DX\s+de\b|\d{3,}\.\d)", re.IGNORECASE)

# Mode keywords searched in the spot comment (in priority order)
_MODE_KEYWORDS: tuple[str, ...] = (
    "FT8",
    "FT4",
    "FT2",
    "CW",
    "SSB",
    "AM",
    "FM",
    "RTTY",
    "PSK",
    "JS8",
)

# FT8/FT4/FT2 channel centres (kHz) → mode.
# A spot within ±1 kHz of a centre is assigned the corresponding mode.
_FT_FREQS: tuple[tuple[float, str], ...] = (
    # FT8
    (1840.0, "FT8"),
    (3573.0, "FT8"),
    (7074.0, "FT8"),
    (10136.0, "FT8"),
    (14074.0, "FT8"),
    (18100.0, "FT8"),
    (21074.0, "FT8"),
    (24915.0, "FT8"),
    (28074.0, "FT8"),
    (50313.0, "FT8"),
    (144174.0, "FT8"),
    # FT4
    (3575.0, "FT4"),
    (7047.5, "FT4"),
    (10140.0, "FT4"),
    (14080.0, "FT4"),
    (18104.0, "FT4"),
    (21140.0, "FT4"),
    (24919.0, "FT4"),
    (28180.0, "FT4"),
    # FT2
    (3578.0, "FT2"),
    (7062.0, "FT2"),
    (10144.0, "FT2"),
    (14084.0, "FT2"),
    (21144.0, "FT2"),
    (28184.0, "FT2"),
    (50316.0, "FT2"),
    (144177.0, "FT2"),
)

# CW sub-band segments (kHz) per IARU Region 2 band plan
_CW_SEGMENTS: tuple[tuple[float, float], ...] = (
    (1800.0, 1840.0),
    (3500.0, 3570.0),
    (7000.0, 7040.0),
    (10100.0, 10130.0),
    (14000.0, 14070.0),
    (18068.0, 18095.0),
    (21000.0, 21070.0),
    (24890.0, 24915.0),
    (28000.0, 28070.0),
    (50000.0, 50100.0),
    (144000.0, 144035.0),
)

# SSB sub-band segments (kHz) per IARU Region 2 band plan
_SSB_SEGMENTS: tuple[tuple[float, float], ...] = (
    (1843.0, 2000.0),
    (3700.0, 4000.0),
    (7100.0, 7300.0),
    (14100.0, 14350.0),
    (18110.0, 18168.0),
    (21100.0, 21450.0),
    (24930.0, 24990.0),
    (28300.0, 29700.0),
    (50100.0, 54000.0),
    (144200.0, 148000.0),
)

# Type alias for the spot callback
SpotCallback = Callable[[dict], None]


def _infer_mode_from_freq(freq_khz: float) -> str:
    # Return likely mode at freq_khz: FT8/4/2 by channel, CW or SSB by sub-band.
    for centre, mode in _FT_FREQS:
        if abs(freq_khz - centre) <= 1.0:
            return mode
    for lo, hi in _CW_SEGMENTS:
        if lo <= freq_khz <= hi:
            return "CW"
    for lo, hi in _SSB_SEGMENTS:
        if lo <= freq_khz <= hi:
            return "SSB"
    return "CW"  # fallback for unrecognized sub-bands


def _strip_iac(data: bytes) -> bytes:
    # Remove telnet IAC option negotiation sequences from raw socket bytes.
    result = bytearray()
    i = 0
    while i < len(data):
        if data[i] == 0xFF:  # IAC
            if i + 1 < len(data):
                if data[i + 1] == 0xFF:  # escaped literal 0xFF
                    result.append(0xFF)
                    i += 2
                elif data[i + 1] in (0xFB, 0xFC, 0xFD, 0xFE):  # 3-byte option
                    i += 3
                else:
                    i += 2
            else:
                i += 1
        else:
            result.append(data[i])
            i += 1
    return bytes(result)


class TelnetCluster(threading.Thread):
    """Background thread connecting to a DX cluster node via telnet.

    After connecting, the operator's callsign is sent as a login credential.
    Incoming lines are parsed for the standard DX cluster spot format, and
    matching spots are forwarded to the :class:`~telnet_cluster.SpotCallback`
    supplied at construction.

    The thread reconnects automatically on connection loss using exponential
    back-off up to 60 seconds between attempts.

    Parameters
    ----------
    host : str
        Hostname or IP address of the DX cluster node.
    port : int
        TCP port of the cluster (typical values: ``7300``, ``23``).
    callsign : str
        Operator callsign sent to the cluster as a login credential.
    on_spot : SpotCallback
        Callable invoked for each parsed spot from the background thread.
        Receives a dict with keys ``call``, ``rc``, ``rl``, ``abs_freq_hz``,
        ``rp``, ``md``, ``unix_time``, and ``comment``.  Must be thread-safe
        — implementations should use Qt signals, not direct widget updates.
    index : int, optional
        Connection index (1 or 2); used in log messages.
    command : str, optional
        Command sent to the node immediately after login (e.g. ``'sh/dx/50'``
        to prime the table with recent spots).  Empty (default) sends nothing.
    """

    def __init__(
        self,
        host: str,
        port: int,
        callsign: str,
        on_spot: SpotCallback,
        index: int = 1,
        command: str = "",
    ) -> None:
        """Initialize the cluster thread without starting it.

        Parameters
        ----------
        host : str
            DX cluster hostname or IP address.
        port : int
            TCP port.
        callsign : str
            Operator callsign (case-insensitive; stored upper-case).
        on_spot : SpotCallback
            Callback invoked for each parsed DX spot dict.
        index : int, optional
            Connection index (1 or 2).
        command : str, optional
            Command sent after login; empty sends nothing.
        """
        super().__init__(daemon=True, name=f"telnet-cluster-{index}")
        self._host = host
        self._port = port
        self._callsign = callsign.upper()
        self._on_spot = on_spot
        self._index = index
        self._command = command.strip()
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._connected: bool = False

    @property
    def connected(self) -> bool:
        """``True`` when a TCP session with the cluster is currently active."""
        return self._connected

    def stop(self) -> None:
        """Signal the thread to stop and disconnect immediately.

        Sets the stop flag and closes the active socket so any blocked
        ``recv()`` call raises :class:`OSError` and the thread exits without
        waiting for the 120-second receive timeout.
        """
        self._stop_event.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def run(self) -> None:
        """Main loop: connect, receive spots, reconnect on failure."""
        delay = 5.0
        while not self._stop_event.is_set():
            try:
                self._connect_and_run()
                delay = 5.0
            except OSError as exc:
                if not self._stop_event.is_set():
                    print(
                        f"Telnet cluster {self._index} "
                        f"({self._host}:{self._port}): {exc}"
                    )
            except Exception as exc:
                if not self._stop_event.is_set():
                    print(f"Telnet cluster {self._index}: unexpected error: {exc}")
            if not self._stop_event.is_set():
                self._stop_event.wait(delay)
                delay = min(delay * 2, 60.0)

    def _connect_and_run(self) -> None:
        # Open TCP connection, log in with callsign, and stream spot lines.
        print(
            f"Telnet cluster {self._index}: " f"connecting to {self._host}:{self._port}"
        )
        with socket.create_connection((self._host, self._port), timeout=15.0) as sock:
            self._sock = sock
            try:
                print(
                    f"Telnet cluster {self._index}: connected, "
                    f"logging in as {self._callsign}"
                )
                self._connected = True
                sock.settimeout(120.0)
                sock.sendall((self._callsign + "\r\n").encode("ascii"))
                if self._command:
                    # Give the node a moment to process the login before the
                    # priming command (e.g. sh/dx) so it is not swallowed.
                    self._stop_event.wait(2.0)
                    if not self._stop_event.is_set():
                        print(
                            f"Telnet cluster {self._index}: "
                            f"sending command {self._command!r}"
                        )
                        sock.sendall((self._command + "\r\n").encode("ascii"))
                buf = b""
                while not self._stop_event.is_set():
                    try:
                        chunk = sock.recv(4096)
                    except socket.timeout:
                        # Send keepalive to detect stale connections
                        try:
                            sock.sendall(b"\r\n")
                        except OSError:
                            break
                        continue
                    if not chunk:
                        print(
                            f"Telnet cluster {self._index}: "
                            f"connection closed by server"
                        )
                        break
                    buf += _strip_iac(chunk)
                    while b"\n" in buf:
                        line_bytes, buf = buf.split(b"\n", 1)
                        line = line_bytes.decode("ascii", errors="replace").rstrip("\r")
                        if line:
                            self._process_line(line, host_index=self._index)
            finally:
                self._connected = False
                self._sock = None

    def _process_line(self, line: str, host_index: str) -> None:
        """Parse one text line; invoke on_spot if it matches either spot format.
         wb5vzl format:
         DX de OE3KLU-#: 28187.90  OE3XAC         CW     5 dB  14 WPM  BEACON  0032Z

         Parameters:
        ----------
        line : str
            One line of text received from the cluster.
        host : str
            Hostname or IP address of the cluster (for logging).

        """

        def _spot_basic(line: str) -> tuple[bool, str, str, str, str] | None:
            m = _SPOT_RE.match(line)
            if m:
                spotter = m.group(1).upper()
                freq_str = m.group(2)
                dx_call = m.group(3).upper().replace(".", "/")
                comment = (m.group(4) or "").strip()
                return True, spotter, freq_str, dx_call, comment
            else:
                return False, "", "", "", ""

        def _spot_alt(line: str) -> tuple[bool, str, str, str, str] | None:
            m = _SPOT_RE_ALT.match(line)
            if m:
                freq_str = m.group(1)
                dx_call = m.group(2).upper().replace(".", "/")
                comment = (m.group(3) or "").strip()
                spotter = m.group(4).upper()
                return True, spotter, freq_str, dx_call, comment
            else:
                return False, "", "", "", ""

        def _spot_rbn(line: str) -> tuple[bool, str, str, str, str] | None:
            m = _SPOT_RE_RBN.match(line)
            if m:
                spotter = m.group(1).upper()
                freq_str = m.group(2)
                dx_call = m.group(3).upper().replace(".", "/")
                comment = (m.group(4) or "").strip()
                return True, spotter, freq_str, dx_call, comment
            else:
                return False, "", "", "", ""

        def _spot_no_colon(line: str) -> tuple[bool, str, str, str, str] | None:
            m = _SPOT_RE_NO_COLON.match(line)
            if m:
                spotter = m.group(1).upper()
                freq_str = m.group(2)
                dx_call = m.group(3).upper().replace(".", "/")
                comment = (m.group(4) or "").strip()
                return True, spotter, freq_str, dx_call, comment
            else:
                return False, "", "", "", ""

        parsed, spotter, freq_str, dx_call, comment = False, "", "", "", ""
        for spot_read in [_spot_basic, _spot_alt, _spot_rbn, _spot_no_colon]:
            parsed, spotter, freq_str, dx_call, comment = spot_read(line)
            if parsed:
                break
        if not parsed:
            # Only warn about lines that look like spots but failed to parse;
            # banners, help text, status lines, and prompts are ignored.
            if _LOOKS_LIKE_SPOT_RE.match(line):
                print(f"Telnet cluster {host_index}: unrecognized line: {line!r}")
            return

        # Strip trailing control characters some nodes append (e.g. terminal-bell
        # '\x07' on alert spots) so they don't leak into the displayed comment.
        comment = re.sub(r"[\x00-\x1f\x7f]+", "", comment).strip()

        try:
            freq_khz = float(freq_str)
        except ValueError:
            return

        snr_m = _SNR_RE.search(comment)
        rp = snr_m.group(1) if snr_m else "0"

        # Mode: keyword from comment first, then frequency-based inference
        mode = ""
        comment_upper = comment.upper()
        for kw in _MODE_KEYWORDS:
            if kw in comment_upper:
                mode = kw
                break
        if not mode:
            mode = _infer_mode_from_freq(freq_khz)
        # if host_index == 2:
        #     print(
        #         f"Telnet cluster {host_index}: "
        #         f"{freq_khz:8.1f} kHz {dx_call:12s} {spotter:12s} {mode:3s} {rp} dB {comment}"
        #     )
        self._on_spot(
            {
                "call": dx_call,
                "rc": spotter,
                "rl": "",
                "abs_freq_hz": int(freq_khz * 1000),
                "rp": rp,
                "md": mode,
                "unix_time": time.time(),
                "comment": comment,
                "host_index": host_index,
            }
        )
