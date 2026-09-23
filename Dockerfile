# Shared image for the API, migrator and demo app (different commands; the
# demo container receives no database/Redis credentials).
FROM ghcr.io/astral-sh/uv:0.12.17 AS uv

FROM python:3.12.14-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /srv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project && rm /usr/local/bin/uv
COPY alembic.ini ./
COPY app ./app
COPY demo_application ./demo_application
COPY migrations ./migrations
COPY dashboard ./dashboard
RUN useradd --system --uid 10001 --no-create-home sentinel \
    && mkdir -p /var/lib/sentinel-secrets /var/lib/sentinel-executor /var/lib/demo-state \
    && chown 10001:10001 /var/lib/sentinel-secrets /var/lib/sentinel-executor /var/lib/demo-state \
    && chmod 700 /var/lib/sentinel-secrets /var/lib/sentinel-executor /var/lib/demo-state
USER 10001
EXPOSE 8000 8001
CMD ["uvicorn", "--factory", "app.api.main:app_factory", "--host", "0.0.0.0", "--port", "8000"]
