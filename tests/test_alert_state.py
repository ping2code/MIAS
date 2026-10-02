"""Phase 12C alert state: the pure dedup and terminal-state model, the replay reducer, and the Redis boundary
adapter (an in-memory fake, plus the disposable test Redis when MIAS_PHASE2J_REDIS_URL is set; never the persistent
Redis). No Telegram, network, database or legacy alert modules."""
import ast
from copy import deepcopy
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
import uuid

import redis

from alert_engine import state as st, state_store as ss
from alert_engine.builder import market_pattern_changed, setup_available, setup_invalidated
from alert_engine.canonical import canonical_json, content_id
from alert_engine.state import (DUPLICATE, NEW, TERMINAL_SUPPRESSED, AlertSubjectState, ReplayState, decide, replay,
                                subject_key, validated_subject_state)
from alert_engine.state_store import AlertStateUnavailable, RedisAlertStateStore
from alert_engine.validation import AlertInputError
from tests import setup_evaluation_cases as sc
from tests import trade_setup_cases as ts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def events():
    """Sealed Phase 12B AlertEvents (plain dicts) for one bullish setup and a META pattern history."""
    other, _ = ts.screened([ts.record(705)])
    return dict(
        available=setup_available(sc.setup()).to_dict(),
        available_other=setup_available(other).to_dict(),
        invalidated=setup_invalidated(sc.check("all_bearish", 86400)).to_dict(),
        invalidated_later=setup_invalidated(sc.check("higher_aligned_5m_opposed", 2 * 86400)).to_dict(),
        a_to_b=market_pattern_changed(ts.market_intelligence("all_bullish"), ts.later_mi("higher_aligned_5m_opposed")).to_dict(),
        b_to_a=market_pattern_changed(ts.later_mi("higher_aligned_5m_opposed"),
                                      ts.later_mi("all_bullish", seconds=2 * 86400)).to_dict())


def classes(result):
    return [d.classification for d in result.decisions]


