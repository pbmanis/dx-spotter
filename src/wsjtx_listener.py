import re
import socket
import struct
import threading
import time
from typing import Callable


_CALL_RE = re.compile(r'^[A-Z0-9]{1,3}\d[A-Z0-9]{0,3}[A-Z](?:/[A-Z0-9]+)?$')
_GRID_RE = re.compile(r'^[A-R]{2}\d{2}([a-x]{2})?$', re.IGNORECASE)


def _looks_like_call(s: str) -> bool:
    return bool(_CALL_RE.match(s.upper()))


def _looks_like_grid(s: str) -> bool:
    return bool(_GRID_RE.match(s))


def freq_to_band(freq_hz: int) -> str:
    MHz = freq_hz / 1_000_000
    for low, high, band in [
        (1.8,    2.0,    "160m"), (3.5,   4.0,   "80m"),  (5.3,    5.4,   "60m"),
        (7.0,    7.3,    "40m"),  (10.1,  10.15, "30m"),  (14.0,   14.35, "20m"),
        (18.068, 18.168, "17m"),  (21.0,  21.45, "15m"),  (24.89,  24.99, "12m"),
        (28.0,   29.7,   "10m"),  (50.0,  54.0,  "6m"),   (144.0,  148.0, "2m"),
    ]:
        if low <= MHz <= high:
            return band
    return f"{MHz:.3f}MHz"


class _BinReader:
    """Simple big-endian binary reader for Qt/WSJT-X UDP packets."""

    def __init__(self, data: bytes):
        self._d = data
        self._p = 0

    def uint8(self) -> int:
        v = self._d[self._p]; self._p += 1; return v

    def uint32(self) -> int:
        (v,) = struct.unpack_from('>I', self._d, self._p); self._p += 4; return v

    def int32(self) -> int:
        (v,) = struct.unpack_from('>i', self._d, self._p); self._p += 4; return v

    def uint64(self) -> int:
        (v,) = struct.unpack_from('>Q', self._d, self._p); self._p += 8; return v

    def float64(self) -> float:
        (v,) = struct.unpack_from('>d', self._d, self._p); self._p += 8; return v

    def bool_(self) -> bool:
        v = self._d[self._p] != 0; self._p += 1; return v

    def utf8(self) -> str:
        n = self.uint32()
        if n in (0xFFFFFFFF, 0):
            return ''
        v = self._d[self._p:self._p + n].decode('utf-8', errors='replace')
        self._p += n
        return v


# Signature: (dx_call, dx_grid, snr, df_hz, mode, band, unix_time) -> None
SpotCallback = Callable[[str, str, int, int, str, str, float], None]


