"""Fixed test-only replay corpus; local captured/synthetic fixtures, no network."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path

from tests import test_macro_pipeline as fixtures
from tests.test_macro_persistence_adapter import sample

MANIFEST = Path(__file__).parent / "fixtures/persistence/macro_readiness_v1.json"


def material(event, *, minutes=1, delta=1):
    result = deepcopy(event)
    if result.get("metrics"):
        first = next(iter(result["metrics"]))
        result["metrics"][first]["value"] += delta
    else:
        result["summary"] += f" Synthetic revised estimate {delta}."
    result["published_at"] = (datetime.fromisoformat(event["published_at"]) + timedelta(minutes=minutes)).isoformat()
    return result


def load_corpus():
    manifest = json.loads(MANIFEST.read_text())
    observed = datetime.fromisoformat(manifest["observed_at"])
    rows = []
    for category in manifest["categories"]:
        initial = sample(category)
        revised = material(initial)
        cosmetic = deepcopy(revised)
        cosmetic["headline"] += " "
        cosmetic["summary"] = cosmetic["summary"].replace(" ", "  ")
        stale = sample(category, scored=False)
        stale["published_at"] = (datetime.fromisoformat(stale["published_at"]) - timedelta(days=10)).isoformat()
        missing = sample(category, scored=False)
        missing.update(published_at=None, timestamp_precision="unknown")
        ai = deepcopy(revised)
        ai.update(ai_summary="Synthetic validated summary", ai_sentiment="NEUTRAL", ai_confidence=80,
                  ai_why_it_matters="Synthetic visible economic context", ai_event_type="macro_release")
        variants = dict(initial=initial, duplicate=deepcopy(initial), material=revised,
                        older=material(initial, minutes=-1, delta=-1), cosmetic=cosmetic,
                        stale=stale, missing_date=missing, with_ai=ai, provenance_duplicate=deepcopy(revised))
        for scenario in manifest["scenarios"]:
            # Date-only releases have no comparable sub-day publication ordering;
            # material facts are retained but must not replace current by arrival.
            source_order_comparable = initial.get("timestamp_precision") in {"minute", "second"}
            rows.append(dict(label=f"{category}:{scenario}", event=variants[scenario],
                             observed_at=(observed + timedelta(minutes=len(rows))).isoformat(),
                             make_current=scenario not in {"stale", "missing_date"},
                             expect_current=(scenario in {"material", "with_ai", "provenance_duplicate"}
                                             and source_order_comparable)))
    corrected = fixtures.bls.normalize_bls_feed(fixtures.pipeline_fixture("cpi").replace(
        "<title>Consumer", "<title>Correction: Consumer"), fixtures.SOURCES["cpi"])
    payload = json.loads((fixtures.FIXTURES / "bls_api.json").read_text())
    corrected = fixtures.bls.enrich_bls_event(corrected, payload)
    corrected = fixtures.macro._analyze_candidate(corrected, False, {"analysis_errors": 0})
    for label in manifest["extra_scenarios"]:
        rows.append(dict(label=label, event=deepcopy(corrected), observed_at=(observed + timedelta(minutes=len(rows))).isoformat(),
                         make_current=True, expect_current=True))
    return manifest, rows
