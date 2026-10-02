"""Redis delivery idempotency for Phase 12D: "has this accepted alert been delivered to this channel?"

This is separate from Phase 12C ("has this AlertEvent been accepted?"); a Phase 12C ``seen`` key is never used here.
Keys (deterministic; ids and the channel name only, no token, chat id or body):
- ``<namespace>:delivery:<channel>:<alert hex>:lease``: a claim with a random token and a TTL
  (``lease_seconds``), so a crashed sender's claim expires and the alert becomes retryable;
- ``<namespace>:delivery:<channel>:<alert hex>:delivered``: the provider message id. **Persistent, no TTL**, so
  expiry can never cause a re-send. This is a live cache; durable delivery receipts belong to Phase 12E.

``deliver(request, adapter)``:
1. already delivered: ``already_delivered`` (no send);
2. otherwise claim the lease (``SET NX EX``). If another sender holds it: ``in_progress`` (no send);
3. recheck delivered under the lease, send once through the adapter (which does its own bounded retries), and write
   the delivered marker **only** for a confirmed ``delivered`` result. A ``failed`` result writes nothing, so the
   alert stays retryable;
4. release the lease with a compare-and-delete (only our own token), as the collectors do.

**Fail closed:** any Redis error before the send raises ``DeliveryStateUnavailable``, and nothing is sent. If the
delivered marker can't be written after a confirmed send, it raises and keeps the lease until its TTL, so no other
sender re-sends at once. **At-least-once:** a crash or Redis
failure between Telegram accepting a message and the marker write can cause one duplicate send after the lease
expires. Exactly-once is not guaranteed. There is no cooldown.
"""
from dataclasses import dataclass
from uuid import uuid4

import redis

from alert_engine.delivery.base import DELIVERED, DeliveryRequest, DeliveryResult

DEFAULT_NAMESPACE = "mias:phase12"
DEFAULT_LEASE_SECONDS = 300
SENT, ALREADY_DELIVERED, IN_PROGRESS = "sent", "already_delivered", "in_progress"
ACTIONS = (SENT, ALREADY_DELIVERED, IN_PROGRESS)
RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""


class DeliveryStateUnavailable(RuntimeError):
    """Delivery state could not be read or written safely (fail closed)."""


@dataclass(frozen=True)
class DeliveryOutcome:
    action: str                       # sent | already_delivered | in_progress
    result: DeliveryResult            # the adapter's result when sent; None otherwise


class RedisDeliveryGuard:
    def __init__(self, client, namespace=DEFAULT_NAMESPACE, lease_seconds=DEFAULT_LEASE_SECONDS):
        if not isinstance(namespace, str) or not namespace or namespace.endswith(":") or " " in namespace:
            raise ValueError("namespace must be a non-empty key prefix without a trailing colon")
        if not (isinstance(lease_seconds, int) and lease_seconds > 0):
            raise ValueError("lease_seconds must be a positive integer")
        self.client, self.namespace, self.lease_seconds = client, namespace, lease_seconds

    def key(self, request, kind):
        return f"{self.namespace}:delivery:{request.channel}:{request.alert_id.split(':', 1)[1]}:{kind}"

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except redis.RedisError as error:
            raise DeliveryStateUnavailable(f"delivery state unavailable ({type(error).__name__})") from None

    def deliver(self, request, adapter):
        if not isinstance(request, DeliveryRequest) or getattr(adapter, "channel", None) != request.channel:
            raise ValueError("request and adapter channels do not match")
        delivered, lease = self.key(request, "delivered"), self.key(request, "lease")
        if self._call(self.client.exists, delivered):
            return DeliveryOutcome(ALREADY_DELIVERED, None)
        token = uuid4().hex
        if not self._call(self.client.set, lease, token, nx=True, ex=self.lease_seconds):
            return DeliveryOutcome(IN_PROGRESS, None)
        keep_lease = False
        try:
            if self._call(self.client.exists, delivered):
                return DeliveryOutcome(ALREADY_DELIVERED, None)
            result = adapter.send(request)
            if result.status == DELIVERED:
                try:
                    self.client.set(delivered, result.provider_message_id)
                except redis.RedisError as error:
                    # Sent but not recorded: keep the lease until its TTL so no other sender re-sends at once.
                    keep_lease = True
                    raise DeliveryStateUnavailable(
                        f"delivered but the delivered marker was not recorded ({type(error).__name__})") from None
            return DeliveryOutcome(SENT, result)
        finally:
            if not keep_lease:
                try:
                    self.client.eval(RELEASE, 1, lease, token)
                except redis.RedisError:
                    pass                 # the lease TTL recovers it; never mask the primary outcome or error
