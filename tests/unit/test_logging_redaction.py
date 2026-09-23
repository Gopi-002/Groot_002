import io
import json
import logging

from pydantic import SecretStr

from app.observability.logging import REDACTED, JsonFormatter, redact, redact_text

SECRET = "Sup3rS3cretValue-XYZ"


def _emit(msg: str, *args, **extra) -> dict:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter("test"))
    lg = logging.getLogger("redaction-test")
    lg.handlers[:] = [handler]
    lg.propagate = False
    lg.warning(msg, *args, extra=extra)
    return json.loads(buf.getvalue())


def test_sensitive_keys_redacted():
    out = _emit(
        "ctx", password=SECRET, api_key=SECRET, headers={"Authorization": SECRET}, safe="visible"
    )
    assert SECRET not in json.dumps(out)
    assert out["password"] == REDACTED and out["headers"]["Authorization"] == REDACTED
    assert out["safe"] == "visible"


def test_message_patterns_redacted():
    out = _emit(
        "connect postgresql+psycopg://user:%s@db/x token=%s key sk-ant-api03-%s Bearer %s",
        SECRET,
        SECRET,
        SECRET,
        SECRET,
    )
    assert SECRET not in out["msg"]
    assert "user:[REDACTED]@db" in out["msg"]


def test_secretstr_values_redacted():
    assert redact({"thing": SecretStr(SECRET)}) == {"thing": REDACTED}


def test_exception_text_redacted():
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter("test"))
    lg = logging.getLogger("redaction-exc")
    lg.handlers[:] = [handler]
    lg.propagate = False
    try:
        raise RuntimeError(f"failed with password={SECRET}")
    except RuntimeError:
        lg.exception("boom")
    assert SECRET not in buf.getvalue()


def test_plain_text_untouched():
    assert redact_text("latency_ms=12 status=ok") == "latency_ms=12 status=ok"
