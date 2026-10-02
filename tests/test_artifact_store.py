"""Phase 13C: the immutable artifact store (publish, validation, layout safety), its operator CLI, and the validated
in-memory index (latest, ties, history, cursors, snapshot swaps). Local temporary directories only: no network,
Redis, PostgreSQL or provider."""
import ast
import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import artifact_store
from artifact_store import files, index as ix, kinds, layout, runner, service, store
from artifact_store.errors import (AmbiguousLatest, ArtifactInvalid, InvalidInput, InvalidQuery, NotFound,
                                   StoreConflict, StoreUnavailable)
from artifact_store.index import build_index
from artifact_store.service import ArtifactStore
from alert_engine.builder import market_pattern_changed, setup_available, setup_invalidated
from tests import setup_evaluation_cases as sc
from tests import trade_setup_cases as ts

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def samples():
    """Sealed objects (plain dicts) for every kind, with distinct sealed as_of values where history needs them."""
    other_setup, _ = ts.screened([ts.record(705)])
    return {
        "market-intelligence": [ts.market_intelligence("all_bullish"), ts.later_mi("all_bullish", 86400),
                                ts.later_mi("higher_aligned_5m_opposed", 2 * 86400)],
        "options-intelligence": [ts.options_intelligence()],
        "trade-setup": [sc.setup(), other_setup.to_dict()],
        "invalidation-check": [sc.check("all_bearish", 86400), sc.check("higher_aligned_5m_opposed", 2 * 86400)],
        "alert": [setup_available(sc.setup()).to_dict(), setup_invalidated(sc.check()).to_dict(),
                  market_pattern_changed(ts.market_intelligence("all_bullish"),
                                         ts.later_mi("higher_aligned_5m_opposed")).to_dict()],
    }


ID_FIELD = {k: v.id_field for k, v in kinds.KINDS.items()}


def raw(obj, pretty=False):
    return (json.dumps(obj, indent=2) if pretty else kinds.KINDS["alert"].canonical_json(obj) + "\n").encode()


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "store")
        os.mkdir(self.root)
        self.objects = samples()

    def tearDown(self):
        self.tmp.cleanup()

    def publish_all(self):
        for kind, objects in self.objects.items():
            for obj in objects:
                store.publish(self.root, kind, raw(obj))

    def path(self, kind, obj):
        return os.path.join(self.root, kind, obj[ID_FIELD[kind]][7:] + ".json")


