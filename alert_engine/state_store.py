"""Redis boundary for Phase 12C alert state: current-state cache, seen markers and an atomic commit. Not the
historical authority; sealed AlertEvents are.

The adapter is given an existing ``redis.Redis`` client (it never creates a connection, reads settings or loads
``.env``) and a namespace, ``mias:phase12`` by default:
- ``<namespace>:seen:<alert hex>`` is the marker that an AlertEvent was accepted (value: its ``alert_id``);
- ``<namespace>:subject:<subject_key>`` is the canonical JSON of that subject's ``AlertSubjectState``.

Keys are deterministic and carry only ids and symbols: no secrets or object bodies.

``record(alert)``:
1. reads the seen marker and the subject state;
2. classifies with the pure ``state.decide``, so the logic stays in the pure layer;
3. for ``new`` only, commits the seen marker and the new subject state together, in one Lua compare-and-set that
   succeeds only if neither key changed since the read.

A concurrent change is re-read and re-decided (bounded); if that keeps failing, it fails closed. ``duplicate`` and
``terminal_suppressed`` write nothing.

**Fail closed:** every Redis error raises ``AlertStateUnavailable``. A Redis failure is never treated as a new alert.
Malformed stored state also fails closed. There is **no TTL and no cooldown**: markers never expire, so expiry can't
re-admit an alert.

``restore(replay_state)`` rebuilds an empty cache from the pure replay of sealed history (``state.replay``), for
example after a restart. It writes only missing keys, and checks every subject first, so a disagreement with the
replay writes nothing and fails closed. Restore is a single-writer recovery step.
"""
import json

import redis

from alert_engine.canonical import canonical_json
from alert_engine.state import NEW, ReplayState, decide, subject_key, validated_subject_state
from alert_engine.validation import AlertInputError, validated_alert

DEFAULT_NAMESPACE = "mias:phase12"
MAX_COMMIT_ATTEMPTS = 3

# KEYS[1] seen marker, KEYS[2] subject state; ARGV[1] expected subject value ('' = absent), ARGV[2] new subject value,
# ARGV[3] marker value. Returns 1 committed, 0 already seen, -1 subject changed since the read.
COMMIT = """
if redis.call('exists', KEYS[1]) == 1 then return 0 end
local current = redis.call('get', KEYS[2])
if current == false then current = '' end
if current ~= ARGV[1] then return -1 end
redis.call('set', KEYS[1], ARGV[3])
redis.call('set', KEYS[2], ARGV[2])
return 1
"""
# KEYS[1] subject key; ARGV[1] replayed value. Returns 1 written, 0 already equal, -1 conflicting.
RESTORE_SUBJECT = """
local current = redis.call('get', KEYS[1])
if current == false then redis.call('set', KEYS[1], ARGV[1]); return 1 end
if current == ARGV[1] then return 0 end
return -1
"""


class AlertStateUnavailable(RuntimeError):
    """Alert state could not be read or committed safely; nothing was recorded (fail closed)."""


def _text(value):
    return value.decode("utf-8") if isinstance(value, bytes) else value


class RedisAlertStateStore:
    def __init__(self, client, namespace=DEFAULT_NAMESPACE):
        if not isinstance(namespace, str) or not namespace or namespace.endswith(":") or " " in namespace:
            raise ValueError("namespace must be a non-empty key prefix without a trailing colon")
        self.client, self.namespace = client, namespace

    def seen_key(self, alert_id):
        return f"{self.namespace}:seen:{alert_id.split(':', 1)[1]}"

    def subject_redis_key(self, key):
        return f"{self.namespace}:subject:{key}"

    def _read(self, alert_id, key):
        try:
            seen = bool(self.client.exists(self.seen_key(alert_id)))
            raw = _text(self.client.get(self.subject_redis_key(key)))
        except redis.RedisError as error:
            raise AlertStateUnavailable(f"alert state unavailable ({type(error).__name__})") from None
        try:
            state = None if raw is None else validated_subject_state(json.loads(raw))
        except (ValueError, AlertInputError):
            raise AlertStateUnavailable("stored alert subject state is malformed") from None
        return seen, raw, state

    def subject_state(self, key):
        """The cached AlertSubjectState dict for a subject key, or None."""
        try:
            raw = _text(self.client.get(self.subject_redis_key(key)))
        except redis.RedisError as error:
            raise AlertStateUnavailable(f"alert state unavailable ({type(error).__name__})") from None
        try:
            return None if raw is None else validated_subject_state(json.loads(raw))
        except (ValueError, AlertInputError):
            raise AlertStateUnavailable("stored alert subject state is malformed") from None

    def record(self, alert):
        """The StateDecision for a sealed AlertEvent, committed atomically when it is new."""
        data = validated_alert(alert)
        key = subject_key(data)
        for _ in range(MAX_COMMIT_ATTEMPTS):
            seen, raw, state = self._read(data["alert_id"], key)
            decision = decide(data, state, seen)
            if decision.classification != NEW:
                return decision
            try:
                result = self.client.eval(COMMIT, 2, self.seen_key(data["alert_id"]), self.subject_redis_key(key),
                                          raw or "", canonical_json(decision.state.to_dict()), data["alert_id"])
            except redis.RedisError as error:
                raise AlertStateUnavailable(f"alert state commit failed ({type(error).__name__})") from None
            if int(result) == 1:
                return decision
        raise AlertStateUnavailable("alert state changed concurrently; nothing was recorded")

    def restore(self, replay_state):
        """Write a pure replay's state into an empty or consistent cache; refuse on any disagreement."""
        if not isinstance(replay_state, ReplayState):
            raise ValueError("restore needs a ReplayState from state.replay")
        written = dict(seen=0, subjects=0)
        values = {self.subject_redis_key(s.subject_key): canonical_json(s.to_dict()) for s in replay_state.subjects}
        try:
            # Check everything first, so a disagreement writes nothing.
            for key, value in values.items():
                current = _text(self.client.get(key))
                if current is not None and current != value:
                    raise AlertStateUnavailable("cached alert state disagrees with sealed history; not restored")
            for key, value in values.items():
                result = int(self.client.eval(RESTORE_SUBJECT, 1, key, value))
                if result == -1:
                    raise AlertStateUnavailable("cached alert state disagrees with sealed history; not restored")
                written["subjects"] += result
            for alert_id in replay_state.seen_alert_ids:
                written["seen"] += bool(self.client.set(self.seen_key(alert_id), alert_id, nx=True))
        except redis.RedisError as error:
            raise AlertStateUnavailable(f"alert state restore failed ({type(error).__name__})") from None
        return written