class PureStateTests(unittest.TestCase):
    def setUp(self):
        self.e = events()

    def test_exact_duplicate(self):
        result = replay([self.e["available"], self.e["available"]])
        self.assertEqual(classes(result), [NEW, DUPLICATE])
        self.assertEqual(result.state.seen_alert_ids, (self.e["available"]["alert_id"],))

    def test_setup_available(self):
        result = replay([self.e["available"], self.e["available_other"], self.e["available"]])
        self.assertEqual(classes(result), [NEW, NEW, DUPLICATE])
        keys = [s.subject_key for s in result.state.subjects]
        self.assertEqual(len(keys), 2)                                 # two assessments, same symbol: two subjects
        self.assertEqual({s.symbol for s in result.state.subjects}, {"META"})
        self.assertTrue(all(not s.terminal for s in result.state.subjects))

    def test_setup_invalidated_is_terminal(self):
        e = self.e
        result = replay([e["available"], e["invalidated"], e["invalidated"], e["invalidated_later"]])
        self.assertEqual(classes(result), [NEW, NEW, DUPLICATE, TERMINAL_SUPPRESSED])
        setup = result.state.subject(subject_key(e["available"]))
        self.assertEqual((setup.terminal, setup.terminal_alert_id, setup.last_alert_id),
                         (True, e["invalidated"]["alert_id"], e["invalidated"]["alert_id"]))  # earliest stays authoritative
        self.assertEqual(setup.current_pattern, None)
        self.assertNotIn(e["invalidated_later"]["alert_id"], result.state.seen_alert_ids)

    def test_no_reactivation(self):
        e = self.e
        result = replay([e["invalidated"], e["available"]])           # availability after invalidation, same setup
        self.assertEqual(classes(result), [NEW, TERMINAL_SUPPRESSED])
        self.assertTrue(result.state.subject(subject_key(e["available"])).terminal)

    def test_market_pattern_changed(self):
        e = self.e
        result = replay([e["a_to_b"], e["a_to_b"], e["b_to_a"]])
        self.assertEqual(classes(result), [NEW, DUPLICATE, NEW])
        symbol = result.state.subject("symbol:META")
        self.assertEqual((symbol.current_pattern, symbol.last_alert_id, symbol.last_source_id),
                         ("all_bullish", e["b_to_a"]["alert_id"],
                          next(x["id"] for x in e["b_to_a"]["source_refs"] if x["role"] == "current")))
        self.assertNotEqual(e["a_to_b"]["alert_id"], e["b_to_a"]["alert_id"])
        self.assertEqual(replay([e["a_to_b"]]).state.subject("symbol:META").current_pattern, "opposed")

    def test_precedence_duplicate_before_terminal(self):
        e = self.e
        terminal = replay([e["invalidated"]]).state.subject(subject_key(e["invalidated"]))
        self.assertEqual(decide(e["invalidated"], terminal, True).classification, DUPLICATE)
        self.assertEqual(decide(e["invalidated_later"], terminal, False).classification, TERMINAL_SUPPRESSED)
        self.assertEqual(decide(e["invalidated_later"], terminal, True).classification, DUPLICATE)

    def test_state_has_no_wall_clock(self):
        result = replay([self.e["available"]])
        state = result.state.subjects[0]
        self.assertEqual(state.last_as_of, self.e["available"]["as_of"])
        self.assertEqual(list(state.to_dict()), list(st.SUBJECT_STATE_FIELDS))
        self.assertEqual(state.state_format_version, "phase12-state-v1")

    def test_fail_closed_inputs(self):
        e = self.e
        tampered = deepcopy(e["available"])
        tampered["facts"]["candidate_count"] = 9
        for bad in (tampered, dict(e["available"], alert_format_version="phase12-v2"), {}):
            with self.assertRaises(AlertInputError):
                decide(bad, None, False)
        other_state = replay([e["available_other"]]).state.subjects[0]
        with self.assertRaises(AlertInputError) as caught:
            decide(e["available"], other_state, False)
        self.assertEqual(str(caught.exception), "subject state does not belong to the alert subject")
        broken = dict(replay([e["invalidated"]]).state.subjects[0].to_dict(), terminal_alert_id=None)
        with self.assertRaises(AlertInputError):
            validated_subject_state(broken)
        with self.assertRaises(AlertInputError):
            decide(e["available"], None, "no")


class ReplayTests(unittest.TestCase):
    def setUp(self):
        e = events()
        self.sequence = [e["available"], e["a_to_b"], e["available"], e["invalidated"], e["available_other"],
                         e["invalidated_later"], e["b_to_a"], e["a_to_b"]]

    def test_deterministic_and_restart_equivalent(self):
        full = replay(self.sequence)
        self.assertEqual(replay(self.sequence), full)
        for cut in range(len(self.sequence) + 1):
            first = replay(self.sequence[:cut])
            rest = replay(self.sequence[cut:], first.state)              # a restart after `cut` events
            self.assertEqual(rest.state, full.state)
            self.assertEqual(classes(first) + classes(rest), classes(full))

    def test_duplicates_do_not_alter_state(self):
        full = replay(self.sequence)
        self.assertEqual(replay(self.sequence + self.sequence).state, full.state)
        self.assertEqual(classes(replay(self.sequence + self.sequence))[len(self.sequence):],
                         [DUPLICATE if d.classification == NEW else d.classification for d in full.decisions])

    def test_dict_order(self):
        def rev(v):
            if isinstance(v, dict):
                return {k: rev(v[k]) for k in reversed(list(v))}
            return [rev(x) for x in v] if isinstance(v, list) else v
        self.assertEqual(replay([rev(x) for x in self.sequence]), replay(self.sequence))

    def test_fresh_processes(self):
        code = ("from tests.test_alert_state import events\nfrom alert_engine.state import replay\n"
                "from alert_engine.canonical import canonical_json, content_id\n"
                "e = events()\nseq = [e['available'], e['a_to_b'], e['available'], e['invalidated'], e['available_other'],"
                " e['invalidated_later'], e['b_to_a'], e['a_to_b']]\n"
                "print(content_id(replay(seq).to_dict()))")
        expected = content_id(replay(self.sequence).to_dict())
        for extra in ({"PYTHONHASHSEED": "0"}, {"PYTHONHASHSEED": "21", "TZ": "Asia/Tokyo"},
                      {"PYTHONHASHSEED": "5", "LANG": "C", "LC_ALL": "C", "HOSTNAME": "elsewhere", "MIAS_UNRELATED": "1"}):
            with tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=dict(os.environ, PYTHONPATH=ROOT, **extra),
                                        capture_output=True, text=True, timeout=300)
            self.assertEqual(result.stdout.strip(), expected, result.stderr[-300:])


