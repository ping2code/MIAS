"""Market data provider settings from the process environment only (never ``.env``); validated, key never shown.

| Variable | Default | Meaning |
|---|---|---|
| ``MARKET_DATA_PROVIDER`` | ``none`` | ``none`` or ``polygon`` (alias ``massive``) |
| ``MARKET_DATA_API_KEY`` | unset | required for ``polygon``; sent only as an HTTP Authorization header |
| ``MARKET_DATA_BASE_URL`` | provider default | HTTPS origin, e.g. ``https://api.polygon.io`` |
| ``MARKET_DATA_HTTP_TIMEOUT_SECONDS`` | 10 | per-request timeout, 1-120 |
| ``MARKET_DATA_MAX_RETRIES`` | 3 | retries for transient failures only, 0-5 |
| ``MARKET_DATA_RETRY_BACKOFF_SECONDS`` | 1.0 | first backoff; doubles per retry, 0-60 |
| ``MARKET_DATA_MAX_RATE_LIMIT_WAIT_SECONDS`` | 60 | cap on a single rate-limit wait, 1-600 |
| ``MARKET_DATA_DELAY_SECONDS`` | 900 | data is treated as available only up to now minus this, 0-86400 |
| ``MARKET_DATA_ADJUSTED`` | true | split-adjusted bars |
| ``MARKET_DATA_INCLUDE_EXTENDED_HOURS`` | false | keep pre/post-market intraday bars |
"""
from dataclasses import dataclass, field
from urllib.parse import urlsplit

PROVIDERS = ("none", "polygon")
ALIASES = dict(massive="polygon")
DEFAULT_BASE_URL = dict(polygon="https://api.polygon.io")


class MarketDataConfigError(ValueError):
    """Invalid market data configuration; messages name the setting, never its value."""


def _bool(environ, name, default):
    raw = (environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw not in ("true", "false"):
        raise MarketDataConfigError(f"{name} must be true or false")
    return raw == "true"


def _number(environ, name, default, low, high, kind=float):
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = kind(raw)
    except ValueError:
        raise MarketDataConfigError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise MarketDataConfigError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class MarketDataSettings:
    provider: str
    base_url: str
    timeout_seconds: float
    max_retries: int
    backoff_seconds: float
    max_rate_limit_wait_seconds: float
    delay_seconds: int
    adjusted: bool
    include_extended_hours: bool
    api_key: str = field(default=None, repr=False)

    def safe_view(self):
        """Printable settings: the API key is reported only as set/unset."""
        return dict(provider=self.provider, base_url=self.base_url, timeout_seconds=self.timeout_seconds,
                    max_retries=self.max_retries, backoff_seconds=self.backoff_seconds,
                    max_rate_limit_wait_seconds=self.max_rate_limit_wait_seconds, delay_seconds=self.delay_seconds,
                    adjusted=self.adjusted, include_extended_hours=self.include_extended_hours,
                    api_key="set" if self.api_key else "unset")


def load_market_data_settings(environ):
    provider = (environ.get("MARKET_DATA_PROVIDER") or "none").strip().lower()
    provider = ALIASES.get(provider, provider)
    if provider not in PROVIDERS:
        raise MarketDataConfigError(f"MARKET_DATA_PROVIDER must be one of {', '.join(PROVIDERS)} (or massive)")
    api_key = (environ.get("MARKET_DATA_API_KEY") or "").strip() or None
    if provider != "none" and not api_key:
        raise MarketDataConfigError("MARKET_DATA_API_KEY is required for the configured provider")
    base_url = (environ.get("MARKET_DATA_BASE_URL") or "").strip().rstrip("/") or DEFAULT_BASE_URL.get(provider)
    if base_url:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.query or parts.fragment or parts.username:
            raise MarketDataConfigError("MARKET_DATA_BASE_URL must be a plain https URL")
    return MarketDataSettings(
        provider=provider, base_url=base_url,
        timeout_seconds=_number(environ, "MARKET_DATA_HTTP_TIMEOUT_SECONDS", 10.0, 1, 120),
        max_retries=_number(environ, "MARKET_DATA_MAX_RETRIES", 3, 0, 5, int),
        backoff_seconds=_number(environ, "MARKET_DATA_RETRY_BACKOFF_SECONDS", 1.0, 0, 60),
        max_rate_limit_wait_seconds=_number(environ, "MARKET_DATA_MAX_RATE_LIMIT_WAIT_SECONDS", 60.0, 1, 600),
        delay_seconds=_number(environ, "MARKET_DATA_DELAY_SECONDS", 900, 0, 86_400, int),
        adjusted=_bool(environ, "MARKET_DATA_ADJUSTED", True),
        include_extended_hours=_bool(environ, "MARKET_DATA_INCLUDE_EXTENDED_HOURS", False),
        api_key=api_key)
