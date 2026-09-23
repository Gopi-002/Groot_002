"""Alembic environment. Online mode only; a PostgreSQL advisory lock makes
concurrent migrators (e.g. two containers starting) serialize safely."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool, text

MIGRATION_LOCK_ID = 7_420_001

config = context.config


def _url() -> str:
    url = config.attributes.get("sqlalchemy_url")
    if url:
        return str(url)
    from app.config import get_settings

    return get_settings().database_url


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": MIGRATION_LOCK_ID})
        conn.commit()
        try:
            context.configure(connection=conn, target_metadata=None, transaction_per_migration=True)
            with context.begin_transaction():
                context.run_migrations()
            conn.commit()
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": MIGRATION_LOCK_ID})
            conn.commit()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("offline migrations are not supported; run against a database")
run_migrations_online()
