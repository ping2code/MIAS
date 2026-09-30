"""Phase 9B OptionsSnapshot: tamper matrix, determinism, cross-process, sealed no-I/O, import boundary, large chain,
and forbidden decision semantics."""
import ast
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

from options_data import canonical, identity, model, normalization, validation
from options_data.canonical import canonical_json, content_id
from options_data.validation import OptionsSnapshotError, validated_snapshot
from tests.options_snapshot_cases import (AFTER, BEFORE, chain_record, full_snapshot, live_snapshot, meta_chain,
                                          nvda_chain)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PURE = (canonical, identity, model, normalization, validation)


def resealed(data):
    data["snapshot_id"] = content_id({k: v for k, v in data.items() if k != "snapshot_id"})
    return data


def live():
    return live_snapshot(meta_chain()).to_dict()


def full():
    """All groups available; the first contract carries a timed quote and trade."""
    records = meta_chain()
    records[0] = chain_record(quote=dict(bid="10.1", ask="10.4", bid_size=5, ask_size=7, observed_at=BEFORE),
                              trade=dict(price="10.3", size=2, observed_at=BEFORE), strike=690)
    return full_snapshot(records).to_dict()


def first(d, group):
    return d["contracts"][0][group]


def _expired(d):
    i = d["contracts"][0]["identity"]
    i.update(provider_symbol="O:META260901C00690000", contract_id="META260901C00690000", expiration="2026-09-01")


def _set(d, **kw):
    d.update(kw)


# (label, factory, mutation, reseal, exact message)
TAMPER = [
    ("wrong format version", live, lambda d: _set(d, snapshot_format_version="phase9-snapshot-v2"), True,
     "unsupported snapshot format version"),
    ("missing key", live, lambda d: d.pop("scope"), True, "snapshot must have exactly the 10 phase9-snapshot-v1 keys"),
    ("extra key", live, lambda d: _set(d, score=1), True, "snapshot must have exactly the 10 phase9-snapshot-v1 keys"),
    ("snapshot_id mismatch", live, lambda d: _set(d, underlying="NVDA"), False,
     "snapshot_id does not match the snapshot body (tampered or corrupt)"),
    ("malformed snapshot_id", live, lambda d: _set(d, snapshot_id="md5:x"), False, "snapshot_id is malformed"),
    ("NaN anywhere", live, lambda d: first(d, "day").update(volume=float("nan")), False,
     "snapshot body is not canonical JSON"),
    ("malformed underlying", live, lambda d: _set(d, underlying="meta"), True, "underlying is malformed"),
    ("malformed as_of", live, lambda d: _set(d, as_of="yesterday"), True, "as_of is not ISO 8601"),
    ("naive as_of", live, lambda d: _set(d, as_of="2026-09-30T14:45:00"), True, "as_of must be timezone-aware"),
    ("non-UTC as_of", live, lambda d: _set(d, as_of="2026-09-30T10:45:00-04:00"), True, "as_of must be canonical UTC"),
    ("wrong session date", live, lambda d: d["session"].update(session_date="2026-09-29"), True,
     "session.session_date must be the as_of exchange date"),
    ("duplicate contract_id", live, lambda d: d["contracts"].insert(1, deepcopy(d["contracts"][0])), True,
     "duplicate contract_id"),
    ("unsorted contracts", live, lambda d: d["contracts"].reverse(), True, "contracts are not in canonical order"),
    ("invalid option type", live, lambda d: first(d, "identity").update(option_type="straddle"), True,
     "contract identity does not match its provider_symbol"),
    ("strike <= 0", live, lambda d: first(d, "identity").update(strike="0"), True,
     "contract strike does not match its symbol"),
    ("expired contract", live, _expired, True, "contract expired before the as_of date"),
    ("negative bid", full, lambda d: first(d, "quote").update(bid="-1"), True, "quote.bid must be non-negative"),
    ("negative ask size", full, lambda d: first(d, "quote").update(ask_size="-7"), True,
     "quote.ask_size must be non-negative"),
    ("negative trade price", full, lambda d: first(d, "trade").update(price="-10.3"), True,
     "trade.price must be non-negative"),
    ("negative day volume", live, lambda d: first(d, "day").update(volume="-1"), True,
     "day.volume must be non-negative"),
    ("negative open interest", live, lambda d: first(d, "open_interest").update(value="-1"), True,
     "open_interest.value must be non-negative"),
    ("negative IV", live, lambda d: first(d, "implied_volatility").update(value="-0.1"), True,
     "implied_volatility.value must be non-negative"),
    ("fractional volume", live, lambda d: first(d, "day").update(volume="1.5"), True,
     "day.volume must be a whole number"),
    ("non-canonical decimal", live, lambda d: first(d, "day").update(open="10.0"), True,
     "day.open must be a canonical Decimal string"),
    ("float value", live, lambda d: first(d, "day").update(open=10.5), True, "day.open must be a Decimal string"),
    ("observed_at after as_of", full, lambda d: first(d, "quote").update(observed_at=AFTER.isoformat()), True,
     "quote.observed_at is later than as_of"),
    ("status/value inconsistent", live, lambda d: first(d, "greeks").update(status="missing"), True,
     "greeks is missing but carries values"),
    ("present without values", live, lambda d: first(d, "implied_volatility").update(value=None), True,
     "implied_volatility is present but has no value"),
    ("time_basis without observed_at", live, lambda d: first(d, "implied_volatility").update(time_basis="observed_at"),
     True, "implied_volatility.observed_at is inconsistent with its time_basis"),
    ("unsupported time_basis", live, lambda d: first(d, "greeks").update(time_basis="provider_as_of_date"), True,
     "greeks.time_basis is not supported for this group"),
    ("invented OI date", live, lambda d: first(d, "open_interest").update(as_of_date="2026-09-29"), True,
     "open_interest.as_of_date is inconsistent with its time_basis"),
    ("IV not from provider", live, lambda d: first(d, "implied_volatility").update(source="model"), True,
     "implied_volatility.source must be provider"),
    ("capability inconsistency", live, lambda d: first(d, "quote").update(status="missing"), True,
     "quote.status is inconsistent with the source capability"),
    ("fact_after_as_of count", live, lambda d: d["exclusions"].append(dict(reason="fact_after_as_of", count=1)), True,
     "fact_after_as_of count is inconsistent with the excluded fact groups"),
    ("records_received", live, lambda d: d["provenance"].update(records_received=99), True,
     "provenance.records_received is inconsistent with contracts and exclusions"),
    ("zero exclusion count", live, lambda d: d["exclusions"].append(dict(reason="malformed_record", count=0)), True,
     "exclusion count must be a positive integer"),
    ("unknown exclusion reason", live, lambda d: d["exclusions"].append(dict(reason="low_liquidity", count=1)), True,
     "exclusion reason is not supported"),
    ("unavailable price with value", live, lambda d: d["underlying_price"].update(value="700"), True,
     "unavailable underlying_price must carry only a reason"),
    ("scope types", live, lambda d: d["scope"].update(contract_types=["put", "call"]), True,
     "scope.contract_types must be a sorted subset of call/put"),
    ("capabilities order", live, lambda d: d["provenance"]["source_capabilities"].reverse(), True,
     "provenance.source_capabilities must list every fact group in order"),
]


