"""Offline SEC collector baseline: dotenv, HTTP, Redis and Telegram are all mocked.

The configured SEC User-Agent contains contact details; these tests only check
it by identity and never print or assert on its contents.
"""

import os
import runpy
import socket
import unittest
import warnings
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

# Prevent configuration imports from consulting local secrets.
with patch("dotenv.load_dotenv"), patch.dict(os.environ, {
    "ALERT_THRESHOLD": "70", "DISPLAY_THRESHOLD": "40",
    "REDIS_HOST": "localhost", "REDIS_PORT": "6379",
    "DEDUP_TTL_SECONDS": "86400", "HEADLINE_TTL_SECONDS": "86400",
    "NEAR_DUPLICATE_THRESHOLD": "0.80",
}, clear=True):
    from collector import sec_collector as sec
    from analyzer import deduplicator
    from analyzer.sec_scoring import SEC_FORM_SCORES


def filing(form="8-K", accession="0001326801-26-000101", symbol="META",
           filing_date="2026-09-22", primary_document="meta-20260922.htm"):
    return dict(symbol=symbol, form=form, accession_number=accession,
                filing_date=filing_date, primary_document=primary_document)


def submissions(*filings):
    return {"filings": {"recent": {
        "form": [f["form"] for f in filings],
        "accessionNumber": [f["accession_number"] for f in filings],
        "filingDate": [f["filing_date"] for f in filings],
        "primaryDocument": [f["primary_document"] for f in filings],
    }}}


