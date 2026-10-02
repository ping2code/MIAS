"""Request ids and the outermost safety net.

``RequestContextMiddleware`` (pure ASGI, outermost):
- accepts an incoming ``X-Request-ID`` only if it matches ``[A-Za-z0-9][A-Za-z0-9._-]{7,63}`` (8-64 characters);
  anything else (absent, empty, too long, other characters, repeated header) is **replaced** with a generated id
  (32 lowercase hex characters), never rejected;
- exposes it through ``current_request_id()`` (a context variable that also reaches sync handlers run in the thread
  pool) and ``request.state.request_id``, for logging now and tracing later;
- returns it as ``X-Request-ID`` on every response, errors included;
- turns any exception that escapes the app into the closed ``internal`` error, logging sanitized metadata only.

The request id is runtime metadata: it lives in headers and error bodies, never inside a canonical analytical object.
"""
from contextvars import ContextVar
import re
from uuid import uuid4

from starlette.datastructures import MutableHeaders

REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")
_current = ContextVar("mias_api_request_id", default=None)


def current_request_id():
    return _current.get()


def new_request_id():
    return uuid4().hex


def accepted_request_id(headers):
    """The client's id if exactly one well-formed X-Request-ID header was sent; otherwise None."""
    values = [v for k, v in headers if k.lower() == REQUEST_ID_HEADER.encode()]
    if len(values) != 1:
        return None
    try:
        value = values[0].decode("ascii")
    except UnicodeDecodeError:
        return None
    return value if REQUEST_ID.fullmatch(value) else None


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from api.errors import error_response, log_unexpected
        request_id = accepted_request_id(scope.get("headers", [])) or new_request_id()
        scope.setdefault("state", {})["request_id"] = request_id
        token = _current.set(request_id)
        started = False

        async def send_with_id(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)
        try:
            await self.app(scope, receive, send_with_id)
        except Exception as error:
            route = getattr(scope.get("route"), "path", "unmatched")
            log_unexpected(request_id, scope.get("method", ""), route, error)
            if started:
                raise
            await error_response("internal", request_id)(scope, receive, send_with_id)
        finally:
            _current.reset(token)
