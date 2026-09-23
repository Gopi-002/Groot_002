"""Typed, validated settings shared by the SentinelOps services
(api, monitor, dispatcher, worker).

Secrets are held as ``SecretStr`` so they never appear in ``repr``/logs.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

_PLACEHOLDERS = {"", "change-me", "changeme", "password", "secret", "postgres"}


class Environment(StrEnum):
    DEVELOPMENT = "development"
    DEMO = "demo"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTINEL_", extra="ignore", frozen=True)

    environment: Environment = Environment.DEVELOPMENT
    log_level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$")

    db_host: str = "postgres"
    db_port: int = Field(default=5432, ge=1, le=65535)
    db_name: str = Field(default="sentinelops", min_length=1)
    db_user: str = Field(default="sentinelops", min_length=1)
    db_password: SecretStr

    redis_host: str = "redis"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_password: SecretStr

    demo_app_url: str = Field(default="http://demo-app:8001", pattern=r"^https?://")
    demo_service_name: str = Field(default="demo-app", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # Read-only status API. Unset => /v1 endpoints fail closed (503).
    api_read_token: SecretStr | None = None

    # Monitor (workflow steps 1-2)
    monitor_interval_seconds: float = Field(default=30.0, ge=1, le=3600)
    probe_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    failure_threshold: int = Field(default=3, ge=1, le=100)
    latency_threshold_seconds: float = Field(default=2.0, gt=0, le=60)
    latency_threshold_count: int = Field(default=3, ge=1, le=100)
    rearm_healthy_checks: int = Field(default=3, ge=1, le=100)
    health_check_retention_days: int = Field(default=7, ge=1, le=365)

    # Queue (workflow step 3)
    stream_prefix: str = Field(default="sentinel", pattern=r"^[a-z0-9:_-]{1,64}$")
    stream_maxlen: int = Field(default=100_000, ge=1_000)
    dispatcher_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    dispatcher_batch_size: int = Field(default=50, ge=1, le=1000)
    publish_retry_base_seconds: float = Field(default=1.0, gt=0, le=60)
    publish_retry_max_seconds: float = Field(default=60.0, gt=0, le=3600)
    redispatch_after_seconds: float = Field(default=300.0, ge=5, le=86_400)

    # Worker
    lease_ttl_seconds: float = Field(default=60.0, ge=2, le=3600)
    heartbeat_seconds: float = Field(default=20.0, gt=0, le=1800)
    task_max_attempts: int = Field(default=3, ge=1, le=20)
    task_retry_base_seconds: float = Field(default=10.0, gt=0, le=3600)
    task_retry_max_seconds: float = Field(default=300.0, gt=0, le=86_400)
    pending_idle_seconds: float = Field(default=90.0, ge=1, le=86_400)
    worker_block_seconds: float = Field(default=5.0, gt=0, le=60)

    # --- AI investigation (Phase 3, workflow steps 4-5) ---------------------
    # "anthropic" = official Anthropic SDK (API-key auth). "mock" = deterministic
    # TEST/DEMO model, never Claude; rejected in production.
    ai_gateway: Literal["anthropic", "mock"] = "anthropic"
    # The API key is read from this file (dedicated Docker volume / secrets
    # manager mount), never from the database or container environment.
    anthropic_api_key_file: Path = Path("/var/lib/sentinel-secrets/anthropic_api_key")
    anthropic_base_url: str = Field(default="https://api.anthropic.com", pattern=r"^https?://")
    ai_request_timeout_seconds: float = Field(default=120.0, ge=5, le=900)
    ai_sdk_max_retries: int = Field(default=2, ge=0, le=5)
    ai_max_output_tokens_per_call: int = Field(default=16_000, ge=1_024, le=64_000)
    # Per-investigation execution budgets, enforced by the orchestrator.
    ai_max_tool_calls: int = Field(default=6, ge=1, le=20)
    ai_max_reasoning_attempts: int = Field(default=3, ge=1, le=5)
    ai_investigation_timeout_seconds: float = Field(default=600.0, ge=10, le=3600)
    ai_max_total_tokens: int = Field(default=300_000, ge=10_000, le=5_000_000)
    # Optional cost budget. Prices are operator-supplied (per million tokens) and
    # are NOT hardcoded; if a cost cap is set, both prices are required.
    ai_input_usd_per_mtok: float | None = Field(default=None, ge=0)
    ai_output_usd_per_mtok: float | None = Field(default=None, ge=0)
    ai_max_cost_usd: float | None = Field(default=None, gt=0)
    # Cited evidence must be at most this old when the result is validated.
    ai_evidence_max_age_seconds: float = Field(default=3600.0, ge=60, le=86_400)
    # How long AI work pauses after an auth/quota failure before re-checking.
    ai_pause_seconds: float = Field(default=300.0, ge=5, le=86_400)

    # --- Remediation policy (Phase 4, workflow steps 6-8) --------------------
    # Autonomous execution is OFF by default; an operator must enable it
    # explicitly AND declare the isolated demo environment. Production can
    # never execute a restart (see policy rule ENV-1).
    remediation_auto_enabled: bool = False
    remediation_approval_enabled: bool = True
    remediation_environment: Literal["isolated-demo"] | None = None
    # Trusted target: never taken from model output.
    remediation_target_service: str = Field(
        default="demo-app", pattern=r"^[a-z0-9][a-z0-9-]{0,62}$"
    )
    remediation_max_restarts_per_incident: int = Field(default=1, ge=0, le=1)
    remediation_max_restarts_per_hour: int = Field(default=3, ge=0, le=20)
    # The failing state must be confirmed by a health check at most this old,
    # immediately before the side effect (no restarting a healthy app).
    policy_health_freshness_seconds: float = Field(default=120.0, ge=5, le=3600)
    approval_ttl_seconds: float = Field(default=900.0, ge=30, le=86_400)
    approval_signing_key: SecretStr | None = None

    # Restricted remediation executor (the ONLY state-changing component).
    executor_url: str = Field(default="http://executor:8003", pattern=r"^https?://")
    executor_token: SecretStr | None = None
    action_signing_key: SecretStr | None = None
    executor_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    action_authorization_ttl_seconds: float = Field(default=60.0, ge=5, le=600)

    # Deterministic recovery verification.
    verify_readiness_deadline_seconds: float = Field(default=120.0, ge=5, le=1800)
    verify_consecutive_successes: int = Field(default=3, ge=1, le=20)
    verify_latency_max_seconds: float = Field(default=2.0, gt=0, le=30)
    verify_probe_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    verify_error_levels: tuple[str, ...] = ("ERROR", "CRITICAL")

    # Restricted read-only operations service (container status/logs/stats).
    ops_reader_url: str = Field(default="http://ops-reader:8002", pattern=r"^https?://")
    ops_reader_token: SecretStr | None = None
    ops_reader_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    # --- Durable AI budgets and concurrency (Phase 5, cost factor) -----------
    # Budgets are enforced from the ai_usage ledger before every model call, for
    # BOTH AI stages (investigation and report). None = no ceiling of that kind.
    ai_daily_max_tokens: int | None = Field(default=None, ge=1_000)
    ai_daily_max_cost_usd: float | None = Field(default=None, gt=0)
    ai_incident_max_tokens: int | None = Field(default=600_000, ge=10_000)
    ai_incident_max_cost_usd: float | None = Field(default=None, gt=0)
    ai_max_concurrent_jobs: int = Field(default=2, ge=1, le=16)

    # --- Reporting (Phase 5, workflow step 9) ---------------------------------
    report_ai_enabled: bool = True
    report_max_attempts: int = Field(default=3, ge=1, le=5)  # AI draft + corrections
    report_job_max_attempts: int = Field(default=4, ge=2, le=10)  # last one is deterministic
    report_timeout_seconds: float = Field(default=180.0, ge=10, le=1800)
    report_max_total_tokens: int = Field(default=150_000, ge=5_000, le=2_000_000)

    # --- Notifications (Phase 5) ------------------------------------------------
    # Channels: log (always safe) and/or webhook (one trusted, allowlisted URL).
    notify_channels: Annotated[tuple[Literal["log", "webhook"], ...], NoDecode] = ("log",)
    notify_webhook_url: str | None = None
    notify_webhook_allowed_hosts: Annotated[tuple[str, ...], NoDecode] = ()
    notify_webhook_secret: SecretStr | None = None
    notify_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    notify_max_response_bytes: int = Field(default=65_536, ge=1_024, le=1_048_576)
    notify_max_attempts: int = Field(default=6, ge=1, le=20)
    notify_retry_base_seconds: float = Field(default=5.0, gt=0, le=3600)
    notify_retry_max_seconds: float = Field(default=600.0, gt=0, le=86_400)
    notify_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    notify_retention_days: int = Field(default=30, ge=1, le=3650)

    # --- Deterministic alerts (evaluated by the notifier) ----------------------
    alert_eval_seconds: float = Field(default=30.0, ge=1, le=3600)
    alert_repeat_seconds: float = Field(default=3600.0, ge=60, le=86_400)
    alert_monitor_silence_seconds: float = Field(default=120.0, ge=10, le=86_400)
    alert_queue_backlog_seconds: float = Field(default=120.0, ge=10, le=86_400)
    alert_stalled_incident_seconds: float = Field(default=3600.0, ge=60, le=604_800)
    alert_notification_backlog_seconds: float = Field(default=900.0, ge=30, le=86_400)
    alert_service_silence_seconds: float = Field(default=120.0, ge=10, le=86_400)
    # None = backups are not expected (dev); set on the VM (e.g. 26 for daily backups).
    backup_max_age_hours: float | None = Field(default=None, gt=0, le=24 * 60)

    # TEST/DEMO ONLY: widen the reporting crash window for resilience tests.
    test_report_delay_seconds: float = Field(default=0.0, ge=0, le=120)

    @field_validator("notify_channels", "notify_webhook_allowed_hosts", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return tuple(x.strip() for x in v.split(",") if x.strip())
        return v

    @field_validator(
        "notify_webhook_url",
        "notify_webhook_secret",
        "ai_input_usd_per_mtok",
        "ai_output_usd_per_mtok",
        "ai_max_cost_usd",
        "ai_daily_max_tokens",
        "ai_daily_max_cost_usd",
        "ai_incident_max_cost_usd",
        "backup_max_age_hours",
        mode="before",
    )
    @classmethod
    def _empty_is_none(cls, v: object) -> object:
        # compose passes unset optional values as "" (${VAR:-})
        return None if v == "" else v

    @model_validator(mode="after")
    def _phase5_invariants(self) -> Settings:
        from app.notifications.channels import InvalidWebhookUrl, validate_webhook_url

        if "webhook" in self.notify_channels:
            if not self.notify_webhook_url:
                raise ValueError("notify_channels includes webhook but notify_webhook_url is unset")
            try:
                validate_webhook_url(
                    self.notify_webhook_url,
                    self.notify_webhook_allowed_hosts,
                    self.environment.value,
                )
            except InvalidWebhookUrl as exc:
                raise ValueError(f"notify_webhook_url rejected: {exc}") from exc
            if self.environment is Environment.PRODUCTION and self.notify_webhook_secret is None:
                raise ValueError("a webhook signing secret is required in production")
        if (
            self.notify_webhook_secret is not None
            and len(self.notify_webhook_secret.get_secret_value()) < 32
        ):
            raise ValueError("notify_webhook_secret must be at least 32 characters")
        priced = self.ai_input_usd_per_mtok is not None and self.ai_output_usd_per_mtok is not None
        if (self.ai_daily_max_cost_usd or self.ai_incident_max_cost_usd) and not priced:
            raise ValueError(
                "AI cost ceilings require ai_input_usd_per_mtok/ai_output_usd_per_mtok"
            )
        if self.notify_retry_base_seconds > self.notify_retry_max_seconds:
            raise ValueError("notify_retry_base_seconds must be <= notify_retry_max_seconds")
        if self.environment is Environment.PRODUCTION and self.test_report_delay_seconds:
            raise ValueError("test hooks are forbidden in production")
        return self

    @model_validator(mode="after")
    def _reject_weak_secrets(self) -> Settings:
        for name in ("db_password", "redis_password"):
            value: SecretStr = getattr(self, name)
            raw = value.get_secret_value()
            if raw.lower() in _PLACEHOLDERS:
                raise ValueError(f"{name} is empty or a placeholder")
            if self.environment is Environment.PRODUCTION and len(raw) < 16:
                raise ValueError(f"{name} must be at least 16 characters in production")
        if self.api_read_token is not None and len(self.api_read_token.get_secret_value()) < 32:
            raise ValueError("api_read_token must be at least 32 characters")
        if self.ops_reader_token is not None and len(self.ops_reader_token.get_secret_value()) < 32:
            raise ValueError("ops_reader_token must be at least 32 characters")
        for name in ("executor_token", "action_signing_key", "approval_signing_key"):
            val: SecretStr | None = getattr(self, name)
            if val is not None and len(val.get_secret_value()) < 32:
                raise ValueError(f"{name} must be at least 32 characters")
        return self

    @field_validator("remediation_environment", mode="before")
    @classmethod
    def _empty_is_undeclared(cls, v: object) -> object:
        return None if v == "" else v

    @model_validator(mode="after")
    def _remediation_invariants(self) -> Settings:
        if self.environment is Environment.PRODUCTION and self.remediation_auto_enabled:
            raise ValueError("autonomous remediation cannot be enabled in production")
        if self.remediation_auto_enabled and self.remediation_environment != "isolated-demo":
            raise ValueError(
                "remediation_auto_enabled requires remediation_environment='isolated-demo'"
            )
        return self

    @model_validator(mode="after")
    def _ai_invariants(self) -> Settings:
        if self.ai_gateway == "mock" and self.environment is Environment.PRODUCTION:
            raise ValueError("the mock AI gateway is test/demo-only and forbidden in production")
        if self.ai_max_cost_usd is not None and (
            self.ai_input_usd_per_mtok is None or self.ai_output_usd_per_mtok is None
        ):
            raise ValueError(
                "ai_max_cost_usd requires ai_input_usd_per_mtok and ai_output_usd_per_mtok"
            )
        if self.ai_request_timeout_seconds > self.ai_investigation_timeout_seconds:
            raise ValueError(
                "ai_request_timeout_seconds must be <= ai_investigation_timeout_seconds"
            )
        return self

    @model_validator(mode="after")
    def _timing_invariants(self) -> Settings:
        if self.probe_timeout_seconds >= self.monitor_interval_seconds:
            raise ValueError("probe_timeout_seconds must be < monitor_interval_seconds")
        if self.latency_threshold_seconds >= self.probe_timeout_seconds:
            raise ValueError("latency_threshold_seconds must be < probe_timeout_seconds")
        if self.heartbeat_seconds * 2 > self.lease_ttl_seconds:
            raise ValueError("heartbeat_seconds must be <= lease_ttl_seconds / 2")
        if self.pending_idle_seconds <= self.lease_ttl_seconds:
            raise ValueError("pending_idle_seconds must be > lease_ttl_seconds")
        if self.task_retry_base_seconds > self.task_retry_max_seconds:
            raise ValueError("task_retry_base_seconds must be <= task_retry_max_seconds")
        if self.publish_retry_base_seconds > self.publish_retry_max_seconds:
            raise ValueError("publish_retry_base_seconds must be <= publish_retry_max_seconds")
        return self

    @property
    def database_url(self) -> str:
        pw = quote(self.db_password.get_secret_value(), safe="")
        user = quote(self.db_user, safe="")
        return f"postgresql+psycopg://{user}:{pw}@{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # populated from environment
