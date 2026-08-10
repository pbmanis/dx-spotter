"""Hamlib ``rigctld`` network rig-control client.

Queries and controls a transceiver via a running ``rigctld`` (Hamlib network
rig daemon) using its extended-response protocol.  This is the same protocol
WSJT-X speaks when configured with ``Rig: Hamlib NET rigctl``, so it is
compatible with any bridge that exposes a standard rigctld TCP interface —
including K2K3Controller's CAT/PTT bridge (default port ``4532``, adjustable
in its own settings).

Protocol summary
-----------------
Commands are prefixed with ``+`` to request the extended-response format,
which always terminates with an ``RPRT <code>`` line (``0`` = success)::

    +f          -> get_freq:\\nFrequency: 14074000\\nRPRT 0
    +F 14074000 -> set_freq: 14074000\\nRPRT 0
    +m          -> get_mode:\\nMode: USB\\nPassband: 2400\\nRPRT 0
    +M USB 0    -> set_mode: USB 0\\nRPRT 0

Frequencies are exchanged in whole Hz; this client converts to/from kHz to
match :class:`~commander_client.CommanderClient`'s interface.
"""
from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field


_DEFAULT_HOST: str = '127.0.0.1'
_DEFAULT_PORT: int = 4532   # Hamlib rigctld standard default
_TIMEOUT: float = 0.2
_VERIFY_DELAY: float = 0.75

# Hamlib mode strings relevant to CW/SSB/digital QSY. PKTUSB is the standard
# Hamlib mode for USB data ("DATA" on Elecraft K2/K3/KX rigs).
VALID_MODES: frozenset[str] = frozenset({
    'AM', 'CW', 'CWR', 'FM', 'LSB', 'USB', 'RTTY', 'RTTYR',
    'PKTUSB', 'PKTLSB', 'PKTFM', 'WFM',
})


