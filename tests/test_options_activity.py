"""The Options Intelligence activity read model (artifact_store.options_activity) and its API surface.

Pure tests use the Phase 9 test builders (real, validated phase9-v2 objects); API tests publish real artifacts at
chosen as_of instants into a temporary store. No network, no clock in the logic under test."""
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from api.app import create_app
from api.projections import PROJECTIONS, options_intelligence_from_summary
from api.settings import load_api_settings
from artifact_store import options_activity as oa
from artifact_store import store
from artifact_store.index import build_index
from artifact_store.service import ArtifactStore
from options_intelligence.builder import build
from tests.options_intelligence_cases import CALENDAR
from tests.options_snapshot_cases import PROVENANCE, assemble, chain_record
from tests.trade_setup_cases import options_intelligence

TOKEN = "test-read-token-0123456789abcdef0123456789"
BANNED = ("bull", "bear", "buy", "sell", "recommend", "entry", "exit", "target", "stop", "long", "short")


def report(as_of, call=(500,), put=(300,), symbol="META", price=None, strikes=None):
    """A real phase9-v2 artifact at ``as_of`` with current-session day volumes per strike (calls, then puts)."""
    observed = as_of - timedelta(minutes=1)
    records = []
    for option_type, volumes in (("call", call), ("put", put)):
        for i, volume in enumerate(volumes):
            strike = (strikes or {}).get((option_type, i), 690 + 10 * i)
            day = dict(open="10", high="11", low="9", close="10", previous_close="9.8", change="0.2",
                       change_percent="2", volume=volume, vwap="10", observed_at=observed)
            records.append(chain_record(symbol, "2026-10-16", option_type, strike, day=day,
                                        open_interest=dict(value=400)))
    kw = dict(provenance=dict(PROVENANCE, truncated=False), calendar_state="regular")
    if price is not None:
        kw["underlying_price"] = dict(value=str(price), observed_at=observed, source="synthetic_stocks")
    return build(assemble(symbol, as_of, records, **kw), calendar=CALENDAR, format="phase9-v2").to_dict()


T1 = datetime(2026, 9, 30, 14, 45, tzinfo=timezone.utc)       # 10:45 ET
T2 = T1 + timedelta(minutes=15)                                # 11:00 ET, same session
T3 = datetime(2026, 10, 1, 14, 45, tzinfo=timezone.utc)       # next session


class SummaryTests(unittest.TestCase):
    def test_copies_phase9_fields_and_counts(self):
        d = options_intelligence()                     # the quoted chain with an underlying price
        s = oa.summarize(d)
        self.assertEqual((s.symbol, s.as_of, s.session_date), ("META", d["snapshot_ref"]["as_of"], "2026-09-30"))
        self.assertEqual((s.call_volume, s.put_volume), (500, 1100))          # activity totals, current session only
        self.assertEqual(s.put_call_volume_ratio, d["activity"]["put_call_volume_ratio"])
        self.assertEqual(s.iv_median, d["volatility"]["overall"]["median"])
        self.assertEqual(s.contract_count, len(d["contracts"]))
        self.assertEqual(s.expiration_count, d["chain_completeness"]["expiration_count"])
        self.assertEqual(s.current_session_volume_gt_oi_count, 1)       # 690 call: 500 > 400, current session

    def test_volume_gt_oi_counts_only_current_session_records(self):
        d = options_intelligence()
        stale = [c for c in d["contracts"] if c["volume_exceeds_open_interest"] is True]
        self.assertTrue(stale)
        d = copy.deepcopy(d)
        for c in d["contracts"]:
            if c["volume_exceeds_open_interest"] is True:
                c["day"]["session_relation"] = "previous_session"        # a pre-open report's day records
        self.assertEqual(oa.summarize(d).current_session_volume_gt_oi_count, 0)

    def test_breadth_counts_distinct_strikes_with_current_session_volume(self):
        s = oa.summarize(options_intelligence())
        # calls: 690 (500, current) counts; 720 has zero volume; 700/710 are other sessions. puts: two at 700.
        self.assertEqual((s.call_breadth, s.put_breadth), (1, 1))
        s = oa.summarize(report(T1, call=(5, 0, 7), put=(1,)))
        self.assertEqual((s.call_breadth, s.put_breadth), (2, 1))

    def test_concentration_uses_phase9_strike_relation_and_never_guesses(self):
        s = oa.summarize(options_intelligence())
        self.assertEqual((s.call_concentration, s.put_concentration, s.concentration_reason),
                         ("below_spot", "at_spot", None))
        s = oa.summarize(report(T1, call=(5, 5), put=(1,), price=695))          # 690 below, 700 above: a tie
        self.assertEqual(s.call_concentration, "mixed")
        s = oa.summarize(report(T1))                                            # no underlying price (live v1)
        self.assertEqual((s.call_concentration, s.put_concentration, s.concentration_reason),
                         ("unavailable", "unavailable", "underlying_price_unavailable"))

    def test_missing_volume_and_iv(self):
        d = options_intelligence("live_like_no_quotes")                        # no current-session day records
        s = oa.summarize(d)
        self.assertEqual((s.call_volume, s.put_volume, s.put_call_volume_ratio), (None, None, None))
        d = copy.deepcopy(options_intelligence())
        d["volatility"]["overall"]["median"] = None
        self.assertIsNone(oa.summarize(d).iv_median)

    def test_session_date_is_new_york(self):
        self.assertEqual(oa.session_date("2026-10-01T01:30:00+00:00"), "2026-09-30")   # 21:30 ET
        self.assertEqual(oa.session_date("2026-10-01T13:30:00+00:00"), "2026-10-01")


