"""Phase 9E: OptionsIntelligence local replay runner and the end-to-end Phase 9 flow (no network)."""
import ast
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import inspect
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from options_intelligence import runner
from options_intelligence.builder import build
from options_intelligence.canonical import canonical_json, content_id
from tests import options_intelligence_cases as cases
from tests.options_snapshot_cases import chain_record, live_snapshot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAL = cases.CALENDAR
LIVE = {sym: f"/tmp/phase9c_{sym}_snapshot.json" for sym in ("META", "NVDA")}
LIVE_IDS = {"META": "sha256:cd3e7f33876ecd0be086aba75acb2d6b121859182fc9ce4c0bb715dc20a8e134",
            "NVDA": "sha256:30fb2023e63523523cc6f763ad54cb2eeaee18e36d1965be465aa591b47d9993"}
SUMMARY_KEYS = {"result", "options_intelligence_id", "snapshot_id", "symbol", "as_of", "contract_count",
                "expiration_count", "truncated", "market_intelligence_attached", "quote_state_counts",
                "day_session_relation_counts", "iv_available_count", "greeks_available_count",
                "open_interest_present_count", "attention_count", "attention_codes", "underlying_price_status",
                "output", "output_bytes", "build_seconds", "verification_seconds"}


class Workspace(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = self._dir.name

    def tearDown(self):
        self._dir.cleanup()

    def path(self, name):
        return os.path.join(self.dir, name)

    def write(self, name, data):
        with open(self.path(name), "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else canonical_json(data) + "\n")
        return self.path(name)

    def run_cli(self, argv, calendar=CAL):
        out = io.StringIO()
        code = runner.main(argv, out=out, calendar=calendar)
        return code, out.getvalue()

    def snapshot_file(self, name="quoted_with_price"):
        return self.write(f"{name}.snapshot.json", cases.snapshot(name).to_dict())

    def mi_file(self):
        return self.write("mi.json", cases.market_intelligence().to_dict())


class RunnerSuccessTests(Workspace):
    def test_snapshot_only(self):
        output = self.path("out.json")
        code, text = self.run_cli(["--snapshot", self.snapshot_file(), "--output", output])
        report = json.loads(text)
        self.assertEqual((code, report["result"], set(report)), (0, "WRITTEN", SUMMARY_KEYS))
        with open(output, encoding="utf-8") as handle:
            written = handle.read()
        expected = build(cases.snapshot("quoted_with_price"), calendar=CAL)
        self.assertEqual(written, canonical_json(expected.to_dict()) + "\n")
        self.assertEqual(written, cases.intelligence_path("quoted_with_price").read_text(encoding="utf-8"))
        self.assertEqual((report["options_intelligence_id"], report["contract_count"], report["expiration_count"],
                          report["market_intelligence_attached"], report["output_bytes"]),
                         (expected.options_intelligence_id, 9, 2, False, len(written.encode())))
        self.assertEqual(report["quote_state_counts"], dict(ask_missing=1, bid_missing=1, both_missing=1, complete=2,
                                                            crossed=1, excluded_after_as_of=1, locked=2))
        self.assertEqual(report["day_session_relation_counts"],
                         dict(current_session=4, previous_session=1, older_session=1, unavailable=3))
        self.assertEqual((report["iv_available_count"], report["greeks_available_count"],
                          report["open_interest_present_count"]), (8, 8, 9))

    def test_market_intelligence_changes_only_reference_provenance_and_id(self):
        snap = self.write("meta.json", cases.snapshot("live_like_meta").to_dict())
        a, b = self.path("a.json"), self.path("b.json")
        self.assertEqual(self.run_cli(["--snapshot", snap, "--output", a])[0], 0)
        code, text = self.run_cli(["--snapshot", snap, "--market-intelligence", self.mi_file(), "--output", b])
        self.assertEqual((code, json.loads(text)["market_intelligence_attached"]), (0, True))
        with open(a) as fa, open(b) as fb:
            plain, referenced = json.load(fa), json.load(fb)
        self.assertEqual(referenced, json.loads(cases.intelligence_path("live_like_meta_with_mi").read_text()))
        changed = {k for k in plain if plain[k] != referenced[k]}
        self.assertEqual(changed, {"market_intelligence_ref", "provenance", "options_intelligence_id"})
        self.assertEqual({k for k in plain["provenance"] if plain["provenance"][k] != referenced["provenance"][k]},
                         {"market_intelligence_id"})


class InputValidationTests(Workspace):
    def assertRejected(self, argv, fragment):
        code, text = self.run_cli(argv + ["--output", self.path("never.json")])
        report = json.loads(text)
        self.assertEqual((code, report["result"]), (1, "NOT WRITTEN"))
        self.assertIn(fragment, report["reason"])
        self.assertFalse(os.path.exists(self.path("never.json")))

    def test_bad_snapshot_files(self):
        good = cases.snapshot("quoted_with_price").to_dict()
        tampered = deepcopy(good)
        tampered["contracts"][0]["open_interest"]["value"] = "1"
        version = deepcopy(good)
        version["snapshot_format_version"] = "phase9-snapshot-v2"
        self.assertRejected(["--snapshot", self.write("bad.json", "{not json")], "not JSON")
        self.assertRejected(["--snapshot", self.write("dup.json", '{"a": 1, "a": 2}')], "duplicate keys")
        self.assertRejected(["--snapshot", self.path("missing.json")], "not found")
        self.assertRejected(["--snapshot", self.write("t.json", tampered)], "snapshot_id does not match")
        self.assertRejected(["--snapshot", self.write("v.json", version)], "unsupported snapshot format version")

    def test_bad_market_intelligence_files(self):
        snap = self.snapshot_file()
        mi = cases.market_intelligence().to_dict()
        self.assertRejected(["--snapshot", snap, "--market-intelligence", self.write("m.json", "[oops")], "not JSON")
        self.assertRejected(["--snapshot", snap, "--market-intelligence",
                             self.write("m.json", dict(mi, intelligence_id="sha256:" + "0" * 64))],
                            "market intelligence id does not match")
        nvda = self.write("nvda.json", live_snapshot([], underlying="NVDA").to_dict())
        self.assertRejected(["--snapshot", nvda, "--market-intelligence", self.mi_file()],
                            "symbol does not match the snapshot underlying")
        from options_data.normalization import assemble
        from tests.options_snapshot_cases import AS_OF, PROVENANCE
        early = self.write("early.json", assemble("META", AS_OF.replace(day=20), [], provenance=PROVENANCE).to_dict())
        self.assertRejected(["--snapshot", early, "--market-intelligence", self.mi_file()],
                            "as_of is later than the snapshot as_of")

    def test_usage(self):
        self.assertEqual(self.run_cli(["--snapshot", "x"])[0], 2)
        self.assertEqual(self.run_cli([])[0], 2)


class OutputTests(Workspace):
    def test_exists_overwrite_failure_cleanup(self):
        snap, output = self.snapshot_file(), self.path("out.json")
        with open(output, "w") as handle:
            handle.write("keep")
        code, text = self.run_cli(["--snapshot", snap, "--output", output])
        self.assertEqual((code, json.loads(text)["result"]), (3, "NOT WRITTEN"))
        with open(output) as handle:
            self.assertEqual(handle.read(), "keep")
        self.assertEqual(self.run_cli(["--snapshot", snap, "--output", output, "--overwrite"])[0], 0)
        with open(output) as handle:
            self.assertTrue(handle.read().startswith('{"activity"'))
        target = self.path("fail.json")
        with patch.object(runner.os, "fsync", side_effect=OSError("disk full")):
            code, text = self.run_cli(["--snapshot", snap, "--output", target])
        self.assertEqual((code, json.loads(text)["reason"]), (3, "cannot write the output file"))
        self.assertEqual(sorted(os.listdir(self.dir)), sorted(["out.json", "quoted_with_price.snapshot.json"]))
        self.assertEqual(self.run_cli(["--snapshot", snap, "--output", self.path("no/dir.json")])[0], 3)


class DeterminismTests(Workspace):
    def test_repeated_runs_identical_bytes(self):
        snap = self.snapshot_file()
        blobs = set()
        for n in range(3):
            output = self.path(f"o{n}.json")
            self.assertEqual(self.run_cli(["--snapshot", snap, "--market-intelligence", self.mi_file(),
                                           "--output", output])[0], 0)
            with open(output, "rb") as handle:
                blobs.add(handle.read())
        self.assertEqual(len(blobs), 1)

    def test_fresh_processes_cwd_seed_and_environment(self):
        snap = self.write("s.json", cases.snapshot("live_like_nvda").to_dict())
        blobs = set()
        for n, extra in enumerate(({"PYTHONHASHSEED": "0"},
                                   {"PYTHONHASHSEED": "9", "TZ": "Asia/Tokyo", "HOSTNAME": "elsewhere"},
                                   {"PYTHONHASHSEED": "31337", "LANG": "C", "TZ": "UTC", "MIAS_UNRELATED": "x",
                                    "OPTIONS_DATA_DELAY_SECONDS": "900", "MARKET_DATA_PROVIDER": "polygon"})):
            output = self.path(f"p{n}.json")
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-m", "options_intelligence.runner", "--snapshot", snap,
                                         "--output", output], cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-300:])
            with open(output, "rb") as handle:
                blobs.add(handle.read())
        self.assertEqual(len(blobs), 1)


