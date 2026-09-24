"""Fixed test-only SEC replay corpus.

Rows are exactly what the real SEC collector (``process_sec_filings``) submits to
its shadow hook while processing synthetic, official-style submissions rows
against an in-memory Redis with a controllable clock. There is no network, live
Redis, OpenAI or Telegram. Accessions and documents are synthetic but shaped like
EDGAR data (insider forms carry an XSL path in ``primaryDocument``).
"""
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_sec_pipeline import sec, deduplicator

MANIFEST = Path(__file__).parent / "fixtures/persistence/sec_readiness_v1.json"
NOW = datetime(2026, 9, 22, 21, tzinfo=timezone.utc)
TTL = timedelta(seconds=86400)  # DEDUP_TTL_SECONDS as configured for these tests.


class SecMemoryRedis:
    """set(nx, ex) with expiry against a controllable clock (as ``is_duplicate`` uses Redis)."""

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


class FailingRedis(SecMemoryRedis):
    def set(self, key, value, **kwargs):
        raise deduplicator.redis.RedisError("unavailable")


def filing(symbol, form, accession, filed, document):
    return dict(symbol=symbol, form=form, accession_number=accession, filing_date=filed, primary_document=document)


FILINGS = dict(
    meta_8k=filing("META", "8-K", "0001326801-26-000101", "2026-09-21", "meta-20260921.htm"),
    meta_10q=filing("META", "10-Q", "0001326801-26-000095", "2026-07-30", "meta-20260630.htm"),
    meta_10k=filing("META", "10-K", "0001326801-26-000012", "2026-01-29", "meta-20251231.htm"),
    meta_form4=filing("META", "4", "0001209191-26-054321", "2026-09-18", "xslF345X05/wk-form4_1758220000.xml"),
    meta_form144=filing("META", "144", "0001950047-26-004321", "2026-09-17", "xsl144X01/primary_doc.xml"),
    meta_form3=filing("META", "3", "0001209191-26-054000", "2026-09-10", "xslF345X02/wk-form3_1757500000.xml"),
    meta_npx=filing("META", "N-PX", "0001326801-26-000090", "2026-08-28", "xslN-PX_X01/primary_doc.xml"),
    meta_8ka=filing("META", "8-K/A", "0001326801-26-000102", "2026-09-22", "meta-20260921a.htm"),
    nvda_8k=filing("NVDA", "8-K", "0001045810-26-000201", "2026-09-21", "nvda-20260921.htm"),
    meta_8k_same_day=filing("META", "8-K", "0001326801-26-000103", "2026-09-21", "meta-20260921b.htm"),
    meta_form4_same_day=filing("META", "4", "0001209191-26-054322", "2026-09-18", "xslF345X05/wk-form4_1758220001.xml"),
    meta_8k_telegram_failure=filing("META", "8-K", "0001326801-26-000104", "2026-09-22", "meta-20260922.htm"),
    missing_metadata=filing("META", "4", "0001209191-26-054323", "", ""),
)

# (label, filing names, clock, redis: "same" | "fresh" | "failing", telegram succeeds)
STEPS = [
    ("meta_8k", ["meta_8k"], NOW, "same", True),
    ("meta_10q", ["meta_10q"], NOW, "same", True),
    ("meta_10k", ["meta_10k"], NOW, "same", True),
    ("meta_form4", ["meta_form4"], NOW, "same", True),
    ("meta_form144", ["meta_form144"], NOW, "same", True),
    ("meta_form3", ["meta_form3"], NOW, "same", True),
    ("meta_npx", ["meta_npx"], NOW, "same", True),
    ("meta_8ka", ["meta_8ka"], NOW, "same", True),
    ("nvda_8k", ["nvda_8k"], NOW, "same", True),
    ("same_accession_repeat", ["meta_8k"], NOW + timedelta(hours=1), "same", True),
    ("same_form_different_accession", ["meta_8k_same_day"], NOW + timedelta(hours=1), "same", True),
    ("same_date_different_accession", ["meta_form4_same_day"], NOW + timedelta(hours=1), "same", True),
    ("telegram_failure", ["meta_8k_telegram_failure"], NOW + timedelta(hours=2), "same", False),
    ("missing_optional_metadata", ["missing_metadata"], NOW + timedelta(hours=2), "same", True),
    ("redis_fail_open", ["meta_10q", "meta_10q"], NOW + timedelta(hours=3), "failing", True),
    ("repeat_after_ttl_expiry", ["meta_8k"], NOW + TTL + timedelta(hours=1), "same", True),
    ("repeat_after_restart", ["meta_8k", "meta_form4"], NOW + TTL + timedelta(hours=2), "fresh", True),
    ("provenance_duplicate", ["nvda_8k"], NOW + TTL + timedelta(hours=3), "same", True),
]


def run_collector(filings, redis, *, now=NOW, enabled=True, submit=None, telegram_ok=True):
    """One real ``process_sec_filings`` pass; returns observable outputs and captured shadow submissions."""
    submitted = []

    def capture(value, **kwargs):
        submitted.append((deepcopy(value), kwargs))
        if submit:
            return submit(value, **kwargs)

    def telegram(message):
        if not telegram_ok:
            raise RuntimeError("telegram unavailable")
        return {"ok": True, "result": {"message_id": 1}}

    from persistence import sec_shadow
    redis.now = now
    log = Mock()
    out = StringIO()
    with patch.object(deduplicator, "redis_client", redis), \
         patch.object(sec, "SEC_PERSISTENCE_SHADOW_ENABLED", enabled), \
         patch.object(sec_shadow, "submit_sec", side_effect=capture), \
         patch.object(sec, "send_telegram_alert", side_effect=telegram) as send, \
         patch.object(sec.logger, "info", log.info), patch.object(sec.logger, "error", log.error), \
         patch.object(deduplicator.logger, "error", log.dedup_error), redirect_stdout(out):
        events = sec.process_sec_filings(deepcopy(filings))
    # Log arguments as text: exception instances compare by identity, not by content.
    logs = [(name, tuple(str(arg) for arg in args)) for name, args, _ in log.mock_calls]
    outputs = dict(events=events, stdout=out.getvalue(), logs=logs,
                   redis=sorted(redis.data.items()), telegram=send.call_args_list)
    return outputs, submitted


def build_rows():
    redis, rows, outcomes = SecMemoryRedis(), [], {}
    for label, names, clock, mode, telegram_ok in STEPS:
        if mode == "fresh":
            redis = SecMemoryRedis()  # New process whose Redis state was lost.
        target = FailingRedis() if mode == "failing" else redis
        outputs, submitted = run_collector([FILINGS[n] for n in names], target, now=clock, telegram_ok=telegram_ok)
        outcomes[label] = dict(processed=len(outputs["events"]), telegram=len(outputs["telegram"]),
                               submitted=len(submitted))
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
