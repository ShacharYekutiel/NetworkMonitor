"""ApiError and the global error envelope handlers."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("netmon.api")


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, details: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def not_found(what: str = "Resource") -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def validation_error(loc: list[str | int], msg: str) -> ApiError:
    return ApiError(422, "validation_error", msg, [{"loc": loc, "msg": msg}])


def conflict(field: str = "name", message: str | None = None) -> ApiError:
    return ApiError(
        409, "conflict", message or f"An item with this {field} already exists", [{"field": field}]
    )


def error_body(code: str, message: str, details: list[dict[str, Any]] | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


def error_response(
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status, headers=headers)


_HTTP_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
}


def _clean_msg(msg: str) -> str:
    for prefix in ("Value error, ", "Assertion failed, "):
        if msg.startswith(prefix):
            return msg[len(prefix) :]
    return msg


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = []
        for err in exc.errors():
            loc = [p for p in err.get("loc", ()) if p is not None]
            if err.get("type") == "json_invalid":
                return error_response(400, "bad_request", "Malformed JSON body")
            details.append({"loc": loc, "msg": _clean_msg(str(err.get("msg", "invalid")))})
        return error_response(422, "validation_error", "Request validation failed", details)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _HTTP_CODES.get(
            exc.status_code, "internal_error" if exc.status_code >= 500 else "bad_request"
        )
        message = exc.detail if isinstance(exc.detail, str) else code
        return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error: %s", exc)
        return error_response(500, "internal_error", "Internal server error")
