"""Opt-in Treasury releases, auctions and separate daily yield observations."""

import argparse
import copy
import json
import math
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

from analyzer import deduplicator
from analyzer.treasury_scoring import score_treasury_event
from alert_engine.decision_engine import evaluate_alert
from alert_engine.formatter import format_alert
from collector import treasury_sources as sources
from collector.treasury_normalizer import (
    EASTERN, normalize_release, normalize_debt_letter, normalize_auction, publication,
)
from shared.config import (
    DEDUP_TTL_SECONDS, TREASURY_MAX_AGE_HOURS, TREASURY_YIELD_MOVE_BPS, TREASURY_PERSISTENCE_SHADOW_ENABLED,
)
from shared.logger import get_logger


logger = get_logger("treasury_collector")
LEASE_SECONDS = 900
_shadow_last_failure = float("-inf")
RELEASE_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
CACHE_IF_OWNER = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('set', KEYS[2], ARGV[2], 'EX', ARGV[3])
    return 1
end
return 0
"""


def analyze_treasury_event(event):
    from analyzer.openai_analyzer import analyze_market_event
    event["summary"] += (
        "\nAnalysis constraints: summarize only stated official facts. Do not revise numeric facts. "
        "No authoritative when-issued or consensus comparison is supplied: do not infer auction "
        "tails, surprises, or unusually strong/weak demand."
    )
    return analyze_market_event(event)


def _shadow(event, *, make_current=True):
    global _shadow_last_failure
    if not TREASURY_PERSISTENCE_SHADOW_ENABLED:
        return
    try:
        from persistence.treasury_shadow import submit_treasury
        submit_treasury(event, make_current=make_current)
    except Exception:
        # Even import/initialization/enqueue failures cannot change collector outcomes.
        now = monotonic()
        if now - _shadow_last_failure >= 60:
            _shadow_last_failure = now
            logger.warning("Treasury shadow submission failed")


def deliver_treasury_alert(message):
    from alert_engine.telegram_notifier import send_telegram_alert
    return send_telegram_alert(message)


def format_treasury_alert(event):
    message = format_alert(event)
    if event.get("metrics"):
        # Parser facts remain visible independently of optional AI commentary.
        message += "\nOfficial numeric facts: " + json.dumps(event["metrics"], sort_keys=True)
    return message


def state_key(event, kind):
    return f"mias:treasury:{kind}:{event['event_id']}"


def freshness(event, now, *, observation_ok=False):
    stamp = event.get("published_at")
    if not stamp and observation_ok and event["event_type"] == "treasury_yield_observation":
        stamp, _ = publication(event["observation_date"])
    if not stamp:
        return "missing_date"
    date = datetime.fromisoformat(stamp)
    if date.tzinfo is None:
        return "missing_date"
    age = (now - date).total_seconds()
    return "future" if age < 0 else "stale" if age > TREASURY_MAX_AGE_HOURS * 3600 else None


def _ttl():
    return max(DEDUP_TTL_SECONDS, TREASURY_MAX_AGE_HOURS * 3600) + 1


def _acquire(key):
    token = uuid4().hex
    return token if deduplicator.redis_client.set(key, token, nx=True, ex=LEASE_SECONDS) else None


def _release(key, token):
    deduplicator.redis_client.eval(RELEASE_LEASE, 1, key, token)


def _analyze(event, enable_ai, stats):
    event = evaluate_alert(score_treasury_event(event, yield_threshold_bps=TREASURY_YIELD_MOVE_BPS))
    event["initial_decision"] = event["alert_decision"]
    # A yield observation can be retained without pretending its date is publication.
    event["alert_eligible"] = not freshness(event, datetime.now(timezone.utc))
    if event["event_type"] == "treasury_yield_observation" and not event["published_at"]:
        logger.info("Retaining Treasury yield observation %s; publication unverified, alerting disabled", event["observation_date"])
    if not event["alert_eligible"] and event["alert_decision"] == "ALERT":
        event["alert_decision"] = "DISPLAY_ONLY"
        event["score_reasons"].append("Publication unverified or no longer fresh; no alert")
    if enable_ai and event["alert_decision"] == "ALERT":
        try:
            candidate = analyze_treasury_event(copy.deepcopy(event))
            confidence = candidate["ai_confidence"]
            if type(confidence) is not int or not 0 <= confidence <= 100:
                raise ValueError("Invalid AI confidence")
            if candidate["ai_sentiment"] not in {"STRONGLY_BULLISH", "BULLISH", "NEUTRAL", "BEARISH", "STRONGLY_BEARISH"}:
                raise ValueError("Invalid AI sentiment")
            fields = ("ai_summary", "ai_why_it_matters", "ai_event_type", "ai_sentiment")
            if any(not isinstance(candidate[k], str) or not candidate[k].strip() or len(candidate[k]) > 700 for k in fields):
                raise ValueError("Invalid AI enrichment")
            # Copy only enrichment. Parser-owned metrics, dates, identity and scores
            # cannot be overwritten by the analyzer, even if it mutates its input.
            event.update({k: candidate[k] for k in (*fields, "ai_confidence")})
        except Exception as error:
            stats["analysis_errors"] += 1
            logger.error("Treasury analysis failed (%s)", type(error).__name__)
    return event


def _deliver(event, stats):
    if event["alert_decision"] != "ALERT":
        return
    redis = deduplicator.redis_client
    key = state_key(event, "event") + ":delivery"
    token = _acquire(key)
    if not token:
        return
    try:
        if freshness(event, datetime.now(timezone.utc)) or redis.get(state_key(event, "delivered")):
            return
        try:
            message = format_treasury_alert(event)
            result = deliver_treasury_alert(message)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError("Telegram delivery unconfirmed")
        except Exception as error:
            stats["delivery_errors"] += 1
            logger.error("Treasury delivery failed (%s)", type(error).__name__)
            return
        redis.set(state_key(event, "delivered"), "1", ex=_ttl())
        stats["delivered"] += 1
    finally:
        _release(key, token)


def _process(event, enable_ai, send_alerts, stats):
    redis = deduplicator.redis_client
    cache_key = state_key(event, "processed")
    cached = redis.get(cache_key)
    if cached:
        cached = json.loads(cached)
        if not isinstance(cached, dict) or cached.get("event_id") != event["event_id"]:
            raise ValueError("Invalid Treasury cache")
        stats["duplicates"] += 1
        if send_alerts:
            _deliver(cached, stats)
        _shadow(cached)
        return None
    lease = state_key(event, "event")
    token = _acquire(lease)
    if not token:
        stats["duplicates"] += 1
        return None
    try:
        if redis.get(cache_key):
            stats["duplicates"] += 1
            return None
        event = _analyze(event, enable_ai, stats)
        if not redis.eval(CACHE_IF_OWNER, 2, lease, cache_key, token, json.dumps(event), _ttl()):
            raise RuntimeError("Treasury processing lease expired")
        stats["processed"] += 1
    finally:
        _release(lease, token)
    if send_alerts:
        _deliver(event, stats)
    _shadow(event)
    return event


def collect_treasury_events(*, enable_ai=True, send_alerts=False):
    stats = dict(fetched=0, relevant=0, processed=0, duplicates=0, stale=0, future=0,
                 missing_date=0, invalid=0, fetch_errors=0, state_errors=0,
                 analysis_errors=0, delivery_errors=0, delivered=0, yield_observations=0, unverified_yields=0)
    pending, events, seen = [], [], set()
    today = datetime.now(EASTERN).date()
    for index in sources.INDEX_URLS:
        try:
            documents = sources.fetch_index_documents(index)
        except Exception as error:
            stats["fetch_errors"] += 1
            logger.error("Treasury index failed (%s)", type(error).__name__)
            continue
        for document in documents:
            url = document["url"]
            if url in seen:
                continue
            seen.add(url)
            try:
                if document["kind"] == "letter":
                    data = sources.fetch(url, {"application/pdf"})
                    text = sources.extract_pdf_text(data)
                    event = normalize_debt_letter(text, url)
                else:
                    data = sources.fetch(url, {"text/html"})
                    event = normalize_release(data.decode("utf-8-sig"), url)
                stats["fetched"] += 1
                pending.append(event)
            except Exception as error:
                stats["invalid"] += 1
                logger.warning("Treasury document unavailable/invalid (%s)", type(error).__name__)
    try:
        rows = sources.fetch_auctions(today, lookback_days=math.ceil(TREASURY_MAX_AGE_HOURS / 24) + 1)
        stats["fetched"] += len(rows)
        for row in rows:
            for stage in ("announcement", "result"):
                if stage == "result" and not row.get("pdfFilenameCompetitiveResults"):
                    continue
                try:
                    pending.append(normalize_auction(row, stage))
                except (ValueError, KeyError, TypeError):
                    stats["invalid"] += 1
                    logger.warning("Invalid Treasury auction %s", stage)
    except Exception as error:
        stats["fetch_errors"] += 1
        logger.error("Treasury auctions unavailable (%s)", type(error).__name__)
    try:
        observations = sources.fetch_yields(today)
        stats["fetched"] += len(observations)
        stats["yield_observations"] += len(observations)
        stats["unverified_yields"] += sum(not e.get("published_at") for e in observations)
        pending.extend(observations)
    except Exception as error:
        stats["fetch_errors"] += 1
        logger.error("Treasury yields unavailable (%s)", type(error).__name__)
    for event in pending:
        stats["relevant"] += 1
        reason = freshness(event, datetime.now(timezone.utc), observation_ok=True)
        if reason:
            stats[reason] += 1
            logger.info("Skipping %s Treasury event category=%s", reason, event["treasury_category"])
            _shadow(event, make_current=False)
            continue
        try:
            result = _process(event, enable_ai, send_alerts, stats)
            if result:
                events.append(result)
        except (deduplicator.redis.RedisError, ValueError, TypeError, KeyError, RuntimeError) as error:
            stats["state_errors"] += 1
            logger.error("Treasury state unavailable (%s)", type(error).__name__)
    return events, stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--send-alerts", action="store_true")
    args = parser.parse_args()
    events, stats = collect_treasury_events(enable_ai=not args.no_ai, send_alerts=args.send_alerts)
    print(json.dumps(stats, indent=2))
    for event in events:
        print(format_treasury_alert(event))
    if any(stats[k] for k in ("invalid", "fetch_errors", "state_errors", "delivery_errors")):
        raise SystemExit(1)
