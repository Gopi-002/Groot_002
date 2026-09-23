import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.persistence.repositories import create_incident, get_incident, upsert_service

pytestmark = pytest.mark.integration


@pytest.fixture
def service_id(engine):
    with engine.begin() as c:
        return upsert_service(c, f"demo-{uuid.uuid4().hex[:8]}", "http://demo-app:8001")


def test_insert_and_retrieve_incident(engine, service_id):
    with engine.begin() as c:
        iid = create_incident(c, service_id, "http_error", severity="high", summary="500s")
    with engine.connect() as c:
        inc = get_incident(c, iid)
    assert inc is not None
    assert (inc.service_id, inc.incident_type, inc.status, inc.severity) == (
        service_id,
        "http_error",
        "open",
        "high",
    )
    assert inc.opened_at.tzinfo is not None and inc.opened_at.utcoffset().total_seconds() == 0
    assert inc.resolved_at is None and inc.occurrence_count == 1


def test_get_missing_incident_returns_none(engine):
    with engine.connect() as c:
        assert get_incident(c, uuid.uuid4()) is None


def test_one_active_incident_per_service_and_type(engine, service_id):
    with engine.begin() as c:
        first = create_incident(c, service_id, "unavailable")
    with pytest.raises(IntegrityError, match="uq_incidents_one_active_per_service_type"):
        with engine.begin() as c:
            create_incident(c, service_id, "unavailable")
    with engine.begin() as c:  # different type is allowed
        create_incident(c, service_id, "high_latency")
        c.execute(
            text("UPDATE incidents SET status='resolved', resolved_at=now() WHERE id=:i"),
            {"i": first},
        )
    with engine.begin() as c:  # after resolution a new one may open
        assert create_incident(c, service_id, "unavailable") != first


@pytest.mark.parametrize(
    "sql",
    [
        # unknown status
        "INSERT INTO incidents (service_id, incident_type, status) VALUES (:s, 'http_error', 'x')",
        # unknown incident type
        "INSERT INTO incidents (service_id, incident_type) VALUES (:s, 'not_a_type')",
        # resolved without resolved_at
        "INSERT INTO incidents (service_id, incident_type, status) "
        "VALUES (:s, 'memory_pressure', 'resolved')",
    ],
)
def test_check_constraints_reject_invalid_rows(engine, service_id, sql):
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(text(sql), {"s": service_id})


def test_services_must_be_demo_environment(engine):
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO services (name, base_url, environment) "
                    "VALUES ('prod-db', 'http://x', 'production')"
                )
            )


def test_audit_events_append_only(engine):
    with engine.begin() as c:
        aid = c.execute(
            text(
                "INSERT INTO audit_events (actor_type, actor_id, action, entity_type) "
                "VALUES ('system','test','created','incident') RETURNING id"
            )
        ).scalar()
    for stmt in (
        "UPDATE audit_events SET action='x' WHERE id=:i",
        "DELETE FROM audit_events WHERE id=:i",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as c:
                c.execute(text(stmt), {"i": aid})


def test_single_active_model_config(engine):
    with engine.begin() as c:
        c.execute(
            text(
                "INSERT INTO model_config (auth_mode, model_id, selected_by) "
                "VALUES ('api_key', 'placeholder-model', 'test')"
            )
        )
    with pytest.raises(IntegrityError):
        with engine.begin() as c:
            c.execute(
                text(
                    "INSERT INTO model_config (auth_mode, model_id, selected_by) "
                    "VALUES ('api_key', 'other-model', 'test')"
                )
            )
