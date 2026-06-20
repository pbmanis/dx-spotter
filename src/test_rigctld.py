#!/usr/bin/env python3
"""
Test rigctld (Hamlib daemon) on localhost:4532.

rigctld must be running before this script is run, e.g.:
    rigctld -m 351 -r /dev/cu.usbmodemXXXX -s 38400

Usage:
    python test_rigctld.py [port]   # default port 4532
"""

import socket
import sys
import time

HOST     = 'localhost'
PORT     = int(sys.argv[1]) if len(sys.argv) > 1 else 4532
TIMEOUT  = 3.0

# Target: 28 010.20 kHz, CW, 250 Hz passband
FREQ_HZ  = 28_010_200
MODE     = 'CW'
PASSBAND = 250


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def recv_until_rprt(sock: socket.socket, timeout: float = 2.0) -> str:
    """Read until we see 'RPRT' (end-of-response marker) or timeout."""
    buf = ''
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            chunk = sock.recv(1024).decode(errors='replace')
            if not chunk:
                break
            buf += chunk
            if 'RPRT' in buf:
                break
        except socket.timeout:
            break
    return buf.strip()


def cmd(sock: socket.socket, command: str, label: str = '') -> str:
    """Send a rigctld command and return the full response."""
    raw = (command + '\n').encode()
    tag = label or command.strip()
    print(f'  TX [{tag}]: {command!r}')
    sock.sendall(raw)
    reply = recv_until_rprt(sock)
    if reply:
        print(f'  RX        : {reply!r}')
    else:
        print('  RX        : (no response / timeout)')
    return reply


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f'rigctld probe — {HOST}:{PORT}')
    print('─' * 50)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT)
    try:
        sock.connect((HOST, PORT))
    except (ConnectionRefusedError, OSError) as e:
        print(f'\nCould not connect: {e}')
        print('\nIs rigctld running?  Try:')
        print('    rigctld -m 351 -r /dev/cu.usbmodemXXXX -s 38400')
        print('(replace -m 351 with your rig model, -r with your serial port)')
        sys.exit(1)

    print('Connected.\n')

    try:
        print('── Read current state ──────────────────────────')
        cmd(sock, 'f',       'get freq')
        cmd(sock, 'm',       'get mode')
        cmd(sock, '\\dump_caps | head -20', 'caps (first 20 lines)')

        print(f'\n── Set: {FREQ_HZ:,} Hz / {MODE} / {PASSBAND} Hz ─────')
        cmd(sock, f'F {FREQ_HZ}',         'set freq')
        cmd(sock, f'M {MODE} {PASSBAND}', 'set mode+passband')

        print('\n── Readback ─────────────────────────────────────')
        cmd(sock, 'f', 'get freq')
        cmd(sock, 'm', 'get mode')

    finally:
        sock.close()

    print('\nDone.')


if __name__ == '__main__':
    main()
