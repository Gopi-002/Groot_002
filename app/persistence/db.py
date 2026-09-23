"""Database engine and schema-version helpers. PostgreSQL is authoritative."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def make_engine(
    url: str, *, timeout_seconds: float = 2.0, statement_timeout_ms: int = 30_000
) -> Engine:
    """Engine whose every call is time-bounded: connect timeout, TCP keepalives
    and ``tcp_user_timeout`` (a query on a connection to a vanished host fails in
    ~10 s instead of blocking for minutes of TCP retransmission), and a server
    statement timeout."""
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={
            "connect_timeout": max(1, int(timeout_seconds)),
            "keepalives": 1,
            "keepalives_idle": 5,
            "keepalives_interval": 2,
            "keepalives_count": 3,
            "tcp_user_timeout": 10_000,
            "options": f"-c statement_timeout={int(statement_timeout_ms)}",
        },
    )


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    if url is not None:
        cfg.attributes["sqlalchemy_url"] = url
    return cfg


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("no migration head found")
    return head


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT to_regclass('public.alembic_version')")).scalar()
        if exists is None:
            return None
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
