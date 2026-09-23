"""Container health check for non-HTTP services: fail if the heartbeat is stale.

usage: python -m app.healthcheck <service> <max_age_seconds>
"""

from __future__ import annotations

import sys

from app.runtime import heartbeat_age


def main(argv: list[str]) -> int:
    service, max_age = argv[1], float(argv[2])
    try:
        return 0 if heartbeat_age(service) <= max_age else 1
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
