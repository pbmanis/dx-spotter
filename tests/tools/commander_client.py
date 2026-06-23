"""DX Lab Suite Commander TCP/IP client.

Queries Commander for transceiver state using the ADIF-style TCP/IP
protocol documented at:
  http://www.dxlabsuite.com/commander/Commander%20TCPIP%20Messages.pdf

Commander listens on the third port of its configured port block.  The
documented default is port 52002 (base 52000 + 2).  Adjust ``_DEFAULT_PORT``
or pass ``port=`` to :class:`CommanderClient` to match your installation.

Protocol summary
----------------
Commands sent to Commander::

    <command:N>CmdName<parameters:M>ParameterString

Responses use ADIF-style length-prefixed fields::

    <FieldName:N>value

Frequencies are returned in kHz with an optional comma thousands separator,
e.g. ``14,074.000``.

Commander does not expose VFO A and VFO B as separate named resources.
Instead it exposes RX and TX frequencies:

* ``CmdGetFreq``   → active receive VFO (VFO A on most rigs)
* ``CmdGetTXFreq`` → active transmit VFO (VFO B when split is ON; same as
  RX when split is OFF)

There is no API call to read the *inactive* VFO's frequency when split is
OFF — Commander simply does not provide it.
"""
from __future__ import annotations

import re
import socket
import time
from dataclasses import dataclass, field


_DEFAULT_HOST: str = '127.0.0.1'
_DEFAULT_PORT: int = 7374   # adjust to match Commander's configured port
_TIMEOUT: float = 0.2
_VERIFY_DELAY: float = 0.75   # seconds to wait before first read-back attempt. absolute minimum is about 0.7 s

# Valid mode strings accepted by Commander.
VALID_MODES: frozenset[str] = frozenset({
    'AM', 'CW', 'CW-R', 'DATA-L', 'DATA-U', 'FM', 'LSB', 'USB',
    'RTTY', 'RTTY-R', 'WBFM',
})


# ---------------------------------------------------------------------------
# Availability probe (standalone, no class instance needed)
# ---------------------------------------------------------------------------