class PublishTests(StoreCase):
    def test_every_kind_publishes_canonical_bytes(self):
        for kind, objects in self.objects.items():
            for obj in objects:
                with self.subTest(kind=kind):
                    result, parsed = store.publish(self.root, kind, raw(obj, pretty=True))
                    self.assertEqual((result, parsed.artifact_id), ("published", obj[ID_FIELD[kind]]))
                    with open(self.path(kind, obj), "rb") as handle:
                        stored = handle.read()
                    self.assertEqual(stored, (kinds.KINDS[kind].canonical_json(obj) + "\n").encode())
                    self.assertEqual(json.loads(stored), obj)         # the canonical object itself, never wrapped
                    self.assertEqual(os.stat(self.path(kind, obj)).st_mode & 0o777, 0o444)
        self.assertEqual(sorted(os.listdir(self.root)), sorted(kinds.KIND_NAMES))

    def test_identical_republish_is_a_no_op(self):
        obj = self.objects["alert"][0]
        store.publish(self.root, "alert", raw(obj))
        before = os.stat(self.path("alert", obj))
        result, _ = store.publish(self.root, "alert", raw(obj, pretty=True))
        self.assertEqual(result, "already_present")
        after = os.stat(self.path("alert", obj))
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_different_body_under_same_id_fails_closed(self):
        obj = self.objects["alert"][0]
        os.mkdir(os.path.join(self.root, "alert"))
        with open(self.path("alert", obj), "wb") as handle:
            handle.write(b"occupied")
        with self.assertRaises(StoreConflict):
            store.publish(self.root, "alert", raw(obj))
        with open(self.path("alert", obj), "rb") as handle:
            self.assertEqual(handle.read(), b"occupied")              # never overwritten

    def test_invalid_inputs(self):
        alert, setup = self.objects["alert"][0], self.objects["trade-setup"][0]
        tampered = dict(alert, as_of="2026-09-30T14:46:00+00:00")
        wrong_id = dict(alert, alert_id="sha256:" + "0" * 64)
        cases = [("alert", raw(setup), ArtifactInvalid),             # wrong kind
                 ("alert", raw(tampered), ArtifactInvalid),          # id no longer matches the body
                 ("alert", raw(wrong_id), ArtifactInvalid),
                 ("alert", b"{not json", InvalidInput),
                 ("alert", b'{"a": 1, "a": 2}', InvalidInput),
                 ("alert", b"\xff\xfe", InvalidInput),
                 ("alert", b"[]", ArtifactInvalid),
                 ("setup-evaluation", raw(alert), InvalidInput),     # not a supported kind
                 ("../alert", raw(alert), InvalidInput)]
        for kind, body, error in cases:
            with self.subTest(kind=kind, body=body[:20]):
                with self.assertRaises(error) as caught:
                    store.publish(self.root, kind, body)
                self.assertNotIn(self.root, str(caught.exception))
        self.assertEqual(os.listdir(self.root), [])

    def test_unsafe_store_layouts(self):
        alert = self.objects["alert"][0]
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.mkdir(elsewhere)
        os.symlink(elsewhere, os.path.join(self.root, "alert"))       # symlinked kind directory
        with self.assertRaises(StoreUnavailable):
            store.publish(self.root, "alert", raw(alert))
        self.assertEqual(os.listdir(elsewhere), [])
        link_root = os.path.join(self.tmp.name, "link-root")
        os.symlink(self.root, link_root)                               # symlinked root
        with self.assertRaises(StoreUnavailable):
            store.publish(link_root, "trade-setup", raw(self.objects["trade-setup"][0]))
        for bad_root in ("relative/root", os.path.join(self.tmp.name, "missing")):
            with self.assertRaises(StoreUnavailable):
                store.publish(bad_root, "trade-setup", raw(self.objects["trade-setup"][0]))
        os.mkdir(os.path.join(self.root, "trade-setup"))
        target = os.path.join(self.tmp.name, "target.json")
        open(target, "w").close()
        setup = self.objects["trade-setup"][0]
        os.symlink(target, self.path("trade-setup", setup))           # symlink occupying the artifact name
        with self.assertRaises(StoreConflict):
            store.publish(self.root, "trade-setup", raw(setup))
        self.assertEqual(os.path.getsize(target), 0)

    def test_atomic_publish_leaves_nothing_partial(self):
        alert = self.objects["alert"][0]
        with mock.patch.object(store.os, "link", side_effect=OSError("disk full")):
            with self.assertRaises(StoreUnavailable):
                store.publish(self.root, "alert", raw(alert))
        self.assertEqual(os.listdir(os.path.join(self.root, "alert")), [])   # no final name, no temporary left
        with mock.patch.object(store.os, "fsync", side_effect=OSError("io error")):
            with self.assertRaises(StoreUnavailable):
                store.publish(self.root, "alert", raw(alert))
        self.assertEqual(os.listdir(os.path.join(self.root, "alert")), [])

    def test_concurrent_publish_race(self):
        alert = self.objects["alert"][0]
        real_link = os.link

        def racing_link(src, dst, **kwargs):                           # another publisher lands first
            real_link(src, dst, **kwargs)
            raise FileExistsError
        with mock.patch.object(store.os, "link", side_effect=racing_link):
            result, _ = store.publish(self.root, "alert", raw(alert))
        self.assertEqual(result, "already_present")
        self.assertEqual(os.listdir(os.path.join(self.root, "alert")), [alert["alert_id"][7:] + ".json"])