class BoundaryTests(Workspace):
    def test_no_network_database_clock_or_ai(self):
        import sqlalchemy
        snap = self.snapshot_file()
        blocked = AssertionError("blocked")
        with patch.object(socket, "socket", side_effect=blocked), \
                patch.object(socket, "create_connection", side_effect=blocked), \
                patch.object(sqlalchemy, "create_engine", side_effect=blocked), \
                patch.object(subprocess, "Popen", side_effect=blocked), \
                patch.object(time, "time", side_effect=blocked), patch.object(time, "time_ns", side_effect=blocked):
            code, _ = self.run_cli(["--snapshot", snap, "--output", self.path("o.json")])
        self.assertEqual(code, 0)
        source = inspect.getsource(runner)
        tree = ast.parse(source)
        calls = {n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
                 for n in ast.walk(tree) if isinstance(n, ast.Call)}
        self.assertFalse(calls & {"now", "utcnow", "today", "time", "time_ns", "getenv", "urlopen", "get_json",
                                  "create_engine", "connect"})
        self.assertNotIn("environ", {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})

    def test_import_boundary(self):
        code = ("import sys, options_intelligence.runner\n"
                "print(sorted(n for n in sys.modules if n.split('.')[0] in {'persistence', 'sqlalchemy', 'evidence', "
                "'evaluation', 'evidence_packet', 'evidence_synthesis', 'market_intelligence', 'openai', 'requests', "
                "'redis', 'market_context', 'technical'} or n in {'options_data.massive', 'options_data.runner', "
                "'options_data.provider', 'options_data.config', 'market_data.http'}))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_no_selection_flags_and_metadata_only_stdout(self):
        tree = ast.parse(inspect.getsource(runner))
        flags = {a.value for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and getattr(n.func, "attr", "") == "add_argument" for a in n.args if isinstance(a, ast.Constant)}
        self.assertEqual(flags, {"--snapshot", "--output", "--market-intelligence", "--overwrite"})
        snap = self.snapshot_file()
        _, text = self.run_cli(["--snapshot", snap, "--output", self.path("o.json")])
        for leaked in ("META261016C", "10.1", "10.4", "0.4123", "0.55", "\"690\"", "\"bid\"", "\"delta\""):
            self.assertNotIn(leaked, text)
        forbidden = {"score", "rank", "best", "recommend", "select", "direction", "target", "stop", "signal", "buy",
                     "sell"}
        for key in json.loads(text):
            self.assertFalse(set(re.split(r"[^a-z]+", key)) & forbidden, key)


class EndToEndTests(Workspace):
    def test_snapshot_runner_then_intelligence_runner_then_replay(self):
        from options_data import runner as snapshot_runner
        from options_data.provider import FixtureOptionsProvider
        from tests.options_snapshot_cases import AS_OF
        from tests.test_options_massive_check import FixedCalendar
        from options_data.massive import map_contract
        from tests.test_options_massive_adapter import live_item
        records = [map_contract(live_item(s, k)) for s in (690, 700, 710) for k in ("call", "put")]
        snap = self.path("META-options-snapshot.json")
        code = snapshot_runner.main(["--symbol", "META", "--output", snap], environ=dict(OPTIONS_DATA_PROVIDER="none"),
                                    provider=FixtureOptionsProvider(records), clock=lambda: AS_OF,
                                    calendar=FixedCalendar(), out=io.StringIO())
        self.assertEqual(code, 0)
        first, replay = self.path("META-options-intelligence.json"), self.path("replay.json")
        self.assertEqual(self.run_cli(["--snapshot", snap, "--output", first])[0], 0)
        self.assertEqual(self.run_cli(["--snapshot", snap, "--output", replay])[0], 0)
        with open(first, "rb") as a, open(replay, "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_large_synthetic_snapshot_file(self):
        expirations = [(date(2026, 10, 2) + timedelta(weeks=n)).isoformat() for n in range(25)]
        records = [chain_record("META", e, kind, 400 + 2.5 * n, iv=n % 3 != 0, greeks=n % 3 != 0)
                   for e in expirations for kind in ("call", "put") for n in range(200)]
        snap = self.write("large.json", live_snapshot(records).to_dict())
        code, text = self.run_cli(["--snapshot", snap, "--output", self.path("large-intelligence.json")])
        report = json.loads(text)
        self.assertEqual((code, report["contract_count"], report["expiration_count"]), (0, 10000, 25))
        self.assertGreater(report["output_bytes"], os.path.getsize(snap) // 2)


@unittest.skipUnless(all(os.path.exists(p) for p in LIVE.values()), "local Phase 9C live snapshots not present")
class LiveReplayTests(Workspace):
    """Local /tmp artifacts only (never committed); metadata assertions only."""

    def test_live_ids_reproduce_phase9d(self):
        from market_data.calendar import default_calendar
        calendar = default_calendar()
        for sym, path in LIVE.items():
            with self.subTest(symbol=sym):
                blobs = set()
                for n in range(2):
                    output = self.path(f"{sym}-{n}.json")
                    code, text = self.run_cli(["--snapshot", path, "--output", output], calendar=calendar)
                    report = json.loads(text)
                    self.assertEqual((code, report["options_intelligence_id"]), (0, LIVE_IDS[sym]))
                    self.assertEqual(report["quote_state_counts"], {"unavailable": report["contract_count"]})
                    self.assertEqual(report["underlying_price_status"], "unavailable")
                    with open(output, "rb") as handle:
                        blobs.add(handle.read())
                self.assertEqual(len(blobs), 1)


if __name__ == "__main__":
    unittest.main()
