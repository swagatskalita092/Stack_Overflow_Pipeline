"""Run inside the scheduler container: SIGKILL PIDs whose cmdline contains argv[1]."""

from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: _kill_in_container.py PATTERN", file=sys.stderr)
        return 2
    pattern = sys.argv[1]
    hits = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        pid = int(name)
        if pid <= 1:
            continue
        try:
            raw = open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\x00", b" ")
            cmd = raw.decode("utf-8", "replace").strip()
        except OSError:
            continue
        if pattern in cmd:
            hits.append((pid, cmd))
    print(f"HITS {len(hits)} pattern={pattern!r}")
    for pid, cmd in hits:
        print(f"MATCH {pid} {cmd[:240]}")
        try:
            os.kill(pid, 9)
            print(f"KILLED {pid}")
        except OSError as exc:
            print(f"KILLFAIL {pid} {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
