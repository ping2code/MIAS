"""Fixed test-only Federal Reserve replay corpus.

Rows are exactly what the real Fed collector submits to its shadow hook while
processing synthetic official-style RSS entries against an in-memory Redis at
fixed clocks (no network, no live Redis, OpenAI or Telegram). Two synthetic
rows derived from a collector event cover source-ordered material revisions,
which the collector itself never produces (cached events are never re-analyzed).
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from tests.test_fed_pipeline import fed, deduplicator, entry

MANIFEST = Path(__file__).parent / "fixtures/persistence/fed_readiness_v1.json"
NOW = datetime(2026, 9, 17, 18, tzinfo=timezone.utc)
AI = dict(ai_summary="Synthetic validated summary", ai_sentiment="NEUTRAL", ai_confidence=80,
          ai_why_it_matters="Synthetic visible monetary-policy context", ai_event_type="official announcement")


class FedMemoryRedis:
    """get/set(nx, ex) with expiry against a controllable clock (as the Fed collector uses Redis)."""

    def __init__(self, now=NOW):
        self.data, self.now = {}, now

    def get(self, key):
        value, expiry = self.data.get(key, (None, self.now))
        return value if expiry > self.now else None

    def set(self, key, value, *, ex, nx=False):
        if nx and self.get(key) is not None:
            return None
        self.data[key] = (value, self.now + timedelta(seconds=ex))
        return True


def enrich(event):
    return dict(event, **AI)


def run_collector(entries, redis, *, now=NOW, enabled=True, submit=None, ai=True, send_alerts=False,
                  deliver=None):
    """One real Fed collector pass; returns observable outputs and captured shadow submissions."""
    submitted = []
    def capture(value, **kwargs):
        submitted.append((deepcopy(value), kwargs))
        if submit:
            return submit(value, **kwargs)
    from persistence import fed_shadow
    redis.now = now
    with patch.object(fed.requests, "get") as http, \
         patch.object(fed.feedparser, "parse", return_value={"entries": deepcopy(entries), "bozo": False}), \
         patch.object(deduplicator, "redis_client", redis), \
         patch.object(fed, "FED_PERSISTENCE_SHADOW_ENABLED", enabled), \
         patch.object(fed_shadow, "submit_fed", side_effect=capture), \
         patch.object(fed, "analyze_fed_event", side_effect=enrich) as analyze, \
         patch.object(fed, "deliver_fed_alert", side_effect=deliver or (lambda message: {"ok": True, "result": {"message_id": 1}})) as send, \
         patch.object(fed.logger, "info"), patch.object(fed, "datetime", wraps=datetime) as clock:
        http.return_value.status_code = 200
        clock.now.side_effect = lambda tz=None: now
        events, stats = fed.collect_fed_events(enable_ai=ai, send_alerts=send_alerts)
        outputs = (events, stats, sorted(redis.data.items()), send.call_count, analyze.call_count)
    return outputs, submitted


ENTRIES = dict(
    policy_statement=entry("Federal Reserve issues FOMC statement", "monetary20260916a"),
    policy_action=entry("Federal Reserve raises federal funds rate target range", "monetary20260916b"),
    economic_projections=entry("FOMC releases summary of economic projections", "monetary20260916c"),
    minutes=entry("Minutes of the Federal Open Market Committee, July 28-29, 2026", "monetary20260916d"),
    policy_communication=entry("Federal Reserve Board announces policy framework review", "monetary20260916e"),
    symbol_mention=entry("Federal Reserve discusses NVIDIA data center lending", "monetary20260916f"),
    stale=dict(entry("Federal Reserve issues FOMC statement", "monetary20260902a"), published="Wed, 02 Sep 2026 14:00:00 -0400"),
    missing_date=dict(entry("Federal Reserve Board issues enforcement communication", "monetary20260916g"), published=None),
)
ENTRIES["cosmetic_summary"] = dict(ENTRIES["policy_statement"], summary="<p>Monetary policy  &amp;  the economy (updated page).</p>")
ENTRIES["headline_case"] = dict(ENTRIES["policy_statement"], title="FEDERAL RESERVE ISSUES FOMC STATEMENT")

# (label, entry names, clock, send_alerts, deliveries succeed, ai enabled)
STEPS = [
    ("policy_statement_dry_run", ["policy_statement"], NOW, False, True, True),
    ("policy_action", ["policy_action"], NOW, False, True, True),
    ("economic_projections", ["economic_projections"], NOW, False, True, True),
    ("minutes_ai_disabled", ["minutes"], NOW, False, True, False),
    ("policy_communication", ["policy_communication"], NOW, False, True, True),
    ("symbol_mention", ["symbol_mention"], NOW, False, True, True),
    ("duplicate", ["policy_statement"], NOW, False, True, True),
    ("cosmetic_summary", ["cosmetic_summary"], NOW, False, True, True),
    ("headline_case", ["headline_case"], NOW, False, True, True),
    ("delivery_failure", ["policy_action"], NOW + timedelta(minutes=5), True, False, True),
    ("delivery_retry", ["policy_action"], NOW + timedelta(minutes=10), True, True, True),
    ("cached_delivery_after_dry_run", ["policy_statement"], NOW + timedelta(minutes=10), True, True, True),
    ("provenance_duplicate", ["policy_statement"], NOW + timedelta(minutes=15), True, True, True),
    ("stale", ["stale"], NOW, False, True, True),
    ("missing_date", ["missing_date"], NOW, False, True, True),
    ("stale_rediscovery", ["policy_statement"], NOW + timedelta(days=3), True, True, True),
]


def material(event, *, minutes, text):
    """Same collector identity, changed prose, explicit source chronology (synthetic)."""
    result = deepcopy(event)
    result["summary"] += f" {text}"
    result["published_at"] = (datetime.fromisoformat(event["published_at"]) + timedelta(minutes=minutes)).isoformat()
    return result


def build_rows():
    redis, rows, outcomes = FedMemoryRedis(), [], {}
    for label, names, clock, send_alerts, ok, ai in STEPS:
        deliver = None if ok else (lambda message: {"ok": False})
        outputs, submitted = run_collector([ENTRIES[n] for n in names], redis, now=clock, ai=ai,
                                           send_alerts=send_alerts, deliver=deliver)
        outcomes[label] = dict(stats=outputs[1], deliveries=outputs[3], ai_calls=outputs[4])
        for index, (event, kwargs) in enumerate(submitted):
            rows.append(dict(label=label if index == 0 else f"{label}:{index}", event=event,
                             make_current=kwargs.get("make_current", True)))
    action = next(r["event"] for r in rows if r["label"] == "policy_action")
    rows.append(dict(label="synthetic_material_revision", make_current=True,
                     event=material(action, minutes=1, text="Synthetic revised implementation note.")))
    rows.append(dict(label="synthetic_older_observation", make_current=True,
                     event=material(action, minutes=-1, text="Synthetic superseded wording.")))
    return rows, outcomes


def load_corpus():
    manifest = json.loads(MANIFEST.read_text())
    observed = datetime.fromisoformat(manifest["observed_at"])
    rows, _ = build_rows()
    for index, row in enumerate(rows):
        row["observed_at"] = (observed + timedelta(minutes=index)).isoformat()
        row["expect_current"] = row["label"] not in manifest["expected_not_current"]
    return manifest, rows
