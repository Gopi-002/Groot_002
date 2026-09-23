import json
import uuid

from app.agent.gateway import InvokeRequest
from app.agent.mock_gateway import DeterministicMockGateway


def result(tool, status="ok", data=None):
    return {
        "type": "tool_result",
        "tool_use_id": "t",
        "is_error": False,
        "content": json.dumps(
            {"evidence_id": str(uuid.uuid4()), "tool": tool, "status": status, "data": data}
        ),
    }


def next_call(results):
    msgs = [{"role": "user", "content": "go"}]
    if results:
        msgs.append({"role": "user", "content": results})
    turn = DeterministicMockGateway().invoke(
        InvokeRequest("mock-investigator-v1", "s", msgs, [], 100, 5)
    )
    return turn.tool_calls[0].name, turn.tool_calls[0].input


def incident(itype):
    return result(
        "get_incident",
        data={"incident": {"id": str(uuid.uuid4()), "incident_type": itype, "occurrence_count": 3}},
    )


def test_first_call_reads_incident():
    assert next_call([])[0] == "get_incident"


def test_branch_http_error_logs_available_goes_to_container_status():
    seq = [incident("http_error"), result("get_application_logs")]
    assert next_call(seq)[0] == "get_container_status"


def test_branch_http_error_logs_unavailable_goes_to_health_history():
    seq = [incident("http_error"), result("get_application_logs", status="unavailable")]
    assert next_call(seq)[0] == "get_health_history"


def test_branch_unavailable_depends_on_container_state():
    running = [incident("unavailable"), result("get_container_status", data={"running": True})]
    stopped = [incident("unavailable"), result("get_container_status", data={"running": False})]
    assert next_call(running)[0] == "get_resource_metrics"
    assert next_call(stopped)[0] == "get_health_history"


def test_submits_citing_only_received_evidence():
    seq = [incident("high_latency"), result("get_resource_metrics"), result("get_health_history")]
    name, payload = next_call(seq)
    received = {json.loads(r["content"])["evidence_id"] for r in seq}
    assert name == "submit_investigation"
    assert set(payload["evidence_ids"]) <= received
    assert payload["proposed_action"]["action"] == "escalate_to_human"  # latency: no restart