class SecPipelineTests(unittest.TestCase):
    def setUp(self):
        # Any real socket connection (HTTP, Redis, Telegram) fails the test.
        self.enterContext(patch.object(socket.socket, "connect",
                                       side_effect=AssertionError("network access in SEC test")))
        self.store = set()
        self.redis = self.enterContext(patch.object(deduplicator, "redis_client"))

        def set_nx(key, value, *, nx, ex):
            if key in self.store:
                return None
            self.store.add(key)
            return True
        self.redis.set.side_effect = set_nx
        self.send = self.enterContext(patch.object(sec, "send_telegram_alert"))
        self.send.return_value = {"ok": True, "result": {"message_id": 7}}

    def process(self, filings):
        out = StringIO()
        with redirect_stdout(out), self.assertLogs("sec_collector", "INFO") as logs:
            events = sec.process_sec_filings(filings)
        return events, out.getvalue(), logs.output

    def test_module_imports_and_exposes_pipeline(self):
        self.assertTrue(callable(sec.process_sec_filings))
        self.assertTrue(callable(sec.collect_sec_filings))
        self.assertEqual(sec.SEC_COMPANIES, {"META": "0001326801", "NVDA": "0001045810"})
        self.assertFalse(hasattr(sec, "events") or hasattr(sec, "filings"))

    def test_returns_expected_event_structure(self):
        events, _, _ = self.process([filing()])
        self.assertEqual(events, [{
            "source": "SEC EDGAR", "publisher": "SEC",
            "headline": "META filed SEC Form 8-K",
            "url": "https://www.sec.gov/Archives/edgar/data/1326801/000132680126000101/meta-20260922.htm",
            "published_at": "2026-09-22", "summary": "SEC filing Form 8-K for META",
            "symbols": ["META"], "direct_symbols": ["META"], "related_symbols": [],
            "relevant": True, "event_type": "sec_filing", "sec_form": "8-K",
            "accession_number": "0001326801-26-000101",
            "impact_score": 70, "impact_level": "HIGH",
            "score_reasons": ["SEC primary source", "SEC Form 8-K", "Direct META filing"],
            "alert_decision": "ALERT",
        }])

    def test_form_scores_and_decisions_unchanged(self):
        self.assertEqual(SEC_FORM_SCORES, {"8-K": 45, "10-Q": 50, "10-K": 55, "6-K": 45, "20-F": 55,
                                           "4": 20, "3": 15, "5": 15, "144": 15, "N-PX": 10})
        expected = {"8-K": (70, "HIGH", "ALERT"), "10-Q": (75, "HIGH", "ALERT"),
                    "10-K": (80, "HIGH", "ALERT"), "4": (45, "MEDIUM", "DISPLAY_ONLY"),
                    "144": (40, "MEDIUM", "DISPLAY_ONLY"), "3": (40, "MEDIUM", "DISPLAY_ONLY"),
                    "N-PX": (35, "LOW", "IGNORE"), "8-K/A": (35, "LOW", "IGNORE")}
        batch = [filing(form=form, accession=f"0001326801-26-{index:06d}")
                 for index, form in enumerate(expected)]
        events, _, _ = self.process(batch)
        self.assertEqual({e["sec_form"]: (e["impact_score"], e["impact_level"], e["alert_decision"])
                          for e in events}, expected)

    def test_duplicate_skipped_with_existing_namespace_and_identity(self):
        events, _, logs = self.process([filing(), filing(), filing(accession="0001326801-26-000102")])
        self.assertEqual([e["accession_number"] for e in events],
                         ["0001326801-26-000101", "0001326801-26-000102"])
        keys = [c.args[0] for c in self.redis.set.call_args_list]
        self.assertEqual(len(keys), 3)
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], "mias:sec:event:" + deduplicator.create_fingerprint(events[0]))
        self.assertEqual({c.kwargs["ex"] for c in self.redis.set.call_args_list}, {86400})
        self.assertIn("INFO:sec_collector:Duplicate SEC filing skipped: ['META'] 8-K", logs)
        self.assertEqual(self.send.call_count, 2)

    def test_redis_unavailable_fails_open(self):
        self.redis.set.side_effect = deduplicator.redis.RedisError("down")
        with self.assertLogs("deduplicator", "ERROR") as dedup_logs:
            events, _, _ = self.process([filing(), filing()])
        self.assertEqual(len(events), 2)
        self.assertEqual(dedup_logs.output, ["ERROR:deduplicator:Redis deduplication unavailable: down"] * 2)

    def test_telegram_only_for_alert(self):
        events, out, logs = self.process([filing(form="10-K", accession="0001326801-26-000001"),
                                          filing(form="4", accession="0001326801-26-000002"),
                                          filing(form="N-PX", accession="0001326801-26-000003")])
        self.assertEqual([e["alert_decision"] for e in events], ["ALERT", "DISPLAY_ONLY", "IGNORE"])
        self.send.assert_called_once_with(sec.format_alert(events[0]))
        self.assertEqual(out, sec.format_alert(events[0]) + "\n")
        self.assertIn("INFO:sec_collector:Telegram SEC alert sent message_id=7", logs)

    def test_telegram_failure_logged_and_processing_continues(self):
        self.send.side_effect = [RuntimeError("telegram down"), {"result": {}}, {"result": {"message_id": 9}}]
        events, _, logs = self.process([filing(accession=f"0001326801-26-00000{i}") for i in range(3)])
        self.assertEqual(len(events), 3)
        self.assertEqual(self.send.call_count, 3)
        self.assertIn("ERROR:sec_collector:Telegram SEC alert failed: telegram down", logs)
        self.assertIn("ERROR:sec_collector:Telegram SEC alert failed: 'message_id'", logs)
        self.assertIn("INFO:sec_collector:Telegram SEC alert sent message_id=9", logs)

    def test_collect_uses_existing_source_and_header_without_network(self):
        response = Mock()
        response.json.return_value = submissions(filing(), filing(form="4", accession="0001326801-26-000102"))
        with patch.object(sec.requests, "get", return_value=response) as get, \
                self.assertLogs("sec_collector", "INFO"):
            filings = sec.collect_sec_filings()
        urls = [c.args[0] for c in get.call_args_list]
        self.assertEqual(urls, ["https://data.sec.gov/submissions/CIK0001326801.json",
                                "https://data.sec.gov/submissions/CIK0001045810.json"])
        self.assertTrue(all(c.kwargs["headers"] is sec.SEC_HEADERS and c.kwargs["timeout"] == 15
                            for c in get.call_args_list), "SEC request header/timeout changed")
        self.assertEqual([f["symbol"] for f in filings], ["META", "META", "NVDA", "NVDA"])

    def test_collect_request_failure_logged_per_company(self):
        response = Mock()
        response.json.return_value = submissions(filing(symbol="NVDA"))
        with patch.object(sec.requests, "get", side_effect=[sec.requests.ConnectionError("offline"), response]), \
                self.assertLogs("sec_collector", "INFO") as logs:
            filings = sec.collect_sec_filings()
        self.assertEqual(len(filings), 1)
        self.assertIn("ERROR:sec_collector:SEC request failed for META: offline", logs.output)

    def test_script_path_matches_extracted_function(self):
        batch = [filing(), filing(form="4", accession="0001326801-26-000102"),
                 filing(form="N-PX", accession="0001326801-26-000103")]
        response = Mock()
        response.json.return_value = submissions(*batch)
        out = StringIO()
        with patch("requests.get", return_value=response), \
                patch("alert_engine.telegram_notifier.send_telegram_alert", self.send), \
                redirect_stdout(out), self.assertLogs("sec_collector", "INFO"), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            namespace = runpy.run_module("collector.sec_collector", run_name="__main__")
        script_events, script_calls = namespace["events"], list(self.send.call_args_list)
        self.assertEqual([e["symbols"] for e in script_events], [["META"]] * 3 + [["NVDA"]] * 3)
        self.assertEqual(len(script_calls), 2)

        self.store.clear()
        self.send.reset_mock()
        with patch.object(sec.requests, "get", return_value=response), self.assertLogs("sec_collector", "INFO"):
            filings = sec.collect_sec_filings()
        function_events, _, _ = self.process(filings)
        self.assertEqual(script_events, function_events)
        self.assertEqual(script_calls, self.send.call_args_list)
        self.assertIn("PROCESSED SEC EVENTS", out.getvalue())
        self.assertIn("Decision  : IGNORE", out.getvalue())
        self.assertNotIn("@", out.getvalue())


if __name__ == "__main__":
    unittest.main()
