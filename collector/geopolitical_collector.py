"""Opt-in official geopolitical ingestion. Telegram is disabled by default."""

import copy
import json
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

from analyzer import deduplicator
from analyzer.geopolitical_relevance import detect_relevance
from analyzer.geopolitical_scoring import score_geopolitical_event
from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert
from collector.geopolitical_normalizer import normalize_document
from collector.geopolitical_identity import PREFIX, resolve_identity
from collector import geopolitical_sources as sources
from shared.config import (
    GEOPOLITICAL_MAX_AGE_HOURS, GEOPOLITICAL_ALIAS_TTL_DAYS, DEDUP_TTL_SECONDS,
    GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED,
)
from shared.logger import get_logger

logger = get_logger("geopolitical_collector")
LEASE_SECONDS = 900
_shadow_last_failure = float("-inf")
RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""
CACHE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
 redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3]); return 1
end
return 0
"""


def state_key(event, kind):
    return PREFIX + kind + ":" + event["event_id"]


def freshness(event, now):
    raw = event.get("published_at")
    if not raw:
        return "missing_date"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        return "missing_date"
    age = (now - dt).total_seconds()
    return "future" if age < 0 else "stale" if age > GEOPOLITICAL_MAX_AGE_HOURS * 3600 else None


def analyze_geopolitical_event(event):
    from analyzer.openai_analyzer import analyze_market_event
    event["summary"] += "\nSummarize stated official facts only. Importance does not imply market direction. Do not infer unstated legal effects or numeric facts."
    return analyze_market_event(event)


def _shadow(event, *, make_current=True):
    """Opt-in historical copy of a resolved result; never touches Redis or delivery."""
    global _shadow_last_failure
    if not GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED:
        return
    try:
        from persistence.geopolitical_shadow import submit_geopolitical
        submit_geopolitical(event, make_current=make_current)
    except Exception:
        # Even import/initialization/enqueue failures cannot change collector outcomes.
        now = monotonic()
        if now - _shadow_last_failure >= 60:
            _shadow_last_failure = now
            logger.warning("Geopolitical shadow submission failed")


def deliver_geopolitical_alert(message):
    from alert_engine.telegram_notifier import send_telegram_alert
    return send_telegram_alert(message)


def format_geopolitical_alert(event):
    message = format_alert(event) + "\nOfficial action: " + event["policy_action"] + "\nStage: " + event["policy_stage"]
    # Show parser evidence independently of AI commentary; bound Telegram size.
    if event.get("evidence"):
        message += "\nOfficial evidence: " + event["evidence"][0]["quote"][:600]
    return message[:4000]


def _acquire(redis, key):
    token = uuid4().hex
    return token if redis.set(key, token, nx=True, ex=LEASE_SECONDS) else None


def _ttl():
    return max(DEDUP_TTL_SECONDS, GEOPOLITICAL_MAX_AGE_HOURS * 3600) + 1


def _analyze(event, enable_ai, stats):
    event = evaluate_alert(score_geopolitical_event(event))
    event["initial_decision"] = event["alert_decision"]
    if enable_ai and event["alert_decision"] == "ALERT":
        try:
            ai = analyze_geopolitical_event(copy.deepcopy(event))
            if type(ai["ai_confidence"]) is not int or not 0 <= ai["ai_confidence"] <= 100:
                raise ValueError("Invalid confidence")
            if ai["ai_sentiment"] not in {"STRONGLY_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONGLY_BEARISH"}:
                raise ValueError("Invalid sentiment")
            fields = ("ai_summary", "ai_why_it_matters", "ai_event_type", "ai_sentiment")
            if any(not isinstance(ai[k], str) or not ai[k].strip() or len(ai[k]) > 700 for k in fields):
                raise ValueError("Invalid AI enrichment")
            event.update({k: ai[k] for k in (*fields, "ai_confidence")})
        except Exception as error:
            stats["analysis_errors"] += 1
            logger.warning("Geopolitical analysis failed (%s)", type(error).__name__)
    return event


def _deliver(event, redis, stats):
    if event["alert_decision"] != "ALERT" or freshness(event, datetime.now(timezone.utc)):
        return
    lease = state_key(event, "delivery")
    token = _acquire(redis, lease)
    if not token:
        return
    try:
        if redis.get(state_key(event, "delivered")) or freshness(event, datetime.now(timezone.utc)):
            return
        try:
            result = deliver_geopolitical_alert(format_geopolitical_alert(event))
            if not isinstance(result, dict) or result.get("ok") is not True or not result.get("result", {}).get("message_id"):
                raise ValueError("Delivery unconfirmed")
        except Exception as error:
            stats["delivery_errors"] += 1
            logger.warning("Geopolitical delivery failed (%s)", type(error).__name__)
            return
        redis.set(state_key(event, "delivered"), str(result["result"]["message_id"]), ex=_ttl())
        stats["delivered"] += 1
    finally:
        redis.eval(RELEASE, 1, lease, token)


def _process(event, enable_ai, send_alerts, stats):
    redis = deduplicator.redis_client
    event = resolve_identity(event, redis, GEOPOLITICAL_ALIAS_TTL_DAYS * 86400)
    if event["identity_status"] != "resolved":
        stats["unresolved"] += 1
        event = evaluate_alert(score_geopolitical_event(event))
        if event["alert_decision"] == "ALERT":
            event["alert_decision"] = "DISPLAY_ONLY"
        event["score_reasons"].append("Unresolved action identity: alert withheld")
        return event
    reason = freshness(event, datetime.now(timezone.utc))
    if reason:
        stats[reason] += 1
        _shadow(event, make_current=False)
        return None
    cache_key, lease = state_key(event, "processed"), state_key(event, "event")
    cached = redis.get(cache_key)
    if cached:
        cached = json.loads(cached)
        required = {"event_id", "policy_id", "published_at", "headline", "symbols", "impact_score", "alert_decision", "policy_stage", "policy_action"}
        if not isinstance(cached, dict) or not required <= cached.keys() or cached["event_id"] != event["event_id"] or cached["policy_id"] != event["policy_id"]:
            raise ValueError("Invalid geopolitical cache")
        # Alias resolution can reveal an earlier original publication.
        cached["published_at"] = event["published_at"]
        cached["provenance"] = event["provenance"]
        stats["duplicates"] += 1
        if send_alerts:
            _deliver(cached, redis, stats)
        _shadow(cached)
        return None
    token = _acquire(redis, lease)
    if not token:
        stats["duplicates"] += 1
        return None
    try:
        if redis.get(cache_key):
            stats["duplicates"] += 1
            return None
        event = _analyze(event, enable_ai, stats)
        if not redis.eval(CACHE, 2, lease, cache_key, token, json.dumps(event), _ttl()):
            raise RuntimeError("Processing ownership expired")
        stats["processed"] += 1
    finally:
        redis.eval(RELEASE, 1, lease, token)
    if send_alerts:
        _deliver(event, redis, stats)
    _shadow(event)
    return event


def collect_geopolitical_events(*, enable_ai=True, send_alerts=False):
    stats = dict(fetched=0, relevant=0, processed=0, duplicates=0, stale=0, future=0,
                 missing_date=0, invalid=0, fetch_errors=0, state_errors=0, unresolved=0,
                 analysis_errors=0, delivery_errors=0, delivered=0)
    events = []
    for agency in sources.SOURCES:
        try:
            documents = sources.fetch_documents(agency)
        except Exception as error:
            stats["fetch_errors"] += 1
            logger.warning("Geopolitical source %s failed (%s)", agency, type(error).__name__)
            continue
        for document in documents:
            if document.get("source_error"):
                stats["fetch_errors"] += 1
                logger.warning("Geopolitical article unavailable from %s (%s)", agency, document["source_error"])
                continue
            stats["fetched"] += 1
            try:
                event = detect_relevance(normalize_document(document))
                if not event["relevant"]:
                    continue
                stats["relevant"] += 1
                reason = freshness(event, datetime.now(timezone.utc))
                if reason:
                    stats[reason] += 1
                    logger.info("Skipping %s geopolitical event %s", reason, event["document_id"])
                    continue
            except (ValueError, KeyError, TypeError, OverflowError):
                stats["invalid"] += 1
                continue
            try:
                result = _process(event, enable_ai, send_alerts, stats)
                if result:
                    events.append(result)
            except (deduplicator.redis.RedisError, ValueError, TypeError, KeyError, RuntimeError) as error:
                stats["state_errors"] += 1
                logger.warning("Geopolitical state failed closed (%s)", type(error).__name__)
    return events, stats
