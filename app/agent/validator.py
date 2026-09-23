"""Deterministic validation of every model-produced investigation result.

Checks (each failure is reported back to the model for a bounded correction):
1. schema (types, bounds, allowlisted action, no extra fields);
2. incident_id equals the task's incident;
3. every cited evidence id is listed in ``evidence_ids`` and every listed id
   exists, belongs to THIS incident, and is fresh;
4. the proposal is consistent (restart must target demo-app);
5. no claims that the AI itself executed/restarted/resolved anything.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import ValidationError
from sqlalchemy import Engine, text

from app.agent.schema import InvestigationResult, ProposedActionType

_ACTION_CLAIM = re.compile(
    r"\b(i|we)\s+(have\s+|already\s+)?(restarted|executed|rebooted|fixed|resolved|remediated"
    r"|redeployed|killed|deleted|modified|changed)\b",
    re.IGNORECASE,
)


@dataclass
class ValidationOutcome:
    result: InvestigationResult | None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.result is not None and not self.errors


def _texts(r: InvestigationResult) -> list[str]:
    out = [o.statement for o in r.observations] + [h.statement for h in r.hypotheses]
    out += [r.proposed_action.rationale, *r.risks, *r.verification_plan, *r.missing_evidence]
    return out


def validate_result(
    engine: Engine,
    payload: dict[str, Any],
    *,
    incident_id: uuid.UUID,
    now: datetime,
    max_age: timedelta,
) -> ValidationOutcome:
    try:
        result = InvestigationResult.model_validate(payload)
    except ValidationError as exc:
        errs = [
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors(include_url=False)[:15]
        ]
        return ValidationOutcome(None, ["schema: " + e for e in errs])

    errors: list[str] = []
    if result.incident_id != incident_id:
        errors.append(f"incident_id must be {incident_id}")

    listed = set(result.evidence_ids)
    unlisted = result.cited_ids() - listed
    if unlisted:
        errors.append(f"cited evidence ids missing from evidence_ids: {sorted(map(str, unlisted))}")

    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT id, incident_id, collected_at FROM evidence "
                    "WHERE id = ANY(CAST(:ids AS uuid[]))"
                ),
                {"ids": [str(i) for i in listed]},
            )
            .mappings()
            .all()
        )
    found = {uuid.UUID(str(r["id"])): r for r in rows}
    for eid in sorted(listed, key=str):
        row = found.get(eid)
        if row is None:
            errors.append(f"evidence {eid} does not exist (never cite ids you did not receive)")
        elif uuid.UUID(str(row["incident_id"])) != incident_id:
            errors.append(f"evidence {eid} belongs to a different incident")
        elif now - row["collected_at"] > max_age:
            errors.append(f"evidence {eid} is stale (collected {row['collected_at'].isoformat()})")

    pa = result.proposed_action
    if pa.action is ProposedActionType.RESTART_DEMO_APP:
        if pa.target_service != "demo-app":
            errors.append("restart_demo_app requires target_service 'demo-app'")
        if not pa.evidence_ids:
            errors.append("a remediation proposal must cite supporting evidence_ids")

    for t in _texts(result):
        if _ACTION_CLAIM.search(t):
            errors.append(
                "statements must not claim actions were performed; you can only propose: " + t[:120]
            )
            break
    return ValidationOutcome(result, errors)