class TamperTests(unittest.TestCase):
    def test_matrix_fails_closed_with_stable_messages(self):
        for label, factory, fn, reseal, message in TAMPER:
            with self.subTest(case=label):
                data = factory()
                fn(data)
                if reseal:
                    data = resealed(data)
                before = deepcopy(data)
                with self.assertRaises(OptionsSnapshotError) as caught:
                    validated_snapshot(data)
                self.assertEqual(str(caught.exception), message)
                self.assertEqual(data, before)

    def test_valid_forms(self):
        for snapshot in (live_snapshot(meta_chain()), live_snapshot(nvda_chain(), underlying="NVDA"),
                         live_snapshot([]), full_snapshot(meta_chain())):
            self.assertEqual(validated_snapshot(snapshot), snapshot.to_dict())
            data = json.loads(canonical_json(snapshot.to_dict()))
            self.assertIs(validated_snapshot(data), data)
        for bad in (None, [], "snapshot", 1):
            with self.assertRaises(OptionsSnapshotError):
                validated_snapshot(bad)


class DeterminismTests(unittest.TestCase):
    def test_100_repeats(self):
        expected = canonical_json(live_snapshot(meta_chain() + nvda_chain()[:0]).to_dict())
        self.assertEqual({canonical_json(live_snapshot(meta_chain()).to_dict()) for _ in range(100)}, {expected})

    def test_fresh_processes_and_environments(self):
        code = ("from options_data.canonical import canonical_json\n"
                "from tests.options_snapshot_cases import live_snapshot, full_snapshot, meta_chain, nvda_chain\n"
                "print(live_snapshot(meta_chain()).snapshot_id, live_snapshot(nvda_chain(), underlying='NVDA')"
                ".snapshot_id, full_snapshot(meta_chain()).snapshot_id)")
        expected = " ".join((live_snapshot(meta_chain()).snapshot_id,
                             live_snapshot(nvda_chain(), underlying="NVDA").snapshot_id,
                             full_snapshot(meta_chain()).snapshot_id))
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "3", "TZ": "Asia/Tokyo", "HOSTNAME": "elsewhere"},
                      {"PYTHONHASHSEED": "8191", "LANG": "C", "TZ": "UTC", "MIAS_UNRELATED": "x"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd,
                                        env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class IsolationTests(unittest.TestCase):
    def test_sealed_assembly_and_validation(self):
        from tests.test_market_intelligence_isolation import sealed
        records = meta_chain() + [chain_record(quote=dict(bid="1", observed_at=BEFORE), strike=720)]
        expected = full_snapshot(records).snapshot_id
        with sealed():
            snapshot = full_snapshot(records)
            validated_snapshot(snapshot)
        self.assertEqual(snapshot.snapshot_id, expected)

    def test_pure_modules_use_no_io_clock_or_environment(self):
        forbidden = {"open", "environ", "getenv", "now", "utcnow", "today", "time", "time_ns", "monotonic",
                     "perf_counter", "system", "Popen", "run", "socket", "create_connection", "print", "input",
                     "write_text", "read_text", "mkdir", "unlink", "getpid", "gethostname"}
        stdlib = set(sys.stdlib_module_names)
        for module in PURE:
            tree = ast.parse(inspect.getsource(module))
            imported, used = set(), set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom):
                    imported.add(node.module.split(".")[0])
                elif isinstance(node, ast.Call):  # Calls only: "open" is also the day group's open-price field.
                    func = node.func
                    used.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
                elif isinstance(node, ast.Attribute) and node.attr in ("environ", "argv", "stdin", "stdout"):
                    used.add(node.attr)
            self.assertLessEqual(imported - stdlib, {"options_data", "market_data"}, module.__name__)
            self.assertFalse(used & forbidden, (module.__name__, used & forbidden))

    def test_runtime_import_boundary(self):
        code = ("import sys, options_data.normalization, options_data.validation\n"
                "roots = {n.split('.')[0] for n in sys.modules}\n"
                "print(sorted(roots & {'persistence', 'sqlalchemy', 'evidence', 'evaluation', 'evidence_packet', "
                "'evidence_synthesis', 'market_intelligence', 'options_intelligence', 'openai', 'requests', 'redis', "
                "'telegram', 'market_context', 'technical', 'analyzer', 'shared'}), "
                "sorted(n for n in sys.modules if n.startswith('market_data')))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[] ['market_data', 'market_data.models']", result.stderr[-300:])


