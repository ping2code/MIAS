"""Hardened Telegram delivery adapter (``phase12-delivery-v1``). The only module here that talks to a provider.

- **Explicit configuration:** the bot token and chat id are constructor arguments. There's no ``load_dotenv``, no
  environment or ``.env`` read, and no secret lookup at import.
- **Confirmation:** a send is ``delivered`` only when the HTTP status is 200, the JSON body has ``ok`` true, and
  ``result.message_id`` exists.
- **Secret-safe:** the token is necessarily in Telegram's URL path, so the URL, request, headers, payload and
  exception text never leave this module. Results carry only a closed ``error_code``, ``repr`` hides the token, and
  provider exceptions are never re-raised.
- **Bounded retries:** at most ``max_attempts`` calls, retrying only ``timeout``, ``transport_error`` and
  ``http_error`` (429/5xx). There's a fixed backoff and no jitter. ``sleep`` is injectable, and the backoff is
  runtime-only.
- **Timeouts:** an explicit ``(connect, read)`` timeout on every call, so no call can block indefinitely.
- **At-least-once:** if Telegram accepts a message but the confirmation is lost (a timeout after acceptance), or the
  process dies before the caller records delivery, the message can be sent again. Exactly-once is not guaranteed.
"""
import time

import requests

from alert_engine.delivery.base import DELIVERED, FAILED, TELEGRAM, DeliveryAdapter, DeliveryRequest, DeliveryResult

API_BASE = "https://api.telegram.org"
DEFAULT_TIMEOUT = (5.0, 10.0)        # (connect, read) seconds
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS = 2.0
MAX_TEXT_LENGTH = 4096               # Telegram's message limit
RETRYABLE = ("timeout", "transport_error", "http_error")


class TelegramAdapter(DeliveryAdapter):
    channel = TELEGRAM

    def __init__(self, bot_token, chat_id, *, session=None, timeout=DEFAULT_TIMEOUT, max_attempts=DEFAULT_MAX_ATTEMPTS,
                 backoff_seconds=DEFAULT_BACKOFF_SECONDS, sleep=time.sleep):
        if not isinstance(bot_token, str) or not bot_token or not isinstance(chat_id, (str, int)) or chat_id == "":
            raise ValueError("bot_token and chat_id must be provided explicitly")
        if not (isinstance(max_attempts, int) and 1 <= max_attempts <= 10):
            raise ValueError("max_attempts must be between 1 and 10")
        self._bot_token, self._chat_id = bot_token, chat_id
        self._session = session or requests.Session()
        self._timeout, self._max_attempts, self._backoff, self._sleep = timeout, max_attempts, backoff_seconds, sleep

    def __repr__(self):
        return f"TelegramAdapter(channel={self.channel!r}, max_attempts={self._max_attempts})"

    def _attempt(self, text):
        """(message_id, None) when confirmed, else (None, error_code). Nothing provider-supplied escapes."""
        try:
            response = self._session.post(f"{API_BASE}/bot{self._bot_token}/sendMessage",
                                          json={"chat_id": self._chat_id, "text": text},
                                          timeout=self._timeout, allow_redirects=False)
        except requests.Timeout:
            return None, "timeout"
        except Exception:                # requests or anything else: never let provider text escape
            return None, "transport_error"
        status = getattr(response, "status_code", None)
        if status == 429 or (isinstance(status, int) and status >= 500):
            return None, "http_error"
        try:
            body = response.json()
        except ValueError:
            return None, "invalid_response"
        if not isinstance(body, dict):
            return None, "invalid_response"
        if status != 200 or body.get("ok") is not True:
            return None, "telegram_rejected"
        message_id = body.get("result", {}).get("message_id") if isinstance(body.get("result"), dict) else None
        if isinstance(message_id, bool) or not isinstance(message_id, (int, str)) or message_id == "":
            return None, "invalid_response"
        return str(message_id), None

    def send(self, request):
        if not isinstance(request, DeliveryRequest) or request.channel != self.channel:
            raise ValueError("request is not a Telegram delivery request")
        if len(request.text) > MAX_TEXT_LENGTH:
            raise ValueError("rendered text exceeds the Telegram message limit")
        error = None
        for attempt in range(1, self._max_attempts + 1):
            message_id, error = self._attempt(request.text)
            if message_id is not None:
                return DeliveryResult(DELIVERED, message_id, attempt, None)
            if error not in RETRYABLE or attempt == self._max_attempts:
                return DeliveryResult(FAILED, None, attempt, error)
            self._sleep(self._backoff)
        return DeliveryResult(FAILED, None, self._max_attempts, error)
