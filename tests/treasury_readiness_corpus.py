"""Fixed test-only Treasury replay corpus; local captured/synthetic fixtures, no network."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from tests.test_treasury_pipeline import (
    FIXTURES, RELEASE_URL, treasury, normalize_release, normalize_auction,
    normalize_debt_letter, normalize_yield_events,
)

MANIFEST = Path(__file__).parent / "fixtures/persistence/treasury_readiness_v1.json"
SCORED_AT = datetime(2026, 9, 21, 14, tzinfo=timezone.utc)
LETTER_URL = "https://home.treasury.gov/system/files/136/debt-limit-letter.pdf"
AI = dict(ai_summary="Synthetic validated summary", ai_sentiment="NEUTRAL", ai_confidence=80,
          ai_why_it_matters="Synthetic visible financing context", ai_event_type="official policy")


def scored(event, ai=False):
    """Run the collector's own scoring/decision step at a fixed clock; AI is synthetic."""
    with patch.object(treasury, "datetime", wraps=datetime) as clock, patch.object(treasury.logger, "info"):
        clock.now.side_effect = lambda tz=None: SCORED_AT.astimezone(tz) if tz else SCORED_AT
        event = treasury._analyze(deepcopy(event), False, {"analysis_errors": 0})
    if ai:
        event.update(AI)
    return event


def release(title=None, slug="test-refunding"):
    html = (FIXTURES / "refunding.html").read_text()
    if title:
        html = html.replace('content="Quarterly Refunding Statement"', f'content="{title}"')
    return normalize_release(html, RELEASE_URL.rsplit("/", 1)[0] + "/" + slug)


def corrected_release():
    html = (FIXTURES / "refunding.html").read_text().replace(
        "</p></div>", "</p><p>Correction: September 22, 2026 at 9:00 a.m.</p></div>")
    return normalize_release(html, RELEASE_URL)


def material(event, *, minutes, text):
    """Same collector identity, changed release prose, explicit source chronology."""
    result = deepcopy(event)
    result["summary"] += f" {text}"
    result["published_at"] = (datetime.fromisoformat(event["published_at"]) + timedelta(minutes=minutes)).isoformat()
    return result


def auction_row(index=1, **changes):
    row = json.loads((FIXTURES / "auctions.json").read_text())[index]
    row.update(changes)
    return row


def yields():
    rows = [{"observation_date": "2026-09-17", "yields_percent": {"BC_2YEAR": 3.98, "BC_10YEAR": 4.49, "BC_30YEAR": 4.99}},
            {"observation_date": "2026-09-18", "yields_percent": {"BC_2YEAR": 4.0, "BC_10YEAR": 4.5, "BC_30YEAR": 5.0}},
            {"observation_date": "2026-09-21", "yields_percent": {"BC_2YEAR": 4.15, "BC_10YEAR": 4.4, "BC_30YEAR": 5.0}}]
    events = normalize_yield_events(rows, date(2026, 9, 22))
    revised_rows = deepcopy(rows)
    revised_rows[1]["yields_percent"]["BC_10YEAR"] = 4.52  # Official data revision, same identity.
    return events[1], events[2], normalize_yield_events(revised_rows, date(2026, 9, 22))[1]


def build_rows():
    """(label, event, make_current, expect_current) after the full ordered replay."""
    refunding = scored(release())
    newer = scored(material(release(), minutes=1, text="Synthetic revised financing estimate."))
    older = scored(material(release(), minutes=-1, text="Synthetic superseded estimate."))
    cosmetic = deepcopy(newer)
    cosmetic["headline"] += " "
    cosmetic["summary"] = cosmetic["summary"].replace(" ", "  ")
    missing = release()
    missing.update(published_at=None, original_published_at=None, timestamp_precision="unknown",
                   publication_basis="unverified")
    borrowing = release("Treasury Announces Marketable Borrowing Estimates", "test-borrowing-estimates")
    stale_borrowing = deepcopy(borrowing)
    stale_borrowing["published_at"] = (datetime.fromisoformat(borrowing["published_at"]) - timedelta(days=10)).isoformat()
    letter = normalize_debt_letter((FIXTURES / "debt_letter.txt").read_text(), LETTER_URL)
    bill_result = scored(normalize_auction(auction_row(), "result"))
    revised_result = scored(normalize_auction(auction_row(bidToCoverRatio="2.800000"), "result"))
    evidence = deepcopy(bill_result)
    evidence["source_hash"] = "b" * 64
    note = auction_row(cusip="91282CQA2", securityType="Note", securityTerm="10-Year",
                       highDiscountRate="", highYield="4.100000")
    reopening = auction_row(0)
    later_reopening = auction_row(0, auctionDate="2026-10-01T00:00:00", announcementDate="2026-09-26T00:00:00",
                                  pdfFilenameAnnouncement="A_20260926_1.pdf",
                                  pdfFilenameCompetitiveResults="R_20261001_1.pdf")
    routine, threshold, revised_yield = yields()
    return [
        ("refunding:initial", refunding, True, False),
        ("refunding:duplicate", deepcopy(refunding), True, False),
        ("refunding:material", newer, True, True),
        ("refunding:older", older, True, False),
        ("refunding:cosmetic", scored(cosmetic), True, False),
        ("refunding:stale_repost", release(), False, False),
        ("refunding:missing_date", missing, False, False),
        ("refunding:with_ai", scored(material(release(), minutes=1, text="Synthetic revised financing estimate."), ai=True), True, True),
        ("refunding:provenance_duplicate", deepcopy(newer), True, True),
        ("refunding:explicit_correction", scored(corrected_release()), True, True),
        ("refunding:correction_duplicate", scored(corrected_release()), True, True),
        ("borrowing_estimates:initial", scored(borrowing), True, True),
        ("borrowing_estimates:stale", stale_borrowing, False, False),
        ("debt_limit:initial_with_ai", scored(letter, ai=True), True, True),
        ("issuance_policy:initial", scored(release("Treasury Financing Update", "test-issuance-policy")), True, True),
        ("bill_auction:announcement", scored(normalize_auction(auction_row(), "announcement")), True, True),
        ("bill_auction:result", bill_result, True, True),
        ("bill_auction:provenance_duplicate", deepcopy(bill_result), True, True),
        ("bill_auction:new_source_evidence", evidence, True, True),
        ("bill_auction:unordered_result_update", revised_result, True, False),
        ("note_auction:result", scored(normalize_auction(note, "result")), True, True),
        ("reopening:result", scored(normalize_auction(reopening, "result")), True, True),
        ("reopening:later_auction_result", scored(normalize_auction(later_reopening, "result")), True, True),
        ("yield:routine", scored(routine), True, True),
        ("yield:unverified_revision", scored(revised_yield), True, False),
        ("yield:threshold", scored(threshold), True, True),
        ("yield:duplicate", scored(threshold), True, True),
    ]


def load_corpus():
    manifest = json.loads(MANIFEST.read_text())
    observed = datetime.fromisoformat(manifest["observed_at"])
    rows = [dict(label=label, event=event, observed_at=(observed + timedelta(minutes=index)).isoformat(),
                 make_current=make_current, expect_current=expect_current)
            for index, (label, event, make_current, expect_current) in enumerate(build_rows())]
    return manifest, rows
