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
        v = self._d[self._p]
        self._p += 1
        return v

    def uint32(self) -> int:
        (v,) = struct.unpack_from('>I', self._d, self._p)
        self._p += 4
        return v

    def int32(self) -> int:
        (v,) = struct.unpack_from('>i', self._d, self._p)
        self._p += 4
        return v

    def uint64(self) -> int:
        (v,) = struct.unpack_from('>Q', self._d, self._p)
        self._p += 8
        return v

    def float64(self) -> float:
        (v,) = struct.unpack_from('>d', self._d, self._p)
        self._p += 8
        return v

    def bool_(self) -> bool:
        v = self._d[self._p] != 0
        self._p += 1
        return v

    def utf8(self) -> str:
        n = self.uint32()
        if n in (0xFFFFFFFF, 0):
            return ''
        v = self._d[self._p:self._p + n].decode('utf-8', errors='replace')
        self._p += n
        return v


# Signature: (dx_call, dx_grid, snr, df_hz, mode, band, unix_time, msg, delta_t) -> None
SpotCallback = Callable[[str, str, int, int, str, str, float, str, float], None]
# Signature: (call) -> None  — called when a CQ caller is observed entering a QSO
BusyCallback = Callable[[str], None]
# Signature: () -> None  — called on first packet and on each incoming Heartbeat (type 0)
HeartbeatCallback = Callable[[], None]


