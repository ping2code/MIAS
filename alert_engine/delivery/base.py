"""Provider-neutral delivery contract (``phase12-delivery-v1``). Standard library only.

- ``DeliveryRequest``: what to send for one accepted AlertEvent on one channel. It's built purely from the alert by
  ``alert_engine.rendering.delivery_request``.
- ``DeliveryResult``: what happened. Status is ``delivered`` or ``failed``; the provider message id is set only when
  delivered; ``attempts`` is the number of provider calls; ``error_code`` comes from a closed set and is set only when
  failed.

Both are **runtime metadata**: they never alter an AlertEvent, its ``alert_id``, or Phase 12C state. Neither carries a
token, chat id, URL, header, payload or exception text.
"""
from dataclasses import dataclass

DELIVERY_CONTRACT_VERSION = "phase12-delivery-v1"
DELIVERED, FAILED = "delivered", "failed"
STATUSES = (DELIVERED, FAILED)
# Closed failure codes. Retried: timeout, transport_error, http_error (429/5xx). Not retried: the provider answered
# definitively (telegram_rejected) or ambiguously (invalid_response).
ERROR_CODES = ("timeout", "transport_error", "http_error", "invalid_response", "telegram_rejected")
TELEGRAM = "telegram"
CHANNELS = (TELEGRAM,)


@dataclass(frozen=True)
class DeliveryRequest:
    alert_id: str
    channel: str
    render_version: str
    text: str

    def __post_init__(self):
        if not (isinstance(self.alert_id, str) and self.alert_id.startswith("sha256:") and self.channel in CHANNELS
                and isinstance(self.render_version, str) and isinstance(self.text, str) and self.text):
            raise ValueError("delivery request is malformed")


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    provider_message_id: str
    attempts: int
    error_code: str

    def __post_init__(self):
        if self.status == DELIVERED:
            shape = isinstance(self.provider_message_id, str) and bool(self.provider_message_id) and self.error_code is None
        else:
            shape = self.status == FAILED and self.provider_message_id is None and self.error_code in ERROR_CODES
        attempts = isinstance(self.attempts, int) and not isinstance(self.attempts, bool) and self.attempts >= 1
        if not (shape and attempts):
            raise ValueError("delivery result is malformed")


class DeliveryAdapter:
    """Interface: ``channel`` and ``send(request) -> DeliveryResult``. An adapter never raises provider errors and
    never returns secret-bearing data."""
    channel = None

    def send(self, request):
        raise NotImplementedError