def is_available(host: str = _DEFAULT_HOST, port: int = _DEFAULT_PORT,
                 timeout: float = 1.0) -> bool:
    """Return ``True`` if Commander is reachable and accepting connections.

    Opens a TCP connection and closes it immediately.  Does not send any
    command — just confirms the port is open.

    Parameters
    ----------
    host : str
        Hostname or IP of the Commander process.
    port : int
        TCP port Commander is listening on.
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


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class TransceiverState:
    """Snapshot of transceiver state as reported by DX Lab Commander.

    Parameters
    ----------
    rx_freq_khz : float
        Receive VFO frequency in kHz (VFO A on most rigs).
    tx_freq_khz : float
        Transmit VFO frequency in kHz.  When split is OFF this equals
        ``rx_freq_khz``; when split is ON this is VFO B.  Commander does
        not expose the inactive VFO when split is OFF.
    mode : str
        Active transceiver mode, e.g. ``'CW'``, ``'USB'``, ``'FT8'``.
    split : bool
        ``True`` when the transceiver is in split mode.
    transmitting : bool
        ``True`` when the transceiver is currently transmitting.  Not all
        rigs report this; ``False`` is the safe fallback.
    error : str
        Non-empty when a query failed (connection refused, timeout, etc.).
        All numeric fields are 0 / empty when this is set.
    """

    rx_freq_khz: float = 0.0
    tx_freq_khz: float = 0.0
    mode: str = ''
    split: bool = False
    transmitting: bool = False
    error: str = ''

    @property
    def rx_freq_mhz(self) -> float:
        """Receive frequency in MHz."""
        return self.rx_freq_khz / 1000.0

    @property
    def tx_freq_mhz(self) -> float:
        """Transmit frequency in MHz."""
        return self.tx_freq_khz / 1000.0

    @property
    def tx_offset_khz(self) -> float:
        """TX–RX offset in kHz (0.0 when not split)."""
        return self.tx_freq_khz - self.rx_freq_khz


@dataclass
class SetResult:
    """Result of a :meth:`~CommanderClient.set_freq_and_mode` call.

    Parameters
    ----------
    success : bool
        ``True`` when all post-set verifications passed.
    intended_freq_khz : float
        The frequency that was requested.
    intended_mode : str
        The mode that was requested.
    actual_state : TransceiverState
        State read back from Commander after the set command.
    errors : list[str]
        List of verification failures; empty when ``success`` is ``True``.
    """

    success: bool
    intended_freq_khz: float
    intended_mode: str
    actual_state: TransceiverState
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CommanderClient:
    """TCP/IP client for DX Lab Suite Commander.

    Each public method opens a fresh TCP connection, sends one command, and
    closes the connection.  Query methods read a response; set methods do not
    (Commander sends no acknowledgement for set commands).

    Use :func:`is_available` to verify the port is open before creating an
    instance in time-critical code.

    Parameters
    ----------
    host : str
        Hostname or IP address of the Commander process.
    port : int
        TCP port Commander is listening on.
    timeout : float
        Per-query receive timeout in seconds.  The budget applies only to
        reading the response — connect and send use the same budget
        independently.  Commander typically replies within a few milliseconds;
        the default of :data:`_TIMEOUT` provides comfortable margin.
    verbose : bool
        When ``True``, print the raw command and response for every query,
        useful for diagnosing unexpected empty or malformed responses.
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

        The ``timeout`` budget is applied only to *receiving* — connect and
        send are given the same budget separately so that neither steals time
        from the recv loop.  Commander closes the connection after every
        response, so an empty ``recv`` is the reliable end-of-response signal.
        """
        buf = b''
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            s.sendall(command.encode('utf-8'))
            # Start recv deadline only now, after connect+send are done.
            deadline = time.monotonic() + self.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                s.settimeout(remaining)
                try:
                    chunk = s.recv(1024)
                    if not chunk:
                        break          # Commander closed connection
                    buf += chunk
                    if b'>' in buf:   # at least one complete ADIF field received
                        break
                except socket.timeout:
                    break
        response = buf.decode('utf-8', errors='replace').strip()
        if self.verbose:
            print(f"  [CMD]  {command!r}")
            print(f"  [RESP] {response!r}")
        return response

    def query_raw(self, command: str) -> str:
        """Send *command* and return the unparsed response string.

        Convenience wrapper around :meth:`_query` for interactive debugging —
        lets you see exactly what Commander sends back before any extraction.
        """
        return self._query(command)

    def _send(self, command: str) -> None:
        """Send a fire-and-forget command (no response expected from Commander)."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(self.timeout)
            s.connect((self.host, self.port))
            s.sendall(command.encode('utf-8'))

    @staticmethod
    def _build(cmd_name: str, params: dict[str, str]) -> str:
        """Build a Commander ADIF command string with correct length prefixes.

        Parameters are passed as an ordered dict of ``{field_name: value}``.
        An empty *params* dict produces ``<parameters:0>``.
        """
        param_str = ''.join(f'<{k}:{len(v)}>{v}' for k, v in params.items())
        return (f'<command:{len(cmd_name)}>{cmd_name}'
                f'<parameters:{len(param_str)}>{param_str}')

    # -- response parsing -----------------------------------------------------

    @staticmethod
    def _extract(response: str, field_name: str) -> str:
        """Return the value for an ADIF-style field in a Commander response.

        Matches ``<field_name:N>value``, case-insensitively.  Returns ``''``
        when the field is not found.
        """
        m = re.search(
            r'<' + re.escape(field_name) + r':\d+>([^<]*)',
            response,
            re.IGNORECASE,
        )
        return m.group(1).strip() if m else ''

    @staticmethod
    def _to_khz(value: str) -> float:
        """Parse a Commander frequency string to kHz.

        Commander may include a comma thousands separator (e.g. ``14,074.000``).
        Returns ``0.0`` for empty or malformed strings.
        """
        try:
            return float(value.replace(',', ''))
        except (ValueError, AttributeError):
            return 0.0

    # -- individual queries ---------------------------------------------------

    def get_rx_freq_khz(self) -> float:
        """Return the current receive (VFO A) frequency in kHz.

        Returns
        -------
        float
            RX frequency in kHz, or ``0.0`` if not yet reported by the rig.
        """
        resp = self._query('<command:10>CmdGetFreq<parameters:0>')
        return self._to_khz(self._extract(resp, 'CmdFreq'))

    def get_tx_freq_khz(self) -> float:
        """Return the current transmit VFO frequency in kHz.

        Returns
        -------
        float
            TX frequency in kHz.  Equals the RX frequency when split is off.
            Returns ``0.0`` if not yet reported by the rig.
        """
        resp = self._query('<command:12>CmdGetTXFreq<parameters:0>')
        return self._to_khz(self._extract(resp, 'CmdTXFreq'))

    def get_vfo_b_khz(self) -> float | None:
        """Return VFO B frequency in kHz, or ``None`` when not available.

        Commander exposes VFO B only when split is ON (it becomes the TX VFO).
        When split is OFF the inactive VFO is not queryable via this API, so
        this method returns ``None`` in that case rather than returning the
        same value as VFO A.

        Returns
        -------
        float or None
            VFO B frequency in kHz, or ``None`` when split is OFF.
        """
        if not self.get_split():
            return None
        return self.get_tx_freq_khz()

    def get_mode(self) -> str:
        """Return the current transceiver mode string.

        Returns
        -------
        str
            Mode such as ``'CW'``, ``'USB'``, ``'LSB'``, ``'RTTY'``, etc.
            Returns ``''`` if not yet reported by the rig.
        """
        resp = self._query('<command:11>CmdSendMode<parameters:0>')
        return self._extract(resp, 'CmdMode')

    def get_split(self) -> bool:
        """Return ``True`` when the transceiver is in split mode.

        Returns
        -------
        bool
            ``True`` if split is ON, ``False`` otherwise.
        """
        resp = self._query('<command:12>CmdSendSplit<parameters:0>')
        return self._extract(resp, 'CmdSplit').upper() == 'ON'

    def get_transmitting(self) -> bool:
        """Return ``True`` when the transceiver is currently transmitting.

        Not all transceivers report TX state.  Returns ``False`` when the rig
        does not support this query.

        Returns
        -------
        bool
            ``True`` if the rig is transmitting, ``False`` otherwise.
        """
        resp = self._query('<command:9>CmdSendTX<parameters:0>')
        return self._extract(resp, 'CmdTX').upper() == 'ON'

    def get_state(self) -> TransceiverState:
        """Query Commander and return a full :class:`TransceiverState` snapshot.

        Issues five sequential queries (RX freq, TX freq, mode, split, TX
        status).  Any ``OSError`` (connection refused, timeout) causes an
        immediate return with ``TransceiverState.error`` set.

        Returns
        -------
        TransceiverState
            Populated state snapshot.  Check ``state.error`` before using
            the numeric fields.
        """
        try:
            return TransceiverState(
                rx_freq_khz=self.get_rx_freq_khz(),
                tx_freq_khz=self.get_tx_freq_khz(),
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
        """Set the transceiver frequency and mode, with split and TX forced off.

        Sequence
        --------
        1. Send ``CmdRX`` to drop out of transmit if active.
        2. Send ``CmdSetFreqMode`` with ``preservesplitanddual:1>N`` to set
           the RX VFO frequency, the mode, and reset split simultaneously.
        3. Wait *verify_delay* seconds for the rig and Commander's poll cycle
           to complete, then read back state once.

        Parameters
        ----------
        freq_khz : float
            Target receive frequency in kHz (e.g. ``14074.0`` for 20 m FT8).
        mode : str
            Target mode string (e.g. ``'CW'``, ``'USB'``).  Must be one of
            :data:`VALID_MODES`; a ``ValueError`` is raised otherwise.
        verify_delay : float, optional
            Seconds to wait before reading back state.  Should cover at least
            one Commander rig-poll cycle.  Default :data:`_VERIFY_DELAY`.
        freq_tol_khz : float, optional
            Maximum acceptable frequency error in kHz.  Default 0.1 kHz.

        Returns
        -------
        SetResult
            ``success`` is ``True`` when the read-back state matches the
            intended frequency and mode and both split and TX are off.
            ``errors`` lists each mismatch on failure.

        Raises
        ------
        ValueError
            If *mode* is not in :data:`VALID_MODES`.
        OSError
            If Commander is unreachable when sending commands.
        """
        mode = mode.upper().strip()
        if mode not in VALID_MODES:
            raise ValueError(
                f"Invalid mode {mode!r}. Valid modes: {sorted(VALID_MODES)}"
            )

        # Drop out of transmit, then set frequency + mode + reset split.
        # preservesplitanddual:1>N = reset split and dual watch (N = No preserve).
        self._send('<command:5>CmdRX<parameters:0>')
        self._send(self._build('CmdSetFreqMode', {
            'xcvrfreq':             f"{freq_khz:.3f}",
            'xcvrmode':             mode,
            'preservesplitanddual': 'N',
        }))

        # Wait for the rig to tune and Commander to complete a poll cycle.
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
                errors.append("Rig is still transmitting after CmdRX")

        return SetResult(
            success=len(errors) == 0,
            intended_freq_khz=freq_khz,
            intended_mode=mode,
            actual_state=actual,
            errors=errors,
        )


# -- entry point --------------------------------------------------------------

def main() -> None:
    """Exercise CommanderClient: probe availability, query state, then set freq/mode."""
    host = _DEFAULT_HOST
    port = _DEFAULT_PORT

    print(f"Checking Commander at {host}:{port} …")
    if not is_available(host, port):
        print(f"  Commander not reachable on {host}:{port} — is it running?")
        return
    print("  Commander is reachable.")

    client = CommanderClient(host=host, port=port)

    # --- current state -------------------------------------------------------
    print("\nCurrent transceiver state:")
    state = client.get_state()
    if state.error:
        print(f"  Error: {state.error}")
        return

    print(f"  VFO A (RX)   : {state.rx_freq_mhz:.3f} MHz  ({state.rx_freq_khz:.3f} kHz)")
    vfo_b = client.get_vfo_b_khz()
    if vfo_b is not None:
        print(f"  VFO B (TX)   : {vfo_b / 1000:.3f} MHz  ({vfo_b:.3f} kHz)")
        print(f"  TX offset    : {state.tx_offset_khz:+.3f} kHz")
    else:
        print("  VFO B (TX)   : not available (split is OFF)")
    print(f"  Mode         : {state.mode}")
    print(f"  Split        : {'ON' if state.split else 'OFF'}")
    print(f"  TX status    : {'TX' if state.transmitting else 'RX'}")

    # --- set frequency and mode ----------------------------------------------
    target_freq_khz = 14042.0
    target_mode = 'CW'

    print(f"\nSetting {target_freq_khz:.3f} kHz / {target_mode} "
          f"(split OFF, TX OFF) …")
    result = client.set_freq_and_mode(target_freq_khz, target_mode)

    if result.success:
        s = result.actual_state
        print(f"  OK — RX {s.rx_freq_khz:.3f} kHz  mode={s.mode}  "
              f"split={'ON' if s.split else 'OFF'}  "
              f"tx={'ON' if s.transmitting else 'OFF'}")
    else:
        print("  FAILED — verification errors:")
        for err in result.errors:
            print(f"    • {err}")
        s = result.actual_state
        if not s.error:
            print(f"  Actual state: RX {s.rx_freq_khz:.3f} kHz  "
                  f"mode={s.mode}  split={'ON' if s.split else 'OFF'}")


if __name__ == '__main__':
    main()
