"""Bounded, credential-safe JSON-over-HTTPS client for market data providers (Phase 4B).

- Every request has a timeout, and redirects are never followed (a redirect is an error).
- Retries (at most ``max_retries``) cover **transient** failures only: connection
  errors, timeouts, HTTP 500/502/503/504 and HTTP 429 (rate limit).
- Backoff is ``backoff x 2^attempt``. For 429, a numeric ``Retry-After`` header is
  honoured, capped at ``max_rate_limit_wait_seconds``.
- Never retried: 401/403 (authentication/entitlement), other 4xx, malformed JSON and
  redirects.
- Errors and logs name the host, path and status only. Query strings, headers and
  the API key never appear.
"""
from decimal import Decimal
import json
import logging
import time
from urllib.parse import urlsplit

import requests

logger = logging.getLogger("market_data.http")
TRANSIENT_STATUS = frozenset({500, 502, 503, 504})
# Only these headers are recorded (short values, never credentials).
RATE_LIMIT_HEADERS = ("Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset")


class ProviderError(RuntimeError):
    """A provider request failed; ``kind`` is auth, rate_limit, http, transport, redirect, payload or budget."""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind, self.status = kind, status


def safe_target(url):
    parts = urlsplit(url)
    return f"{parts.hostname}{parts.path}"


class JsonHttpClient:
    def __init__(self, *, headers, timeout_seconds, max_retries, backoff_seconds, max_rate_limit_wait_seconds,
                 session=None, sleep=time.sleep):
        self._headers = dict(headers)
        self.timeout_seconds, self.max_retries = timeout_seconds, max_retries
        self.backoff_seconds, self.max_rate_limit_wait_seconds = backoff_seconds, max_rate_limit_wait_seconds
        self._session = session or requests.Session()
        self._sleep = sleep
        self.requests_made = 0
        self.max_requests = None      # Optional hard cap (live checks); None means unlimited.
        self.status_counts = {}       # HTTP status -> count (diagnostics; no payloads).
        self.rate_limit_headers = {}  # Last seen values of the safe, documented rate-limit headers.

    def get_json(self, url, params=None):
        target = safe_target(url)
        for attempt in range(self.max_retries + 1):
            last = attempt == self.max_retries
            if self.max_requests is not None and self.requests_made >= self.max_requests:
                raise ProviderError("budget", f"request budget of {self.max_requests} exhausted before {target}")
            self.requests_made += 1
            try:
                response = self._session.get(url, params=params, headers=self._headers, timeout=self.timeout_seconds,
                                             allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError) as error:
                kind = "timeout" if isinstance(error, requests.Timeout) else "connection error"
                if last:
                    raise ProviderError("transport", f"{kind} for {target} after {attempt + 1} attempts") from None
                self._wait(attempt, None, f"{kind}", target)
                continue
            status = response.status_code
            self.status_counts[status] = self.status_counts.get(status, 0) + 1
            for name in RATE_LIMIT_HEADERS:
                value = response.headers.get(name)
                if value is not None:
                    self.rate_limit_headers[name] = str(value)[:32]
            if status == 200:
                try:
                    return json.loads(response.text, parse_float=Decimal)
                except ValueError:
                    raise ProviderError("payload", f"malformed JSON from {target}") from None
            if status in (401, 403):
                raise ProviderError("auth", f"HTTP {status} (not authorized or not entitled) for {target}", status)
            if 300 <= status < 400:
                raise ProviderError("redirect", f"unexpected redirect (HTTP {status}) from {target}", status)
            if status == 429 or status in TRANSIENT_STATUS:
                kind = "rate_limit" if status == 429 else "http"
                if last:
                    raise ProviderError(kind, f"HTTP {status} for {target} after {attempt + 1} attempts", status)
                self._wait(attempt, response.headers.get("Retry-After") if status == 429 else None, f"HTTP {status}",
                           target)
                continue
            raise ProviderError("http", f"HTTP {status} for {target}", status)
        raise AssertionError("unreachable")

    def _wait(self, attempt, retry_after, reason, target):
        delay = self.backoff_seconds * (2 ** attempt)
        if retry_after is not None:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        delay = min(delay, self.max_rate_limit_wait_seconds)
        logger.warning("event=market_data_retry target=%s reason=%s attempt=%d wait_seconds=%.1f", target,
                       reason.replace(" ", "_"), attempt + 1, delay)
        self._sleep(delay)
