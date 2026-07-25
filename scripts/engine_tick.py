"""Entrypoint for the scheduled engine tick. Invoked by the systemd timer.

Reads its whole configuration from the environment (see deploy/README.md), runs
one tick, publishes it, and prints one structured line. Exits non-zero only when
the run errored, so `systemctl status heimdall-tick` reflects real failures while
a dormant or health-skipped tick stays green.

    source ~/.heimdall/env && ~/fresh-e2e/v/bin/python scripts/engine_tick.py
"""

from __future__ import annotations

import sys

from heimdall.scheduler import main

if __name__ == "__main__":
    sys.exit(main())
