"""Options data settings from the process environment only (never ``.env``); validated, key never shown.

These are separate from ``MARKET_DATA_*``: options configuration never reads, reuses or changes the market data
(and frozen Phase 6) settings. Phase 9A uses them for the contract check only; they are not a frozen contract.

| Variable | Default | Meaning |
|---|---|---|
| ``OPTIONS_DATA_PROVIDER`` | ``none`` | ``none`` or ``massive`` |
| ``OPTIONS_DATA_API_KEY`` | unset | required for ``massive``; sent only as an HTTP Authorization header |
| ``OPTIONS_DATA_BASE_URL`` | ``https://api.massive.com`` | HTTPS origin, no path, query or credentials |
| ``OPTIONS_DATA_DELAY_SECONDS`` | unset | the delay an operator intends to apply, 0-86400; reported only (the check measures raw data) |
| ``OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS`` | 1.0 | minimum spacing between requests, 0-120 |
| ``OPTIONS_DATA_MAX_PAGES`` | 2 | pages followed per paginated endpoint, 1-5 |
"""
from dataclasses import dataclass
from urllib.parse import urlsplit

PROVIDERS = ("none", "massive")
DEFAULT_BASE_URL = "https://api.massive.com"


class OptionsDataConfigError(ValueError):
    """Invalid options data configuration (the message never contains the key)."""


@dataclass(frozen=True)
class OptionsDataSettings:
    provider: str
    api_key: str
    base_url: str
    delay_seconds: int
    min_request_interval_seconds: float
    max_pages: int

    def __repr__(self):  # Never show the key.
        return (f"OptionsDataSettings(provider={self.provider!r}, base_url={self.base_url!r}, "
                f"delay_seconds={self.delay_seconds!r}, "
                f"min_request_interval_seconds={self.min_request_interval_seconds!r}, "
                f"max_pages={self.max_pages!r}, api_key=<redacted>)")

    __str__ = __repr__

    def public(self):
        """Settings safe to print (no key)."""
        return dict(provider=self.provider, base_url=self.base_url, delay_seconds=self.delay_seconds,
                    min_request_interval_seconds=self.min_request_interval_seconds, max_pages=self.max_pages,
                    api_key_configured=bool(self.api_key))


def _number(environ, name, default, low, high, cast=float):
    raw = environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = cast(str(raw).strip())
    except ValueError:
        raise OptionsDataConfigError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise OptionsDataConfigError(f"{name} must be between {low} and {high}")
    return value


def load_options_data_settings(environ):
    provider = (environ.get("OPTIONS_DATA_PROVIDER") or "none").strip().lower()
    if provider not in PROVIDERS:
        raise OptionsDataConfigError(f"OPTIONS_DATA_PROVIDER must be one of {', '.join(PROVIDERS)}")
    api_key = (environ.get("OPTIONS_DATA_API_KEY") or "").strip()
    if provider != "none" and not api_key:
        raise OptionsDataConfigError("OPTIONS_DATA_API_KEY is required for the configured options provider")
    base_url = (environ.get("OPTIONS_DATA_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    parts = urlsplit(base_url)
    if parts.scheme != "https" or not parts.hostname or parts.path or parts.query or parts.username or parts.password:
        raise OptionsDataConfigError("OPTIONS_DATA_BASE_URL must be an https origin without path, query or credentials")
    return OptionsDataSettings(
        provider=provider, api_key=api_key, base_url=base_url,
        delay_seconds=_number(environ, "OPTIONS_DATA_DELAY_SECONDS", None, 0, 86400, int),
        min_request_interval_seconds=_number(environ, "OPTIONS_DATA_MIN_REQUEST_INTERVAL_SECONDS", 1.0, 0, 120),
        max_pages=_number(environ, "OPTIONS_DATA_MAX_PAGES", 2, 1, 5, int))