class LayoutTests(unittest.TestCase):
    def test_ids_and_names(self):
        good = "sha256:" + "a" * 64
        self.assertEqual(layout.file_name(good), "a" * 64 + ".json")
        for bad in ("sha256:" + "A" * 64, "sha256:" + "a" * 63, "sha512:" + "a" * 64, "a" * 64, "sha256:../" + "a" * 61,
                    "sha256:" + "g" * 64, "", None, "sha256:" + "a" * 64 + "\n"):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidQuery):
                    layout.file_name(bad)
        with self.assertRaises(InvalidQuery):
            layout.kind_dir("../etc")


class RunnerTests(StoreCase):
    def run_cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        code = runner.main(list(argv), out=out, err=err, environ={"MIAS_ARTIFACT_ROOT": self.root} if environ is None
                           else environ)
        text = (out.getvalue() or err.getvalue()).strip()
        return code, json.loads(text) if text.startswith("{") else text

    def source(self, obj, name="src.json", pretty=True):
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(raw(obj, pretty=pretty))
        return path

    def test_publish_summary_and_codes(self):
        alert = self.objects["alert"][0]
        src = self.source(alert)
        with open(src, "rb") as handle:
            before = handle.read()
        code, summary = self.run_cli("publish", "--kind", "alert", "--file", src)
        self.assertEqual((code, summary["result"], summary["artifact_id"], summary["kind"], summary["symbol"],
                          summary["canonicalized"], summary["store_format_version"]),
                         (0, "PUBLISHED", alert["alert_id"], "alert", "META", True, "artifact-store-v1"))
        self.assertEqual(self.run_cli("publish", "--kind", "alert", "--file", src)[1]["result"], "ALREADY_PRESENT")
        canonical_src = self.source(alert, "canonical.json", pretty=False)
        self.assertFalse(self.run_cli("publish", "--kind", "alert", "--file", canonical_src)[1]["canonicalized"])
        with open(src, "rb") as handle:
            self.assertEqual(handle.read(), before)                    # the source is never modified
        self.assertNotIn(self.root, json.dumps(summary))
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w") as handle:
            handle.write('{"x": 1, "x": 1}')
        self.assertEqual(self.run_cli("publish", "--kind", "alert", "--file", bad)[0], 2)
        self.assertEqual(self.run_cli("publish", "--kind", "alert", "--file", os.path.join(self.tmp.name, "nope"))[0], 2)
        self.assertEqual(self.run_cli("publish", "--kind", "setup-evaluation", "--file", src)[0], 2)
        self.assertEqual(self.run_cli("publish", "--kind", "trade-setup", "--file", src)[0], 4)
        self.assertEqual(self.run_cli("publish", "--kind", "alert", "--file", src, environ={})[0], 3)
        link = os.path.join(self.tmp.name, "link.json")
        os.symlink(src, link)
        self.assertEqual(self.run_cli("publish", "--kind", "alert", "--file", link)[0], 2)   # source symlink refused
        os.chmod(self.path("alert", alert), 0o644)
        with open(self.path("alert", alert), "ab") as handle:
            handle.write(b" ")
        code, failure = self.run_cli("publish", "--kind", "alert", "--file", src)
        self.assertEqual((code, failure["result"]), (3, "FAILED"))
        self.assertNotIn(self.root, failure["error"])
        self.assertEqual(self.run_cli("verify")[0], 4)

    def test_verify(self):
        self.publish_all()
        code, summary = self.run_cli("verify")
        self.assertEqual((code, summary["counts"]), (0, {k: len(v) for k, v in self.objects.items()}))
        self.assertEqual(self.run_cli("bogus")[0], 2)
        self.assertEqual(self.run_cli("publish", "--kind", "alert")[0], 2)

    def test_no_destructive_commands(self):
        source = inspect.getsource(runner)
        for word in ("unlink", "remove", "rmtree", "delete", "purge", "replace("):
            self.assertNotIn(word, source.split('"""', 2)[2])


