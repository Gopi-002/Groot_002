"""Deterministic validation of an AI report draft against the canonical record.

Structured claims must equal the record EXACTLY (both directions: nothing
invented, nothing omitted): incident identity and detection time, every
policy decision, approval (incl. the deciding operator), action attempt and
verification with its status, the proposed action, the final outcome, and
the investigation model. Every cited id must exist in THIS incident's record;
timeline timestamps must match a timestamp of a cited record.

Model prose is then checked clause by clause for unsupported assertions:
  * execution claims ("restarted ...")      need an executed action record;
  * recovery claims ("recovered ...")       need passed verification or a
                                            monitor-confirmed auto-recovery;
  * root-cause claims                       must be hedged - the evidence
                                            contract never confirms a root cause;
  * approval / policy / verification claims need the matching record;
  * "approved by <name>"                    must name an actual decider;
  * Claude/Anthropic model claims           need a real (non-mock) Claude call;
  * timestamps, costs and token figures     are not allowed in prose (they are
                                            rendered from records only).
Negated clauses ("was not restarted") and questions are not claims.
These heuristics are a second line: the published facts sections are always
rendered from the record, never from the draft.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError

from app.reporting.record import IncidentRecord
from app.reporting.schema import ReportDraft

TIME_TOLERANCE = timedelta(seconds=2)
# evidence ids not in this record -> {id: owning incident id} for ids that exist elsewhere
ForeignLookup = Callable[[set[uuid.UUID]], dict[uuid.UUID, uuid.UUID]]

_I = re.IGNORECASE
_NEG = re.compile(
    r"\b(no|not|never|without|neither|nor|none|cannot|unable to|failed to|could not|did not|"
    r"was not|were not|has not|have not|is not)\b|n't\b",
    _I,
)
_HEDGE = re.compile(
    r"\b(likely|possibl[ey]|may|might|could|suspect(ed|s)?|hypothes\w*|unconfirmed|"
    r"not (?:been )?confirmed|appears?|seems?|perhaps|probabl[ey]|potential(?:ly)?|candidate|"
    r"unknown|undetermined|unverified)\b",
    _I,
)
_CLAUSE_SPLIT = re.compile(
    r"(?<=[.!;:,])\s+|\s+(?=\b(?:but|although|however|while|whereas|though)\b)", _I
)

EXECUTION = re.compile(
    r"\b(restarted|rebooted|re-started|restart (?:was|has been) (?:performed|executed|completed|"
    r"carried out|done)|(?:executed|performed|carried out|completed) (?:a|the|one) restart|"
    r"remediation (?:was|has been) (?:performed|executed|applied))\b",
    _I,
)
RECOVERY = re.compile(
    r"\b(recovered|recovery (?:was |has been )?(?:confirmed|verified|successful|achieved|"
    r"complete)|(?:is|was|has been|were) (?:resolved|restored|fixed|healthy again)|back to "
    r"normal|healthy again|successfully remediated|remediated successfully)\b",
    _I,
)
ROOT_CAUSE = re.compile(
    r"\b(root[- ]cause|caused by|(?:was|is|were) due to|because of|the cause (?:was|is|of)|"
    r"caused the (?:outage|incident|failure|errors?))\b",
    _I,
)
APPROVAL = re.compile(
    r"\b(approved|approval (?:was |has been )?(?:granted|given|obtained)|human[- ]approved)\b", _I
)
POLICY_ALLOW = re.compile(r"\bpolicy(?: engine)? (?:allowed|permitted|authori[sz]ed)\b", _I)
POLICY_DENY = re.compile(
    r"\bpolicy(?: engine)? (?:denied|blocked|refused|rejected)\b|\bdenied by (?:the )?policy\b",
    _I,
)
POLICY_APPROVAL = re.compile(r"\b(?:required|requires|requiring) (?:a )?(?:human )?approval\b", _I)
VERIFY_PASS = re.compile(
    r"\bverification (?:passed|succeeded|was successful|confirmed recovery)\b", _I
)
VERIFY_FAIL = re.compile(r"\bverification (?:failed|did not pass)\b", _I)
DECIDED_BY = re.compile(
    r"\b(?:approved|rejected|authori[sz]ed|decided|signed off)\s+by\s+(?:the\s+|an?\s+)?"
    r"(?:operator\s+|user\s+|human\s+)?[\"'`]?([A-Za-z0-9][A-Za-z0-9._@-]{1,63})",
    _I,
)
GENERIC_ACTORS = {
    "policy",
    "sentinelops",
    "operator",
    "human",
    "approver",
    "system",
    "deterministic",
    "a",
    "an",
    "the",
}
CLAUDE = re.compile(r"\b(claude|anthropic|opus|sonnet|haiku)\b", _I)
TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2})?|\b\d{1,2}:\d{2}(?::\d{2})?(?:\s*(?:UTC|Z|am|pm))?\b",
    _I,
)
MONEY_TOKENS = re.compile(
    r"[$€£]\s?\d|\b\d[\d,.]*\s*(?:USD|dollars?|cents?)\b|\b\d[\d,]*\s*(?:input\s+|output\s+)?"
    r"tokens?\b",
    _I,
)


@dataclass
class DraftValidation:
    draft: ReportDraft | None
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.draft is not None and not self.errors


def _clauses(text: str) -> Iterable[str]:
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        if not sentence or sentence.rstrip().endswith("?"):
            continue  # questions are not claims
        yield from (c for c in _CLAUSE_SPLIT.split(sentence) if c and c.strip())


def affirmed(pattern: re.Pattern[str], text: str) -> list[str]:
    """Clauses where ``pattern`` is asserted (not negated in the same clause)."""
    hits = []
    for clause in _clauses(text):
        for m in pattern.finditer(clause):
            if not _NEG.search(clause[: m.start()]):
                hits.append(clause.strip())
                break
    return hits


def _close(a: datetime, b: datetime) -> bool:
    # a naive timestamp from the model is read as UTC (all records are UTC)
    a = a if a.tzinfo else a.replace(tzinfo=UTC)
    b = b if b.tzinfo else b.replace(tzinfo=UTC)
    return abs(a - b) <= TIME_TOLERANCE


def _check_ids(
    record: IncidentRecord, ids: Iterable[uuid.UUID], where: str, lookup: ForeignLookup
) -> list[str]:
    known = record.evidence_ids
    missing = {i for i in ids if i not in known}
    if not missing:
        return []
    owners = lookup(missing)
    errs = []
    for i in sorted(missing, key=str):
        if i in owners:
            errs.append(f"{where}: evidence {i} belongs to another incident ({owners[i]})")
        else:
            errs.append(f"{where}: evidence {i} does not exist (cite only ids in the record)")
    return errs


def _structured(d: ReportDraft, r: IncidentRecord, lookup: ForeignLookup) -> list[str]:
    errs: list[str] = []
    inc = r.incident
    if d.incident_id != inc.id:
        errs.append(f"incident_id must be {inc.id}")
    for name, got, want in (
        ("service", d.service, inc.service),
        ("incident_type", d.incident_type, inc.incident_type),
        ("severity", d.severity, inc.severity),
    ):
        if got != want:
            errs.append(f"{name} must be {want!r} (record), not {got!r}")
    if not _close(d.detected_at, inc.opened_at):
        errs.append(f"detected_at must equal the record's opened_at {inc.opened_at.isoformat()}")

    for i, o in enumerate(d.observations):
        errs += _check_ids(r, o.evidence_ids, f"observations[{i}]", lookup)
    for i, h in enumerate(d.hypotheses):
        errs += _check_ids(r, h.evidence_ids, f"hypotheses[{i}]", lookup)

    index = r.id_index()
    for i, t in enumerate(d.timeline):
        unknown = [x for x in t.record_ids if x not in index]
        if unknown:
            errs.append(f"timeline[{i}]: unknown record ids {sorted(map(str, unknown))}")
            continue
        stamps = [ts for x in t.record_ids for ts in index[x]]
        if not any(_close(t.at, ts) for ts in stamps):
            errs.append(
                f"timeline[{i}]: at {t.at.isoformat()} matches no timestamp of the cited "
                "records (do not invent timestamps)"
            )

    inv = r.investigation
    want_pa = inv.proposed_action if inv and inv.status == "completed" else None
    if want_pa is None and d.proposed_action is not None:
        errs.append("proposed_action must be null: the record holds no validated proposal")
    if want_pa is not None:
        if d.proposed_action is None:
            errs.append(f"proposed_action missing: the investigation proposed {want_pa.action!r}")
        elif (d.proposed_action.action, d.proposed_action.investigation_id) != (
            want_pa.action,
            inv.id if inv else None,
        ):
            errs.append(
                f"proposed_action must be {want_pa.action!r} from investigation "
                f"{inv.id if inv else None}"
            )

    want_pd = {p.id: (p.phase, p.decision, tuple(p.rule_ids)) for p in r.policy_decisions}
    got_pd = {p.decision_id: (p.phase, p.decision, tuple(p.rule_ids)) for p in d.policy_decisions}
    for k in got_pd.keys() - want_pd.keys():
        errs.append(f"policy decision {k} does not exist in the record (invented)")
    for k in want_pd.keys() - got_pd.keys():
        errs.append(f"policy decision {k} {want_pd[k]} is missing from policy_decisions")
    for k in got_pd.keys() & want_pd.keys():
        if got_pd[k] != want_pd[k]:
            errs.append(f"policy decision {k} must be {want_pd[k]}, not {got_pd[k]}")

    want_ap = {a.id: (a.status, a.decided_by) for a in r.approvals}
    got_ap = {a.approval_id: (a.status, a.decided_by) for a in d.approvals}
    for k in got_ap.keys() - want_ap.keys():
        errs.append(f"approval {k} does not exist in the record (invented approval)")
    for k in want_ap.keys() - got_ap.keys():
        errs.append(f"approval {k} {want_ap[k]} is missing from approvals")
    for k in got_ap.keys() & want_ap.keys():
        if got_ap[k] != want_ap[k]:
            errs.append(
                f"approval {k} must be (status, decided_by)={want_ap[k]}, not {got_ap[k]} "
                "(do not invent decisions or operator identities)"
            )

    want_ac = {a.id: a.status for a in r.actions}
    got_ac = {a.action_attempt_id: a.status for a in d.actions_taken}
    for k in got_ac.keys() - want_ac.keys():
        errs.append(f"action attempt {k} does not exist in the record (invented action)")
    for k in want_ac.keys() - got_ac.keys():
        errs.append(f"action attempt {k} ({want_ac[k]}) is missing from actions_taken")
    for k in got_ac.keys() & want_ac.keys():
        if got_ac[k] != want_ac[k]:
            errs.append(f"action attempt {k} status is {want_ac[k]!r}, not {got_ac[k]!r}")

    want_v = {v.id: v.status for v in r.verifications}
    got_v = {v.verification_id: v.status for v in d.verifications}
    for k in got_v.keys() - want_v.keys():
        errs.append(f"verification {k} does not exist in the record (invented verification)")
    for k in want_v.keys() - got_v.keys():
        errs.append(f"verification {k} ({want_v[k]}) is missing from verifications")
    for k in got_v.keys() & want_v.keys():
        if got_v[k] != want_v[k]:
            errs.append(f"verification {k} status is {want_v[k]!r}, not {got_v[k]!r}")

    task = r.task
    want_out = (
        inc.status,
        inc.resolution,
        task.status if task else None,
        task.outcome if task else None,
    )
    out = d.outcome
    got_out = (out.incident_status, out.incident_resolution, out.task_status, out.task_outcome)
    if got_out != want_out:
        errs.append(
            "outcome must be (incident_status, incident_resolution, task_status, task_outcome)="
            f"{want_out}, not {got_out}"
        )

    if inv is None and d.investigation_model is not None:
        errs.append("investigation_model must be null: no investigation is recorded")
    if inv is not None and (
        d.investigation_model is None
        or (d.investigation_model.model_id, d.investigation_model.auth_mode)
        != (inv.model_id, inv.auth_mode)
    ):
        errs.append(
            f"investigation_model must be model_id={inv.model_id!r}, auth_mode={inv.auth_mode!r}"
        )
    return errs


def _prose(d: ReportDraft, r: IncidentRecord, report_model_is_real_claude: bool) -> list[str]:
    errs: list[str] = []
    texts = d.free_texts(include_hypotheses=True)
    decisions = {p.decision for p in r.policy_decisions}

    def claim(pattern: re.Pattern[str], ok: bool, why: str, pool: list[tuple[str, str]]) -> None:
        if ok:
            return
        for where, txt in pool:
            hits = affirmed(pattern, txt)
            if hits:
                errs.append(f"{where}: unsupported claim ({why}): {hits[0][:140]!r}")
                return

    claim(EXECUTION, bool(r.executed_actions), "no executed action is recorded", texts)
    claim(
        RECOVERY,
        r.recovery_confirmed,
        "recovery is not confirmed by verification or the monitor",
        texts,
    )
    claim(APPROVAL, r.human_approved, "no human approval is recorded", texts)
    claim(POLICY_ALLOW, "ALLOW" in decisions, "no ALLOW policy decision is recorded", texts)
    claim(POLICY_DENY, "DENY" in decisions, "no DENY policy decision is recorded", texts)
    claim(
        POLICY_APPROVAL,
        "REQUIRE_APPROVAL" in decisions,
        "no REQUIRE_APPROVAL decision is recorded",
        texts,
    )
    claim(VERIFY_PASS, r.verification_passed, "no passed verification is recorded", texts)
    claim(VERIFY_FAIL, r.verification_failed, "no failed verification is recorded", texts)
    claim(
        CLAUDE,
        r.claude_used or report_model_is_real_claude,
        "no Claude model was used for this incident (the model was a deterministic mock or none)",
        texts,
    )

    # root cause: never confirmed by the evidence contract; hypotheses are exempt
    for where, txt in d.free_texts(include_hypotheses=False):
        for c in affirmed(ROOT_CAUSE, txt):
            if not _HEDGE.search(c):
                errs.append(
                    f"{where}: states a root cause as fact ({c[:140]!r}); only unconfirmed "
                    "hypotheses are supported by the record"
                )
                break

    deciders = {x.lower() for x in r.deciders}
    for where, txt in texts:
        for m in DECIDED_BY.finditer(txt):
            name = m.group(1).lower()
            if name not in deciders and name not in GENERIC_ACTORS:
                errs.append(f"{where}: names operator {m.group(1)!r}, who decided nothing here")
        if TIMESTAMP.search(txt):
            errs.append(f"{where}: put timestamps only in timeline[].at (not in prose)")
        if MONEY_TOKENS.search(txt):
            errs.append(
                f"{where}: costs and token counts are rendered from records; do not state them"
            )
    return errs


def validate_draft(
    payload: dict[str, Any],
    record: IncidentRecord,
    lookup: ForeignLookup,
    *,
    report_model_is_real_claude: bool = False,
) -> DraftValidation:
    try:
        draft = ReportDraft.model_validate(payload)
    except ValidationError as exc:
        return DraftValidation(
            None,
            [
                "schema: " + f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
                for e in exc.errors(include_url=False)[:15]
            ],
        )
    errors = _structured(draft, record, lookup) + _prose(draft, record, report_model_is_real_claude)
    # de-duplicate, keep order, bound the feedback sent back to the model
    return DraftValidation(draft, list(dict.fromkeys(errors))[:25])
