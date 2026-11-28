"""Consistent, non-leaking API error responses.

Handlers still raise typed HTTP errors for client mistakes.  This boundary owns
the part the client must never see: server exception text, SQL, file paths and
provider messages.  A 5xx response carries a generic message plus the same
request id that the internal log records.
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from fastapi import FastAPI, Request
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from novacontrol.api.middleware import current_request_id
from novacontrol.audit.redact import Redactor

_INTERNAL_MESSAGES = {
    500: ("internal_server_error", "Internal server error."),
    501: ("not_implemented", "The requested operation is not implemented."),
    502: ("bad_gateway", "An upstream service failed."),
    503: ("service_unavailable", "The service is temporarily unavailable."),
    504: ("gateway_timeout", "An upstream service timed out."),
}
_REDACTOR = Redactor(enabled=True)


def _request_id(request: Request) -> str:
    state = getattr(request, "state", None)
    value = str(getattr(state, "request_id", "") or "")
    return value or current_request_id() or "-"


def _safe_trace(exc: BaseException) -> str:
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return _REDACTOR.redact_text(text).text


def _log_internal(request: Request, status: int, exc: BaseException | None, detail: Any) -> None:
    request_id = _request_id(request)
    rendered = detail if detail is not None else exc
    message = _REDACTOR.redact_text(str(rendered or "")).text
    logging.getLogger("novacontrol.api.errors").error(
        "api_error status=%s method=%s path=%s request_id=%s detail=%s\n%s",
        status,
        request.method,
        request.url.path,
        request_id,
        message,
        _safe_trace(exc) if exc is not None else "",
    )


def _internal_response(
    request: Request, status: int, exc: BaseException | None, detail: Any
) -> JSONResponse:
    _log_internal(request, status, exc, detail)
    code, message = _INTERNAL_MESSAGES.get(status, _INTERNAL_MESSAGES[500])
    request_id = _request_id(request)
    return JSONResponse(
        status_code=status,
        content={"detail": message, "error": code, "request_id": request_id},
        headers={"cache-control": "no-store", "x-request-id": request_id},
    )


def install_error_handlers(app: FastAPI) -> None:
    """Install the one external error boundary for the API app."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        status = int(exc.status_code)
        if status >= 500:
            return _internal_response(request, status, exc, exc.detail)
        if status in {401, 403}:
            logging.getLogger("novacontrol.api.security").warning(
                "access_denied status=%s method=%s path=%s request_id=%s",
                status,
                request.method,
                request.url.path,
                _request_id(request),
            )
        return JSONResponse(
            status_code=status,
            content={"detail": exc.detail},
            headers={"x-request-id": _request_id(request)},
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return _internal_response(request, 500, exc, None)