class IndexTests(StoreCase):
    def test_empty_and_full(self):
        self.assertEqual(build_index(self.root).counts(), {k: 0 for k in kinds.KIND_NAMES})
        self.publish_all()
        index = build_index(self.root)
        self.assertEqual(index.counts(), {k: len(v) for k, v in self.objects.items()})
        for kind, objects in self.objects.items():
            for obj in objects:
                entry = index.get(kind, obj[ID_FIELD[kind]])
                self.assertEqual((entry.kind, entry.symbol), (kind, "META"))
        with self.assertRaises(NotFound):
            index.get("alert", "sha256:" + "0" * 64)
        with self.assertRaises(NotFound):                               # ids are per kind
            index.get("alert", self.objects["trade-setup"][0]["assessment_id"])

    def test_listing_order_independent(self):
        self.publish_all()
        real = os.listdir
        forward = build_index(self.root)
        with mock.patch.object(ix.os, "listdir", side_effect=lambda p: list(reversed(real(p)))):
            backward = build_index(self.root)
        for kind in kinds.KIND_NAMES:
            self.assertEqual(forward.history(kind, limit=200), backward.history(kind, limit=200))

    def test_any_bad_entry_fails_the_whole_build(self):
        self.publish_all()
        alert = self.objects["alert"][0]
        alert_dir = os.path.join(self.root, "alert")
        cases = {
            "unexpected file": ("notes.txt", b"x"),
            "uppercase name": (alert["alert_id"][7:].upper() + ".json", raw(alert)),
            "name/id mismatch": ("0" * 64 + ".json", raw(alert)),
            "non-canonical bytes": (None, raw(alert, pretty=True)),
            "invalid object": ("1" * 64 + ".json", raw({"alert_id": "x"})),
        }
        for label, (name, body) in cases.items():
            with self.subTest(label=label):
                target = os.path.join(alert_dir, name or alert["alert_id"][7:] + ".json")
                original = None
                if os.path.exists(target):
                    with open(target, "rb") as handle:
                        original = handle.read()
                    os.chmod(target, 0o644)
                with open(target, "wb") as handle:
                    handle.write(body)
                with self.assertRaises(ArtifactInvalid):
                    build_index(self.root)
                if original is None:
                    os.unlink(target)
                else:
                    with open(target, "wb") as handle:
                        handle.write(original)
        build_index(self.root)                                           # restored: valid again
        os.mkdir(os.path.join(alert_dir, "f" * 64 + ".json"))          # a directory under an artifact name
        with self.assertRaises(ArtifactInvalid):
            build_index(self.root)
        os.rmdir(os.path.join(alert_dir, "f" * 64 + ".json"))
        outside = os.path.join(self.tmp.name, "outside.json")
        with open(outside, "wb") as handle:
            handle.write(raw(alert))
        os.symlink(outside, os.path.join(alert_dir, "e" * 64 + ".json"))   # symlinked artifact file
        with self.assertRaises(ArtifactInvalid):
            build_index(self.root)

    def test_temp_files_ignored_and_root_extras_not_scanned(self):
        self.publish_all()
        with open(os.path.join(self.root, "alert", ".artifact-abc.tmp"), "w") as handle:
            handle.write("partial")
        os.mkdir(os.path.join(self.root, "lost+found"))
        self.assertEqual(build_index(self.root).counts()["alert"], 3)

    def test_duplicate_id_across_kinds(self):
        entry = ix.Entry("alert", "sha256:" + "a" * 64, "META", "2026-01-01T00:00:00+00:00",
                         ix.utc("2026-01-01T00:00:00+00:00"), "d", 1)
        with self.assertRaises(ArtifactInvalid):
            ix.Index([entry, ix.Entry(**dict(entry.__dict__, kind="trade-setup"))])

    def test_latest_cutoff_and_ties(self):
        self.publish_all()
        index = build_index(self.root)
        mis = self.objects["market-intelligence"]
        self.assertEqual(index.latest("market-intelligence", "META").artifact_id, mis[2]["intelligence_id"])
        cutoff = ts.later_mi("all_bullish", 86400)["synthesis_ref"]["as_of"]
        self.assertEqual(index.latest("market-intelligence", "META", cutoff).artifact_id, mis[1]["intelligence_id"])
        with self.assertRaises(NotFound):
            index.latest("market-intelligence", "META", "2000-01-01T00:00:00Z")
        with self.assertRaises(NotFound):
            index.latest("market-intelligence", "NVDA")
        # Two distinct MIs with the same sealed as_of (the Phase 8 goldens share one): latest is ambiguous.
        tie = ts.market_intelligence("positive_market_return")
        self.assertEqual(tie["synthesis_ref"]["as_of"], mis[0]["synthesis_ref"]["as_of"])
        store.publish(self.root, "market-intelligence", raw(tie))
        index = build_index(self.root)
        with self.assertRaises(AmbiguousLatest):
            index.latest("market-intelligence", "META", mis[0]["synthesis_ref"]["as_of"])
        self.assertEqual(index.latest("market-intelligence", "META").artifact_id, mis[2]["intelligence_id"])
        for bad in ("2026-09-24T20:05:00", "2026-09-24", "yesterday", "2026-09-24T20:05:00+0000"):
            with self.assertRaises(InvalidQuery):
                index.latest("market-intelligence", "META", bad)
        with self.assertRaises(InvalidQuery):
            index.latest("market-intelligence", "meta")                 # no silent case folding

    def test_equal_instants_with_different_offsets_tie(self):
        a = ix.Entry("alert", "sha256:" + "a" * 64, "META", "2026-01-01T05:00:00+05:00",
                     ix.utc("2026-01-01T05:00:00+05:00"), "d", 1)
        b = ix.Entry("alert", "sha256:" + "b" * 64, "META", "2026-01-01T00:00:00+00:00",
                     ix.utc("2026-01-01T00:00:00+00:00"), "d", 1)
        with self.assertRaises(AmbiguousLatest):
            ix.Index([a, b]).latest("alert", "META")

    def test_history_order_window_and_cursor(self):
        entries = [ix.Entry("alert", f"sha256:{c * 64}", "META", f"2026-01-0{d}T00:00:00+00:00",
                            ix.utc(f"2026-01-0{d}T00:00:00+00:00"), "d", 1)
                   for c, d in (("a", 1), ("b", 2), ("c", 2), ("d", 3), ("e", 4))]
        index = ix.Index(entries)
        page, cursor = index.history("alert", limit=2)
        self.assertEqual([e.artifact_id[7] for e in page], ["e", "d"])
        page2, cursor2 = index.history("alert", limit=2, cursor=cursor)
        self.assertEqual([e.artifact_id[7] for e in page2], ["b", "c"])     # same instant: id ascending
        page3, cursor3 = index.history("alert", limit=2, cursor=cursor2)
        self.assertEqual(([e.artifact_id[7] for e in page3], cursor3), (["a"], None))
        window, _ = index.history("alert", as_of_from="2026-01-02T00:00:00Z", as_of_to="2026-01-03T00:00:00Z")
        self.assertEqual([e.artifact_id[7] for e in window], ["d", "b", "c"])
        self.assertEqual(len(index.history("alert")[0]), 5)
        with self.assertRaises(InvalidQuery):
            index.history("alert", as_of_from="2026-01-03T00:00:00Z", as_of_to="2026-01-02T00:00:00Z")
        for limit in (0, 201, -1, True, "5"):
            with self.assertRaises(InvalidQuery):
                index.history("alert", limit=limit)
        with self.assertRaises(InvalidQuery):                             # a cursor belongs to its query
            index.history("alert", symbol="META", limit=2, cursor=cursor)
        for bad in ("", "!!!", "a" * 513, "eyJ2IjoxfQ", cursor[:-3], "e30"):
            with self.subTest(bad=bad[:10]):
                with self.assertRaises(InvalidQuery):
                    index.history("alert", limit=2, cursor=bad)

    def test_bounded_default(self):
        entries = [ix.Entry("alert", f"sha256:{i:064x}", "META", "2026-01-01T00:00:00+00:00",
                            ix.utc("2026-01-01T00:00:00+00:00"), "d", 1) for i in range(450)]
        index = ix.Index(entries)
        page, cursor = index.history("alert")
        self.assertEqual((len(page), cursor is not None), (50, True))
        seen, cursor = [], None
        while True:
            page, cursor = index.history("alert", limit=200, cursor=cursor)
            seen += [e.artifact_id for e in page]
            if cursor is None:
                break
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), 450)