class WsjtxListener:
    """UDP listener for the WSJT-X network protocol (big-endian Qt serialization)."""

    _MAGIC            = 0xADBCCBDA
    _MSG_STATUS       = 1
    _MSG_DECODE       = 2
    _RESHOW_SECS      = 300  # re-show a call after 5 minutes of silence

    def __init__(self, port: int, decode_filter: str, my_call: str | None,
                 on_spot: SpotCallback,
                 on_call_busy: BusyCallback | None = None,
                 on_call_active: BusyCallback | None = None,
                 on_heartbeat: 'HeartbeatCallback | None' = None,
                 mcast_addr: str = '224.0.0.1'):
        self.port            = port
        self.decode_filter   = decode_filter.upper()  # 'CQ', 'ALL', 'ME'
        self.my_call         = my_call.upper() if my_call else None
        self.on_spot         = on_spot
        self.on_call_busy    = on_call_busy
        self.on_call_active  = on_call_active
        self.on_heartbeat    = on_heartbeat
        self._mcast_addr   = mcast_addr
        self._dial_freq    = 0
        self._wsjt_mode    = ''
        self._de_grid      = ''
        self._call_times: dict[str, float] = {}
        self._stop         = threading.Event()
        self._thread: threading.Thread | None = None
        self._wsjt_host: str | None = None  # set from first received packet
        self._wsjt_src_port: int  = 0     # WSJT-X's own socket port (send commands here)
        self._send_sock: socket.socket | None = None  # persistent send socket
        # Diagnostic state — only log Status when these change
        self._first_status: bool = True   # always print the very first Status
        self._last_dx_call: str = ''
        self._last_tx_en:   bool = False
        self._last_txing:   bool = False
        # Always-current decode fields for Reply (updated every decode period)
        self._latest_decode: dict[str, dict] = {}
        self._last_highlighted: str = ''  # call currently highlighted in band activity
        self._wsjt_client_id: str = ''   # WSJT-X's own client ID (from incoming packet headers)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="wsjt-udp")
        self._thread.start()
        print(f"WSJT-X listener started on UDP port {self.port}")

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        # Persistent send socket with a fixed ephemeral port so WSJT-X always
        # sees the same (ip, port) client identity across all outgoing messages.
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.bind(('', 0))

        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, 'SO_REUSEPORT'):
            recv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        recv_sock.bind(('', self.port))
        # Join the WSJT-X multicast group so we get our own independent copy of
        # every packet even when RUMlogNG / GridTracker / JTAlert are also bound
        # to this port.  On macOS, SO_REUSEPORT for unicast UDP load-balances
        # across sockets (only one gets each packet); multicast bypasses that by
        # delivering a copy to every socket that has joined the group.
        # Requires WSJT-X Settings → Reporting → UDP Server = 224.0.0.1
        _MCAST_GRP = self._mcast_addr
        mreq = struct.pack('4sL', socket.inet_aton(_MCAST_GRP), socket.INADDR_ANY)
        try:
            recv_sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            print(f"WSJT-X recv socket bound to 0.0.0.0:{self.port} (multicast {_MCAST_GRP})")
        except OSError as e:
            print(f"WSJT-X recv socket bound to 0.0.0.0:{self.port} "
                  f"(multicast join failed: {e} — unicast only)")
        recv_sock.settimeout(1.0)

        last_hb      = 0.0
        last_warn    = time.time()
        pkts_rx      = 0
        try:
            while not self._stop.is_set():
                try:
                    data, addr = recv_sock.recvfrom(65536)
                    pkts_rx += 1
                    if self._wsjt_host is None:
                        self._wsjt_host     = addr[0]
                        self._wsjt_src_port = addr[1]
                        print(f"WSJT-X: first packet from {addr[0]}:{addr[1]}"
                              f" — commands will go to that port")
                        self._send_heartbeat()
                        last_hb = time.time()
                        if self.on_heartbeat:
                            self.on_heartbeat()
                    self._handle(data)
                except socket.timeout:
                    pass
                except Exception as exc:
                    print(f"WSJT-X packet error: {exc}")
                now = time.time()
                # Warn every 30 s if still nothing heard
                if self._wsjt_host is None and now - last_warn >= 30:
                    last_warn = now
                    print(f"WSJT-X: no packets received on port {self.port} "
                          f"— is WSJT-X running and sending to this address?")
                # Periodic heartbeat keeps our client registered
                if self._wsjt_host and now - last_hb >= 15:
                    self._send_heartbeat()
                    last_hb = now
        finally:
            recv_sock.close()
            self._send_sock.close()
            self._send_sock = None

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
            + self._encode_utf8('dxspotter')
            + payload
        )

    def _send(self, data: bytes) -> None:
        if self._wsjt_host is None or self._wsjt_src_port == 0 or self._send_sock is None:
            return
        # WSJT-X sends FROM its own ephemeral port (e.g. 63326) TO our listen
        # port (2237).  Commands back to WSJT-X must go TO that source port —
        # NOT to our listen port which our own recv_sock would swallow.
        self._send_sock.sendto(data, (self._wsjt_host, self._wsjt_src_port))

    def _send_heartbeat(self) -> None:
        """Register this client with WSJT-X (type 0 — Heartbeat)."""
        payload = (
            struct.pack('>I', 3)        # max schema we support
            + self._encode_utf8('1.0')  # version
            + self._encode_utf8('')     # revision
        )
        self._send(self._build_msg(0, payload))
        print("WSJT-X: heartbeat sent")

    def reply_to_decode(self, time_ms: int, snr: int, df: int,
                        mode: str, message: str, delta_t: float = 0.0,
                        low_confidence: bool = False) -> None:
        """Send Reply (type 4) to WSJT-X.

        WSJT-X searches its band-activity table for a row matching all these
        fields exactly (time_ms, snr, delta_t, df, mode, message, low_confidence)
        then simulates a double-click on that row, setting the DX call, Rx DF,
        and generating the standard exchange messages.

        Requires 'Accept UDP requests' in WSJT-X Settings → Reporting.
        All fields must exactly match the corresponding Decode packet fields.
        """
        payload = (
            struct.pack('>I', time_ms)     # QTime quint32 ms since midnight
            + struct.pack('>i', snr)       # qint32 snr
            + struct.pack('>d', delta_t)   # double delta_time (seconds)
            + struct.pack('>I', max(0, df))  # quint32 delta_frequency Hz
            + self._encode_utf8(mode)
            + self._encode_utf8(message)
            + struct.pack('>?', low_confidence)  # must match original Decode
            + struct.pack('>B', 0)               # modifiers (none)
        )
        pkt = self._build_msg(4, payload)
        self._send(pkt)
        my_port  = self._send_sock.getsockname()[1] if self._send_sock else '?'
        dst_port = self._wsjt_src_port or '?'
        print(f"WSJT-X Reply → msg={message!r}  df={df} Hz  dt={delta_t:.2f}s  "
              f"snr={snr:+d}  mode={mode}  time_ms={time_ms}  lc={low_confidence}")
        print(f"  from :{my_port} → {self._wsjt_host}:{dst_port}  "
              f"({len(pkt)} bytes)  hex: {pkt.hex()}")

    def configure(self, rx_df: int, dx_call: str, dx_grid: str = '',
                  generate_messages: bool = True) -> None:
        """Send Configure (type 15) to WSJT-X.

        Directly sets the DX call, Rx DF audio frequency, and optionally
        triggers Generate Std Msgs — equivalent to typing the call and clicking
        that button.  Does not require a matching entry in band activity.

        Fields documented as "if max → no change" use 0xFFFFFFFF.
        T/R Period 0 → no change; Fast Mode False → standard FT8/FT4.
        """
        MAX_U32 = 0xFFFFFFFF
        payload = (
            self._encode_utf8(None)               # Mode: null = no change
            + struct.pack('>I', MAX_U32)           # Frequency Tolerance: max = no change
            + self._encode_utf8(None)              # Submode: null = no change
            + struct.pack('>?', False)             # Fast Mode: False (standard FT8/FT4)
            + struct.pack('>I', 0)                # T/R Period: 0 = no change
            + struct.pack('>I', max(0, rx_df))    # Rx DF: audio frequency Hz
            + self._encode_utf8(dx_call or None)  # DX Call: null if empty → no change
            + self._encode_utf8(dx_grid or None)  # DX Grid: null if empty → no change
            + struct.pack('>?', generate_messages)
        )
        self._send(self._build_msg(15, payload))
        print(f"WSJT-X Configure → DX={dx_call!r}  grid={dx_grid!r}  "
              f"rx_df={rx_df} Hz  genMsg={generate_messages}")

    def reset_call_times(self) -> None:
        """Clear the rate-limiting gate so all decoded calls can re-appear."""
        self._call_times.clear()

    def switch_configuration(self, name: str) -> None:
        """Send Switch Configuration (type 14) to WSJT-X."""
        self._send(self._build_msg(14, self._encode_utf8(name)))
        print(f"WSJT-X: requested Switch Configuration → '{name}'")

    @staticmethod
    def _pack_qcolor(r: int, g: int, b: int, valid: bool = True) -> bytes:
        """Serialize a Qt QColor (big-endian QDataStream schema >= 7).

        Format: qint8 spec, quint16 alpha, quint16 r, quint16 g, quint16 b, quint16 pad
        Pass valid=False to encode an invalid (clear) color.
        """
        if not valid:
            return struct.pack('>bHHHHH', 0, 0, 0, 0, 0, 0)
        return struct.pack('>bHHHHH', 1, 0xFFFF, r * 257, g * 257, b * 257, 0)

    def highlight_call(self, callsign: str,
                       bg: tuple[int, int, int] | None = (255, 200, 0),
                       fg: tuple[int, int, int] | None = (0, 0, 0),
                       last_only: bool = False) -> None:
        """Send Highlight Callsign (type 13) to WSJT-X.

        Clears the previous highlight then highlights the new callsign.
        Pass bg=None to use an invalid (clear) color.
        """
        clear = self._pack_qcolor(0, 0, 0, valid=False)
        if self._last_highlighted and self._last_highlighted != callsign:
            prev_payload = (
                self._encode_utf8(self._last_highlighted)
                + clear + clear
                + struct.pack('>?', False)
            )
            self._send(self._build_msg(13, prev_payload))

        self._last_highlighted = callsign
        bg_bytes = self._pack_qcolor(*bg) if bg else clear
        fg_bytes = self._pack_qcolor(*fg) if fg else clear
        payload = (
            self._encode_utf8(callsign)
            + bg_bytes
            + fg_bytes
            + struct.pack('>?', last_only)
        )
        self._send(self._build_msg(13, payload))
        print(f"WSJT-X Highlight → {callsign!r}")

    def get_latest_decode(self, call: str) -> dict | None:
        """Return the most recently received WSJT-X decode for this callsign.

        Fields: ms (int), snr (int), delta_t (float), df (int), mode (str), msg (str).
        Updated on every decode period regardless of the _RESHOW_SECS display gate.
        """
        return self._latest_decode.get(call.upper())

    # -- incoming packet parsing -----------------------------------------------

    _MSG_NAMES: dict[int, str] = {
        0: 'Heartbeat',  1: 'Status',       2: 'Decode',       3: 'Clear',
        4: 'Reply',      5: 'QSOLogged',    6: 'Close',        7: 'Replay',
        8: 'HaltTx',     9: 'FreeText',    10: 'WSPRDecode',  11: 'Location',
        12: 'LoggedADIF', 13: 'HighlightCall', 14: 'SwitchConfig', 15: 'Configure',
    }

    def _handle(self, data: bytes) -> None:
        mtype = -1
        r = _BinReader(data)
        try:
            if r.uint32() != self._MAGIC:
                return
            r.uint32()            # schema
            mtype     = r.uint32()
            client_id = r.utf8()
            if client_id:
                self._wsjt_client_id = client_id

            if mtype == 0:  # Heartbeat from WSJT-X
                if self.on_heartbeat:
                    self.on_heartbeat()
                name = self._MSG_NAMES.get(mtype, f'type{mtype}')
                print(f"WSJT-X ← {name} (type={mtype}  {len(data)}B  id={client_id!r})")

            elif mtype == self._MSG_STATUS:
                self._dial_freq = r.uint64()
                self._wsjt_mode = r.utf8()
                dx_call  = r.utf8()
                r.utf8()          # report
                r.utf8()          # tx_mode
                tx_en    = r.bool_()
                txing    = r.bool_()
                r.bool_()         # decoding
                rx_df    = r.uint32()
                r.uint32()        # tx_df
                de_call  = r.utf8()
                self._de_grid = r.utf8()
                changed = (self._first_status or
                           dx_call != self._last_dx_call or
                           tx_en   != self._last_tx_en   or
                           txing   != self._last_txing)
                if changed:
                    self._first_status = False
                    self._last_dx_call = dx_call
                    self._last_tx_en   = tx_en
                    self._last_txing   = txing
                    print(f"WSJT-X Status: dial_freq={self._dial_freq}  "
                          f"mode={self._wsjt_mode}  dx_call={dx_call!r}  "
                          f"tx_en={tx_en}  txing={txing}  "
                          f"rx_df={rx_df}  de={de_call}")

            elif mtype == 4:
                # Reply packet echoed back (e.g. from another companion app)
                try:
                    ms_e  = r.uint32()
                    snr_e = r.int32()
                    dt_e  = r.float64()
                    df_e  = r.uint32()
                    md_e  = r.utf8()
                    msg_e = r.utf8()
                    print(f"WSJT-X Reply echo ← time_ms={ms_e}  df={df_e} Hz  "
                          f"dt={dt_e:.2f}s  snr={snr_e:+d}  mode={md_e!r}  msg={msg_e!r}")
                except Exception:
                    print("WSJT-X Reply echo ← (parse error)")

            elif mtype == self._MSG_DECODE:
                r.bool_()                   # new
                ms       = r.uint32()       # ms since midnight UTC (exact integer)
                snr      = r.int32()
                delta_t  = r.float64()      # delta time (seconds, needed for Reply)
                df       = r.uint32()       # delta freq Hz
                raw_mode = r.utf8()
                mode     = raw_mode if raw_mode and raw_mode != '~' else self._wsjt_mode
                msg      = r.utf8()         # keep verbatim — WSJT-X Reply needs exact match
                low_conf = r.bool_()        # must match exactly in Reply

                # With CQ filter: detect when a CQ caller enters a QSO so the
                # spot table can remove them.  "K1XYZ G4XYZ -05" → G4XYZ busy.
                parts = msg.split()
                if (self.decode_filter == 'CQ'
                        and self.on_call_busy is not None
                        and len(parts) >= 2
                        and parts[0] != 'CQ'
                        and _looks_like_call(parts[0])
                        and _looks_like_call(parts[1])):
                    self.on_call_busy(parts[1].upper())

                result = self._extract_call_grid(msg)
                if result is None:
                    return
                dx_call, dx_grid = result

                # If the station is back to calling CQ, un-dim it in the table.
                if parts[0] == 'CQ' and self.on_call_active is not None:
                    self.on_call_active(dx_call)

                # Cache freshest decode for Reply regardless of dial_freq.
                # dial_freq=0 (no radio) must not block _latest_decode population.
                self._latest_decode[dx_call] = {
                    'ms': ms, 'snr': snr, 'delta_t': delta_t,
                    'df': df, 'mode': mode, 'msg': msg,
                    'low_confidence': low_conf,
                }

                if not self._dial_freq:
                    return   # can't compute band — skip spot table emission

                now = time.time()
                if now - self._call_times.get(dx_call, 0.0) < self._RESHOW_SECS:
                    return   # suppress table update; latest decode already saved
                self._call_times[dx_call] = now

                freq_hz   = self._dial_freq + df
                band      = freq_to_band(freq_hz)
                midnight  = now - (now % 86400)
                unix_time = midnight + ms / 1000.0
                self.on_spot(dx_call, dx_grid, snr, df, mode, band, unix_time, msg, delta_t)

            else:
                # Log anything else WSJT-X sends so we can see all traffic
                name = self._MSG_NAMES.get(mtype, f'type{mtype}')
                print(f"WSJT-X ← {name} (type={mtype}  {len(data)}B  id={client_id!r})")

        except Exception as exc:
            name = self._MSG_NAMES.get(mtype, f'type{mtype}')
            print(f"WSJT-X packet parse error ({name}): {exc}")

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