class FakeRedis:
    """In-memory stand-in for the commands the adapter uses (exists, get, set with nx, eval of its two scripts).
    It records every write's options so tests can prove no TTL is ever set."""

    def __init__(self, fail=None):
        self.data, self.writes, self.fail, self.before_eval = {}, [], fail or set(), None

    def _check(self, op):
        if op in self.fail:
            raise redis.ConnectionError("simulated outage")

    def exists(self, key):
        self._check("read")
        return int(key in self.data)

    def get(self, key):
        self._check("read")
        return self.data.get(key)

    def set(self, key, value, nx=False, ex=None, px=None):
        self._check("write")
        self.writes.append((key, ex, px))
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def eval(self, script, numkeys, *args):
        self._check("write")
        if self.before_eval:
            hook, self.before_eval = self.before_eval, None
            hook(self)
        keys, argv = args[:numkeys], args[numkeys:]
        if script == ss.COMMIT:
            if keys[0] in self.data:
                return 0
            if self.data.get(keys[1], "") != argv[0]:
                return -1
            self.data[keys[0]], self.data[keys[1]] = argv[2], argv[1]
            self.writes.extend([(keys[0], None, None), (keys[1], None, None)])
            return 1
        if script == ss.RESTORE_SUBJECT:
            current = self.data.get(keys[0])
            if current is None:
                self.data[keys[0]] = argv[0]
                self.writes.append((keys[0], None, None))
                return 1
            return 0 if current == argv[0] else -1
        raise AssertionError("unexpected script")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.e = events()

    def test_record_matches_pure_replay(self):
        fake = FakeRedis()
        store = RedisAlertStateStore(fake)
        sequence = [self.e[k] for k in ("available", "a_to_b", "available", "invalidated", "invalidated_later",
                                        "b_to_a", "a_to_b", "available_other")]
        recorded = [store.record(x).classification for x in sequence]
        expected = replay(sequence)
        self.assertEqual(recorded, classes(expected))
        for state in expected.state.subjects:
            self.assertEqual(store.subject_state(state.subject_key), state.to_dict())
        self.assertTrue(all(ex is None and px is None for _, ex, px in fake.writes))  # no TTL, no cooldown

    def test_keys_are_deterministic_and_clean(self):
        store = RedisAlertStateStore(FakeRedis())
        alert = self.e["available"]
        self.assertEqual(store.seen_key(alert["alert_id"]), "mias:phase12:seen:" + alert["alert_id"].split(":")[1])
        self.assertEqual(store.subject_redis_key(subject_key(alert)), "mias:phase12:subject:" + subject_key(alert))
        self.assertEqual(store.subject_redis_key("symbol:META"), "mias:phase12:subject:symbol:META")
        for bad in ("", "x:", "a b", None):
            with self.assertRaises(ValueError):
                RedisAlertStateStore(FakeRedis(), namespace=bad)

    def test_non_new_writes_nothing(self):
        fake = FakeRedis()
        store = RedisAlertStateStore(fake)
        store.record(self.e["invalidated"])
        before = (dict(fake.data), len(fake.writes))
        self.assertEqual(store.record(self.e["invalidated"]).classification, DUPLICATE)
        self.assertEqual(store.record(self.e["invalidated_later"]).classification, TERMINAL_SUPPRESSED)
        self.assertEqual((dict(fake.data), len(fake.writes)), before)

    def test_redis_errors_fail_closed(self):
        for fail in ({"read"}, {"write"}):
            store = RedisAlertStateStore(FakeRedis(fail=fail))
            with self.subTest(fail=fail), self.assertRaises(AlertStateUnavailable):
                store.record(self.e["available"])
        fake = FakeRedis()
        fake.data["mias:phase12:subject:symbol:META"] = "{not json"
        with self.assertRaises(AlertStateUnavailable) as caught:
            RedisAlertStateStore(fake).record(self.e["a_to_b"])
        self.assertEqual(str(caught.exception), "stored alert subject state is malformed")

    def test_concurrent_change_is_redecided(self):
        fake = FakeRedis()
        store = RedisAlertStateStore(fake)
        other = RedisAlertStateStore(fake)
        # A concurrent writer invalidates the setup between this process's read and commit.
        fake.before_eval = lambda _: other.record(self.e["invalidated"])
        self.assertEqual(store.record(self.e["available"]).classification, TERMINAL_SUPPRESSED)
        self.assertNotIn(store.seen_key(self.e["available"]["alert_id"]), fake.data)

    def test_persistent_conflict_fails_closed(self):
        fake = FakeRedis()
        store = RedisAlertStateStore(fake)
        key = store.subject_redis_key(subject_key(self.e["a_to_b"]))

        value, attempts = canonical_json(replay([self.e["b_to_a"]]).state.subjects[0].to_dict()), []

        def keep_changing(client):                       # a different competing value before every commit
            attempts.append(1)
            client.data[key] = value + " " * len(attempts)
            client.before_eval = keep_changing
        fake.before_eval = keep_changing
        with self.assertRaises(AlertStateUnavailable) as caught:
            store.record(self.e["a_to_b"])
        self.assertEqual(str(caught.exception), "alert state changed concurrently; nothing was recorded")
        self.assertEqual(len(attempts), ss.MAX_COMMIT_ATTEMPTS)
        self.assertNotIn(store.seen_key(self.e["a_to_b"]["alert_id"]), fake.data)

    def test_restore_from_sealed_history(self):
        sequence = [self.e["available"], self.e["invalidated"], self.e["a_to_b"]]
        history = replay(sequence)
        fake = FakeRedis()
        store = RedisAlertStateStore(fake)
        self.assertEqual(store.restore(history.state), dict(seen=3, subjects=2))
        self.assertEqual(store.restore(history.state), dict(seen=0, subjects=0))          # idempotent
        self.assertEqual(store.record(self.e["invalidated_later"]).classification, TERMINAL_SUPPRESSED)
        self.assertEqual(store.record(self.e["available"]).classification, DUPLICATE)
        conflicting = FakeRedis()
        conflicting.data[store.subject_redis_key("symbol:META")] = canonical_json(
            replay([self.e["b_to_a"]]).state.subjects[0].to_dict())
        snapshot = dict(conflicting.data)
        with self.assertRaises(AlertStateUnavailable):
            RedisAlertStateStore(conflicting).restore(history.state)
        self.assertEqual(conflicting.data, snapshot)                                       # nothing written