class ServiceTests(StoreCase):
    def test_snapshot_swap_and_failed_refresh(self):
        s = ArtifactStore(self.root)
        with self.assertRaises(StoreUnavailable):
            s.snapshot()
        self.assertFalse(s.healthy)
        self.publish_all()
        self.assertTrue(s.refresh())
        first = s.snapshot()
        with open(os.path.join(self.root, "alert", "notes.txt"), "w") as handle:
            handle.write("x")
        self.assertFalse(s.refresh())
        self.assertIs(s.snapshot(), first)                               # the previous snapshot still serves
        self.assertFalse(s.healthy)
        os.unlink(os.path.join(self.root, "alert", "notes.txt"))
        self.assertTrue(s.refresh())
        self.assertIsNot(s.snapshot(), first)
        self.assertTrue(s.healthy)

    def test_readers_never_see_a_partial_index(self):
        self.publish_all()
        s = ArtifactStore(self.root)
        s.refresh()
        expected = s.snapshot().counts()
        stop, problems = threading.Event(), []

        def reader():
            while not stop.is_set():
                if s.snapshot().counts() != expected:
                    problems.append("partial")
        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for _ in range(5):
            s.refresh()
        stop.set()
        for t in threads:
            t.join()
        self.assertEqual(problems, [])

    def test_read_bytes_detects_changes(self):
        self.publish_all()
        s = ArtifactStore(self.root)
        s.refresh()
        alert = self.objects["alert"][0]
        entry = s.snapshot().get("alert", alert["alert_id"])
        with open(self.path("alert", alert), "rb") as handle:
            self.assertEqual(s.read_bytes(entry), handle.read())
        os.chmod(self.path("alert", alert), 0o644)
        with open(self.path("alert", alert), "r+b") as handle:
            handle.write(b"[")
        with self.assertRaises(ArtifactInvalid):
            s.read_bytes(entry)
        os.unlink(self.path("alert", alert))
        with self.assertRaises(ArtifactInvalid):
            s.read_bytes(entry)
        os.rename(os.path.join(self.root, "alert"), os.path.join(self.root, "alert-moved"))
        os.symlink(os.path.join(self.root, "alert-moved"), os.path.join(self.root, "alert"))
        with self.assertRaises(ArtifactInvalid):
            s.read_bytes(s.snapshot().get("alert", self.objects["alert"][1]["alert_id"]))


