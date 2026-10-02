"""Closed API error contract.

Every error response has exactly this body, with a closed ``code`` and that code's fixed message:

    {"error": {"code": "<code>", "message": "<message>", "request_id": "<id>"}}

Messages are fixed templates: raw exception text, tracebacks, secrets, URLs and paths never reach a response. An
unexpected exception becomes ``internal``, and only sanitized metadata (request id, method, route template, exception
type name) is logged.
"""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.request_context import current_request_id

logger = logging.getLogger("mias.api")

# code: (HTTP status, fixed message)
ERRORS = {
    "invalid_request": (400, "the request is invalid"),
    "unauthorized": (401, "authentication is required"),
    "forbidden": (403, "this operation is not permitted"),
    "not_found": (404, "the requested resource was not found"),
    "method_not_allowed": (405, "the method is not allowed for this resource"),
    "ambiguous_latest": (409, "more than one object matches the latest request"),
    "conflict": (409, "the request conflicts with existing state"),
    "payload_too_large": (413, "the request body is too large"),
    "artifact_invalid": (500, "a stored artifact failed validation"),
    "dependency_unavailable": (503, "a required dependency is unavailable"),
    "internal": (500, "an internal error occurred"),
}
CODES = tuple(ERRORS)


class ApiError(Exception):
    """Raised by routes and dependencies with a closed code; never carries free text into the response."""

    def __init__(self, code, headers=None):
        if code not in ERRORS:
            raise ValueError("unknown API error code")
        super().__init__(code)
        self.code, self.headers = code, dict(headers or {})


def error_body(code, request_id):
    return {"error": {"code": code, "message": ERRORS[code][1], "request_id": request_id}}


def error_response(code, request_id, headers=None):
    return JSONResponse(error_body(code, request_id), status_code=ERRORS[code][0], headers=headers)


def _http_code(status):
    if status == 404:
        return "not_found"
    if status == 405:
        return "method_not_allowed"
    if status == 413:
        return "payload_too_large"
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    return "internal" if status >= 500 else "invalid_request"


def log_unexpected(request_id, method, route, error):
    """Sanitized metadata only: never the message, arguments, traceback, headers or query string."""
    logger.error("event=unhandled_exception request_id=%s method=%s route=%s error_type=%s",
                 request_id, method, route, type(error).__name__)


def install_error_handlers(app: FastAPI):
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError):
        return error_response(exc.code, current_request_id(), exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        headers = {"Allow": exc.headers["Allow"]} if exc.status_code == 405 and exc.headers and "Allow" in exc.headers \
            else None
        return error_response(_http_code(exc.status_code), current_request_id(), headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response("invalid_request", current_request_id())
