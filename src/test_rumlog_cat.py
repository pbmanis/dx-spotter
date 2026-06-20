#!/usr/bin/env python3
"""
Probe RumLogNG's network interface — protocol sniffer + CAT test.

Phase 1 (sniff): connect, listen for a banner, then try several protocol
                 variants to identify what the port speaks.
Phase 2 (CAT):  if a working variant is found, set 28 010.20 kHz / CW / 250 Hz.

Usage:
    python test_rumlog_cat.py [port]     # auto-scan if no port given
"""

import socket
import sys
import time

HOST      = 'localhost'
TIMEOUT   = 2.0          # seconds per recv attempt
PORTS     = [7374, 4711, 4712, 4713, 12060, 52000, 51000]

FREQ_HZ   = 28_010_200   # 28 010.20 kHz
MODE_CW   = '3'          # Kenwood MD: 3 = CW
FILTER_HZ = 250


# ---------------------------------------------------------------------------
# Raw recv helper — reads whatever arrives within the timeout
# ---------------------------------------------------------------------------

def recv_raw(sock: socket.socket, timeout: float = 1.0, max_bytes: int = 2048) -> bytes:
    """Read all bytes that arrive within *timeout* seconds."""
    buf = b''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            chunk = sock.recv(max_bytes)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            break
    return buf


def send_recv(sock: socket.socket, data: bytes, label: str,
              timeout: float = 1.0) -> bytes:
    print(f"  TX ({label}): {data!r}")
    sock.sendall(data)
    reply = recv_raw(sock, timeout)
    if reply:
        print(f"  RX          : {reply!r}")
        try:
            print(f"  RX (text)   : {reply.decode(errors='replace')!r}")
        except Exception:
            pass
    else:
        print("  RX          : (no response)")
    return reply


# ---------------------------------------------------------------------------
# Phase 1 — sniff the protocol on the connected port
# ---------------------------------------------------------------------------

def sniff(sock: socket.socket) -> str | None:
    """
    Try to identify what protocol the port speaks.
    Returns one of: 'kenwood', 'http', 'telnet', 'unknown', or None if silent.
    """
    print('\n── Phase 1: listen for banner (1 s) ───────────')
    banner = recv_raw(sock, timeout=1.0)
    if banner:
        print(f"  Banner: {banner!r}")
        if banner.startswith(b'HTTP') or b'HTTP/' in banner:
            return 'http'
        if banner[0:1] == b'\xff':          # IAC = telnet negotiation
            return 'telnet'
    else:
        print("  (silent — server waits for client to speak first)")

    print('\n── Trying Kenwood CAT (no line ending) ─────────')
    r = send_recv(sock, b'ID;', 'Kenwood bare')
    if r and b';' in r:
        return 'kenwood'

    print('\n── Trying Kenwood CAT + CR+LF ──────────────────')
    r = send_recv(sock, b'ID;\r\n', 'Kenwood CRLF')
    if r and b';' in r:
        return 'kenwood_crlf'

    print('\n── Trying Kenwood CAT + CR ─────────────────────')
    r = send_recv(sock, b'ID;\r', 'Kenwood CR')
    if r and b';' in r:
        return 'kenwood_cr'

    print('\n── Trying HTTP GET / ───────────────────────────')
    r = send_recv(sock, b'GET / HTTP/1.0\r\n\r\n', 'HTTP GET')
    if b'HTTP' in r or b'html' in r.lower():
        return 'http'

    print('\n── Trying plain newline (Logger32 / N1MM style) ')
    r = send_recv(sock, b'\r\n', 'newline')
    if r:
        return 'unknown_responds_to_newline'

    return None


# ---------------------------------------------------------------------------
# Phase 2 — send the actual CAT commands once we know the framing
# ---------------------------------------------------------------------------

def cat_set(sock: socket.socket, suffix: bytes) -> None:
    def tx(data: str, wait: float = 0.1) -> bytes:
        raw = data.encode() + suffix
        print(f"  TX: {raw!r}")
        sock.sendall(raw)
        time.sleep(wait)
        r = recv_raw(sock, timeout=0.3)
        if r:
            print(f"  RX: {r!r}")
        return r

    print('\n── Phase 2: read current state ─────────────────')
    tx('ID;')
    tx('FA;')
    tx('MD;')
    tx('FW;')

    print(f'\n── Setting: {FREQ_HZ:,} Hz / CW / {FILTER_HZ} Hz ────')
    tx(f'FA{FREQ_HZ:011d};', wait=0.15)
    tx(f'MD{MODE_CW};',      wait=0.10)
    # K3: FW value in Hz (4 digits).  If this has no effect try FW0025 (10-Hz steps).
    tx(f'FW{FILTER_HZ:04d};', wait=0.15)

    print('\n── Readback ─────────────────────────────────────')
    tx('FA;')
    tx('MD;')
    tx('FW;')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def try_connect(port: int) -> socket.socket | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(TIMEOUT)
    try:
        s.connect((HOST, port))
        return s
    except (ConnectionRefusedError, OSError):
        return None


def main() -> None:
    ports = [int(sys.argv[1])] if len(sys.argv) > 1 else PORTS

    print('RumLogNG network interface probe')
    print(f'Host: {HOST}   Ports: {ports}\n')

    sock = None
    connected_port = None
    for port in ports:
        print(f'Trying {port} ...', end=' ', flush=True)
        sock = try_connect(port)
        if sock:
            print('CONNECTED')
            connected_port = port
            break
        print('refused')

    if sock is None:
        print('\nNo open port found. Check RumLogNG preferences.')
        sys.exit(1)

    print(f'\nConnected to {HOST}:{connected_port}')

    try:
        proto = sniff(sock)
        print(f'\n── Protocol guess: {proto!r} ─────────────────────')

        if proto in ('kenwood', 'kenwood_crlf', 'kenwood_cr', None):
            suffix = {
                'kenwood':      b'',
                'kenwood_crlf': b'\r\n',
                'kenwood_cr':   b'\r',
            }.get(proto, b'')    # None (silent) → try bare first
            cat_set(sock, suffix)
        elif proto == 'http':
            print('\nPort speaks HTTP — this is likely RumLogNG\'s web UI.')
            print('Try: http://localhost:{connected_port}/ in a browser.')
            print('Raw Kenwood CAT won\'t work here; look for a separate CAT port.')
        else:
            print('\nUnknown protocol. Raw bytes above may give clues.')
            print('Try opening the port in a terminal:')
            print(f'    nc localhost {connected_port}')
            print('and type  ID;  followed by Enter to see if it responds.')
    finally:
        sock.close()

    print('\nDone.')


if __name__ == '__main__':
    main()