class BoundaryTests(unittest.TestCase):
    MODULES = (artifact_store, files, ix, kinds, layout, runner, service, store)

    def test_framework_free_and_side_effect_free(self):
        for module in self.MODULES:
            for node in ast.walk(ast.parse(inspect.getsource(module))):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
                for name in names:
                    self.assertNotIn(name.split(".")[0], {"fastapi", "starlette", "pydantic", "uvicorn", "httpx",
                                                          "requests", "redis", "sqlalchemy", "dotenv", "shared",
                                                          "openai", "subprocess", "socket"}, (module.__name__, name))
        code = ("import sys, artifact_store.runner, artifact_store.service\n"
                "bad = {'dotenv', 'shared', 'collector', 'analyzer', 'openai', 'requests', 'redis', 'sqlalchemy',"
                " 'persistence', 'orchestrator', 'options_data', 'setup_evaluation', 'evidence', 'fastapi', 'pydantic',"
                " 'market_context', 'technical', 'pandas', 'numpy'}\n"
                "print(sorted({n.split('.')[0] for n in sys.modules} & bad))")
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120,
                                env={"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(result.stdout.strip(), "[]", result.stderr[-300:])

    def test_closed_kinds(self):
        self.assertEqual(kinds.KIND_NAMES, ("market-intelligence", "options-intelligence", "trade-setup",
                                            "invalidation-check", "alert"))
        self.assertEqual(kinds.STORE_FORMAT_VERSION, "artifact-store-v1")


if __name__ == "__main__":
    unittest.main()
