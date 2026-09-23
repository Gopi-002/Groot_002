"""Entry point: python -m app.monitoring"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.monitoring.probe import HealthProbe
from app.monitoring.service import MonitorService
from app.observability.logging import configure_logging
from app.persistence.db import make_engine
from app.runtime import StopFlag


def main() -> None:
    settings = get_settings()
    configure_logging("sentinel-monitor", settings.log_level)
    stop = StopFlag()
    stop.install_signal_handlers()
    engine = make_engine(settings.database_url)
    with httpx.Client(follow_redirects=False, trust_env=False) as client:
        probe = HealthProbe(
            client,
            settings.demo_app_url.rstrip("/") + "/health",
            timeout_seconds=settings.probe_timeout_seconds,
            latency_threshold_seconds=settings.latency_threshold_seconds,
        )
        MonitorService(settings, engine, probe, stop).run()
    engine.dispose()


if __name__ == "__main__":
    main()
