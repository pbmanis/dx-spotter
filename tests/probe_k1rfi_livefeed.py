"""Live diagnostic: after priming a DX cluster node with a command, does a
LIVE spot feed actually flow, or is the command just a one-shot snapshot?

This is **not** a pytest unit test — it opens a real TCP connection and needs
network access, so it is named ``probe_*`` to keep pytest from collecting it.
Run it by hand::

    python tests/probe_k1rfi_livefeed.py

It logs in, primes with ``sh/dx/3``, then watches for 50 seconds and counts
only spot-shaped lines that arrive AFTER the snapshot block (i.e. after the
post-command prompt) — those are genuine live-feed spots.  Written to confirm
the K1RFI/NN1D node feeds live (just slowly); edit the constants for others.
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


def main() -> None:
    with socket.create_connection((HOST, PORT), timeout=15) as s:
        s.settimeout(1.0)
        buf = b""
        seen_prompt_after_cmd = False
        live_lines = 0

        def readlines(seconds: float, mark_live: bool) -> None:
            nonlocal buf, seen_prompt_after_cmd, live_lines
            end = time.time() + seconds
            while time.time() < end:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    print("server closed")
                    return
                buf += _strip_iac(chunk)
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    txt = line.decode("ascii", "replace").rstrip("\r")
                    if not txt.strip():
                        continue
                    if (
                        mark_live
                        and seen_prompt_after_cmd
                        and (
                            txt.lstrip()[:1].isdigit()
                            or txt.upper().startswith("DX DE")
                        )
                    ):
                        live_lines += 1
                        print(f"[LIVE {time.strftime('%H:%M:%S')}] {txt!r}")
                    if "dxspider >" in txt:
                        seen_prompt_after_cmd = True

        readlines(3, False)
        s.sendall((CALL + "\r\n").encode())
        readlines(4, False)
        s.sendall(b"sh/dx/3\r\n")
        readlines(4, False)
        # seen_prompt_after_cmd is now True; count live spots for 50 seconds.
        print("--- watching 50s for LIVE feed after sh/dx snapshot ---")
        readlines(50, True)
        print(f"LIVE spot lines in 50s: {live_lines}")


if __name__ == "__main__":
    main()
