"""Phase 2M real collector process for staging validation (test utility, never deployed).

One OS process = one collector process. Configuration comes only from the
explicit process environment supplied by ``tests.real_staging``; ``.env`` is
never read (``dotenv.load_dotenv`` is disabled while config is imported). The
process uses the real Redis client, the real shadow writer/lifecycle, the real
durable identity lookup and live official HTTP. Telegram and OpenAI entry
points are replaced by guards that raise and count: runs use
``send_alerts=False`` and ``enable_ai=False``, so the guards must stay at zero.

Commands arrive as JSON lines on stdin; one JSON result line is written per
command on stdout (logs go to stderr):

    {"op": "live"}                                  one live collection cycle
    {"op": "controlled", "docs": [...], "clock": ISO}  labelled fixture cycle (no network)
    {"op": "drain_wait"}                            wait until queued shadow work is written
    {"op": "exit"}                                  drain, reconcile, graceful exit
    {"op": "hard_exit"}                             drain, reconcile, os._exit (no atexit)

SEC (Phase 2P, ``--collector sec``): ``live`` fetches the configured watchlist;
``controlled``/``replay`` process the given ``filings`` (no network). The SEC
collector always delivers ``ALERT`` decisions, so its Telegram entry point is a
recording stub (would-be sends are reported, nothing is sent); the real Telegram
transport (``requests.post``) and OpenAI client construction stay tripwires.

News (Phase 2R, ``--collector news``): ``live`` reads the configured RSS feeds;
``controlled``/``replay`` feed the given ``feeds`` entries through the real
``read_feed`` (no network). ``analyze_market_event`` is a deterministic stub that
counts attempts and applies only enrichment supplied in the command (otherwise it
raises, exercising the collector's AI-failure path); no OpenAI call is possible.
Telegram is a recording stub as for SEC. A fixed ``clock`` pins recency scoring.
"""
import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime
import hashlib
import io
import json
import logging
import os
import sys
from unittest.mock import patch

with patch("dotenv.load_dotenv"):  # Never read .env; explicit environment only.
    import shared.config  # noqa: F401
    import requests
    import feedparser

LIVE_GEO_SOURCES = ("fr", "fr_inspection", "ftc", "moea")


