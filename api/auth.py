"""Authorization foundation: two scopes, ``read`` and ``operate``, as FastAPI dependencies.

- **read** (``/api/v1`` routes): open only when no read token is configured, which settings allow only on a loopback
  bind. With a read token, a ``Authorization: Bearer <token>`` header must match it.
- **operate** (future action routes): always needs the operator token. If none is configured, operate routes are
  forbidden outright.

Tokens are compared with ``hmac.compare_digest``. A missing or wrong token is ``unauthorized`` (401, with
``WWW-Authenticate: Bearer``). The Authorization header is never logged or echoed. Health routes take no auth.
"""
import hmac

from fastapi import Request

from api.errors import ApiError

CHALLENGE = {"WWW-Authenticate": "Bearer"}


def bearer_token(request):
    """The bearer credential, or None. Exactly one Authorization header with the Bearer scheme is accepted."""
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return None
    scheme, _, credential = values[0].partition(" ")
    if scheme.lower() != "bearer" or not credential or " " in credential:
        return None
    return credential


def token_matches(presented, expected):
    if presented is None:
        return False
    return hmac.compare_digest(presented.encode("utf-8", "surrogateescape"), expected.encode("utf-8"))


def require_read(request: Request):
    expected = request.app.state.settings.read_token
    if expected is None:
        return                                   # loopback-only by settings validation
    if not token_matches(bearer_token(request), expected):
        raise ApiError("unauthorized", CHALLENGE)


def require_operate(request: Request):
    expected = request.app.state.settings.operator_token
    if expected is None:
        raise ApiError("forbidden")
    if not token_matches(bearer_token(request), expected):
        raise ApiError("unauthorized", CHALLENGE)
