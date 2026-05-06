#!/usr/bin/env python3
"""Legacy no-op hook kept for older deployment scripts.

Older grok-register images patched a removed grok2api image streaming module at
container startup.  Current grok2api no longer has that file layout; keeping this
script successful avoids breaking users who still mount/call it.
"""

from __future__ import annotations


def main() -> int:
    print("[patch] skipped: current grok2api does not require the legacy streaming patch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