class Guard:
    """Telegram/OpenAI tripwire: counts and raises; must never be reached."""

    def __init__(self, name):
        self.name, self.calls = name, 0

    def __call__(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError(f"{self.name} forbidden during staging")


class HttpEvidence:
    """Wrap requests.get without changing behavior; record status/type/size only."""

    def __init__(self):
        self.real, self.records = requests.get, []

    def __call__(self, url, *args, **kwargs):
        response = self.real(url, *args, **kwargs)
        return _Recorded(response, self.records, url)

    def summary(self):
        by_endpoint = {}
        for record in self.records:
            key = record["endpoint"]
            entry = by_endpoint.setdefault(key, dict(requests=0, statuses={}, content_types=set(), bytes=0))
            entry["requests"] += 1
            entry["statuses"][str(record["status"])] = entry["statuses"].get(str(record["status"]), 0) + 1
            entry["content_types"].add(record["content_type"])
            entry["bytes"] += record["bytes"]
        return {k: dict(v, content_types=sorted(v["content_types"])) for k, v in sorted(by_endpoint.items())}


class _Recorded:
    def __init__(self, response, records, url):
        from urllib.parse import urlsplit
        parts = urlsplit(url)
        self._response, self._size = response, 0
        self._record = dict(endpoint=f"{parts.hostname}{_bucket(parts.path)}", status=response.status_code,
                            content_type=response.headers.get("Content-Type", "").split(";")[0], bytes=0)
        records.append(self._record)

    def __getattr__(self, name):
        return getattr(self._response, name)

    def __enter__(self):
        self._response.__enter__()
        return self

    def __exit__(self, *args):
        return self._response.__exit__(*args)

    def iter_content(self, *args, **kwargs):
        for chunk in self._response.iter_content(*args, **kwargs):
            self._record["bytes"] += len(chunk)
            yield chunk

    @property
    def content(self):
        data = self._response.content
        self._record["bytes"] = len(data)
        return data


def _bucket(path):
    """Group per-document URLs into endpoint families (no document identifiers in reports)."""
    for prefix in ("/api/v1/documents/", "/documents/full_text/", "/public-inspection/", "/news-events/",
                   "/Mns/english/news/", "/feeds/"):
        if path.startswith(prefix):
            return prefix + "*"
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--collector", choices=("geo", "fed", "sec", "news"), required=True)
    args = parser.parse_args()
    http = HttpEvidence()
    telegram, openai = Guard("Telegram"), Guard("OpenAI")
    submissions = []
    with patch.object(requests, "get", http):
        if args.collector == "geo":
            run = GeoProcess(telegram, openai, submissions)
        elif args.collector == "sec":
            run = SecProcess(telegram, openai, submissions)
        elif args.collector == "news":
            run = NewsProcess(telegram, openai, submissions)
        else:
            run = FedProcess(telegram, openai, submissions)
        for line in sys.stdin:
            command = json.loads(line)
            if command["op"] in ("exit", "hard_exit"):
                result = run.finish()
                result.update(pid=os.getpid(), http=http.summary(), telegram_calls=telegram.calls,
                              openai_calls=openai.calls, submissions=len(submissions))
                print(json.dumps(result, sort_keys=True, default=str), flush=True)
                if command["op"] == "hard_exit":
                    sys.stderr.flush()
                    os._exit(0)  # Abrupt exit after committed writes: no atexit, no cleanup.
                return 0
            CURRENT["label"] = command.get("label") or command["op"]
            if command["op"] == "drain_wait":
                result = run.drain_wait()
            else:
                result = run.cycle(command)
            result.update(pid=os.getpid(), telegram_calls=telegram.calls, openai_calls=openai.calls)
            print(json.dumps(result, sort_keys=True, default=str), flush=True)
    return 0


CURRENT = {"label": None}


def _spy(module, name, submissions, family):
    real = getattr(module, name)
    def submit(event, **kwargs):
        submissions.append((family, deepcopy(event), kwargs.get("make_current", True), CURRENT["label"]))
        return real(event, **kwargs)
    return submit


def _reconcile(submissions, reconcile, snapshot_filter=lambda e: e):
    """Read-only reconciliation of this process's own submissions; never repairs."""
    from persistence.config import DatabaseSettings
    from persistence.database import make_engine, transaction
    from persistence.repository import EventRepository
    import sqlalchemy as sa
    try:
        engine = make_engine(DatabaseSettings.from_env())
        try:
            results = []
            with transaction(engine) as session:
                session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                repo = EventRepository(session)
                for _, event, make_current, label in submissions:
                    results.append((label, reconcile(snapshot_filter(event), repo, expect_current=make_current)))
        finally:
            engine.dispose()
    except Exception as error:
        return dict(status="database_unavailable", error_type=type(error).__name__)
    integrity = [(label, sorted(set(r["mismatches"]) - {"current_version_match"})) for label, r in results
                 if set(r["mismatches"]) - {"current_version_match"}]
    pointer = sorted({label or "unlabelled" for label, r in results if "current_version_match" in r["mismatches"]})
    return dict(status="ok", checked=len(results), integrity_mismatches=integrity,
                current_pointer_differs=len([1 for _, r in results if "current_version_match" in r["mismatches"]]),
                current_pointer_differs_labels=pointer,
                ai_expected=sum(r["ai_match"] is not None for _, r in results))


class GeoProcess:
    def __init__(self, telegram, openai, submissions):
        from collector import geopolitical_collector as geo
        from collector import geopolitical_sources as sources
        from persistence import geopolitical_shadow as shadow
        from persistence import geopolitical_durable_identity as durable
        # Fixture modules import test modules that briefly clear os.environ; import them now,
        # before any writer/lookup thread exists, never mid-run.
        import tests.geopolitical_readiness_corpus  # noqa: F401
        import tests.staging_rollout  # noqa: F401
        self.geo, self.sources, self.shadow, self.durable = geo, sources, shadow, durable
        self.submissions = submissions
        self.stack = [patch.object(geo, "deliver_geopolitical_alert", telegram),
                      patch.object(geo, "analyze_geopolitical_event", openai),
                      patch.object(shadow, "submit_geopolitical", _spy(shadow, "submit_geopolitical", submissions, "geo")),
                      # Bounded live staging subset: API/RSS sources only (see report).
                      patch.object(sources, "SOURCES", {k: sources.SOURCES[k] for k in LIVE_GEO_SOURCES})]
        for p in self.stack:
            p.start()
        self.real_fetch = sources.fetch_documents

    def cycle(self, command):
        before = len(self.submissions)
        per_source = {}
        if command["op"] == "live":
            def fetch(agency):
                documents = self.real_fetch(agency)
                per_source[agency] = dict(items=len(documents), source_errors=sum(1 for d in documents if d.get("source_error")))
                return documents
            with patch.object(self.sources, "fetch_documents", side_effect=fetch):
                events, stats = self.geo.collect_geopolitical_events(enable_ai=False, send_alerts=False)
        else:
            from tests.geopolitical_readiness_corpus import DOCS
            from tests.staging_rollout import FR_WITH_EO, TRADE_WITH_EO
            docs = {**DOCS, "FR_WITH_EO": FR_WITH_EO, "TRADE_WITH_EO": TRADE_WITH_EO}
            clock_value = datetime.fromisoformat(command["clock"])
            with patch.object(self.sources, "fetch_documents", side_effect=lambda _: deepcopy([docs[n] for n in command["docs"]])), \
                 patch.object(self.sources, "SOURCES", {"controlled_fixture": "synthetic"}), \
                 patch.object(self.geo, "datetime") as clock:
                clock.now.side_effect = lambda *a: clock_value
                clock.fromisoformat = datetime.fromisoformat
                events, stats = self.geo.collect_geopolitical_events(enable_ai=False, send_alerts=False)
        new = self.submissions[before:]
        return dict(op=command["op"], label=command.get("label"), stats=stats, per_source=per_source,
                    events=[e["event_id"] for e in events], submitted=len(new),
                    submitted_current=sum(1 for _, _, current, _ in new if current),
                    resolved=[e.get("event_id") for _, e, _, _ in new],
                    durable_counters=self._counters())

    def drain_wait(self):
        return _drain(self.shadow)

    def _counters(self):
        stats = self.durable.get_durable_identity_stats()
        return {k: stats[k] for k in self.durable.STATS_LOG_FIELDS}

    def finish(self):
        drained = self.shadow.shutdown(drain=True, timeout=20)
        counters = self._counters()
        self.durable._close()
        from persistence.reconciliation import reconcile_geopolitical_event
        return dict(op="finish", shadow_stopped=drained["stopped"], shadow_stats=_shadow_summary(drained["stats"]),
                    durable_counters=counters, reconciliation=_reconcile(self.submissions, reconcile_geopolitical_event))


class FedProcess:
    def __init__(self, telegram, openai, submissions):
        from collector import fed_collector as fed
        from persistence import fed_shadow as shadow
        import tests.fed_readiness_corpus  # noqa: F401  (see GeoProcess: import before threads start)
        self.fed, self.shadow, self.submissions = fed, shadow, submissions
        self.entries = {}
        real_parse = feedparser.parse
        def parse(data, *args, **kwargs):
            parsed = real_parse(data, *args, **kwargs)
            self.entries["last"] = len(parsed.get("entries", []))
            return parsed
        for p in (patch.object(fed, "deliver_fed_alert", telegram), patch.object(fed, "analyze_fed_event", openai),
                  patch.object(shadow, "submit_fed", _spy(shadow, "submit_fed", submissions, "fed")),
                  patch.object(fed.feedparser, "parse", side_effect=parse)):
            p.start()

    def cycle(self, command):
        before = len(self.submissions)
        if command["op"] == "live":
            events, stats = self.fed.collect_fed_events(enable_ai=False, send_alerts=False)
        else:
            # Labelled controlled fixture: synthetic official-style entries, no network, fixed clock.
            from tests.fed_readiness_corpus import ENTRIES
            clock_value = datetime.fromisoformat(command["clock"])
            entries = [_synthetic_fed(deepcopy(ENTRIES[n])) for n in command["docs"]]
            self.entries["last"] = len(entries)
            with patch.object(self.fed.requests, "get") as http, \
                 patch.object(self.fed.feedparser, "parse", return_value={"entries": entries, "bozo": False}), \
                 patch.object(self.fed, "datetime", wraps=datetime) as clock:
                http.return_value.status_code = 200
                clock.now.side_effect = lambda tz=None: clock_value
                events, stats = self.fed.collect_fed_events(enable_ai=False, send_alerts=False)
        new = self.submissions[before:]
        return dict(op=command["op"], label=command.get("label"), stats=stats, feed_entries=self.entries.get("last"),
                    processed_events=len(events), submitted=len(new),
                    stale_or_undated_submitted=sum(1 for _, _, current, _ in new if not current),
                    undated_submitted=sum(1 for _, e, _, _ in new if not e.get("published_at")),
                    fingerprints=sorted({e["fed_fingerprint"] for _, e, _, _ in new}))

    def drain_wait(self):
        return _drain(self.shadow)

    def finish(self):
        drained = self.shadow.shutdown(drain=True, timeout=20)
        from persistence.reconciliation import reconcile_fed_event
        return dict(op="finish", shadow_stopped=drained["stopped"], shadow_stats=_shadow_summary(drained["stats"]),
                    reconciliation=_reconcile(self.submissions, reconcile_fed_event))


class _Capture(logging.Handler):
    """Collector-visible log records (logger, level, message) for parity comparison."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append((record.name, record.levelname, record.getMessage()))


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class SecProcess:
    FILING_KEYS = ("symbol", "form", "accession_number", "filing_date", "primary_document")

    def __init__(self, telegram, openai, submissions):
        # alert_engine.telegram_notifier calls load_dotenv() at import: keep .env unread.
        with patch("dotenv.load_dotenv"):
            from collector import sec_collector as sec
        from persistence import sec_shadow as shadow
        self.sec, self.shadow, self.submissions = sec, shadow, submissions
        self.would_send, self.capture = [], _Capture()
        for name in ("sec_collector", "deduplicator"):
            logging.getLogger(name).addHandler(self.capture)
        def stub(message):
            self.would_send.append(message)
            return {"ok": True, "result": {"message_id": len(self.would_send)}}
        patches = [patch.object(sec, "send_telegram_alert", stub), patch.object(requests, "post", telegram),
                   patch.object(shadow, "submit_sec", _spy(shadow, "submit_sec", submissions, "sec"))]
        try:
            import openai as openai_module
            patches.append(patch.object(openai_module, "OpenAI", openai))
        except ImportError:
            pass
        for p in patches:
            p.start()

    def cycle(self, command):
        before, sent_before, logs_before = len(self.submissions), len(self.would_send), len(self.capture.records)
        fetched = self.sec.collect_sec_filings() if command["op"] == "live" else deepcopy(command["filings"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):  # The collector prints ALERT messages; keep the protocol channel clean.
            events = self.sec.process_sec_filings(deepcopy(fetched))
        new = self.submissions[before:]
        logs = self.capture.records[logs_before:]
        sent = self.would_send[sent_before:]
        from persistence.adapters.sec import filing_document
        return dict(op=command["op"], label=command.get("label"),
                    fetched=[{k: f.get(k) for k in self.FILING_KEYS} for f in fetched] if command["op"] == "live" else None,
                    input_count=len(fetched), processed=len(events),
                    events=[dict(symbol=e["symbols"][0], form=e["sec_form"], accession=e["accession_number"],
                                 primary_document=filing_document(e)[1], filing_date=e["published_at"],
                                 score=e["impact_score"], level=e["impact_level"], decision=e["alert_decision"],
                                 fingerprint=self.sec.deduplicator.create_fingerprint(e)) for e in events],
                    events_digest=_digest(events), stdout_digest=_digest(stdout.getvalue()),
                    stdout_lines=len(stdout.getvalue().splitlines()),
                    collector_logs=[r for r in logs if "shadow" not in r[2].lower()],
                    shadow_logs=[r for r in logs if "shadow" in r[2].lower()],
                    would_send=len(sent), would_send_digests=[_digest(m) for m in sent],
                    submitted=len(new), submitted_fingerprints=[e["sec_fingerprint"] for _, e, _, _ in new])

    def drain_wait(self):
        return _drain(self.shadow)

    def finish(self):
        drained = self.shadow.shutdown(drain=True, timeout=20)
        from persistence.reconciliation import reconcile_sec_event
        stats = drained["stats"]
        return dict(op="finish", shadow_stopped=drained["stopped"], shadow_stats=_shadow_summary(stats),
                    shadow_timestamps=dict(last_success_at=stats.get("last_success_at"),
                                           last_failure_at=stats.get("last_failure_at")),
                    would_send_total=len(self.would_send),
                    reconciliation=_reconcile(self.submissions, reconcile_sec_event))


class NewsProcess:
    ENTRY_KEYS = ("title", "link", "published", "summary")

    def __init__(self, telegram, openai, submissions):
        # alert_engine.telegram_notifier calls load_dotenv() at import: keep .env unread.
        with patch("dotenv.load_dotenv"):
            from collector import rss_reader as news
            from shared.sources import RSS_SOURCES
        from analyzer import scoring_engine
        from analyzer.headline_similarity import normalize_headline
        from persistence import news_shadow as shadow
        self.news, self.scoring, self.shadow, self.sources = news, scoring_engine, shadow, RSS_SOURCES
        self.normalize_headline, self.submissions = normalize_headline, submissions
        self.would_send, self.ai_attempts, self.ai_results, self.capture = [], [], {}, _Capture()
        self.controlled, self.feed_evidence = None, {}
        for name in ("collector", "deduplicator"):
            logging.getLogger(name).addHandler(self.capture)
        real_parse = feedparser.parse
        def parse(target, *args, **kwargs):
            if self.controlled is not None:
                return self.controlled
            parsed = real_parse(target, *args, **kwargs)
            self.feed_evidence[target] = dict(
                http_status=parsed.get("status"), bozo=bool(parsed.get("bozo")), items=len(parsed.get("entries", [])),
                content_type=(parsed.get("headers") or {}).get("content-type", "").split(";")[0],
                entries=[self._plain(e) for e in parsed.get("entries", [])])
            return parsed
        def ai(event):
            self.ai_attempts.append(event["url"])
            result = self.ai_results.get(event["url"])
            if result is None:
                raise RuntimeError("deterministic AI stub: no enrichment configured")
            for key, value in result.items():
                event[f"ai_{key}"] = value
            return event
        def stub(message):
            self.would_send.append(message)
            return {"ok": True, "result": {"message_id": len(self.would_send)}}
        patches = [patch.object(news, "analyze_market_event", ai), patch.object(news, "send_telegram_alert", stub),
                   patch.object(requests, "post", telegram), patch.object(news.feedparser, "parse", parse),
                   patch.object(shadow, "submit_news", _spy(shadow, "submit_news", submissions, "news"))]
        try:
            import openai as openai_module
            patches.append(patch.object(openai_module, "OpenAI", openai))
        except ImportError:
            pass
        for p in patches:
            p.start()

    def _plain(self, entry):
        plain = {k: entry.get(k) for k in self.ENTRY_KEYS if entry.get(k) is not None}
        if isinstance(entry.get("source"), dict) and entry["source"].get("title"):
            plain["source"] = {"title": entry["source"]["title"]}
        return plain

    def _read(self, url, label, clock):
        if clock is None:
            return self.news.read_feed(url, source_label=label)
        now = datetime.fromisoformat(clock)
        with patch.object(self.scoring, "datetime", wraps=datetime) as fixed:
            fixed.now.side_effect = lambda tz=None: now
            return self.news.read_feed(url, source_label=label)

    def cycle(self, command):
        from types import SimpleNamespace
        before, sent_before, ai_before, logs_before = (len(self.submissions), len(self.would_send),
                                                       len(self.ai_attempts), len(self.capture.records))
        self.ai_results = command.get("ai") or {}
        feeds, fetched, stdout = [], {}, io.StringIO()
        if command["op"] == "live":
            plan = [(source["url"], source["name"], None) for source in self.sources]
        else:
            plan = [(f"controlled://{f['label']}", f["label"], f["entries"]) for f in command["feeds"]]
        with redirect_stdout(stdout):  # The collector prints ALERT messages; keep the protocol channel clean.
            for url, label, entries in plan:
                self.controlled = None if entries is None else SimpleNamespace(bozo=False, feed={}, entries=deepcopy(entries))
                feed_before, feed_ai_before = len(self.submissions), len(self.ai_attempts)
                events, stats = self._read(url, label, command.get("clock"))
                evidence = self.feed_evidence.pop(url, None)
                if evidence is not None:
                    fetched[label] = evidence.pop("entries")[:self.news.RSS_ENTRY_LIMIT]
                outcomes = [e["news_collector_outcome"] for _, e, _, _ in self.submissions[feed_before:]]
                feeds.append(dict(label=label, http=evidence, stats=stats, returned=len(events),
                                  alert_candidates=len(self.ai_attempts) - feed_ai_before,  # AI runs for ALERT only.
                                  near_duplicate_suppressed=outcomes.count("near_duplicate_suppressed"),
                                  submissions=len(outcomes), events=[self._event(e) for e in events if e.get("relevant")]))
        self.controlled = None
        new = self.submissions[before:]
        logs, sent, attempts = self.capture.records[logs_before:], self.would_send[sent_before:], self.ai_attempts[ai_before:]
        return dict(op=command["op"], label=command.get("label"), feeds=feeds, fetched=fetched or None,
                    processed=sum(f["stats"]["processed"] for f in feeds),
                    events_digest=_digest([f["events"] for f in feeds]), stdout_digest=_digest(stdout.getvalue()),
                    stdout_lines=len(stdout.getvalue().splitlines()),
                    collector_logs=[r for r in logs if "shadow" not in r[2].lower()],
                    shadow_logs=[r for r in logs if "shadow" in r[2].lower()],
                    ai_attempts=len(attempts), ai_attempts_digest=_digest(attempts),
                    would_send=len(sent), would_send_digests=[_digest(m) for m in sent],
                    submitted=len(new), submitted_outcomes=[e["news_collector_outcome"] for _, e, _, _ in new],
                    submitted_keys=[self._key(e) for _, e, _, _ in new])

    def _event(self, event):
        return dict(url_digest=_digest(event.get("url")), publisher=event.get("publisher"), symbols=event.get("symbols"),
                    score=event.get("impact_score"), level=event.get("impact_level"), decision=event.get("alert_decision"),
                    original_score=event.get("original_impact_score"), adjustment=event.get("quality_adjustment"),
                    ai_event_type=event.get("ai_event_type"), score_reasons=event.get("score_reasons"),
                    **self._key(event))

    def _key(self, event):
        from persistence.adapters.news import article_identity
        fingerprint = self.news.deduplicator.create_fingerprint(event)
        normalized = self.normalize_headline((event.get("headline") or "").strip())
        identity = article_identity(dict(event, news_fingerprint=fingerprint))
        return dict(fingerprint=fingerprint, headline_key=hashlib.sha256(normalized.encode()).hexdigest(),
                    identity=identity[0], identity_key=identity[1])

    def drain_wait(self):
        return _drain(self.shadow)

    def finish(self):
        drained = self.shadow.shutdown(drain=True, timeout=20)
        from persistence.reconciliation import reconcile_news_event
        stats = drained["stats"]
        return dict(op="finish", shadow_stopped=drained["stopped"], shadow_stats=_shadow_summary(stats),
                    shadow_timestamps=dict(last_success_at=stats.get("last_success_at"),
                                           last_failure_at=stats.get("last_failure_at")),
                    would_send_total=len(self.would_send), ai_attempts_total=len(self.ai_attempts),
                    reconciliation=_reconcile(self.submissions, reconcile_news_event))


def _synthetic_fed(entry):
    """Fixture links must never share an identity with real Fed releases (they did: monetary20260916a)."""
    slug = entry["link"].rsplit("/", 1)[-1]
    entry["link"] = "https://www.federalreserve.gov/newsevents/pressreleases/mias-staging-fixture-" + slug
    return entry


def _drain(shadow, timeout=15):
    from time import monotonic, sleep
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        stats = shadow.get_persistence_stats()
        if stats["queue_depth"] == 0 and stats["in_flight"] == 0:
            return dict(op="drain_wait", drained=True, shadow_stats=_shadow_summary(stats))
        sleep(0.05)
    return dict(op="drain_wait", drained=False, shadow_stats=_shadow_summary(shadow.get_persistence_stats()))


def _shadow_summary(stats):
    keys = ("queued", "persisted", "duplicate", "failed", "dropped_queue_full", "dropped_shutdown", "queue_depth",
            "in_flight", "worker_started", "worker_stopped", "promotion_held", "promotion_ambiguous",
            "failed_initializing", "rejected_shutdown")
    summary = {k: stats.get(k, 0) for k in keys}
    summary.update(last_success=bool(stats.get("last_success_at")), last_failure=bool(stats.get("last_failure_at")))
    return summary


if __name__ == "__main__":
    sys.exit(main())
