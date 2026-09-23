"""Fixed test-only geopolitical replay corpus.

Rows are exactly what the real collector submits to the shadow hook while
processing synthetic official documents against the in-memory Redis test double
at fixed clocks. No network, no live Redis, no OpenAI and no Telegram.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from tests.test_geopolitical_pipeline import FIXTURES, NOW, MemoryRedis, geo, sources, identity

MANIFEST = Path(__file__).parent / "fixtures/persistence/geopolitical_readiness_v1.json"
AI = dict(ai_summary="Synthetic validated summary", ai_sentiment="NEUTRAL", ai_confidence=80,
          ai_why_it_matters="Synthetic visible policy context", ai_event_type="official policy")


def enrich(event):
    """Synthetic validated enrichment; also tries (and fails) to overwrite parser facts."""
    return dict(event, **AI, policy_stage="fake", symbols=["FAKE"], impact_score=1)


def run_collector(docs, redis, *, enabled=True, submit=None, ai=True, send_alerts=True):
    """One real collector pass; returns observable outputs and captured submissions."""
    submitted = []
    def capture(value, **kwargs):
        submitted.append((deepcopy(value), kwargs))
        if submit:
            return submit(value, **kwargs)
    from persistence import geopolitical_shadow as shadow
    with patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP forbidden")), \
         patch.object(geo, "GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED", enabled), \
         patch.object(geo.deduplicator, "redis_client", redis), \
         patch.object(sources, "SOURCES", {"fixture": "synthetic"}), \
         patch.object(sources, "fetch_documents", side_effect=lambda _: deepcopy(docs)), \
         patch.object(shadow, "submit_geopolitical", side_effect=capture), \
         patch.object(geo, "analyze_geopolitical_event", side_effect=enrich) as analyze, \
         patch.object(geo, "deliver_geopolitical_alert", return_value={"ok": True, "result": {"message_id": 7}}) as send, \
         patch.object(geo.logger, "info"), patch.object(geo, "datetime") as clock:
        clock.now.side_effect = lambda *a: redis.now
        clock.fromisoformat = datetime.fromisoformat
        events, stats = geo.collect_geopolitical_events(enable_ai=ai, send_alerts=send_alerts)
        outputs = (events, stats, sorted(redis.data.items()), send.call_args_list, analyze.call_count)
    return outputs, submitted


def doc(agency, native, url, headline, body, published="2026-09-22T12:00:00Z", anchors=(), **extra):
    return dict(agency=agency, native_id=native, url=url, headline=headline, published_at=published,
                body=body, identity_anchors=list(anchors), **extra)


EXPORT = "The Department imposes export licensing requirements on advanced computing chips exported to China."
FR = "https://www.federalregister.gov/documents/2026/09/22/"
DOCS = dict(
    bis_final=FIXTURES[0], fr_companion=FIXTURES[1], platform_remedy=FIXTURES[2], moea_disruption=FIXTURES[3],
    cosmetic=dict(FIXTURES[0], headline=FIXTURES[0]["headline"] + " (updated page title)"),
    entity_list=doc("bis", "synthetic-entity-list", "https://www.bis.gov/press-release/synthetic-entity-list",
        "Commerce adds entities to the Entity List",
        "The Department of Commerce adds NVIDIA Corporation subsidiaries in China to the Entity List. "
        "The final rule establishes the stated obligations for the listed parties.", anchors=["fr:2026-99910"]),
    sanctions=doc("ofac", "20260922", "https://ofac.treasury.gov/recent-actions/20260922",
        "Technology procurement network designations",
        "OFAC imposes sanctions on networks in China that procure advanced computing chips for prohibited end users. "
        "The designation sets out binding obligations for the named persons."),
    trade=doc("ustr", "synthetic-tariff-action",
        "https://ustr.gov/about-us/policy-offices/press-office/press-releases/2026/september/synthetic-tariff-action",
        "USTR tariff action on semiconductor equipment",
        "USTR imposes tariffs on semiconductor manufacturing equipment imported from China under Section 301. "
        "The action sets out duties for the covered goods.", anchors=["fr:2026-99920"]),
    complaint=doc("ftc", "synthetic-complaint", "https://www.ftc.gov/news-events/news/press-releases/2026/09/synthetic-complaint",
        "Commission files platform data complaint",
        "The Commission files a complaint that alleges Facebook used personal data for targeted advertising without consent. "
        "The complaint seeks relief for the affected consumers.", anchors=["ftc-case:2026001"]),
    proposal=doc("fr", "2026-99930", FR + "2026-99930/synthetic-proposed-rule",
        "Proposed export controls on advanced computing items",
        "The Department proposes export licensing requirements on advanced computing chips exported to China. "
        "Comments are requested on the covered products.", document_type="Proposed Rule"),
    final_of_proposal=doc("fr", "2026-99931", FR + "2026-99931/synthetic-final-rule",
        "Final export controls on advanced computing items",
        EXPORT + " This final rule adopts the proposal with the stated obligations.", document_type="Rule"),
    clarification=doc("bis", "synthetic-chip-clarification", "https://www.bis.gov/press-release/synthetic-chip-clarification",
        "Commerce clarifies advanced computing controls",
        "The Department amends export licensing requirements on advanced computing chips to clarify the covered items. "
        "The clarification establishes the stated scope.", anchors=["fr:2026-99901"]),
    amendment=doc("fr", "2026-99940", FR + "2026-99940/synthetic-amendment",
        "Amendment to advanced computing export controls",
        "The Department amends export licensing requirements on advanced computing chips exported to China by expanding the covered items. "
        "The amendment establishes additional obligations.", document_type="Rule"),
    whitehouse_action=doc("whitehouse", "https://www.whitehouse.gov/presidential-actions/2026/09/synthetic-tariff-order/",
        "https://www.whitehouse.gov/presidential-actions/2026/09/synthetic-tariff-order/", "Adjusting imports of AI accelerators",
        "The President imposes tariffs on AI accelerators imported from China. The proclamation sets out duties for the covered goods.",
        anchors=["eo:99960"]),
    whitehouse_fr_companion=doc("fr", "2026-99961", FR + "2026-99961/synthetic-tariff-order", "Adjusting imports of AI accelerators",
        "The President imposes tariffs on AI accelerators imported from China. The proclamation sets out duties for the covered goods.",
        published="2026-09-22T13:00:00Z", anchors=["eo:99960"], document_type="Presidential Document"),
    correction=doc("fr", "2026-99970", FR + "2026-99970/synthetic-correction", "Correction to advanced computing export controls",
        "This document corrects the final rule published on September 22, 2026. " + EXPORT, document_type="Rule"),
    missing_date=dict(FIXTURES[0], native_id="synthetic-undated", published_at=None),
    unresolved_identity=dict(FIXTURES[0], native_id="synthetic-unanchored", identity_anchors=[]),
    broad_no_symbols=doc("bis", "synthetic-meeting", "https://www.bis.gov/press-release/synthetic-meeting",
        "Officials discuss technology cooperation",
        "Officials discussed AI, semiconductor and China and Taiwan technology cooperation at a meeting. No action is announced."),
    public_inspection=doc("fr", "2026-99950", "https://www.federalregister.gov/public-inspection/2026-99950/synthetic-investment-rule",
        "Outbound investment restrictions",
        "The Department of the Treasury imposes outbound investment restrictions on advanced computing chips development in China. "
        "The rule establishes binding obligations.", published="2026-09-22T09:15:00Z",
        publication_basis="public_inspection_filed_at", scheduled_publication="2026-09-23", document_type="Rule"),
)
DOCS["published_edition"] = dict(DOCS["public_inspection"], url=FR.replace("09/22", "09/23") + "2026-99950/synthetic-investment-rule",
                                 published_at="2026-09-23", publication_basis="publication_date")
DOCS["stale_companion"] = dict(FIXTURES[1], published_at="2026-09-25T13:00:00Z")
# Companion FR notice for the USTR action that discloses it an hour earlier.
DOCS["earlier_companion_disclosure"] = doc("fr", "2026-99920", FR + "2026-99920/synthetic-tariff-notice",
    "Notice of action on semiconductor equipment", DOCS["trade"]["body"], published="2026-09-22T11:00:00Z",
    document_type="Notice")

# (label, document, clock, Redis state change before the pass)
STEPS = [
    ("bis_final", "bis_final", NOW, None),
    ("fr_companion", "fr_companion", NOW, None),
    ("duplicate", "bis_final", NOW, None),
    ("cosmetic", "cosmetic", NOW, None),
    ("entity_list", "entity_list", NOW, None),
    ("sanctions", "sanctions", NOW, None),
    ("trade", "trade", NOW, None),
    ("platform_remedy", "platform_remedy", NOW, None),
    ("complaint", "complaint", NOW, None),
    ("proposal", "proposal", NOW, None),
    ("clarification", "clarification", NOW, None),
    ("amendment", "amendment", NOW, None),
    ("moea_disruption", "moea_disruption", NOW, None),
    ("earlier_companion_disclosure", "earlier_companion_disclosure", NOW, None),
    ("whitehouse_action", "whitehouse_action", NOW, None),
    ("whitehouse_fr_companion", "whitehouse_fr_companion", NOW, None),
    ("correction", "correction", NOW, None),
    ("final_of_proposal", "final_of_proposal", NOW, None),
    ("missing_date", "missing_date", NOW, None),
    ("unresolved_identity", "unresolved_identity", NOW, None),
    ("broad_no_symbols", "broad_no_symbols", NOW, None),
    ("alias_expiry", "bis_final", NOW, "expire_aliases"),
    ("full_state_expiry", "bis_final", NOW, "expire_all"),
    ("public_inspection", "public_inspection", datetime(2026, 9, 23, 6, tzinfo=timezone.utc), None),
    ("published_edition", "published_edition", datetime(2026, 9, 23, 6, tzinfo=timezone.utc), None),
    ("edition_duplicate", "published_edition", datetime(2026, 9, 23, 6, tzinfo=timezone.utc), None),
    ("stale_companion", "stale_companion", datetime(2026, 9, 25, 14, tzinfo=timezone.utc), None),
]


def expire(redis, mode):
    """Simulate Redis TTL expiry in the test double; PostgreSQL is never consulted."""
    for key in list(redis.data):
        if mode == "expire_all" or key.startswith((identity.PREFIX + "alias:", identity.PREFIX + "policy:")):
            redis.data.pop(key)


def build_rows():
    redis = MemoryRedis()
    rows, outcomes = [], {}
    for label, name, clock, change in STEPS:
        redis.now = clock
        if change:
            expire(redis, change)
        outputs, submitted = run_collector([DOCS[name]], redis)
        outcomes[label] = outputs[1]
        for index, (event, kwargs) in enumerate(submitted):
            rows.append(dict(label=label if index == 0 else f"{label}:{index}", event=event,
                             make_current=kwargs.get("make_current", True)))
    return rows, outcomes


def load_corpus():
    manifest = json.loads(MANIFEST.read_text())
    observed = datetime.fromisoformat(manifest["observed_at"])
    rows, _ = build_rows()
    for index, row in enumerate(rows):
        row["observed_at"] = (observed + timedelta(minutes=index)).isoformat()
        row["expect_current"] = row["label"] not in manifest["expected_not_current"]
    return manifest, rows
