"""Live diagnostic: does a DX cluster node stream spots on its own, or does it
need a command (e.g. ``sh/dx``) to start?

This is **not** a pytest unit test — it opens a real TCP connection and needs
network access, so it is named ``probe_*`` to keep pytest from collecting it.
Run it by hand::

    python tests/probe_k1rfi.py

It logs in, waits, sends ``sh/dx/30`` to pull recent spots, and prints every
line with a timestamp so you can see whether live spots arrive before the
command, only after it, or not at all.  Originally written to diagnose the
K1RFI/NN1D node; edit ``HOST``/``PORT``/``CALL`` to probe a different node.
"""

from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

from telnet_cluster import _strip_iac  # noqa: E402

HOST, PORT, CALL = "K1RFI.com", 7300, "NC3G"


def _log(tag: str, msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {tag}: {msg}")


def main() -> None:
    with socket.create_connection((HOST, PORT), timeout=15) as s:
        s.settimeout(2.0)
        _log("CONN", f"{HOST}:{PORT}")
        buf = b""

        def drain(seconds: float) -> int:
            nonlocal buf
            end = time.time() + seconds
            n = 0
            while time.time() < end:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    _log("CLOSE", "server closed")
                    return n
                buf += _strip_iac(chunk)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    txt = line.decode("ascii", "replace").rstrip("\r")
                    if txt.strip():
                        n += 1
                        _log("RX", repr(txt))
            return n

        drain(3)
        _log("TX", f"login {CALL}")
        s.sendall((CALL + "\r\n").encode())
        n1 = drain(20)
        _log("STAT", f"lines in 20s after login (before any command): {n1}")
        _log("TX", "sh/dx/30")
        s.sendall(b"sh/dx/30\r\n")
        n2 = drain(15)
        _log("STAT", f"lines in 15s after sh/dx/30: {n2}")


if __name__ == "__main__":
    main()