@unittest.skipUnless(os.environ.get("MIAS_PHASE2J_REDIS_URL"), "disposable test Redis not configured")
class DisposableRedisTests(unittest.TestCase):
    """Against the disposable test Redis only, in a unique namespace; keys are deleted by exact name (no flush)."""

    def setUp(self):
        self.client = redis.Redis.from_url(os.environ["MIAS_PHASE2J_REDIS_URL"], decode_responses=True, socket_timeout=5)
        self.namespace = f"mias:phase12test:{uuid.uuid4().hex}"
        self.store = RedisAlertStateStore(self.client, namespace=self.namespace)
        self.e = events()

    def tearDown(self):
        keys = [self.store.seen_key(x["alert_id"]) for x in self.e.values()]
        keys += [self.store.subject_redis_key(subject_key(x)) for x in self.e.values()]
        self.client.delete(*keys)

    def test_record_and_restore(self):
        sequence = [self.e[k] for k in ("available", "invalidated", "invalidated_later", "a_to_b", "a_to_b", "b_to_a")]
        self.assertEqual([self.store.record(x).classification for x in sequence], classes(replay(sequence)))
        for key in (self.store.seen_key(self.e["available"]["alert_id"]),
                    self.store.subject_redis_key(subject_key(self.e["available"]))):
            self.assertEqual(self.client.ttl(key), -1)                                    # no expiry
        self.tearDown()                                                                    # simulate a lost cache
        self.store.restore(replay(sequence).state)
        self.assertEqual(self.store.record(self.e["invalidated_later"]).classification, TERMINAL_SUPPRESSED)

    def test_concurrent_writer(self):
        original_eval = self.client.eval
        competitor = RedisAlertStateStore(self.client, namespace=self.namespace)
        state = dict(armed=True)

        def racing_eval(*args, **kwargs):
            if state.pop("armed", False):
                competitor.record(self.e["invalidated"])
            return original_eval(*args, **kwargs)
        self.client.eval = racing_eval
        try:
            self.assertEqual(self.store.record(self.e["available"]).classification, TERMINAL_SUPPRESSED)
        finally:
            self.client.eval = original_eval

    def test_unreachable_redis_fails_closed(self):
        dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.5, socket_timeout=0.5)
        with self.assertRaises(AlertStateUnavailable):
            RedisAlertStateStore(dead, namespace=self.namespace).record(self.e["available"])