def summary(**overrides):
    base = oa.Summary(symbol="META", as_of="2026-10-06T14:00:00+00:00", session_date="2026-10-06",
                      options_intelligence_format_version="phase9-v2", rules_version="phase9-rules-v2",
                      snapshot_id="sha256:" + "0" * 64, contract_count=7326, expiration_count=24,
                      call_volume=100_000, put_volume=80_000, put_call_volume_ratio="0.8",
                      put_call_volume_ratio_reason=None, iv_median="0.314", current_session_volume_gt_oi_count=157, call_breadth=50,
                      put_breadth=40, call_concentration="unavailable", put_concentration="unavailable",
                      concentration_reason="underlying_price_unavailable", underlying_price_status="unavailable")
    return replace(base, **overrides)


LATER = dict(as_of="2026-10-06T14:15:00+00:00")


class CompareTests(unittest.TestCase):
    def test_first_snapshot_of_session_fabricates_nothing(self):
        v = oa.compare(summary(**LATER), None, oa.NO_PRIOR)
        self.assertEqual(v["comparison"]["status"], "no_prior_snapshot")
        for key in ("call_volume_change", "call_volume_change_pct", "put_volume_change", "current_session_volume_gt_oi_change",
                    "call_breadth_change", "put_breadth_change"):
            self.assertIsNone(v[key], key)
        self.assertEqual(v["call_volume_change_reason"], "not_comparable")
        self.assertEqual((v["activity_bias"], v["momentum_15m"]), ("CALL", "INSUFFICIENT_PRIOR"))
        self.assertEqual(v["trend_summary"], oa.TREND_INSUFFICIENT)

    def test_call_dominant_and_accelerating(self):
        cur = summary(call_volume=118_000, put_volume=85_000, current_session_volume_gt_oi_count=184, call_breadth=58, put_breadth=40,
                      **LATER)
        v = oa.compare(cur, summary(), oa.COMPARABLE)
        self.assertEqual((v["call_volume_change"], v["call_volume_change_pct"]), (18_000, "18.00"))
        self.assertEqual((v["put_volume_change"], v["put_volume_change_pct"]), (5_000, "6.25"))
        self.assertEqual((v["current_session_volume_gt_oi_change"], v["call_breadth_change"], v["put_breadth_change"]), (27, 8, 0))
        self.assertEqual((v["activity_bias"], v["momentum_15m"]), ("CALL", "CALL"))
        self.assertEqual(v["trend_summary"], "Call activity strengthening. Call activity broadening across more strikes.")
        self.assertEqual(v["comparison"], {"status": "comparable", "prior_as_of": "2026-10-06T14:00:00+00:00",
                                           "session_date": "2026-10-06"})

    def test_calls_dominant_while_puts_accelerate_and_broaden(self):
        cur = summary(call_volume=105_000, put_volume=95_000, put_breadth=45, **LATER)
        v = oa.compare(cur, summary(), oa.COMPARABLE)
        self.assertEqual((v["activity_bias"], v["momentum_15m"]), ("CALL", "PUT"))
        self.assertEqual(v["trend_summary"],
                         "Calls remain dominant, while put activity is accelerating. Put activity broadening across more strikes.")

    def test_put_dominant(self):
        prior = summary(call_volume=50_000, put_volume=90_000)
        v = oa.compare(summary(call_volume=52_000, put_volume=99_000, **LATER), prior, oa.COMPARABLE)
        self.assertEqual((v["activity_bias"], v["momentum_15m"], v["trend_summary"]),
                         ("PUT", "PUT", "Put activity strengthening."))

    def test_no_new_volume_after_the_close(self):
        v = oa.compare(summary(**LATER), summary(), oa.COMPARABLE)
        self.assertEqual((v["momentum_15m"], v["call_volume_change_pct"]), ("BALANCED", "0.00"))
        self.assertEqual(v["trend_summary"], "Calls remain dominant; no new volume since the prior snapshot.")

    def test_equal_volume_is_balanced(self):
        prior = summary(call_volume=80_000, put_volume=80_000)
        v = oa.compare(summary(call_volume=90_000, put_volume=90_000, **LATER), prior, oa.COMPARABLE)
        self.assertEqual((v["activity_bias"], v["momentum_15m"], v["trend_summary"]),
                         ("BALANCED", "BALANCED", "Activity broadly balanced."))

    def test_zero_prior_volume_has_no_percentage(self):
        v = oa.compare(summary(call_volume=10, put_volume=0, **LATER), summary(call_volume=0, put_volume=0),
                       oa.COMPARABLE)
        self.assertEqual((v["call_volume_change"], v["call_volume_change_pct"], v["call_volume_change_reason"]),
                         (10, None, "prior_zero"))
        self.assertEqual(v["momentum_15m"], "CALL")

    def test_volume_decrease_is_reported_but_not_compared(self):
        v = oa.compare(summary(call_volume=99_000, put_volume=81_000, **LATER), summary(), oa.COMPARABLE)
        self.assertEqual((v["call_volume_change"], v["call_volume_change_pct"]), (-1_000, "-1.00"))
        self.assertEqual((v["momentum_15m"], v["trend_summary"]), ("VOLUME_CORRECTION", oa.TREND_CORRECTION))

    def test_prior_from_previous_trading_day_is_never_used(self):
        prior = summary(as_of="2026-10-05T19:45:00+00:00", session_date="2026-10-05")
        v = oa.compare(summary(**LATER), prior, oa.COMPARABLE)
        self.assertEqual(v["comparison"]["status"], "prior_from_other_session")
        self.assertIsNone(v["call_volume_change"])
        self.assertIsNone(v["comparison"]["prior_as_of"])
        self.assertEqual(v["momentum_15m"], "INSUFFICIENT_PRIOR")

    def test_symbol_mismatch_and_lookahead_are_refused(self):
        with self.assertRaises(ValueError):
            oa.compare(summary(**LATER), summary(symbol="NVDA"), oa.COMPARABLE)
        with self.assertRaises(ValueError):
            oa.compare(summary(), summary(**LATER), oa.COMPARABLE)               # "prior" is later

    def test_missing_volume_is_unavailable(self):
        v = oa.compare(summary(call_volume=None, **LATER), summary(), oa.COMPARABLE)
        self.assertEqual((v["call_volume_change"], v["call_volume_change_reason"]), (None, "value_unavailable"))
        self.assertEqual((v["activity_bias"], v["momentum_15m"], v["trend_summary"]),
                         ("UNAVAILABLE", "UNAVAILABLE", oa.TREND_UNAVAILABLE))

    def test_ambiguous_prior(self):
        v = oa.compare(summary(**LATER), None, oa.PRIOR_AMBIGUOUS)
        self.assertEqual((v["comparison"]["status"], v["momentum_15m"]), ("prior_ambiguous", "INSUFFICIENT_PRIOR"))

    def test_trend_vocabulary_is_closed_deterministic_and_descriptive(self):
        bases = {oa.TREND_INSUFFICIENT, oa.TREND_UNAVAILABLE, oa.TREND_CORRECTION, "Activity broadly balanced.",
                 "Call activity strengthening.", "Put activity strengthening.",
                 "Calls remain dominant, while put activity is accelerating.",
                 "Puts remain dominant, while call activity is accelerating.",
                 "Activity balanced overall, while call activity is accelerating.",
                 "Activity balanced overall, while put activity is accelerating.",
                 "Calls remain dominant; new activity is balanced.", "Puts remain dominant; new activity is balanced.",
                 "Calls remain dominant; no new volume since the prior snapshot.",
                 "Puts remain dominant; no new volume since the prior snapshot.",
                 "Activity broadly balanced; no new volume since the prior snapshot."}
        clauses = {"", " Call activity broadening across more strikes.", " Put activity broadening across more strikes.",
                   " Call and Put activity broadening across more strikes."}
        allowed = {base + clause for base in bases for clause in clauses}
        sides = ("CALL", "PUT", "BALANCED", "UNAVAILABLE")
        for bias in sides:
            for momentum in sides + ("INSUFFICIENT_PRIOR", "VOLUME_CORRECTION"):
                for breadth in ((None, None), (1, 0), (0, 1), (2, 3), (-1, -1)):
                    for new_volume in (True, False):
                        text = oa.trend_summary(bias, momentum, *breadth, new_volume=new_volume)
                        self.assertIn(text, allowed)
                        self.assertEqual(text, oa.trend_summary(bias, momentum, *breadth, new_volume=new_volume))
                        self.assertFalse(any(word in text.lower() for word in BANNED), text)