def is_available(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT,
                 timeout: float = 1.0) -> bool:
    """Return ``True`` if rigctld is reachable and accepting connections.

    Opens a TCP connection and closes it immediately.  Does not send any
    command — just confirms the port is open.

    Parameters
    ----------
    host : str
        Hostname or IP of the rigctld process.
    port : int
        TCP port rigctld is listening on.
    timeout : float
        Connection timeout in seconds.

    Returns
    -------
    bool
        ``True`` if the connection succeeds, ``False`` on any error.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, port))
        return True
    except OSError:
        return False


@dataclass
class TransceiverState:
    """Snapshot of transceiver state as reported by rigctld.

    Parameters
    ----------
    rx_freq_khz : float
        VFO frequency in kHz.
    mode : str
        Active transceiver mode, e.g. ``'CW'``, ``'USB'``, ``'PKTUSB'``.
    split : bool
        ``True`` when the transceiver is in split mode.
    transmitting : bool
        ``True`` when the transceiver is currently transmitting (PTT).
    error : str
        Non-empty when a query failed (connection refused, timeout, etc.).
        All other fields are 0 / empty when this is set.
    """

    rx_freq_khz: float = 0.0
    mode: str = ''
    split: bool = False
    transmitting: bool = False
    error: str = ''


@dataclass
class SetResult:
    """Result of a :meth:`~RigctldClient.set_freq_and_mode` call.

    Parameters
    ----------
    success : bool
        ``True`` when all post-set verifications passed.
    intended_freq_khz : float
        The frequency that was requested.
    intended_mode : str
        The mode that was requested.
    actual_state : TransceiverState
        State read back from rigctld after the set command.
    errors : list[str]
        List of verification failures; empty when ``success`` is ``True``.
    """

    success: bool
    intended_freq_khz: float
    intended_mode: str
    actual_state: TransceiverState
    errors: list[str] = field(default_factory=list)


class RigctldClient:
    """TCP/IP client for a Hamlib ``rigctld`` network rig daemon.

    Each public method opens a fresh TCP connection, sends one extended-format
    command, and closes the connection once the trailing ``RPRT`` line has
    been received (or the timeout expires).

    Use :func:`is_available` to verify the port is open before creating an
    instance in time-critical code.

    Parameters
    ----------
    host : str
        Hostname or IP address of the rigctld process.
    port : int
        TCP port rigctld is listening on.
    timeout : float
        Per-query receive timeout in seconds.
    verbose : bool
        When ``True``, print the raw command and response for every query.
    """

    def __init__(self, host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT,
                 timeout: float = _TIMEOUT, verbose: bool = False) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose

    # -- low-level transport --------------------------------------------------

    def _query(self, command: str) -> str:
        """Send *command* and return the raw response string.

        Reads until an ``RPRT`` line (the extended-protocol terminator) has
        been seen, or the timeout budget is exhausted.
        """
        buf = b''
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            s.sendall((command + '\n').encode('utf-8'))
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                s.settimeout(remaining)
                try:
                    chunk = s.recv(1024)
                    if not chunk:
                        break          # rigctld closed the connection
                    buf += chunk
                    if b'RPRT' in buf:
                        break
                except socket.timeout:
                    break
        response = buf.decode('utf-8', errors='replace').strip()
        if self.verbose:
            print(f"  [CMD]  {command!r}")
            print(f"  [RESP] {response!r}")
        return response

    # -- response parsing -----------------------------------------------------

    @staticmethod
    def _field(response: str, label: str) -> str:
        """Return the value following ``label:`` in an extended-format response."""
        m = re.search(re.escape(label) + r':\s*(\S+)', response)
        return m.group(1) if m else ''

    @staticmethod
    def _rprt_ok(response: str) -> bool:
        """Return ``True`` when the response's trailing ``RPRT`` code is ``0``."""
        m = re.search(r'RPRT\s+(-?\d+)', response)
        return m is not None and m.group(1) == '0'

    # -- individual queries ---------------------------------------------------

    def get_rx_freq_khz(self) -> float:
        """Return the current VFO frequency in kHz.

        Returns
        -------
        float
            RX frequency in kHz, or ``0.0`` if not yet reported by the rig.
        """
        resp = self._query('+f')
        try:
            return float(self._field(resp, 'Frequency')) / 1000.0
        except ValueError:
            return 0.0

    def get_mode(self) -> str:
        """Return the current transceiver mode string.

        Returns
        -------
        str
            Mode such as ``'CW'``, ``'USB'``, ``'PKTUSB'``.  Returns ``''``
            if not yet reported by the rig.
        """
        resp = self._query('+m')
        return self._field(resp, 'Mode')

    def get_split(self) -> bool:
        """Return ``True`` when the transceiver is in split mode."""
        resp = self._query('+s')
        return self._field(resp, 'Split') == '1'

    def get_transmitting(self) -> bool:
        """Return ``True`` when the transceiver is currently transmitting (PTT)."""
        resp = self._query('+t')
        return self._field(resp, 'PTT') not in ('', '0')

    def get_state(self) -> TransceiverState:
        """Query rigctld and return a full :class:`TransceiverState` snapshot.

        Issues four sequential queries (freq, mode, split, PTT).  Any
        ``OSError`` (connection refused, timeout) causes an immediate return
        with ``TransceiverState.error`` set.

        Returns
        -------
        TransceiverState
            Populated state snapshot.  Check ``state.error`` before using
            the other fields.
        """
        try:
            return TransceiverState(
                rx_freq_khz=self.get_rx_freq_khz(),
                mode=self.get_mode(),
                split=self.get_split(),
                transmitting=self.get_transmitting(),
            )
        except OSError as exc:
            return TransceiverState(error=str(exc))

    # -- set commands ---------------------------------------------------------

    def set_freq_and_mode(self, freq_khz: float, mode: str,
                          verify_delay: float = _VERIFY_DELAY,
                          freq_tol_khz: float = 0.1) -> SetResult:
        """Set the transceiver frequency and mode, with split and PTT forced off.

        Sequence
        --------
        1. Send ``T 0`` to drop out of transmit if active.
        2. Send ``S 0 VFOA`` to turn split off.
        3. Send ``F <hz>`` and ``M <mode> 0`` (passband ``0`` = rig default)
           to set frequency and mode.
        4. Wait *verify_delay* seconds for the rig to tune, then read back
           state once.

        Parameters
        ----------
        freq_khz : float
            Target VFO frequency in kHz (e.g. ``14074.0`` for 20 m FT8).
        mode : str
            Target mode string (e.g. ``'CW'``, ``'USB'``, ``'PKTUSB'``).  Must
            be one of :data:`VALID_MODES`; a ``ValueError`` is raised
            otherwise.
        verify_delay : float, optional
            Seconds to wait before reading back state.  Default
            :data:`_VERIFY_DELAY`.
        freq_tol_khz : float, optional
            Maximum acceptable frequency error in kHz.  Default ``0.1`` kHz.

        Returns
        -------
        SetResult
            ``success`` is ``True`` when the read-back state matches the
            intended frequency and mode and both split and PTT are off.
            ``errors`` lists each mismatch on failure.

        Raises
        ------
        ValueError
            If *mode* is not in :data:`VALID_MODES`.
        OSError
            If rigctld is unreachable when sending commands.
        """
        mode = mode.upper().strip()
        if mode not in VALID_MODES:
            raise ValueError(
                f"Invalid mode {mode!r}. Valid modes: {sorted(VALID_MODES)}"
            )

        freq_hz = int(round(freq_khz * 1000))
        self._query('+T 0')
        self._query('+S 0 VFOA')
        self._query(f'+F {freq_hz}')
        self._query(f'+M {mode} 0')

        time.sleep(verify_delay)
        actual = self.get_state()

        errors: list[str] = []
        if actual.error:
            errors.append(f"State read-back failed: {actual.error}")
        else:
            freq_err = abs(actual.rx_freq_khz - freq_khz)
            if freq_err > freq_tol_khz:
                errors.append(
                    f"Frequency mismatch: requested {freq_khz:.3f} kHz, "
                    f"got {actual.rx_freq_khz:.3f} kHz "
                    f"({freq_err:.3f} kHz error)"
                )
            if actual.mode.upper() != mode:
                errors.append(
                    f"Mode mismatch: requested {mode!r}, got {actual.mode!r}"
                )
            if actual.split:
                errors.append("Split is ON after set (expected OFF)")
            if actual.transmitting:
                errors.append("Rig is still transmitting after PTT off")

        return SetResult(
            success=len(errors) == 0,
            intended_freq_khz=freq_khz,
            intended_mode=mode,
            actual_state=actual,
            errors=errors,
        )
