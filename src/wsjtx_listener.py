"""WSJT-X UDP listener and command sender for DX Spotter.

Implements :class:`WsjtxListener`, which runs on a background thread and
communicates with WSJT-X via the Qt/WSJT-X binary UDP protocol
(big-endian ``QDataStream`` serialisation, magic ``0xADBCCBDA``).

Incoming messages parsed:

* **Heartbeat (type 0)** — fires :attr:`~WsjtxListener.on_heartbeat`.
* **Status (type 1)** — updates dial frequency, mode, DX call, and TX state.
* **Decode (type 2)** — extracts DX callsign / grid, applies the decode filter,
  rate-limits spot emission, and calls :attr:`~WsjtxListener.on_spot`.
* **Reply echo (type 4)** — logged for diagnostics only.

Outgoing messages sent:

* **Heartbeat (type 0)** — sent on first packet and every 15 s to keep our
  client registered.
* **Highlight Callsign (type 13)** — highlights a call in the band-activity
  window.
* **Switch Configuration (type 14)** — switches WSJT-X configuration preset.
* **Configure (type 15)** — sets DX call, Rx DF, and optionally generates
  standard messages.
* **Reply (type 4)** — triggers a double-click on a matching band-activity row.

Type aliases
------------
SpotCallback
    ``(dx_call, dx_grid, snr, df_hz, mode, band, unix_time, msg, delta_t) → None``
BusyCallback
    ``(call) → None``
HeartbeatCallback
    ``() → None``
"""
import re
import socket
import struct
import threading
import time
from typing import Callable


_CALL_RE = re.compile(r'^[A-Z0-9]{1,3}\d[A-Z0-9]{0,3}[A-Z](?:/[A-Z0-9]+)?$')
_GRID_RE = re.compile(r'^[A-R]{2}\d{2}([a-x]{2})?$', re.IGNORECASE)


def _looks_like_call(s: str) -> bool:
    # Heuristic callsign check against _CALL_RE; used to distinguish calls from
    # signal reports and grid squares in decoded WSJT-X message text.
    return bool(_CALL_RE.match(s.upper()))


def _looks_like_grid(s: str) -> bool:
    # Heuristic Maidenhead locator check against _GRID_RE (4- or 6-char form).
    return bool(_GRID_RE.match(s))