class LargeChainTests(unittest.TestCase):
    def test_ten_thousand_contracts_no_filtering(self):
        expirations = [(date(2026, 10, 2) + timedelta(weeks=i)).isoformat() for i in range(25)]
        records = [chain_record("META", e, kind, 400 + 2.5 * i, iv=i % 3 != 0, greeks=i % 3 != 0)
                   for e in expirations for kind in ("call", "put") for i in range(200)]
        started = time.perf_counter()
        snapshot = live_snapshot(records)
        elapsed = time.perf_counter() - started
        text = canonical_json(snapshot.to_dict())
        self.assertEqual((len(snapshot.contracts), snapshot.exclusions, snapshot.provenance.records_received),
                         (10000, (), 10000))
        self.assertLess(elapsed, 120)  # Characterization guard only (about 5 s on the development machine).
        self.assertGreater(len(text), 10_000_000)  # ~1.2 KB per contract; nothing dropped.


FORBIDDEN = {"score", "confidence", "rank", "ranking", "recommend", "recommendation", "buy", "sell", "signal",
             "prediction", "predict", "forecast", "probability", "target", "stop", "select", "selection", "pick",
             "best", "cheap", "expensive", "liquid", "liquidity", "mid", "spread", "moneyness", "itm", "otm", "atm",
             "dte", "sentiment", "severity", "priority"}


def words(identifier):
    return set(re.split(r"[^a-z0-9]+", identifier.lower())) - {""}


class ForbiddenSemanticsTests(unittest.TestCase):
    def test_snapshot_keys(self):
        for snapshot in (live_snapshot(meta_chain()), full_snapshot(meta_chain())):
            stack = [snapshot.to_dict()]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        self.assertFalse(words(key) & FORBIDDEN, key)
                        stack.append(child)
                elif isinstance(value, list):
                    stack.extend(value)

    def test_vocabularies_and_identifiers(self):
        for value in (*model.FACT_STATUSES, *model.TIME_BASES, *model.EXCLUSION_REASONS, *model.FACT_GROUPS):
            self.assertFalse(words(value) & FORBIDDEN, value)
        for module in PURE:
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                names = [node.id] if isinstance(node, ast.Name) else [node.attr] if isinstance(node, ast.Attribute) \
                    else [node.name] if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else []
                for name in names:
                    self.assertFalse(words(name) & FORBIDDEN, (module.__name__, name))


if __name__ == "__main__":
    unittest.main()
