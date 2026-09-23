"""Deterministic TEST/DEMO model gateways. These are NOT Claude and are never
presented as Claude: their provider is "mock", their model ids start with
``mock-``, and the settings forbid them in production.

* ``ScriptedGateway``        - replays a fixed list of turns/errors (unit tests).
* ``DeterministicMockGateway`` - a rule-based investigator that chooses its next
  tool from the RESULTS of previous tools, so tests can prove the orchestrator
  supports evidence-driven branching end to end without a live model. For the
  report stage it drafts strictly from the supplied incident record.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Sequence
from typing import Any

from app.agent.gateway import (
    AuthCheck,
    InvokeRequest,
    ModelDescriptor,
    ModelTurn,
    ModelUnavailable,
    ToolCall,
    Usage,
)

MOCK_MODEL = ModelDescriptor(
    id="mock-investigator-v1",
    display_name="Deterministic mock investigator (TEST/DEMO ONLY - not Claude)",
    max_input_tokens=100_000,
    max_output_tokens=8_000,
    capabilities={},
)
_ids = itertools.count(1)


def turn(
    *calls: tuple[str, dict[str, Any]],
    text: str = "",
    stop: str | None = None,
    usage: Usage | None = None,
) -> ModelTurn:
    """Build a ModelTurn (test helper)."""
    tcs = tuple(ToolCall(f"toolu_mock_{next(_ids)}", n, i) for n, i in calls)
    raw: list[dict[str, Any]] = []
    if text:
        raw.append({"type": "text", "text": text})
    raw += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in tcs]
    return ModelTurn(
        stop_reason=stop or ("tool_use" if tcs else "end_turn"),
        text=text,
        tool_calls=tcs,
        raw_content=tuple(raw),
        usage=usage or Usage(input_tokens=500, output_tokens=100),
        model=MOCK_MODEL.id,
    )


class _MockBase:
    provider = "mock"
    auth_mode = "mock"

    def __init__(self) -> None:
        self.requests: list[InvokeRequest] = []

    def authenticate(self) -> AuthCheck:
        return AuthCheck(self.provider, self.auth_mode, None)

    def list_models(self) -> list[ModelDescriptor]:
        return [MOCK_MODEL]

    def get_model(self, model_id: str) -> ModelDescriptor:
        if model_id != MOCK_MODEL.id:
            raise ModelUnavailable(f"model: {model_id} not available from the mock gateway")
        return MOCK_MODEL


class ScriptedGateway(_MockBase):
    def __init__(self, script: Sequence[ModelTurn | Exception]) -> None:
        super().__init__()
        self.script = list(script)

    def invoke(self, request: InvokeRequest) -> ModelTurn:
        self.requests.append(request)
        if not self.script:
            raise AssertionError("scripted gateway exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All tool_result envelopes the orchestrator has returned so far."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.get("role") != "user" or not isinstance(m.get("content"), list):
            continue
        for block in m["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                try:
                    body = json.loads(block.get("content") or "{}")
                except ValueError:
                    body = {}
                out.append({**body, "_is_error": bool(block.get("is_error"))})
    return out


class DeterministicMockGateway(_MockBase):
    """Evidence-driven rule-based investigator (see module docstring).

    Branching rules (decided from actual tool results):
      incident -> type http_error  -> error logs -> (logs available? container status)
               -> type unavailable -> container status -> (running? metrics : health history)
               -> type high_latency -> resource metrics -> health history
    then submit, citing only evidence ids it actually received.
    """

    def invoke(self, request: InvokeRequest) -> ModelTurn:
        self.requests.append(request)
        if any(t.get("name") == "submit_incident_report" for t in request.tools):
            return self._draft_report(request)
        results = [r for r in tool_results(request.messages) if not r["_is_error"]]
        rejected = [r for r in tool_results(request.messages) if r["_is_error"]]
        by_tool = {r.get("tool"): r for r in results if r.get("tool")}
        called = [r.get("tool") for r in results]
        if "get_incident" not in by_tool:
            return turn(("get_incident", {}))
        inc = ((by_tool["get_incident"].get("data") or {}).get("incident")) or {}
        itype = inc.get("incident_type")
        nxt = self._next_tool(itype, by_tool, called)
        if nxt is not None and not rejected:
            return turn(nxt)
        return turn(("submit_investigation", self._result(inc, by_tool)))

    @staticmethod
    def _draft_report(request: InvokeRequest) -> ModelTurn:
        """Report stage: draft ONLY from the supplied canonical record (the mock
        never adds facts), so the validator path is exercised end to end."""
        from app.reporting.record import IncidentRecord
        from app.reporting.render import deterministic_draft

        first = request.messages[0]["content"] if request.messages else ""
        body = str(first).split("<incident_record>", 1)[-1].split("</incident_record>", 1)[0]
        record = IncidentRecord.model_validate(json.loads(body))
        draft = deterministic_draft(record).model_dump(mode="json")
        return turn(("submit_incident_report", draft), usage=Usage(1200, 400))

    @staticmethod
    def _next_tool(
        itype: str | None, by: dict[Any, dict[str, Any]], called: list[Any]
    ) -> tuple[str, dict[str, Any]] | None:
        def ok(tool: str) -> bool:
            return by.get(tool, {}).get("status") == "ok"

        if itype == "http_error":
            if "get_application_logs" not in called:
                return ("get_application_logs", {"tail": 50, "level": "error"})
            if ok("get_application_logs") and "get_container_status" not in called:
                return ("get_container_status", {})
            if not ok("get_application_logs") and "get_health_history" not in called:
                return ("get_health_history", {"limit": 10})
        elif itype == "unavailable":
            if "get_container_status" not in called:
                return ("get_container_status", {})
            running = (by.get("get_container_status", {}).get("data") or {}).get("running")
            if running and "get_resource_metrics" not in called:
                return ("get_resource_metrics", {})
            if not running and "get_health_history" not in called:
                return ("get_health_history", {"limit": 10})
        elif itype == "high_latency":
            if "get_resource_metrics" not in called:
                return ("get_resource_metrics", {})
            if "get_health_history" not in called:
                return ("get_health_history", {"limit": 10})
        return None

    @staticmethod
    def _result(inc: dict[str, Any], by: dict[Any, dict[str, Any]]) -> dict[str, Any]:
        ev = {t: r["evidence_id"] for t, r in by.items() if r.get("evidence_id")}
        all_ids = list(ev.values())
        observations = [
            {
                "statement": f"The monitor recorded a {inc.get('incident_type')} incident with "
                f"{inc.get('occurrence_count')} failing check(s).",
                "evidence_ids": [ev["get_incident"]],
            }
        ]
        for tool, r in by.items():
            if tool != "get_incident":
                observations.append(
                    {
                        "statement": f"Tool {tool} returned status '{r.get('status')}'.",
                        "evidence_ids": [ev[tool]],
                    }
                )
        missing = [f"{t} was unavailable" for t, r in by.items() if r.get("status") != "ok"]
        restart = inc.get("incident_type") in ("http_error", "unavailable") and not missing
        return {
            "incident_id": inc.get("id"),
            "observations": observations,
            "evidence_ids": all_ids,
            "hypotheses": [
                {
                    "statement": "The application process is in a degraded state that a "
                    "restart may clear (unconfirmed).",
                    "certainty": "hypothesis",
                    "confidence": "medium" if restart else "low",
                    "supporting_evidence_ids": all_ids[:3],
                }
            ],
            "missing_evidence": missing,
            "proposed_action": {
                "action": "restart_demo_app" if restart else "escalate_to_human",
                "target_service": "demo-app" if restart else None,
                "rationale": "Proposal only; the deterministic policy decides.",
                "evidence_ids": all_ids[:3],
            },
            "risks": ["A restart briefly interrupts the demo application."],
            "verification_plan": ["Confirm 3 consecutive healthy checks after any action."],
            "next_step": "propose_remediation" if restart else "request_human_review",
        }