class BoundaryTests(unittest.TestCase):
    def imports(self, module):
        names = set()
        for node in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(node, ast.Import):
                names |= {a.name for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        return names

    def test_pure_state_imports_and_calls(self):
        self.assertEqual(self.imports(st), {"dataclasses", "alert_engine", "alert_engine.canonical", "alert_engine.validation"})
        for node in ast.walk(ast.parse(inspect.getsource(st))):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
                self.assertNotIn(name, {"open", "print", "now", "utcnow", "today", "getenv", "time", "sleep", "sorted_by_time"})
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, ("environ", "argv"))

    def test_store_imports(self):
        self.assertEqual(self.imports(ss), {"json", "redis", "alert_engine.canonical", "alert_engine.state",
                                            "alert_engine.validation"})

    def test_runtime_imports(self):
        code = ("import sys, alert_engine.state\n"
                "pure = sorted({n.split('.')[0] for n in sys.modules} & {'redis', 'requests', 'sqlalchemy', 'dotenv', 'shared',"
                " 'collector', 'orchestrator', 'openai', 'socket'})\n"
                "import alert_engine.state_store\n"
                "store = sorted({n.split('.')[0] for n in sys.modules} & {'requests', 'sqlalchemy', 'dotenv', 'shared',"
                " 'collector', 'orchestrator', 'openai', 'persistence'})\n"
                "legacy = sorted(set(sys.modules) & {'alert_engine.decision_engine', 'alert_engine.formatter',"
                " 'alert_engine.telegram_notifier'})\n"
                "print(pure, store, legacy)")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.stdout.strip(), "[] [] []", result.stderr[-300:])

    def test_no_cooldown_or_delivery_identifiers(self):
        banned = {"cooldown", "ttl", "expire", "delivery", "delivered", "telegram", "retry", "severity", "score", "rank"}
        for module in (st, ss):
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                name = getattr(node, "id", None) or getattr(node, "attr", None) or getattr(node, "name", None)
                if isinstance(name, str):
                    self.assertFalse(set(name.lower().split("_")) & banned, (module.__name__, name))


if __name__ == "__main__":
    unittest.main()
