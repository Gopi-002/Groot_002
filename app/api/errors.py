"""Consistent error envelope: {"error": {"code", "message", "request_id"}}.

Internal exception details are never returned to the client.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.observability.logging import log_context

log = logging.getLogger("sentinelops.api.errors")


def error_body(code: str, message: str, request_id: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _rid(request: Request) -> str:
    return str(getattr(request.state, "request_id", "unknown"))


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id_mw(request, call_next):  # type: ignore[no-untyped-def]
        incoming = request.headers.get("x-request-id", "")
        try:
            rid = str(uuid.UUID(incoming))
        except ValueError:
            rid = str(uuid.uuid4())
        request.state.request_id = rid
        with log_context(request_id=rid):  # correlates every log line of this request
            response = await call_next(request)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            404: "not_found",
            405: "method_not_allowed",
            401: "unauthorized",
            403: "forbidden",
            503: "unavailable",
        }.get(exc.status_code, "http_error")
        return JSONResponse(
            error_body(code, str(exc.detail), _rid(request)),
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exc(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            error_body("validation_error", "request validation failed", _rid(request)),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "unhandled error", extra={"request_id": _rid(request), "error_type": type(exc).__name__}
        )
        return JSONResponse(
            error_body("internal_error", "internal server error", _rid(request)),
            status_code=500,
        )
