"""Phase 2R real-process News/RSS operational validation (test utility; never a deployment tool).

Drives ``tests.real_staging_worker --collector news`` processes (separate OS
processes, real restarts), the separate-process ``persistence.news_audit`` CLI and
disposable, labelled ``phase2r`` PostgreSQL/Redis containers, then writes a
credential-free, counts-only report (no live headlines, URLs or article text).
Reuses the Phase 2M/2P harness helpers; their defaults are unchanged.

Safety: refuses unless every container carries the ``phase2r`` label with
loopback-only bindings and private storage, both Redis instances are empty, and
the staging database is a new loopback ``mias_test_phase2r*`` database. Never
touches ``mias-redis``, never reads ``.env``, never sends Telegram (recording stub;
transport tripwire) and never calls OpenAI (deterministic stub; client tripwire).
Workers receive test-only canary credentials, and every artifact is scanned for
them and for generic credential patterns.

    python -m tests.real_staging_news --report docs/persistence-phase2r-news-operational-report.md
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
from time import monotonic, sleep

import redis
import sqlalchemy as sa
from alembic import command

from persistence.config import DatabaseSettings
from persistence.database import make_engine
from tests import real_staging as staging
from tests.real_staging import StagingStop, base_env, db_counts, namespace_counts, redis_snapshot
from tests.test_persistence import migration_config

LABEL = "phase2r"
PG_CONTAINER, PG_PORT, PG_ADMIN_DB = "mias-test-phase2r-postgres", 55432, "mias_test_phase2r"
STAGING_DB = "mias_test_phase2r_news"
REDIS = dict(staging=("mias-test-phase2r-redis", 56379), control=("mias-test-phase2r-redis-control", 56380))
DEDUP_TTL = 86400
TTL_SLACK = 1800
CANARIES = dict(TELEGRAM_BOT_TOKEN="123456789:MIASCANARYtelegramTESTONLYtoken00000000",
                TELEGRAM_CHAT_ID="-1009999999999", OPENAI_API_KEY="sk-mias-canary-test-only-000000000000000000")
CREDENTIAL_PATTERNS = {
    "openai_key": r"sk-[A-Za-z0-9_-]{20,}", "telegram_token": r"\b\d{8,10}:[A-Za-z0-9_-]{30,}\b",
    "telegram_api": r"api\.telegram\.org/bot", "aws_key": r"AKIA[0-9A-Z]{16}", "bearer": r"Bearer [A-Za-z0-9._-]{20,}",
    "url_password": r"(?:postgres(?:ql)?|redis|https?)://[^\s/:@]+:[^\s/@]+@",
    "env_assignment": r"\b(?:OPENAI_API_KEY|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID|DATABASE_URL|REDIS_PASSWORD)\s*[=:]",
}

# ------------------------------------------------------------------ synthetic fixtures (clearly labelled)

FIXTURE = "https://www.reuters.com/technology/mias-staging-fixture-"
GNEWS = "https://news.google.com/rss/articles/mias-staging-fixture-"
YAHOO = "https://finance.yahoo.com/news/mias-staging-fixture-"
VARIANT = "https://www.example-news.test/markets/mias-staging-fixture-story"


def rss_date(clock, hours_before):
    return (clock - timedelta(hours=hours_before)).strftime("%a, %d %b %Y %H:%M:%S +0000")


def item(title, link, clock, *, publisher=None, hours=2, summary="Synthetic staging fixture."):
    entry = dict(title=title, summary=summary, published=rss_date(clock, hours))
    if link:
        entry["link"] = link
    if publisher:
        entry["source"] = {"title": publisher}
    return entry


def base_feeds(clock):
    return [
        dict(label="Google News META", entries=[
            item("Meta launches staging fixture AI model for creators", FIXTURE + "meta-ai", clock, publisher="Reuters"),
            item("Opinion: Meta staging fixture AI spending looks set to rise", GNEWS + "meta-opinion", clock, publisher="Bloomberg"),
            item("Meta expands staging fixture WhatsApp tools", GNEWS + "whatsapp", clock, publisher="Yahoo Finance"),
            item("Meta launches staging fixture AI model for creators", FIXTURE + "meta-ai-copy", clock, publisher="Reuters"),
            item("Meta launches staging fixture AI model for creators", GNEWS + "meta-ai-inv", clock, publisher="Investing.com"),
            item("Meta faces staging fixture EU fine over data transfers", GNEWS + "eu-fine", clock, publisher="Reuters"),
            item("Meta faces staging fixture EU fine over data transfer rules", GNEWS + "eu-fine-2", clock, publisher="Investing.com"),
            item("Meta staging fixture ad pricing update", VARIANT + "?id=7&utm_source=yahoo", clock, publisher="CNBC"),
            item("Instagram staging fixture creator payouts grow", VARIANT + "?id=7&utm_source=google", clock, publisher="CNBC"),
        ]),
        dict(label="Google News NVDA", entries=[
            item("Nvidia unveils staging fixture AI networking chip", FIXTURE + "nvda-chip", clock, publisher="Reuters"),
            item("Nvidia stock rises as staging fixture chip demand grows", GNEWS + "nvda-demand", clock, publisher="Reuters"),
            item("Nvidia shares fall after staging fixture export limits", GNEWS + "nvda-export", clock, publisher="Reuters"),
            item("Chipmakers rally in staging fixture session", GNEWS + "chip-rally", clock, publisher="CNBC",
                 summary="Nvidia and peers gained in a synthetic session."),
        ]),
        dict(label="Yahoo Finance META/NVDA", entries=[
            item("Nvidia staging fixture earnings beat estimates", YAHOO + "nvda-earnings", clock),
            item("Nvidia staging fixture supplier note without link", None, clock),
            item("Content policy review for Instagram posted without a link", None, clock),
        ]),
    ]


AI = {FIXTURE + "meta-ai": dict(summary="Synthetic model launch.", sentiment="BULLISH", confidence=78,
                                why_it_matters="Synthetic.", event_type="product launch"),
      GNEWS + "meta-opinion": dict(summary="Synthetic opinion.", sentiment="NEUTRAL", confidence=55,
                                   why_it_matters="Synthetic.", event_type="prediction article"),
      GNEWS + "nvda-demand": dict(summary="Synthetic demand.", sentiment="BULLISH", confidence=70,
                                  why_it_matters="Synthetic.", event_type="analyst commentary")}
# FIXTURE + "nvda-chip" is an ALERT candidate with no configured enrichment: the collector's AI-failure path.

SAME_URL = FIXTURE + "same-url-story"


def same_url(clock, title, hours):
    return [dict(label="Google News META", entries=[item(title, SAME_URL, clock, publisher="Reuters", hours=hours)])]


OUTAGE_TITLES = dict(D1="Nvidia board approves staging fixture buyback plan", A="Datacenter staging fixture orders lift Nvidia",
                     B="Nvidia settles staging fixture patent dispute", C="Analysts revisit Nvidia staging fixture margins",
                     E="Nvidia opens staging fixture research campus")


def outage_item(name, clock):
    return [dict(label="Google News NVDA", entries=[item(OUTAGE_TITLES[name], GNEWS + f"outage-{name.lower()}", clock,
                                                         publisher="Reuters")])]


# ------------------------------------------------------------------ safety

def credential_hits(text):
    hits = {name: len(re.findall(pattern, text)) for name, pattern in CREDENTIAL_PATTERNS.items()}
    hits["canaries"] = sum(text.count(value) for value in CANARIES.values())
    return {k: v for k, v in hits.items() if v}


def scan(text, where):
    hits = credential_hits(text)
    if hits:
        raise StagingStop(f"Credential-like content in {where}: {sorted(hits)}")  # Names only, never values.
    return text


class NewsWorker(staging.Worker):
    """Phase 2M worker process with a news-appropriate scan (live headlines may contain words like "secret")."""

    def send(self, op, **kwargs):
        self.process.stdin.write(json.dumps(dict(op=op, **kwargs)) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise StagingStop(f"Worker {self.label} exited unexpectedly")
        result = json.loads(scan(line, f"{self.label} protocol output"))
        if result["telegram_calls"] or result["openai_calls"]:
            raise StagingStop("Telegram/OpenAI guard tripped")
        self.results.append(result)
        return result

    def finish(self, *, hard=False):
        result = self.send("hard_exit" if hard else "exit")
        code = self.process.wait(timeout=60)
        logs = scan(self.logfile.read_text(), f"{self.label} stderr")
        result.update(exit_code=code, exit_mode="hard (os._exit after drain)" if hard else "graceful",
                      warnings=sorted({line.split(" ", 3)[3] for line in logs.splitlines() if " WARNING " in line}))
        if code != 0:
            raise StagingStop(f"Worker {self.label} exit code {code}")
        return result


def verify_services():
    info = staging.verify_container(PG_CONTAINER, PG_PORT, volume=True, label=LABEL)
    for container, port in REDIS.values():
        staging.verify_container(container, port, volume=False, label=LABEL)
    return info


def redis_client(which):
    return redis.Redis(host="127.0.0.1", port=REDIS[which][1], decode_responses=True, socket_timeout=5)


def database_url(name):
    return staging.database_url(name, prefix="mias_test_phase2r", port=PG_PORT)


# ------------------------------------------------------------------ evidence helpers

def news_redis(snapshot):
    keys = {k: v for k, v in snapshot.items() if k.startswith("mias:news:")}
    ttls = [v["ttl"] for v in keys.values()]
    return dict(keys=len(keys), namespaces=namespace_counts(keys),
                ttl_within_contract=all(DEDUP_TTL - TTL_SLACK <= t <= DEDUP_TTL for t in ttls),
                other_keys=len([k for k in snapshot if not k.startswith("mias:news:")]))


def redis_parity(on, off, keys=None):
    select = (lambda s: {k: v["value"] for k, v in s.items() if k.startswith("mias:news:")
                         and (keys is None or k in keys)})
    return dict(identical_keys_and_values=select(on) == select(off), keys=len(select(on)),
                ttl_within_contract=news_redis(on)["ttl_within_contract"] and news_redis(off)["ttl_within_contract"])


PARITY_FIELDS = ("processed", "events_digest", "stdout_digest", "stdout_lines", "collector_logs", "ai_attempts",
                 "ai_attempts_digest", "would_send", "would_send_digests")


def feed_view(cycle):
    return [dict(label=f["label"], stats=f["stats"], returned=f["returned"], alert_candidates=f["alert_candidates"],
                 http=None if not f["http"] else {k: f["http"][k] for k in ("http_status", "content_type", "items", "bozo")})
            for f in cycle["feeds"]]


def cycle_parity(on, off):
    differing = [field for field in PARITY_FIELDS if on.get(field) != off.get(field)]
    if feed_view(on) != feed_view(off):
        differing.append("feeds")
    if (on.get("fetched") or off.get("fetched")) and on.get("fetched") != off.get("fetched"):
        differing.append("fetched")
    return differing


def outage_lost(label, fields):
    """Explained: a submission made while PostgreSQL was deliberately stopped was never written (no replay)."""
    return bool(label) and label.endswith("(DB stopped)") and "event_found" in fields and set(fields) <= {
        "event_found", "version_found", "version_match", "provenance_match", "score_match", "decision_match", "ai_match"}


def keys_for(cycle, predicate=lambda event: True):
    """Exact and headline Redis keys of processed events in a cycle (both kinds, for real expiry)."""
    return [key for feed in cycle["feeds"] for e in feed["events"] if predicate(e)
            for key in (f"mias:news:event:{e['fingerprint']}", f"mias:news:headline:{e['headline_key']}")]


def expire(client, keys, timeout=10):
    """Real Redis expiry of selected keys (test-time PEXPIRE on a disposable instance; product TTL untouched)."""
    ttls = sorted({client.ttl(k) for k in keys})
    for key in keys:
        if not client.pexpire(key, 50):
            raise StagingStop("Expected news Redis key missing before expiry")
    deadline = monotonic() + timeout
    while monotonic() < deadline and client.exists(*keys):
        sleep(0.02)
    if client.exists(*keys):
        raise StagingStop("Redis did not expire the selected keys")
    return dict(keys=len(keys), ttl_before=ttls, exist_after=0)


def replace_redis(which):
    container, port = REDIS[which]
    staging.docker("restart", "--time", "2", container)
    staging.verify_container(container, port, volume=False, label=LABEL)
    client = redis_client(which)
    deadline = monotonic() + 15
    while monotonic() < deadline:
        try:
            size = client.dbsize()
            break
        except redis.RedisError:
            sleep(0.1)
    else:
        raise StagingStop("Replacement Redis did not become ready")
    if size:
        raise StagingStop("Replacement Redis is not empty")
    return dict(container_restarted=container, keys_after_restart=size)


def query(url, sql, **params):
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with engine.connect() as connection:
            return [dict(r) for r in connection.execute(sa.text(sql), params).mappings()]
    finally:
        engine.dispose()


def news_db(url):
    rows = query(url, "SELECT count(*) AS events, sum(CASE WHEN current_version_id IS NULL THEN 1 ELSE 0 END) AS no_current "
                      "FROM events WHERE source_family = 'news'")[0]
    counts = dict(events=rows["events"], without_current=int(rows["no_current"] or 0))
    for table, join in (("event_versions", "v"), ("event_provenance", "p"), ("event_history", "h")):
        sql = {"v": "SELECT count(*) AS n FROM event_versions v JOIN events e ON e.id = v.event_id WHERE e.source_family = 'news'",
               "p": "SELECT count(*) AS n FROM event_provenance p JOIN event_versions v ON v.id = p.event_version_id "
                    "JOIN events e ON e.id = v.event_id WHERE e.source_family = 'news'",
               "h": "SELECT count(*) AS n FROM event_history h JOIN event_versions v ON v.id = h.event_version_id "
                    "JOIN events e ON e.id = v.event_id WHERE e.source_family = 'news'"}[join]
        counts[table] = query(url, sql)[0]["n"]
    return counts


def history_by_kind(url):
    rows = query(url, "SELECT h.kind, count(*) AS n FROM event_history h JOIN event_versions v ON v.id = h.event_version_id "
                      "JOIN events e ON e.id = v.event_id WHERE e.source_family = 'news' GROUP BY h.kind ORDER BY h.kind")
    return {r["kind"]: r["n"] for r in rows}


def event_view(url, key):
    rows = query(url, "SELECT e.id, e.identity_version, v.id AS version_id, v.published_at, v.headline, "
                      "(e.current_version_id = v.id) AS current FROM events e JOIN event_versions v ON v.event_id = e.id "
                      "WHERE e.source_family = 'news' AND e.event_key = :k ORDER BY v.recorded_at", k=key)
    return rows


def outcomes_for(url, key):
    rows = query(url, "SELECT h.attributes FROM event_history h JOIN event_versions v ON v.id = h.event_version_id "
                      "JOIN events e ON e.id = v.event_id WHERE e.event_key = :k AND h.kind = 'decision'", k=key)
    return sorted({(r["attributes"] if isinstance(r["attributes"], dict) else json.loads(r["attributes"]))
                   ["collector_outcome"] for r in rows})


def dump_rows(url):
    return json.dumps([query(url, f"SELECT * FROM {t}") for t in ("events", "event_versions", "event_provenance",
                                                                    "event_history")], default=str)


def audit_cli(url, name):
    env = base_env(db_url=url, redis_port=REDIS["staging"][1])
    result = subprocess.run([sys.executable, "-m", "persistence.news_audit", name, "--json"], cwd=staging.ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    scan(result.stdout + result.stderr, f"news_audit {name}")
    if result.returncode:
        raise StagingStop(f"news_audit {name} failed with exit code {result.returncode}")
    return json.loads(result.stdout), result.stdout + result.stderr


# ------------------------------------------------------------------ runbook

def create_database(evidence=None):
    admin = make_engine(DatabaseSettings(url=database_url(PG_ADMIN_DB)))
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if connection.execute(sa.text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": STAGING_DB}).scalar_one():
                raise StagingStop("Staging database already exists; refusing to reuse unknown state")
            connection.execute(sa.text(f'CREATE DATABASE "{STAGING_DB}"'))
            if evidence is not None:
                evidence["postgresql_version"] = connection.execute(sa.text("SHOW server_version")).scalar_one()
    finally:
        admin.dispose()
    engine = make_engine(DatabaseSettings(url=database_url(STAGING_DB)))
    try:
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), "head")
            revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    finally:
        engine.dispose()
    if evidence is not None:
        evidence["migration_revision"] = revision


def reset_attempt():
    """Discard a live-parity attempt whose paired live reads differed: fresh database and empty Redis."""
    admin = make_engine(DatabaseSettings(url=database_url(PG_ADMIN_DB)))
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(sa.text(f'DROP DATABASE "{STAGING_DB}" WITH (FORCE)'))
    finally:
        admin.dispose()
    for which in REDIS:
        redis_client(which).flushdb()  # Disposable, verified phase2r instances only.
    create_database()


def run(workdir):
    evidence = dict(started_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    info = verify_services()
    for which in REDIS:
        if redis_client(which).dbsize():
            raise StagingStop("Disposable Redis must be empty at start")
    create_database(evidence)
    url = database_url(STAGING_DB)
    staging_redis, control_redis = redis_client("staging"), redis_client("control")
    evidence["redis_version"] = staging_redis.info("server")["redis_version"]
    evidence["environment"] = dict(
        postgresql=dict(container=PG_CONTAINER, image_digest=info["Image"][:19] + "…", binding=f"127.0.0.1:{PG_PORT}",
                        storage="private anonymous volume", database=STAGING_DB),
        redis=dict(staging=f"{REDIS['staging'][0]} 127.0.0.1:{REDIS['staging'][1]} tmpfs, RDB/AOF off",
                   control=f"{REDIS['control'][0]} 127.0.0.1:{REDIS['control'][1]} tmpfs, RDB/AOF off"),
        untouched=["mias-redis", "primary local PostgreSQL"], configuration="explicit process environment only; .env never read",
        canaries="workers receive test-only canary Telegram/OpenAI values; artifacts are scanned for them",
        synthetic_fixtures="mias-staging-fixture URLs and headlines (cannot match real articles)")
    live_clock = datetime.now(timezone.utc).replace(microsecond=0)
    clock = live_clock  # Synthetic items are dated relative to the same pinned clock.
    processes, workers = [], []
    evidence["processes"] = processes

    def worker(label, *, shadow=True, db=url, redis_which="staging"):
        env = base_env(db_url=db, redis_port=REDIS[redis_which][1], NEWS_PERSISTENCE_SHADOW_ENABLED=shadow, **CANARIES)
        w = NewsWorker("news", env, label, workdir)
        workers.append(w)
        processes.append(dict(label=label, pid=w.pid, shadow=shadow, redis=redis_which,
                              db="staging" if db == url else "unavailable (reserved closed port)",
                              started_utc=datetime.now(timezone.utc).strftime("%H:%M:%S")))
        return w

    def finish(w, *, hard=False):
        result = w.finish(hard=hard)
        entry = next(p for p in processes if p["label"] == w.label)
        integrity = result["reconciliation"].get("integrity_mismatches") or []
        explained = [i for i in integrity if outage_lost(*i)]
        entry.update(ended_utc=datetime.now(timezone.utc).strftime("%H:%M:%S"), exit_mode=result["exit_mode"],
                     exit_code=result["exit_code"], cycles=[_cycle(r) for r in w.results[:-1]],
                     shadow_stats=result["shadow_stats"], shadow_timestamps=result["shadow_timestamps"],
                     reconciliation=dict({k: result["reconciliation"].get(k) for k in (
                         "status", "checked", "current_pointer_differs")},
                         integrity_mismatches=[i for i in integrity if i not in explained], explained_outage_lost=explained),
                     telegram_transport_calls=result["telegram_calls"], openai_calls=result["openai_calls"],
                     would_send_total=result["would_send_total"], ai_attempts_total=result["ai_attempts_total"],
                     submissions=result["submissions"], warnings=result["warnings"])
        if result["reconciliation"]["status"] == "ok" and entry["reconciliation"]["integrity_mismatches"]:
            raise StagingStop(f"Unexplained reconciliation integrity mismatches in {w.label}")
        return result

    synthetic = dict(op="controlled", label="synthetic mix (identity, near-duplicate, AI, variants, link-less)",
                     feeds=base_feeds(clock), ai=AI, clock=clock.isoformat())

    # ---- S1. Persistence ON/OFF parity. Live pair retried only if the paired live reads differed.
    attempts = []
    for attempt in range(1, 4):
        s1, s1c = worker(f"S1-news-on#{attempt}"), worker(f"S1c-news-off-control#{attempt}", shadow=False,
                                                        redis_which="control")
        live_on = s1.send("live", label="live feeds", clock=live_clock.isoformat())
        live_off = s1c.send("live", label="live feeds", clock=live_clock.isoformat())
        same_input = live_on["fetched"] == live_off["fetched"]
        attempts.append(dict(attempt=attempt, identical_live_input=same_input))
        if same_input:
            break
        finish(s1), finish(s1c)
        for entry in processes[-2:]:
            entry["discarded"] = "live input differed between paired reads; database and Redis reset"
        reset_attempt()
    else:
        raise StagingStop("Live feeds changed between paired reads in three attempts; rerun later")
    evidence["live_parity_attempts"] = attempts
    live_items = [dict(label=label, entries=entries) for label, entries in live_on["fetched"].items()]
    live_replay = dict(op="replay", label="replay live items", feeds=live_items, clock=live_clock.isoformat())
    pairs = [("live feeds", live_on, live_off)]
    for kwargs in (synthetic, dict(live_replay, label="replay live items within TTL")):
        pairs.append((kwargs["label"], s1.send(**kwargs), s1c.send(**kwargs)))
    s1_result, s1c_result = finish(s1), finish(s1c)
    differing = {label: cycle_parity(on, off) for label, on, off in pairs}
    snap_on, snap_off = redis_snapshot(staging_redis), redis_snapshot(control_redis)
    evidence["parity"] = dict(
        cycles={label: dict(differing=differing[label], processed=on["processed"], ai_attempts=on["ai_attempts"],
                            would_send=on["would_send"], submitted=on["submitted"],
                            near_duplicate_suppressed=on["submitted_outcomes"].count("near_duplicate_suppressed"))
                for label, on, _ in pairs},
        redis=redis_parity(snap_on, snap_off), redis_on=news_redis(snap_on), redis_off=news_redis(snap_off),
        shadow_logs=dict(on=sum(len(c["shadow_logs"]) for _, c, _ in pairs), off=sum(len(c["shadow_logs"]) for _, _, c in pairs)))
    if any(differing.values()):
        raise StagingStop(f"Collector-visible behavior differs with persistence on: {differing}")
    if not (evidence["parity"]["redis"]["identical_keys_and_values"] and evidence["parity"]["redis"]["ttl_within_contract"]):
        raise StagingStop("Redis state differs with persistence on")
    evidence["live"] = _live_summary(live_on)
    if s1_result["shadow_stats"]["failed"]:
        raise StagingStop("Persistence failures with a healthy database")
    synthetic_on = pairs[1][1]
    baseline = news_db(url)
    evidence["db_after_S1"] = baseline

    # ---- S2. Real restart with Redis intact: fresh process skips everything; durable rows unchanged.
    s2 = worker("S2-news-restart-redis-intact")
    s2_cycles = [s2.send(**live_replay), s2.send(**synthetic)]
    s2_result = finish(s2, hard=True)
    evidence["restart_redis_intact"] = dict(processed=[c["processed"] for c in s2_cycles],
                                            submitted=[c["submitted"] for c in s2_cycles],
                                            ai_attempts=sum(c["ai_attempts"] for c in s2_cycles),
                                            would_send=sum(c["would_send"] for c in s2_cycles), db=news_db(url))
    if any(c["processed"] or c["submitted"] for c in s2_cycles) or evidence["restart_redis_intact"]["db"] != baseline:
        raise StagingStop("Restart with Redis intact changed collector or durable state")

    # ---- S3. Real Redis expiry of selected articles, then a fresh process a day later.
    live_processed = [e for f in live_on["feeds"] for e in f["events"] if e["decision"]]
    chosen = set()
    if live_processed:
        chosen.add(live_processed[0]["fingerprint"])
    for e in (e for f in synthetic_on["feeds"] for e in f["events"]):
        if e["decision"] == "ALERT" and e["ai_event_type"] == "product launch" or e["identity"] == "news-fingerprint-v1":
            chosen.add(e["fingerprint"])
    expire_keys = keys_for(live_on, lambda e: e["fingerprint"] in chosen) + keys_for(
        synthetic_on, lambda e: e["fingerprint"] in chosen)
    history_before = history_by_kind(url)
    evidence["ttl_expiry"] = dict(expired=expire(staging_redis, expire_keys),
                                  control_expired=expire(control_redis, expire_keys))  # Same state for the OFF control.
    later = (clock + timedelta(hours=25)).isoformat()
    s3, s3c = worker("S3-news-after-ttl-expiry"), worker("S3c-news-off-control", shadow=False, redis_which="control")
    s3_ops = [dict(live_replay, label="replay live items after expiry", clock=later),
              dict(synthetic, label="synthetic mix after expiry", clock=later)]
    s3_pairs = [(op["label"], s3.send(**op), s3c.send(**op)) for op in s3_ops]
    s3_cycles = [on for _, on, _ in s3_pairs]
    s3_result, _ = finish(s3, hard=True), finish(s3c)
    evidence["ttl_expiry"]["on_off_differing"] = {label: cycle_parity(on, off) for label, on, off in s3_pairs}
    if any(evidence["ttl_expiry"]["on_off_differing"].values()):
        raise StagingStop("Collector behavior after Redis expiry differs with persistence on")
    reprocessed = sorted(e["fingerprint"] for c in s3_cycles for f in c["feeds"] for e in f["events"])
    db3 = news_db(url)
    evidence["ttl_expiry"].update(
        articles_expired=len(chosen), reprocessed=len(reprocessed), reprocessed_matches_expired=reprocessed == sorted(chosen),
        ai_attempts=sum(c["ai_attempts"] for c in s3_cycles), would_send_repeat=sum(c["would_send"] for c in s3_cycles),
        shadow=s3_result["shadow_stats"], rows_unchanged={k: db3[k] == baseline[k] for k in (
            "events", "event_versions", "event_provenance")},
        history_added={k: v - history_before.get(k, 0) for k, v in history_by_kind(url).items() if v != history_before.get(k, 0)},
        redis_after=news_redis(redis_snapshot(staging_redis)))
    if not evidence["ttl_expiry"]["reprocessed_matches_expired"] or not all(evidence["ttl_expiry"]["rows_unchanged"].values()):
        raise StagingStop("Redis expiry did not reprocess exactly the expired articles as the same durable events")

    # ---- S4. Redis loss: staging Redis instance replaced; fresh process replays everything.
    evidence["redis_loss"] = dict(replacement=replace_redis("staging"), control_replacement=replace_redis("control"))
    staging_redis, control_redis = redis_client("staging"), redis_client("control")
    history_before = history_by_kind(url)
    even_later = (clock + timedelta(hours=26)).isoformat()
    s4, s4c = worker("S4-news-after-redis-loss"), worker("S4c-news-off-control", shadow=False, redis_which="control")
    s4_ops = [dict(live_replay, label="replay live items after Redis loss", clock=even_later),
              dict(synthetic, label="synthetic mix after Redis loss", clock=even_later)]
    s4_pairs = [(op["label"], s4.send(**op), s4c.send(**op)) for op in s4_ops]
    s4_cycles = [on for _, on, _ in s4_pairs]
    s4_result, _ = finish(s4), finish(s4c)
    evidence["redis_loss"]["on_off_differing"] = {label: cycle_parity(on, off) for label, on, off in s4_pairs}
    evidence["redis_loss"]["redis_parity"] = redis_parity(redis_snapshot(staging_redis), redis_snapshot(control_redis))
    if any(evidence["redis_loss"]["on_off_differing"].values()) \
            or not evidence["redis_loss"]["redis_parity"]["identical_keys_and_values"]:
        raise StagingStop("Collector behavior after Redis loss differs with persistence on")
    db4 = news_db(url)
    expected_submissions = sum(c["submitted"] for _, c, _ in pairs[:2])
    evidence["redis_loss"].update(
        reprocessed=sum(c["processed"] for c in s4_cycles), submitted=sum(c["submitted"] for c in s4_cycles),
        expected_submissions=expected_submissions, ai_attempts=sum(c["ai_attempts"] for c in s4_cycles),
        would_send_repeat=sum(c["would_send"] for c in s4_cycles), shadow=s4_result["shadow_stats"],
        rows_unchanged={k: db4[k] == baseline[k] for k in ("events", "event_versions", "event_provenance")},
        history_added={k: v - history_before.get(k, 0) for k, v in history_by_kind(url).items() if v != history_before.get(k, 0)},
        redis_after=news_redis(redis_snapshot(staging_redis)))
    if evidence["redis_loss"]["submitted"] != expected_submissions or not all(evidence["redis_loss"]["rows_unchanged"].values()):
        raise StagingStop("Redis loss did not map every article to its existing durable event")

    # ---- S5. Same URL / changed headline across fresh processes (same, then later publication time).
    key = None
    same_url_steps = [("M1", "Meta staging fixture same-URL story first headline", 3),
                      ("M2", "Quarterly staging fixture review reshapes Meta outlook", 3),
                      ("M3", "Regulators question Meta staging fixture ad terms", 2)]
    for label, title, hours in same_url_steps:
        w = worker(f"{label}-news-same-url")
        cycle = w.send("controlled", label=f"same URL, {label}", feeds=same_url(clock, title, hours), clock=clock.isoformat())
        finish(w)
        key = cycle["submitted_keys"][0]["identity_key"]
    view = event_view(url, key)
    evidence["same_url_changed_headline"] = dict(
        events=len({r["id"] for r in view}), versions=len(view), identity=view[0]["identity_version"],
        current_version_index=next(i for i, r in enumerate(view, 1) if r["current"]),
        publication_order=[str(r["published_at"]) for r in view],
        rule="same time -> held (ambiguous); strictly later -> promoted (newer_material)")
    if evidence["same_url_changed_headline"]["events"] != 1 or len(view) != 3 or not view[2]["current"]:
        raise StagingStop("Same-URL changed headline did not behave as one event with the later version current")

    # ---- S6. Identity, near-duplicate and link-less checks from durable state.
    identity = {}
    for f in synthetic_on["feeds"]:
        for e in f["events"]:
            identity.setdefault(e["identity"], set()).add(e["identity_key"])
    submitted_keys = synthetic_on["submitted_keys"]
    entries = [e for f in base_feeds(clock) for e in f["entries"]]
    if len(entries) != len(submitted_keys):  # Every synthetic item is relevant and submitted, in feed order.
        raise StagingStop("Synthetic items and submissions do not align")
    by_url = {}
    for entry, keys in zip(entries, submitted_keys):
        by_url[entry.get("link", "no-link:" + entry["title"])] = keys["identity_key"]
    near = dict(same_headline_different_url=[by_url[FIXTURE + "meta-ai"], by_url[FIXTURE + "meta-ai-copy"], by_url[GNEWS + "meta-ai-inv"]],
                above_threshold=[by_url[GNEWS + "eu-fine"], by_url[GNEWS + "eu-fine-2"]],
                below_threshold=[by_url[GNEWS + "nvda-demand"], by_url[GNEWS + "nvda-export"]])
    evidence["near_duplicates"] = {case: dict(distinct_durable_events=len(set(keys)),
                                              outcomes=[outcomes_for(url, k) for k in keys]) for case, keys in near.items()}
    linkless = [k["identity_key"] for k in submitted_keys if k["identity"] == "news-fingerprint-v1"]
    evidence["missing_link"] = dict(items=len(linkless), distinct_durable_events=len(set(linkless)),
                                    events_in_db=sum(len({r["id"] for r in event_view(url, k)}) for k in set(linkless)),
                                    repeat_after_expiry_same_event=evidence["ttl_expiry"]["rows_unchanged"]["events"])
    if any(v["distinct_durable_events"] != len(near[c]) for c, v in evidence["near_duplicates"].items()) \
            or evidence["missing_link"]["distinct_durable_events"] != 2 or evidence["missing_link"]["events_in_db"] != 2:
        raise StagingStop("Near-duplicate or link-less items did not stay separate durable events")

    # ---- S7. PostgreSQL outage across process boundaries.
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))  # Bound, never listening: cannot be any database.
    down_url = url.replace(f":{PG_PORT}/", f":{holder.getsockname()[1]}/")
    try:
        d1, d1c = worker("D1-news-db-unavailable-at-start", db=down_url), worker("D1c-news-control", shadow=False,
                                                                                 redis_which="control")
        d1_on = d1.send("controlled", label="synthetic outage item D1 (DB unavailable)", feeds=outage_item("D1", clock),
                        clock=clock.isoformat())
        d1_off = d1c.send("controlled", label="synthetic outage item D1 (DB unavailable)", feeds=outage_item("D1", clock),
                          clock=clock.isoformat())
        d1_result, _ = finish(d1), finish(d1c)
    finally:
        holder.close()
    d2, d2c = worker("D2-news-midrun-outage"), worker("D2c-news-control", shadow=False, redis_which="control")
    d2_pairs = []
    for name, note in (("A", "DB up"), ("B", "DB stopped"), ("C", "DB recovered")):
        if name == "B":
            if not d2.send("drain_wait")["drained"]:
                raise StagingStop("Writer did not drain before the planned outage")
            staging.docker("stop", "--time", "5", PG_CONTAINER)
        try:
            label = f"synthetic outage item {name} ({note})"
            kwargs = dict(label=label, feeds=outage_item(name, clock), clock=clock.isoformat())
            d2_pairs.append((label, d2.send("controlled", **kwargs), d2c.send("controlled", **kwargs)))
        finally:
            if name == "B":
                staging.docker("start", PG_CONTAINER)
                staging.pg_ready(container=PG_CONTAINER, database=PG_ADMIN_DB)
    d2_result, _ = finish(d2), finish(d2c)
    d3 = worker("D3-news-restart-after-recovery")
    d3_cycle = d3.send("controlled", label="outage item B again + new item E after recovery",
                       feeds=[dict(label="Google News NVDA", entries=outage_item("B", clock)[0]["entries"]
                                   + outage_item("E", clock)[0]["entries"])], clock=clock.isoformat())
    d3_result = finish(d3)
    outage_keys = {name: pair[1]["submitted_keys"][0] for name, pair in zip("ABC", d2_pairs)}
    outage_keys["D1"] = d1_on["submitted_keys"][0]
    exists = lambda k: bool(event_view(url, k["identity_key"]))
    parity_keys = {f"mias:news:{kind}:{k[field]}" for k in outage_keys.values()
                   for kind, field in (("event", "fingerprint"), ("headline", "headline_key"))}
    outage_differing = {label: cycle_parity(on, off) for label, on, off in [("D1", d1_on, d1_off)] + d2_pairs}
    evidence["db_outage"] = dict(
        collector_parity_differing=outage_differing,
        redis_parity=redis_parity(redis_snapshot(staging_redis), redis_snapshot(control_redis), parity_keys),
        unavailable_at_start=dict(shadow=d1_result["shadow_stats"], reconciliation=d1_result["reconciliation"]["status"]),
        midrun=dict(shadow=d2_result["shadow_stats"]),
        after_recovery=dict(processed=d3_cycle["processed"], shadow=d3_result["shadow_stats"]),
        durable=dict(d1_item=exists(outage_keys["D1"]), a_before_outage=exists(outage_keys["A"]),
                     b_during_outage=exists(outage_keys["B"]), c_after_recovery=exists(outage_keys["C"]),
                     e_new_after_restart=bool(d3_cycle["submitted_keys"]) and exists(d3_cycle["submitted_keys"][-1])))
    outage = evidence["db_outage"]
    if any(outage_differing.values()) or not outage["redis_parity"]["identical_keys_and_values"]:
        raise StagingStop("DB outage changed collector-visible behavior")
    if d1_result["shadow_stats"]["failed"] < 1 or d2_result["shadow_stats"]["failed"] < 1:
        raise StagingStop("Outage did not register persistence failures")
    if outage["durable"] != dict(d1_item=False, a_before_outage=True, b_during_outage=False, c_after_recovery=True,
                                 e_new_after_restart=True) or d3_cycle["processed"] != 1:
        raise StagingStop(f"Unexpected durable state around the outage: {outage['durable']}")

    # ---- S8. Read-only audits (separate CLI processes).
    variants, variants_text = audit_cli(url, "url-variants")
    repeats, repeats_text = audit_cli(url, "repeats")
    synthetic_group = [g for g in variants["groups"] if "mias-staging-fixture" in g["path"]]
    evidence["url_variants"] = dict(
        events_scanned=variants["events_scanned"], truncated=variants["truncated"], groups=variants["variant_group_count"],
        synthetic=[{k: g[k] for k in ("host", "path", "count", "urls", "event_ids", "publishers", "first_observed",
                                      "last_observed")} for g in synthetic_group],
        live_groups=[dict(host=g["host"], count=g["count"]) for g in variants["groups"] if g not in synthetic_group],
        note=variants["note"])
    if len(synthetic_group) != 1 or synthetic_group[0]["count"] != 2:
        raise StagingStop("URL-variant audit did not report the labelled synthetic variant pair")
    tally = _observation_tally(processes, workers)
    re_observed = {k for k, n in tally.items() if n > 1}
    audited = {_identity_of(url, e["event_row_id"]) for e in repeats["events"]}
    evidence["repeat_observations"] = dict(
        events_scanned=repeats["events_scanned"], repeated_events=repeats["repeated_events"], note=repeats["note"],
        max_versions=max((e["versions"] for e in repeats["events"]), default=0),
        max_provenance=max((e["provenance"] for e in repeats["events"]), default=0),
        synthetic_examples=[{k: e[k] for k in ("identity_version", "versions", "provenance", "span_seconds",
                                                "observation_count", "duplicate_count")}
                            for e in repeats["events"] if e["canonical_url"] and "mias-staging-fixture" in e["canonical_url"]][:5],
        process_evidence=dict(identities_observed=len(tally), re_observed=len(re_observed),
                              max_observations=max(tally.values(), default=0)),
        matches_process_evidence=re_observed == audited)
    if re_observed != audited:
        raise StagingStop("Repeat-observation audit disagrees with process evidence")

    # ---- S9. Final state, reconciliation, guards and credential scan.
    final = news_db(url)
    multi = query(url, "SELECT e.event_key, count(v.id) AS n FROM events e JOIN event_versions v ON v.event_id = e.id "
                       "WHERE e.source_family = 'news' GROUP BY e.event_key HAVING count(v.id) > 1")
    evidence["db_final"] = dict(final, multi_version_events=len(multi),
                                multi_version_is_same_url_story=[m["event_key"] for m in multi] == [key],
                                history_by_kind=history_by_kind(url), all_tables=db_counts(url))
    if final["without_current"] or evidence["db_final"]["multi_version_is_same_url_story"] is not True:
        raise StagingStop("Unexpected versions or missing current pointers")
    evidence["redis_final"] = dict(staging=news_redis(redis_snapshot(staging_redis)),
                                   control=news_redis(redis_snapshot(control_redis)))
    evidence["reconciliation"] = [dict(process=p["label"], **p["reconciliation"]) for p in processes if "reconciliation" in p]
    evidence["guards"] = dict(telegram_transport_calls=sum(p["telegram_transport_calls"] for p in processes),
                              openai_calls=sum(p["openai_calls"] for p in processes),
                              would_send_stub_total=sum(p["would_send_total"] for p in processes),
                              ai_stub_attempts_total=sum(p["ai_attempts_total"] for p in processes))
    if evidence["guards"]["telegram_transport_calls"] or evidence["guards"]["openai_calls"]:
        raise StagingStop("Telegram/OpenAI tripwire reached")
    artifacts = {"worker protocol lines": json.dumps([w.results for w in workers], default=str),
                 "worker stderr logs": "".join(w.logfile.read_text() for w in workers),
                 "audit CLI output": variants_text + repeats_text, "stored rows": dump_rows(url),
                 "Redis keys and values": json.dumps([redis_snapshot(staging_redis), redis_snapshot(control_redis)]),
                 "evidence": json.dumps(evidence, default=str)}
    evidence["credential_scan"] = dict(patterns=sorted(CREDENTIAL_PATTERNS) + ["canaries"],
                                       artifacts={name: dict(bytes=len(text), hits=sum(credential_hits(text).values()))
                                                  for name, text in artifacts.items()})
    if any(a["hits"] for a in evidence["credential_scan"]["artifacts"].values()):
        raise StagingStop("Credential-like content found in an operational artifact")
    evidence["acceptance"] = "PASS"
    return evidence


def _identity_of(url, event_row_id):
    return query(url, "SELECT event_key FROM events WHERE id = :i", i=event_row_id)[0]["event_key"]


def _observation_tally(processes, workers):
    """Durable-identity observations submitted by shadow-on processes while the database was reachable."""
    tally = {}
    for entry, w in zip(processes, workers):
        if not entry["shadow"] or entry["db"] != "staging" or entry.get("discarded"):
            continue
        for result in w.results[:-1]:
            if (result.get("label") or "").endswith("(DB stopped)"):
                continue
            for keys in result.get("submitted_keys", []):
                tally[keys["identity_key"]] = tally.get(keys["identity_key"], 0) + 1
    return tally


def _cycle(result):
    keep = ("op", "label", "processed", "submitted", "ai_attempts", "would_send", "drained")
    summary = {k: result[k] for k in keep if k in result}
    if "feeds" in result:
        summary["feeds"] = [dict(label=f["label"], stats=f["stats"], alert_candidates=f["alert_candidates"],
                                 near_duplicate_suppressed=f["near_duplicate_suppressed"], submissions=f["submissions"],
                                 decisions=_decisions(f["events"])) for f in result["feeds"]]
    if "submitted_outcomes" in result:
        summary["outcomes"] = {o: result["submitted_outcomes"].count(o) for o in sorted(set(result["submitted_outcomes"]))}
    return summary


def _decisions(events):
    counts = {}
    for event in events:
        counts[event["decision"]] = counts.get(event["decision"], 0) + 1
    return dict(sorted(counts.items()))


def _live_summary(live):
    feeds = {}
    for f in live["feeds"]:
        stats = f["stats"]
        feeds[f["label"]] = dict(http_status=f["http"]["http_status"], content_type=f["http"]["content_type"],
                                 feed_items=f["http"]["items"], bozo=f["http"]["bozo"], fetched=stats["fetched"],
                                 normalized=stats["fetched"], relevant=stats["relevant"],
                                 exact_duplicates=stats["duplicates"] - f["near_duplicate_suppressed"],
                                 near_duplicate_suppressions=f["near_duplicate_suppressed"],
                                 processed=stats["processed"], alert_candidates=f["alert_candidates"],
                                 submissions=f["submissions"], decisions=_decisions(f["events"]),
                                 identities=sorted({e["identity"] for e in f["events"]}))
    return dict(feeds=feeds, processed=live["processed"], ai_stub_attempts=live["ai_attempts"],
                would_send=live["would_send"])


# ------------------------------------------------------------------ report

RISKS = [
    "- The collector re-runs OpenAI and re-sends `ALERT` articles after Redis expiry or Redis loss (existing, unchanged",
    "  behavior; measured above as AI attempts and would-be sends). Persistence records durable duplicates only.",
    "- Recomputed scores differ after time passes (recency bonus), so repeats can append score/decision history rows",
    "  on the same version; the writer's `duplicate` counter does not count those writes.",
    "- URL query/fragment variants of one story are separate durable events (the URL-variant audit reports them for a",
    "  future product decision; nothing is canonicalized or merged).",
    "- An article submitted during a DB outage stays unpersisted until re-observed, which Redis dedup prevents for 24 h.",
    "- The exact number of repeat observations is not stored durably; counters are process-local.",
    "- Time-boxed local staging with one paired live read; not a multi-day soak.",
]


def summarize(evidence):
    parity, live = evidence["parity"], evidence["live"]
    ttl, loss, outage = evidence["ttl_expiry"], evidence["redis_loss"], evidence["db_outage"]
    unexplained = sum(len(r.get("integrity_mismatches") or []) for r in evidence["reconciliation"])
    explained = sum(len(r.get("explained_outage_lost") or []) for r in evidence["reconciliation"])
    rows = [
        ("Live feeds", "; ".join(f"{name}: HTTP {f['http_status']} {f['content_type']}, {f['fetched']} fetched, "
                                 f"{f['relevant']} relevant, {f['processed']} processed, {f['alert_candidates']} ALERT candidates"
                                 for name, f in live["feeds"].items())),
        ("Persistence ON/OFF parity", f"{len(parity['cycles'])} paired cycles identical (events, scores, decisions, AI attempts, "
         f"would-be sends, stdout, collector logs, feed HTTP evidence); Redis keys/values identical: "
         f"{parity['redis']['identical_keys_and_values']}, TTL within contract: {parity['redis']['ttl_within_contract']}; "
         f"live pair attempts: {len(evidence['live_parity_attempts'])}"),
        ("Restart with Redis intact (S2)", f"processed {evidence['restart_redis_intact']['processed']}; durable rows unchanged"),
        ("Redis TTL expiry (S3)", f"{ttl['expired']['keys']} keys really expired for {ttl['articles_expired']} articles; "
         f"{ttl['reprocessed']} reprocessed; events/versions/provenance unchanged; history added {ttl['history_added']}; "
         f"AI attempts {ttl['ai_attempts']}, would-be sends {ttl['would_send_repeat']}"),
        ("Redis loss (S4)", f"instance replaced (0 keys); {loss['submitted']} submissions re-observed; events/versions/"
         f"provenance unchanged; history added {loss['history_added']}; AI attempts {loss['ai_attempts']}, would-be sends "
         f"{loss['would_send_repeat']}"),
        ("DB unavailable at start (D1)", f"shadow failed={outage['unavailable_at_start']['shadow']['failed']}; collector and "
         "Redis identical to control"),
        ("DB stopped between cycles, recovered (D2/D3)", f"shadow failed={outage['midrun']['shadow']['failed']}; "
         + ", ".join(f"{k}={v}" for k, v in outage["durable"].items())),
        ("Same URL, changed headline", f"{evidence['same_url_changed_headline']['events']} event, "
         f"{evidence['same_url_changed_headline']['versions']} versions; current = version "
         f"{evidence['same_url_changed_headline']['current_version_index']} (strictly later publication)"),
        ("Near-duplicates", "; ".join(f"{case}: {v['distinct_durable_events']} separate events"
                                      for case, v in evidence["near_duplicates"].items())),
        ("Missing-link fallback", f"{evidence['missing_link']['distinct_durable_events']} separate fallback events; "
         "repeat after expiry mapped to the same event"),
        ("URL-variant audit", f"{evidence['url_variants']['groups']} group(s): {len(evidence['url_variants']['synthetic'])} "
         f"labelled synthetic, {len(evidence['url_variants']['live_groups'])} live; read-only, nothing merged"),
        ("Repeat-observation audit", f"{evidence['repeat_observations']['repeated_events']} events re-observed; matches process "
         f"evidence: {evidence['repeat_observations']['matches_process_evidence']}"),
        ("Reconciliation", f"{unexplained} unexplained mismatches; {explained} explained (submitted while PostgreSQL was "
         "deliberately stopped; never written, not replayed)"),
        ("Credential scan", f"{sum(a['hits'] for a in evidence['credential_scan']['artifacts'].values())} hits across "
         f"{len(evidence['credential_scan']['artifacts'])} artifact kinds (patterns + test-only canaries)"),
        ("Telegram transport / OpenAI calls", f"{evidence['guards']['telegram_transport_calls']} / {evidence['guards']['openai_calls']} "
         f"(stub: {evidence['guards']['would_send_stub_total']} would-be sends, {evidence['guards']['ai_stub_attempts_total']} "
         "AI attempts)"),
    ]
    return ["## Executive summary", "", "| Check | Evidence |", "|---|---|"] + [f"| {c} | {v} |" for c, v in rows] + [""]


def render(evidence):
    def block(value):
        return "```json\n" + json.dumps(value, sort_keys=True, indent=2, default=str) + "\n```"
    lines = ["# Persistence Phase 2R: News/RSS operational validation report", "",
             f"Run: {evidence['started_utc']}. Generated by `python -m tests.real_staging_news`.",
             "Separate OS processes (`tests.real_staging_worker --collector news`), real restarts, the configured live",
             "feeds plus clearly labelled synthetic items, disposable PostgreSQL/Redis, separate-process audit CLI.",
             "No Telegram sends and no OpenAI calls. Counts only for live data; no credentials.", "",
             f"- PostgreSQL: `{evidence['postgresql_version']}`", f"- Redis: `{evidence['redis_version']}`",
             f"- Migration revision: `{evidence['migration_revision']}`", f"- Acceptance: **{evidence['acceptance']}**", ""]
    lines += summarize(evidence)
    sections = [("1. Environment", "environment"), ("5. Live feed results", "live"),
                ("6. Persistence ON/OFF parity", ("parity", "live_parity_attempts")),
                ("7. Restart with Redis intact", "restart_redis_intact"), ("8. Redis TTL expiry", "ttl_expiry"),
                ("9. Redis loss", "redis_loss"), ("10. PostgreSQL outage and recovery", "db_outage"),
                ("12. URL-variant audit", "url_variants"), ("13. Repeat-observation audit", "repeat_observations"),
                ("14. Near-duplicate validation", "near_duplicates"),
                ("15. Same URL, changed headline", "same_url_changed_headline"), ("16. Missing-link fallback", "missing_link"),
                ("17-18. AI and Telegram guards", "guards"), ("19. Reconciliation (read-only, per process)", "reconciliation"),
                ("20. Process lifecycle and counters (process-local)", "processes"),
                ("21. Credential scan (hit counts only)", "credential_scan"),
                ("Durable state", ("db_after_S1", "db_final", "redis_final"))]
    for title, key in sections:
        value = {k: evidence[k] for k in key} if isinstance(key, tuple) else evidence[key]
        lines += [f"## {title}", block(value)]
    lines += ["## 11. Exact identity behavior", "",
              "Every durable key above is `news-url-v1` (collector-normalized URL) except the two link-less items, which",
              "use `news-fingerprint-v1`. Restart, expiry and Redis loss re-mapped every article to its existing event;",
              "different URLs (same headline, near-duplicates, query variants) stayed separate.", "",
              "## 17-18. AI and Telegram parity", "",
              "AI attempts and would-be Telegram sends are part of every paired-cycle comparison (ON vs OFF, including",
              "during the outage). The AI stub never contacts OpenAI; the Telegram stub never sends; both tripwires read 0.",
              "", "## 24. Remaining risks", "", *RISKS, ""]
    return scan("\n".join(lines) + "\n", "report")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--evidence", help="also write raw evidence JSON here (local scratch)")
    parser.add_argument("--render-from", help="render the report from a saved evidence JSON (no services touched)")
    args = parser.parse_args(argv)
    if args.render_from:
        evidence = json.loads(Path(args.render_from).read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="mias-phase2r-") as workdir:
            try:
                evidence = run(workdir)
            except StagingStop as error:
                print(f"STAGING STOP: {error}", file=sys.stderr)
                return 1
    try:
        report = render(evidence)
    except StagingStop as error:
        print(f"STAGING STOP: {error}", file=sys.stderr)
        return 1
    if args.evidence and not args.render_from:
        Path(args.evidence).write_text(json.dumps(evidence, sort_keys=True, indent=2, default=str))
    Path(args.report).write_text(report)
    print(f"acceptance: {evidence['acceptance']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
