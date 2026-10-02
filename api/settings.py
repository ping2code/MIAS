"""API settings from the process environment only (never ``.env``); validated, secrets never shown.

| Variable | Default | Meaning |
|---|---|---|
| ``MIAS_API_HOST`` | ``127.0.0.1`` | bind address: an IP literal or ``localhost`` (hostnames aren't resolved) |
| ``MIAS_API_PORT`` | ``8080`` | 1-65535 |
| ``MIAS_API_DOCS_ENABLED`` | ``true`` | serve ``/docs`` and ``/openapi.json`` |
| ``MIAS_API_READ_TOKEN`` | unset | bearer token for read routes; **required** when the bind is not loopback |
| ``MIAS_API_OPERATOR_TOKEN`` | unset | bearer token for future operate routes (reserved) |
| ``MIAS_API_ACTIONS_ENABLED`` | ``false`` | reserved; there are no action routes in this version, so ``true`` is rejected |
| ``MIAS_ARTIFACT_ROOT`` | unset | future artifact store root (absolute path); checked by readiness when set |
| ``MIAS_RECEIPT_ROOT`` | unset | Phase 12E delivery receipt directory (absolute path); checked by readiness when set |
| ``MIAS_BUILD_ID`` | ``unknown`` | build identifier reported by ``/api/v1/version`` |

**Bind safety:** a non-loopback bind (``0.0.0.0``, ``::`` or any non-loopback address) without a read token is a
configuration error. The service refuses to start rather than expose unauthenticated reads.

Tokens are at least 32 printable, non-space ASCII characters, and the read and operator tokens must differ. Errors
name the setting only, never its value; tokens are excluded from ``repr``.
"""
from dataclasses import dataclass, field
import ipaddress
import os
import re

DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 8080
MIN_TOKEN_LENGTH, MAX_TOKEN_LENGTH = 32, 256
TOKEN = re.compile(r"[\x21-\x7e]+")
BUILD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}")
MAX_PATH_LENGTH = 4096


class ApiConfigurationError(ValueError):
    """Invalid API configuration; messages name the setting, never a value."""


def is_loopback(host):
    if host == "localhost":
        return True
    return ipaddress.ip_address(host).is_loopback


@dataclass(frozen=True)
class ApiSettings:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    docs_enabled: bool = True
    read_token: str = field(default=None, repr=False)
    operator_token: str = field(default=None, repr=False)
    actions_enabled: bool = False
    artifact_root: str = None
    receipt_root: str = None
    build_id: str = "unknown"

    def __post_init__(self):
        if not isinstance(self.host, str) or not self.host:
            raise ApiConfigurationError("MIAS_API_HOST must be an IP address or localhost")
        if self.host != "localhost":
            try:
                ipaddress.ip_address(self.host)
            except ValueError:
                raise ApiConfigurationError("MIAS_API_HOST must be an IP address or localhost") from None
        if not (isinstance(self.port, int) and not isinstance(self.port, bool) and 1 <= self.port <= 65535):
            raise ApiConfigurationError("MIAS_API_PORT must be an integer from 1 to 65535")
        for name, value in (("MIAS_API_DOCS_ENABLED", self.docs_enabled),
                            ("MIAS_API_ACTIONS_ENABLED", self.actions_enabled)):
            if not isinstance(value, bool):
                raise ApiConfigurationError(f"{name} must be true or false")
        for name, value in (("MIAS_API_READ_TOKEN", self.read_token),
                            ("MIAS_API_OPERATOR_TOKEN", self.operator_token)):
            if value is not None and not (isinstance(value, str)
                                          and MIN_TOKEN_LENGTH <= len(value) <= MAX_TOKEN_LENGTH
                                          and TOKEN.fullmatch(value)):
                raise ApiConfigurationError(f"{name} must be {MIN_TOKEN_LENGTH}-{MAX_TOKEN_LENGTH} printable "
                                            "non-space ASCII characters")
        if self.read_token is not None and self.read_token == self.operator_token:
            raise ApiConfigurationError("MIAS_API_READ_TOKEN and MIAS_API_OPERATOR_TOKEN must differ")
        if not is_loopback(self.host) and self.read_token is None:
            raise ApiConfigurationError("MIAS_API_READ_TOKEN is required when MIAS_API_HOST is not a loopback "
                                        "address")
        if self.actions_enabled:
            raise ApiConfigurationError("MIAS_API_ACTIONS_ENABLED is reserved; this version has no action routes")
        for name, value in (("MIAS_ARTIFACT_ROOT", self.artifact_root), ("MIAS_RECEIPT_ROOT", self.receipt_root)):
            if value is not None and not (isinstance(value, str) and os.path.isabs(value)
                                          and len(value) <= MAX_PATH_LENGTH and "\x00" not in value):
                raise ApiConfigurationError(f"{name} must be an absolute path")
        if not (isinstance(self.build_id, str) and BUILD_ID.fullmatch(self.build_id)):
            raise ApiConfigurationError("MIAS_BUILD_ID must be 1-64 characters of [A-Za-z0-9._+-]")

    @property
    def loopback(self):
        return is_loopback(self.host)


def _bool(environ, name, default):
    raw = environ.get(name)
    if raw is None or raw == "":
        return default
    value = raw.strip().lower()
    if value not in ("true", "false"):
        raise ApiConfigurationError(f"{name} must be true or false")
    return value == "true"


def _optional(environ, name):
    raw = environ.get(name)
    return None if raw is None or raw == "" else raw


def load_api_settings(environ):
    """``ApiSettings`` from an explicit environment mapping (pass ``os.environ`` at the process boundary)."""
    raw_port = (environ.get("MIAS_API_PORT") or "").strip()
    if raw_port and not raw_port.isdigit():
        raise ApiConfigurationError("MIAS_API_PORT must be an integer from 1 to 65535")
    return ApiSettings(
        host=(environ.get("MIAS_API_HOST") or DEFAULT_HOST).strip(),
        port=int(raw_port) if raw_port else DEFAULT_PORT,
        docs_enabled=_bool(environ, "MIAS_API_DOCS_ENABLED", True),
        read_token=_optional(environ, "MIAS_API_READ_TOKEN"),
        operator_token=_optional(environ, "MIAS_API_OPERATOR_TOKEN"),
        actions_enabled=_bool(environ, "MIAS_API_ACTIONS_ENABLED", False),
        artifact_root=_optional(environ, "MIAS_ARTIFACT_ROOT"),
        receipt_root=_optional(environ, "MIAS_RECEIPT_ROOT"),
        build_id=(environ.get("MIAS_BUILD_ID") or "unknown").strip())
