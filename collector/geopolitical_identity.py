"""Deterministic action registry with atomic aliases; never fuzzy/AI matching."""

import json
import re

from collector.geopolitical_normalizer import digest

PREFIX = "mias:geopolitical:"
# All roots and aliases are checked and committed in one Redis transaction.
# Conflicting established roots are withheld, never silently merged after delivery.
RESOLVE = """
local root = nil
for i=1,#KEYS do
  local value = redis.call('get', KEYS[i])
  if value then
    if root and root ~= value then return redis.error_reply('Conflicting policy aliases') end
    root = value
  end
end
root = root or ARGV[1]
local key = ARGV[2] .. root
local record = cjson.decode(ARGV[3])
local existing = redis.call('get', key)
if existing then
  local old = cjson.decode(existing)
  if old.published_at and old.published_at ~= cjson.null and
     (record.published_at == cjson.null or old.published_at < record.published_at) then
    record.published_at = old.published_at
  end
  local seen = {}
  for _,p in ipairs(record.provenance) do seen[p.document_id] = true end
  for _,p in ipairs(old.provenance) do
    if not seen[p.document_id] then table.insert(record.provenance, p) end
  end
end
redis.call('set', key, cjson.encode(record), 'EX', ARGV[4])
for i=1,#KEYS do redis.call('set', KEYS[i], root, 'EX', ARGV[4]) end
return {root, cjson.encode(record)}
"""


def _durable_candidate(durable, redis, aliases, anchors, stage, candidate):
    """Phase 2H opt-in: consult the durable registry only when no alias exists in Redis.

    Redis stays first: any existing alias means the unchanged Redis path decides
    (and the RESOLVE script re-checks atomically, so Redis also wins races). A
    durable root is only a *candidate*; misses, conflicts, errors and timeouts
    leave today's candidate unchanged.
    """
    if any(redis.get(key) for key in aliases):
        try:
            durable.redis_hit()
        except Exception:
            pass
        return candidate
    try:
        root = durable.lookup(anchors, stage)
    except Exception:
        return candidate
    return root if isinstance(root, str) and re.fullmatch(r"[0-9a-f]{64}", root) else candidate


def resolve_identity(event, redis, alias_ttl, durable=None):
    anchors = sorted(set(event["identity_anchors"]))
    if not anchors:
        event["identity_status"] = "unresolved"
        return event
    # A link to an old rule alone does not make a new press statement a new action.
    # Stage-specific aliases permit proposal/final transitions without collision.
    stage = [event["event_type"], event["policy_stage"], event["revision_id"]]
    aliases = [PREFIX + "alias:" + digest([a, *stage]) for a in anchors]
    # A publisher can reuse a URL/native release ID for a different instrument.
    # Bind its document alias to the explicit anchors as well as the stage.
    aliases.append(PREFIX + "alias:" + digest([event["document_id"], anchors, *stage]))
    candidate = digest(["geopolitical-v1", anchors[0], *stage])
    if durable is not None:
        candidate = _durable_candidate(durable, redis, aliases, anchors, stage, candidate)
    record = {"published_at": event["published_at"], "provenance": event["provenance"]}
    root, payload = redis.eval(RESOLVE, len(aliases), *aliases, candidate,
                               PREFIX + "policy:", json.dumps(record), alias_ttl)
    if isinstance(root, bytes):
        root = root.decode()
    record = json.loads(payload)
    event.update(policy_id=root, event_id=digest(["event-v1", root]), identity_status="resolved",
                 published_at=record["published_at"], provenance=record["provenance"])
    return event