def freq_to_band(freq_hz: int) -> str:
    """Convert an absolute frequency in Hz to a ham-band string.

    Parameters
    ----------
    freq_hz : int
        Absolute frequency in Hz (e.g. ``14_074_000`` for 20 m FT8).

    Returns
    -------
    str
        Band string such as ``'20m'``, ``'40m'``, etc.  Returns a fallback
        string of the form ``'14.074MHz'`` when the frequency does not fall
        within any known amateur band.
    """
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
    """Simple big-endian binary reader for Qt/WSJT-X UDP packets.

    Maintains an internal position pointer that advances as each field is read.
    All integer and float types follow big-endian (network) byte order, matching
    Qt's ``QDataStream`` default.

    Parameters
    ----------
    data : bytes
        Raw UDP datagram bytes to read from.
    """

    def __init__(self, data: bytes) -> None:
        # _d = raw packet bytes; _p = current read position
        self._d = data
        self._p = 0

    def uint8(self) -> int:
        """Read one unsigned 8-bit integer and advance the position by 1."""
        v = self._d[self._p]
        self._p += 1
        return v

    def uint32(self) -> int:
        """Read one unsigned 32-bit big-endian integer and advance by 4."""
        (v,) = struct.unpack_from('>I', self._d, self._p)
        self._p += 4
        return v

    def int32(self) -> int:
        """Read one signed 32-bit big-endian integer and advance by 4."""
        (v,) = struct.unpack_from('>i', self._d, self._p)
        self._p += 4
        return v

    def uint64(self) -> int:
        """Read one unsigned 64-bit big-endian integer and advance by 8."""
        (v,) = struct.unpack_from('>Q', self._d, self._p)
        self._p += 8
        return v

    def float64(self) -> float:
        """Read one IEEE 754 double (big-endian) and advance by 8."""
        (v,) = struct.unpack_from('>d', self._d, self._p)
        self._p += 8
        return v

    def bool_(self) -> bool:
        """Read one boolean byte (non-zero → ``True``) and advance by 1."""
        v = self._d[self._p] != 0
        self._p += 1
        return v

    def utf8(self) -> str:
        """Read a Qt length-prefixed UTF-8 string and advance past it.

        The string is encoded as a big-endian ``uint32`` length followed by
        that many UTF-8 bytes.  A length of ``0xFFFFFFFF`` or ``0`` is
        treated as a null / empty string and returns ``''``.

        Returns
        -------
        str
            Decoded string, or ``''`` for null/empty strings.
        """
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
    """UDP listener for the WSJT-X network protocol (big-endian Qt serialisation).

    Runs on a daemon background thread started by :meth:`start`.  All public
    methods except :meth:`start` and :meth:`stop` may be called from any thread
    (attribute writes are GIL-protected for simple types).

    Outgoing commands use a persistent send socket bound to a fixed ephemeral
    port so WSJT-X always sees the same ``(ip, port)`` client identity.

    Class attributes
    ----------------
    _MAGIC : int
        WSJT-X protocol magic number ``0xADBCCBDA`` used to validate incoming
        packets.
    _MSG_STATUS : int
        WSJT-X message type 1 (Status).
    _MSG_DECODE : int
        WSJT-X message type 2 (Decode).
    """

    _MAGIC = 0xADBCCBDA
    _MSG_STATUS = 1
    _MSG_DECODE = 2

    def __init__(self, port: int, decode_filter: str, my_call: str | None,
                 on_spot: SpotCallback,
                 on_call_busy: BusyCallback | None = None,
                 on_call_active: BusyCallback | None = None,
                 on_heartbeat: 'HeartbeatCallback | None' = None,
                 mcast_addr: str = '224.0.0.1',
                 reshow_secs: int = 300) -> None:
        """Initialise the listener; call :meth:`start` to begin receiving packets.

        Parameters
        ----------
        port : int
            UDP port to listen on (must match WSJT-X Settings → Reporting →
            UDP Server port, typically ``2237``).
        decode_filter : str
            Initial decode filter: ``'CQ'`` (CQ calls only), ``'ALL'`` (every
            decode), or ``'ME'`` (only decodes addressed to ``my_call``).
            Case-insensitive; stored upper-cased.
        my_call : str or None
            Operator's callsign, used by the ``'ME'`` filter.  ``None``
            disables the ME filter.
        on_spot : SpotCallback
            Callable invoked (on the listener thread) for each new spot that
            passes the rate-limiting gate.
            Signature: ``(dx_call, dx_grid, snr, df_hz, mode, band,
            unix_time, msg, delta_t) → None``.
        on_call_busy : BusyCallback or None, optional
            Called when a CQ caller is observed entering a QSO.
            Signature: ``(call) → None``.
        on_call_active : BusyCallback or None, optional
            Called when a previously busy station resumes calling CQ.
            Signature: ``(call) → None``.
        on_heartbeat : HeartbeatCallback or None, optional
            Called on the first received packet and on each incoming Heartbeat
            (type 0).  Signature: ``() → None``.
        mcast_addr : str, optional
            Multicast group address to join on the receive socket.  Defaults to
            ``'224.0.0.1'`` (standard WSJT-X multicast address).
        reshow_secs : int, optional
            Minimum seconds between successive spot-table entries for the same
            callsign (rate-limiting gate).  Default is ``300`` (5 minutes).
        """
        self.port = port
        self.decode_filter = decode_filter.upper()  # 'CQ', 'ALL', 'ME'
        self.my_call = my_call.upper() if my_call else None
        self.on_spot = on_spot
        self.on_call_busy = on_call_busy
        self.on_call_active = on_call_active
        self.on_heartbeat = on_heartbeat
        self._mcast_addr = mcast_addr
        self._reshow_secs = reshow_secs
        self._dial_freq = 0
        self._wsjt_mode = ''
        self._de_grid = ''
        self._call_times: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._wsjt_host: str | None = None  # set from first received packet
        self._wsjt_src_port: int = 0  # WSJT-X's own socket port (send commands here)
        self._send_sock: socket.socket | None = None  # persistent send socket
        # Diagnostic state — only log Status when these change
        self._first_status: bool = True  # always print the very first Status
        self._last_dx_call: str = ''
        self._last_tx_en: bool = False
        self._last_txing: bool = False
        # Always-current decode fields for Reply (updated every decode period)
        self._latest_decode: dict[str, dict] = {}
        self._last_highlighted: str = ''  # call currently highlighted in band activity
        self._wsjt_client_id: str = ''   # WSJT-X's own client ID (from incoming packet headers)

    def start(self) -> None:
        """Start the background UDP listener thread.

        Spawns a daemon thread named ``'wsjt-udp'`` that binds to
        :attr:`port`, joins the multicast group, and loops calling
        :meth:`_handle` on each received datagram until :meth:`stop` is
        called.
        """
        self._thread = threading.Thread(target=self._run, daemon=True, name="wsjt-udp")
        self._thread.start()
        print(f"WSJT-X listener started on UDP port {self.port}")

    def stop(self) -> None:
        """Signal the listener thread to exit on its next iteration."""
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
        """Send a Reply (type 4) command to WSJT-X.

        WSJT-X searches its band-activity table for a row whose fields exactly
        match ``(time_ms, snr, delta_t, df, mode, message, low_confidence)``
        and simulates a double-click on that row, setting the DX call, Rx DF,
        and generating the standard exchange messages.

        Requires **Accept UDP requests** to be enabled in
        WSJT-X Settings → Reporting.  All fields must exactly match the
        values from the original :class:`_BinReader`-parsed Decode packet.

        Parameters
        ----------
        time_ms : int
            Milliseconds since UTC midnight from the Decode packet
            (``QTime`` as ``quint32``).
        snr : int
            Signal-to-noise ratio in dB (signed, from the Decode packet).
        df : int
            Delta frequency (audio offset) in Hz (from the Decode packet).
        mode : str
            Mode string (e.g. ``'FT8'``), verbatim from the Decode packet.
        message : str
            Decoded message text, verbatim from the Decode packet
            (e.g. ``'CQ W1AW FN31'``).
        delta_t : float, optional
            Time delta in seconds from the Decode packet (default ``0.0``).
        low_confidence : bool, optional
            Low-confidence flag from the Decode packet (default ``False``).
            Must match exactly.
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
        """Send a Configure (type 15) command to WSJT-X.

        Directly sets the DX call, Rx DF audio frequency, and optionally
        triggers **Generate Std Msgs** — equivalent to typing the callsign
        into the DX Call box and clicking that button.  Does not require a
        matching entry in band activity.

        Fields documented as "if max → no change" are sent as ``0xFFFFFFFF``.
        T/R Period ``0`` means no change; Fast Mode ``False`` targets standard
        FT8 / FT4 operation.

        Parameters
        ----------
        rx_df : int
            Receive audio frequency offset (DF) in Hz to set.
        dx_call : str
            DX callsign to enter in WSJT-X.  An empty string sends a null
            QString (no change).
        dx_grid : str, optional
            DX station's Maidenhead grid square (default ``''`` = no change).
        generate_messages : bool, optional
            If ``True`` (default), WSJT-X generates standard exchange messages
            immediately after setting the DX call.
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
        """Clear the rate-limiting gate so all decoded calls can re-appear.

        :attr:`_call_times` maps each callsign to the Unix timestamp of the
        last time it was forwarded to the spot table.  Clearing it means every
        callsign will appear on the next decode regardless of ``reshow_secs``.
        Called when the decode filter changes so the table is repopulated with
        fresh spots.
        """
        self._call_times.clear()

    def switch_configuration(self, name: str) -> None:
        """Send Switch Configuration (type 14) to WSJT-X.

        Parameters
        ----------
        name : str
            Name of the WSJT-X configuration preset to activate (must match
            a name defined in WSJT-X Settings → Configurations).
        """
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
        """Send a Highlight Callsign (type 13) command to WSJT-X.

        First clears the highlight on the previously highlighted callsign
        (tracked in :attr:`_last_highlighted`), then applies the new highlight.

        Parameters
        ----------
        callsign : str
            Callsign to highlight in the WSJT-X band-activity window.
        bg : tuple[int, int, int] or None, optional
            RGB background colour as ``(r, g, b)`` with values 0–255.
            Default is amber ``(255, 200, 0)``.  ``None`` sends an invalid
            (transparent / clear) colour.
        fg : tuple[int, int, int] or None, optional
            RGB foreground (text) colour as ``(r, g, b)``.  Default is
            black ``(0, 0, 0)``.  ``None`` sends an invalid colour.
        last_only : bool, optional
            If ``True``, highlight only the last occurrence of the callsign
            in band activity (default ``False`` = highlight all occurrences).
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

        The cache (``_latest_decode``) is updated on every decode period
        regardless of the ``reshow_secs`` display gate, so this always
        reflects the freshest available data for constructing a Reply.

        Parameters
        ----------
        call : str
            Callsign to look up (case-insensitive).

        Returns
        -------
        dict or None
            Dict with keys:

            * ``ms`` (int) — milliseconds since UTC midnight.
            * ``snr`` (int) — signal-to-noise ratio in dB.
            * ``delta_t`` (float) — time delta in seconds.
            * ``df`` (int) — audio frequency offset in Hz.
            * ``mode`` (str) — mode string (e.g. ``'FT8'``).
            * ``msg`` (str) — verbatim decoded message text.
            * ``low_confidence`` (bool) — low-confidence flag.

            Returns ``None`` when no decode has been seen for this callsign.
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
                if now - self._call_times.get(dx_call, 0.0) < self._reshow_secs:
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