class ApiTests(unittest.TestCase):
    """Real artifacts in a temporary store, served through the app."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = cls.tmp.name
        cls.reports = {
            "t1": report(T1, call=(100, 200), put=(150,)),
            "t2": report(T2, call=(150, 250), put=(160, 40)),
            "t3": report(T3, call=(10,), put=(20,)),
            "nvda": report(T2, call=(5,), put=(1,), symbol="NVDA"),
        }
        for d in cls.reports.values():
            store.publish(cls.root, "options-intelligence", json.dumps(d).encode())
        cls.ids = {k: d["options_intelligence_id"] for k, d in cls.reports.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def client(self):
        artifacts = ArtifactStore(self.root)
        settings = load_api_settings({"MIAS_API_READ_TOKEN": TOKEN, "MIAS_ARTIFACT_ROOT": self.root})
        return artifacts, TestClient(create_app(settings, artifact_store=artifacts, refresh_interval=0))

    def get(self, client, path):
        response = client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_summary_view_from_read_model_is_identical_to_the_full_projection(self):
        for d in list(self.reports.values()) + [options_intelligence(n) for n in ("quoted_complete", "nvda",
                                                                                   "live_like_no_quotes")]:
            self.assertEqual(options_intelligence_from_summary(d["options_intelligence_id"], oa.summarize(d)),
                             PROJECTIONS["options-intelligence"](d))

    def test_existing_routes_unchanged_and_never_read_the_file(self):
        artifacts, client = self.client()
        with client, patch.object(ArtifactStore, "read_bytes", side_effect=AssertionError("full artifact read")):
            listing = self.get(client, "/api/v1/options-intelligence")
            self.assertEqual(listing["meta"]["view"], "options-intelligence-summary-v1")
            by_id = {row["options_intelligence_id"]: row for row in listing["data"]}
            for key, d in self.reports.items():
                self.assertEqual(by_id[self.ids[key]], PROJECTIONS["options-intelligence"](d))
            self.get(client, f"/api/v1/options-intelligence/{self.ids['t2']}")
            self.get(client, "/api/v1/options-intelligence/latest?symbol=META")
            activity = self.get(client, "/api/v1/options-intelligence/activity")
        self.assertEqual(len(activity["data"]), 4)
        with client:                                                      # /canonical still reads and verifies
            raw = client.get(f"/api/v1/options-intelligence/{self.ids['t1']}/canonical",
                             headers={"Authorization": f"Bearer {TOKEN}"})
            self.assertEqual(json.loads(raw.content), self.reports["t1"])

    def test_activity_compares_only_with_the_previous_same_session_report(self):
        _, client = self.client()
        with client:
            body = self.get(client, "/api/v1/options-intelligence/activity?symbol=META")
        self.assertEqual(body["meta"]["view"], "options-intelligence-activity-v1")
        rows = {row["options_intelligence_id"]: row for row in body["data"]}
        self.assertEqual([r["options_intelligence_id"] for r in body["data"]], [self.ids["t3"], self.ids["t2"],
                                                                                self.ids["t1"]])
        t3, t2, t1 = rows[self.ids["t3"]], rows[self.ids["t2"]], rows[self.ids["t1"]]
        self.assertEqual(t3["comparison"]["status"], "prior_from_other_session")        # T2 is the previous day
        self.assertIsNone(t3["call_volume_change"])
        self.assertEqual(t1["comparison"]["status"], "no_prior_snapshot")
        self.assertEqual(t2["comparison"], {"status": "comparable", "prior_options_intelligence_id": self.ids["t1"],
                                            "prior_as_of": T1.isoformat(), "session_date": "2026-09-30"})
        self.assertEqual((t2["call_volume"], t2["put_volume"]), (400, 200))
        self.assertEqual((t2["call_volume_change"], t2["call_volume_change_pct"]), (100, "33.33"))
        self.assertEqual((t2["put_volume_change"], t2["put_volume_change_pct"]), (50, "33.33"))
        self.assertEqual((t2["call_breadth"], t2["put_breadth"], t2["put_breadth_change"]), (2, 2, 1))
        self.assertEqual((t2["activity_bias"], t2["momentum_15m"]), ("CALL", "CALL"))
        self.assertEqual(t2["expiration_count"], 1)
        nvda = self.get(client, "/api/v1/options-intelligence/activity?symbol=NVDA")["data"][0]
        self.assertEqual(nvda["comparison"]["status"], "no_prior_snapshot")             # never META's report

    def test_activity_pages_with_the_history_cursor(self):
        _, client = self.client()
        with client:
            first = self.get(client, "/api/v1/options-intelligence/activity?limit=2")
            self.assertIsNotNone(first["meta"]["next_cursor"])
            rest = self.get(client, "/api/v1/options-intelligence/activity?limit=2&cursor="
                            + first["meta"]["next_cursor"])
            history = self.get(client, "/api/v1/options-intelligence")
        ids = [r["options_intelligence_id"] for r in first["data"] + rest["data"]]
        self.assertEqual(ids, [r["options_intelligence_id"] for r in history["data"]])
        response = client.get("/api/v1/options-intelligence/activity?bogus=1", headers={"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(response.status_code, 400)

    def test_index_prior_lookup(self):
        index = build_index(self.root)
        entry = index.get("options-intelligence", self.ids["t2"])
        status, prior = index.prior("options-intelligence", entry)
        self.assertEqual((status, prior.artifact_id), (oa.COMPARABLE, self.ids["t1"]))
        self.assertEqual(index.prior("options-intelligence", index.get("options-intelligence", self.ids["t1"])),
                         (oa.NO_PRIOR, None))

    def test_a_read_model_defect_never_fails_the_index_and_falls_back_to_the_file(self):
        with patch.dict("artifact_store.index.SUMMARIZERS", {"options-intelligence": lambda d: 1 / 0}):
            index = build_index(self.root)
            artifacts, client = self.client()
            with client:
                self.assertTrue(artifacts.healthy)
                row = self.get(client, f"/api/v1/options-intelligence/{self.ids['t1']}")["data"]
                activity = self.get(client, "/api/v1/options-intelligence/activity?symbol=META")["data"]
        self.assertIsNone(index.get("options-intelligence", self.ids["t1"]).summary)
        self.assertEqual(row, PROJECTIONS["options-intelligence"](self.reports["t1"]))
        self.assertEqual(len(activity), 3)


if __name__ == "__main__":
    unittest.main()