class WsjtxListener:
    """UDP listener for the WSJT-X network protocol (big-endian Qt serialization)."""

    _MAGIC            = 0xADBCCBDA
    _MSG_STATUS       = 1
    _MSG_DECODE       = 2
    _RESHOW_SECS      = 300  # re-show a call after 5 minutes of silence

    def __init__(self, port: int, decode_filter: str, my_call: str | None,
                 on_spot: SpotCallback):
        self.port          = port
        self.decode_filter = decode_filter.upper()  # 'CQ', 'ALL', 'ME'
        self.my_call       = my_call.upper() if my_call else None
        self.on_spot       = on_spot
        self._dial_freq    = 0
        self._wsjt_mode    = ''
        self._de_grid      = ''
        self._call_times: dict[str, float] = {}
        self._stop         = threading.Event()
        self._thread: threading.Thread | None = None
        self._wsjt_host: str | None = None  # set from first received packet

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="wsjt-udp")
        self._thread.start()
        print(f"WSJT-X listener started on UDP port {self.port}")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', self.port))
        sock.settimeout(1.0)
        try:
            while not self._stop.is_set():
                try:
                    data, addr = sock.recvfrom(65536)
                    if self._wsjt_host is None:
                        self._wsjt_host = addr[0]
                    self._handle(data)
                except socket.timeout:
                    continue
                except Exception as exc:
                    print(f"WSJT-X packet error: {exc}")
        finally:
            sock.close()

    # -- outgoing messages to WSJT-X ------------------------------------------

    @staticmethod
    def _encode_utf8(s: str | None) -> bytes:
        if s is None:
            return struct.pack('>I', 0xFFFFFFFF)   # Qt null QString
        b = s.encode('utf-8')
        return struct.pack('>I', len(b)) + b

    def _build_msg(self, msg_type: int, payload: bytes) -> bytes:
        return (
            struct.pack('>III', self._MAGIC, 2, msg_type)
            + self._encode_utf8('pskspotter')
            + payload
        )

    def _send(self, data: bytes) -> None:
        if self._wsjt_host is None:
            return
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.sendto(data, (self._wsjt_host, self.port))

    def switch_configuration(self, name: str) -> None:
        """Send Switch Configuration (type 14) to WSJT-X.

        WSJT-X must have a saved configuration with this exact name and
        'Accept UDP requests' enabled in its settings.
        """
        self._send(self._build_msg(14, self._encode_utf8(name)))
        print(f"WSJT-X: requested Switch Configuration → '{name}'")

    # -- incoming packet parsing -----------------------------------------------

    def _handle(self, data: bytes) -> None:
        r = _BinReader(data)
        try:
            if r.uint32() != self._MAGIC:
                return
            r.uint32()           # schema (ignored; we read only schema-2 fields)
            mtype = r.uint32()
            r.utf8()             # client id

            if mtype == self._MSG_STATUS:
                self._dial_freq = r.uint64()
                self._wsjt_mode = r.utf8()
                r.utf8(); r.utf8(); r.utf8()     # dx_call, report, tx_mode
                r.bool_(); r.bool_(); r.bool_()  # tx_enabled, transmitting, decoding
                r.uint32(); r.uint32()            # rx_df, tx_df
                r.utf8()                          # de_call
                self._de_grid = r.utf8()

            elif mtype == self._MSG_DECODE and self._dial_freq:
                r.bool_()                 # new
                ms   = r.uint32()         # ms since midnight UTC
                snr  = r.int32()
                r.float64()               # delta time
                df   = r.uint32()         # delta freq Hz
                mode = r.utf8() or self._wsjt_mode
                msg  = r.utf8().strip()

                result = self._extract_call_grid(msg)
                if result is None:
                    return
                dx_call, dx_grid = result

                now = time.time()
                if now - self._call_times.get(dx_call, 0.0) < self._RESHOW_SECS:
                    return
                self._call_times[dx_call] = now

                freq_hz   = self._dial_freq + df
                band      = freq_to_band(freq_hz)
                midnight  = now - (now % 86400)
                unix_time = midnight + ms / 1000.0
                self.on_spot(dx_call, dx_grid, snr, df, mode, band, unix_time)

        except Exception:
            pass

    def _extract_call_grid(self, message: str) -> tuple[str, str] | None:
        parts = message.split()
        if not parts:
            return None

        if parts[0] == 'CQ':
            i = 1
            # skip 'DX' and short region tokens (e.g. 'EU', 'NA')
            while i < len(parts) and (
                parts[i] == 'DX' or (len(parts[i]) <= 2 and parts[i].isalpha())
            ):
                i += 1
            if i < len(parts) and _looks_like_call(parts[i]):
                grid = (
                    parts[i + 1]
                    if i + 1 < len(parts) and _looks_like_grid(parts[i + 1])
                    else ''
                )
                return parts[i], grid
            return None

        if self.decode_filter == 'ALL' and _looks_like_call(parts[0]):
            return parts[0], ''

        if self.decode_filter == 'ME' and self.my_call and len(parts) >= 2:
            target = parts[1].upper().rstrip('/P')
            if target == self.my_call and _looks_like_call(parts[0]):
                return parts[0], ''

        return None
