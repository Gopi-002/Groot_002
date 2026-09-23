"""AI usage accounting, budgets and concurrency (cost factor).

* Every model call - success or typed failure - is written to ``ai_usage`` with
  the provider-reported token usage (``Usage`` from the SDK response), latency
  and the provider request id. Nothing is estimated when the provider reports
  it; nothing is invented when it does not.
* Cost is an ESTIMATE, computed only when the operator configured prices
  (``SENTINEL_AI_INPUT_USD_PER_MTOK`` / ``..._OUTPUT_...``). No provider price
  is hardcoded. Conservative: cache reads/writes are charged at the input price.
* Budgets are checked BEFORE each call from the durable ledger, so they hold
  across crashes, restarts and both AI stages (investigation and report):
  daily (UTC day) tokens/cost, and per-incident tokens/cost.
* ``ai_job_slots`` caps concurrently running AI jobs across all workers with
  leased, expiring slots (a crashed holder frees its slot at expiry).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, text

from app.agent.gateway import GatewayError, InvokeRequest, ModelGateway, ModelTurn, Usage
from app.config import Settings

log = logging.getLogger("sentinelops.ai_usage")


@dataclass(frozen=True)
class CallMeta:
    stage: str  # investigation | report
    incident_id: uuid.UUID | None
    model_id: str
    auth_mode: str
    task_id: uuid.UUID | None = None
    report_job_id: uuid.UUID | None = None


def estimate_cost(settings: Settings, usage: Usage) -> float | None:
    """Operator-priced estimate, or None when prices are not configured."""
    if settings.ai_input_usd_per_mtok is None or settings.ai_output_usd_per_mtok is None:
        return None
    billable_in = (
        usage.input_tokens + usage.cache_read_input_tokens + usage.cache_creation_input_tokens
    )
    return round(
        billable_in * settings.ai_input_usd_per_mtok / 1e6
        + usage.output_tokens * settings.ai_output_usd_per_mtok / 1e6,
        6,
    )


def record_usage(
    engine: Engine,
    settings: Settings,
    meta: CallMeta,
    *,
    outcome: str,
    usage: Usage,
    latency_ms: float,
    request_id: str | None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_usage (stage, incident_id, task_id, report_job_id, model_id, "
                "auth_mode, outcome, input_tokens, output_tokens, cache_read_tokens, "
                "cache_write_tokens, latency_ms, cost_usd_estimate, provider_request_id) "
                "VALUES (:st, :i, :t, :r, :m, :a, :o, :it, :ot, :cr, :cw, :l, :c, :rid)"
            ),
            {
                "st": meta.stage,
                "i": meta.incident_id,
                "t": meta.task_id,
                "r": meta.report_job_id,
                "m": meta.model_id,
                "a": meta.auth_mode,
                "o": outcome,
                "it": usage.input_tokens,
                "ot": usage.output_tokens,
                "cr": usage.cache_read_input_tokens,
                "cw": usage.cache_creation_input_tokens,
                "l": round(latency_ms, 1),
                "c": estimate_cost(settings, usage),
                "rid": (request_id or None) and str(request_id)[:120],
            },
        )


def metered_invoke(
    engine: Engine,
    settings: Settings,
    gateway: ModelGateway,
    request: InvokeRequest,
    meta: CallMeta,
) -> ModelTurn:
    """The ONLY way AI stages call a model: invoke + durable usage record."""
    started = time.monotonic()
    try:
        turn = gateway.invoke(request)
    except GatewayError as exc:
        record_usage(
            engine,
            settings,
            meta,
            outcome=exc.kind,
            usage=Usage(),
            latency_ms=(time.monotonic() - started) * 1000,
            request_id=exc.request_id,
        )
        raise
    record_usage(
        engine,
        settings,
        meta,
        outcome="ok",
        usage=turn.usage,
        latency_ms=(time.monotonic() - started) * 1000,
        request_id=turn.request_id,
    )
    return turn


# --- budgets ------------------------------------------------------------------------------


class AiBudgetExceeded(Exception):
    """A durable AI budget is spent. ``scope`` is 'daily' (pause AI until the next
    UTC day) or 'incident' (no more AI for this incident)."""

    def __init__(self, scope: str, message: str, resume_in_seconds: float | None) -> None:
        super().__init__(message)
        self.scope = scope
        self.resume_in_seconds = resume_in_seconds


@dataclass(frozen=True)
class BudgetUsage:
    daily_tokens: int
    daily_cost_usd: float
    incident_tokens: int
    incident_cost_usd: float


def seconds_until_next_utc_day(now: datetime) -> float:
    nxt = (now.astimezone(UTC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1.0, (nxt - now).total_seconds())


def budget_usage(engine: Engine, incident_id: uuid.UUID | None) -> BudgetUsage:
    tokens = "input_tokens + output_tokens + cache_read_tokens + cache_write_tokens"
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"SELECT COALESCE(sum({tokens}) FILTER (WHERE occurred_at >= "  # noqa: S608 - constant
                "  date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS dt, "
                "COALESCE(sum(cost_usd_estimate) FILTER (WHERE occurred_at >= "
                "  date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'), 0) AS dc, "
                f"COALESCE(sum({tokens}) FILTER (WHERE incident_id = :i), 0) AS it, "
                "COALESCE(sum(cost_usd_estimate) FILTER (WHERE incident_id = :i), 0) AS ic "
                "FROM ai_usage WHERE occurred_at >= now() - interval '2 days' "
                "OR incident_id = :i"
            ),
            {"i": incident_id},
        ).one()
    return BudgetUsage(int(row.dt), float(row.dc), int(row.it), float(row.ic))


def check_budgets(
    engine: Engine, settings: Settings, incident_id: uuid.UUID | None, now: datetime | None = None
) -> BudgetUsage:
    """Raise ``AiBudgetExceeded`` if any configured durable budget is spent."""
    now = now or datetime.now(UTC)
    u = budget_usage(engine, incident_id)
    if settings.ai_daily_max_tokens is not None and u.daily_tokens >= settings.ai_daily_max_tokens:
        raise AiBudgetExceeded(
            "daily",
            f"daily AI token budget spent ({u.daily_tokens}/{settings.ai_daily_max_tokens})",
            seconds_until_next_utc_day(now),
        )
    if (
        settings.ai_daily_max_cost_usd is not None
        and u.daily_cost_usd >= settings.ai_daily_max_cost_usd
    ):
        raise AiBudgetExceeded(
            "daily",
            f"daily AI cost budget spent (estimate {u.daily_cost_usd:.4f} USD)",
            seconds_until_next_utc_day(now),
        )
    if (
        settings.ai_incident_max_tokens is not None
        and u.incident_tokens >= settings.ai_incident_max_tokens
    ):
        raise AiBudgetExceeded(
            "incident",
            f"per-incident AI token budget spent ({u.incident_tokens}/"
            f"{settings.ai_incident_max_tokens})",
            None,
        )
    if (
        settings.ai_incident_max_cost_usd is not None
        and u.incident_cost_usd >= settings.ai_incident_max_cost_usd
    ):
        raise AiBudgetExceeded(
            "incident",
            f"per-incident AI cost budget spent (estimate {u.incident_cost_usd:.4f} USD)",
            None,
        )
    return u


# --- concurrency slots ----------------------------------------------------------------------


def acquire_slot(engine: Engine, holder: str, max_slots: int, ttl_seconds: float) -> int | None:
    """Lease one of ``max_slots`` global AI job slots, or None if all are busy."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_job_slots (slot) SELECT g FROM generate_series(1, :n) g "
                "ON CONFLICT (slot) DO NOTHING"
            ),
            {"n": max_slots},
        )
        slot = conn.execute(
            text(
                "UPDATE ai_job_slots SET holder=:h, acquired_at=now(), "
                "expires_at=now() + make_interval(secs => :ttl) "
                "WHERE slot = (SELECT slot FROM ai_job_slots WHERE slot <= :n AND "
                "  (holder IS NULL OR expires_at < now() OR holder = :h) "
                "  ORDER BY slot LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING slot"
            ),
            {"h": holder, "n": max_slots, "ttl": ttl_seconds},
        ).scalar_one_or_none()
    return int(slot) if slot is not None else None


def release_slot(engine: Engine, holder: str) -> None:
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE ai_job_slots SET holder=NULL, acquired_at=NULL, expires_at=NULL "
                    "WHERE holder=:h"
                ),
                {"h": holder},
            )
    except Exception as exc:  # slot expires on its own; never mask the job outcome
        log.warning("AI slot release failed", extra={"error_type": type(exc).__name__})
