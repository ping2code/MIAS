"""Phase 2S all-source real-process stabilization and soak runner (test utility; never a deployment tool).

Orchestrates ``tests.real_staging_worker`` collector processes for all six
persisted families (macro, treasury, geopolitical, fed, sec, news). Each
collector runs in its own OS process; nothing is imported into this coordinator
to "run" a collector. It uses disposable, labelled ``phase2s`` PostgreSQL 16 /
Redis 7 containers and writes a credential-free, counts-only readiness report.

    # Bounded Phase 2S validation (what this phase ran):
    python -m tests.real_staging_all_sources --cycles 3 --cycle-seconds 60 \
        --report docs/persistence-phase2s-readiness-report.md --json-report /tmp/phase2s.json

    # Future multi-day shared-staging soak (see docs/persistence-phase2s-soak-runbook.md):
    python -m tests.real_staging_all_sources --duration-minutes 4320 --cycle-seconds 900 --no-faults \
        --report soak-report.md --json-report soak.json

Stages:

1. **Safety, containers and migrations:** verify or create the containers (they
   are removed on success, failure and Ctrl+C). Migrate an empty database to
   head, compare the schema to the models, downgrade to 0002 and re-upgrade.
2. **On/off parity:** paired persistence-on/off processes per family.
3. **Soak cycles:** a fresh process per family per cycle. Every process adds a
   restart-matrix row, and every new durable event must be explained by a newly
   observed identity.
4. **Cross-family look-alikes:** identical URLs, similar headlines and the same
   publication day across families.
5. **Controlled PostgreSQL outage** and recovery (no replay).
6. **Redis instance loss.**
7. **Audits:** every read-only audit, run as a separate CLI process.
8. **Scans and report:** resource and credential scans.

Safety: refuses non-loopback or unlabeled services, unknown database names and
non-empty Redis; never touches ``mias-redis``, never reads ``.env``, never sends
Telegram or calls OpenAI (stubs and tripwires; counts are recorded). The SEC
contact from the configured User-Agent is held in memory only for scanning.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
from time import monotonic, sleep

import redis
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from persistence import family_audit
from persistence.config import DatabaseSettings
from persistence.database import make_engine
from persistence.models import metadata
from tests import real_staging as staging
from tests import real_staging_news as news_staging
from tests import real_staging_sec as sec_staging
from tests.real_staging import StagingStop, base_env, namespace_counts, redis_snapshot
from tests.test_persistence import migration_config

LABEL = "phase2s"
PG_CONTAINER, PG_PORT, PG_ADMIN_DB = "mias-test-phase2s-postgres", 55432, "mias_test_phase2s"
STAGING_DB = "mias_test_phase2s_all"
REDIS = dict(staging=("mias-test-phase2s-redis", 56379), control=("mias-test-phase2s-redis-control", 56380))
PG_IMAGE = "postgres@sha256:a3b7f434b2dc57ce85a67e171163eb8ab1a1ebcb39d27484661f26b1dfbe30d6"
REDIS_IMAGE = "redis@sha256:c7d14d623c137a1bb6c3a6755b0b0aad499177087c2140eefcf2f122950b172d"
HEAD, EARLIER = "0007_technical_evidence_ledger", "0002_macro_shadow_history"  # Head moved with Phase 6 (0007: evidence ledger).
FAMILIES = family_audit.FAMILIES
COLLECTOR = dict(macro="macro", treasury="treasury", geopolitical="geo", fed="fed", sec="sec", news="news")
SWITCHES = dict(macro=dict(MACRO_PERSISTENCE_SHADOW_ENABLED=True), treasury=dict(TREASURY_PERSISTENCE_SHADOW_ENABLED=True),
                geopolitical=dict(GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED=True, GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED=True),
                fed=dict(FED_PERSISTENCE_SHADOW_ENABLED=True), sec=dict(SEC_PERSISTENCE_SHADOW_ENABLED=True),
                news=dict(NEWS_PERSISTENCE_SHADOW_ENABLED=True))
REDIS_PREFIX = dict(macro="mias:macro:", treasury="mias:treasury:", geopolitical="mias:geopolitical:", fed="mias:fed:",
                    sec="mias:sec:", news="mias:news:")
RSS_LIMIT_KB, THREAD_LIMIT = 1_500_000, 128  # Generous sanity bounds, not SLAs.
# Phase 2S-A: Treasury's bounded queue is configurable (default 256); every other family keeps the shared 64.
EXPECTED_CAPACITY = dict(macro=64, treasury=256, geopolitical=64, fed=64, sec=64, news=64)
FED_CLOCK, GEO_CLOCK = staging.FED_CLOCK, staging.GEO_CLOCK
FED_FIXTURE_URL = "https://www.federalreserve.gov/newsevents/pressreleases/mias-staging-fixture-monetary20260916a.htm"
# The archive URL the SEC normalizer builds for the synthetic META 8-K (accession 0000000000-26-900101).
SEC_FIXTURE_URL = "https://www.sec.gov/Archives/edgar/data/1326801/000000000026900101/mias-staging-fixture-8k.htm"
STOP = {"requested": False}


# ------------------------------------------------------------------ safety and services

def credential_hits(text, contact):
    hits = news_staging.credential_hits(text)
    contact_hits = sec_staging.contact_hits(text, contact)
    if contact_hits:
        hits["sec_contact"] = contact_hits
    return hits


class AllWorker(staging.Worker):
    """Phase 2M worker process scanned with credential patterns, canaries and the SEC contact (names only)."""

    contact = ()

    def scan(self, text, where):
        hits = credential_hits(text, self.contact)
        if hits:
            raise StagingStop(f"Credential-like content in {where}: {sorted(hits)}")
        return text

    def send(self, op, **kwargs):
        self.process.stdin.write(json.dumps(dict(op=op, **kwargs)) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise StagingStop(f"Worker {self.label} exited unexpectedly")
        result = json.loads(self.scan(line, f"{self.label} protocol output"))
        if result["telegram_calls"] or result["openai_calls"]:
            raise StagingStop("Telegram/OpenAI guard tripped")
        self.results.append(result)
        return result

    def finish(self, *, hard=False):
        result = self.send("hard_exit" if hard else "exit")
        code = self.process.wait(timeout=90)
        logs = self.scan(self.logfile.read_text(), f"{self.label} stderr")
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


def ensure_absent():
    """Refuse (without touching anything) if any Phase 2S container name already exists."""
    names = [PG_CONTAINER] + [c for c, _ in REDIS.values()]
    existing = staging.docker("ps", "-a", "--format", "{{.Names}}").split()
    if set(names) & set(existing):
        raise StagingStop("Phase 2S containers already exist; refusing to reuse unknown state")


def start_containers():
    """Create the three labelled, loopback-only disposable containers (caller ran ensure_absent first)."""
    staging.docker("run", "--detach", "--name", PG_CONTAINER, "--label", f"mias.disposable-test={LABEL}",
                   "--mount", "type=volume,destination=/var/lib/postgresql/data", "-e", f"POSTGRES_DB={PG_ADMIN_DB}",
                   "-e", "POSTGRES_USER=mias_test_user", "-e", "POSTGRES_HOST_AUTH_METHOD=trust",
                   "-p", f"127.0.0.1:{PG_PORT}:5432", PG_IMAGE)
    for container, port in REDIS.values():
        staging.docker("run", "--detach", "--name", container, "--label", f"mias.disposable-test={LABEL}", "--tmpfs",
                       "/data", "-p", f"127.0.0.1:{port}:6379", REDIS_IMAGE, "redis-server", "--save", "", "--appendonly", "no")
    staging.pg_ready(timeout=60, container=PG_CONTAINER, database=PG_ADMIN_DB)
    for which in REDIS:
        wait_redis(which)


def remove_containers():
    """Remove only the labelled phase2s containers (with anonymous volumes); never anything else."""
    removed = []
    for name in [PG_CONTAINER] + [c for c, _ in REDIS.values()]:
        result = subprocess.run(["docker", "inspect", "--format", "{{index .Config.Labels \"mias.disposable-test\"}}", name],
                                capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip() == LABEL:
            subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True, timeout=60)
            removed.append(name)
    return removed


def redis_client(which):
    return redis.Redis(host="127.0.0.1", port=REDIS[which][1], decode_responses=True, socket_timeout=5)


def wait_redis(which, timeout=20):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        try:
            return redis_client(which).dbsize()
        except redis.RedisError:
            sleep(0.1)
    raise StagingStop("Disposable Redis did not become ready")


def database_url(name):
    return staging.database_url(name, prefix="mias_test_phase2s", port=PG_PORT)


def replace_redis(which):
    container, port = REDIS[which]
    staging.docker("restart", "--time", "2", container)
    staging.verify_container(container, port, volume=False, label=LABEL)
    if wait_redis(which):
        raise StagingStop("Replacement Redis is not empty")
    return container


# ------------------------------------------------------------------ database evidence (read-only)

def audit(url, **kwargs):
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with family_audit.read_only(engine) as session:
            return family_audit.audit_families(session, **kwargs)
    finally:
        engine.dispose()


def counts(url):
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with family_audit.read_only(engine) as session:
            return family_audit.family_counts(session)
    finally:
        engine.dispose()


def existing_keys(url, family, keys):
    """Which of the given identities exist durably for this family (read-only)."""
    if not keys:
        return set()
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with engine.connect() as connection:
            query = sa.text("SELECT event_key FROM events WHERE source_family = :f AND event_key IN :k").bindparams(
                sa.bindparam("k", expanding=True))
            return set(connection.execute(query, dict(f=family, k=sorted(keys))).scalars())
    finally:
        engine.dispose()


def pg_resources(url):
    engine = make_engine(DatabaseSettings(url=database_url(PG_ADMIN_DB)))
    try:
        with engine.connect() as connection:
            size = connection.execute(sa.text("SELECT pg_database_size(:n)"), {"n": STAGING_DB}).scalar_one()
            rows = connection.execute(sa.text("SELECT coalesce(application_name, ''), count(*) FROM pg_stat_activity "
                                              "WHERE datname = :n GROUP BY 1"), {"n": STAGING_DB}).all()
        return dict(database_bytes=size, connections={name or "(none)": n for name, n in rows})
    finally:
        engine.dispose()


def redis_resources(client):
    info = client.info("memory")
    return dict(keys=client.dbsize(), used_memory=info.get("used_memory"))


def migration_check(url):
    """Empty DB -> head, compare to models, downgrade to 0002, re-upgrade, compare again."""
    engine = make_engine(DatabaseSettings(url=url))
    result = {}
    try:
        for step, target in (("upgrade_from_empty", HEAD), ("downgrade", EARLIER), ("re_upgrade", HEAD)):
            with engine.begin() as connection:
                config = migration_config(connection)
                (command.downgrade if step == "downgrade" else command.upgrade)(config, target)
            with engine.connect() as connection:
                inspector = sa.inspect(connection)
                tables = sorted(t for t in inspector.get_table_names() if t != "alembic_version")
                revision = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
                entry = dict(revision=revision, tables=tables)
                if target == HEAD:
                    diff = compare_metadata(MigrationContext.configure(connection), metadata)
                    indexes = {t: sorted(i["name"] for i in inspector.get_indexes(t)) for t in tables}
                    checks = {t: sorted(c["name"] for c in inspector.get_check_constraints(t) if c.get("name")) for t in tables}
                    expected = {t.name: sorted(i.name for i in t.indexes) for t in metadata.sorted_tables}
                    entry.update(schema_matches_models=diff == [], differences=len(diff),
                                 indexes_present=all(set(expected.get(t, [])) <= set(indexes[t]) for t in tables),
                                 index_count=sum(len(v) for v in indexes.values()),
                                 check_constraint_count=sum(len(v) for v in checks.values()))
                result[step] = entry
    finally:
        engine.dispose()
    if result["upgrade_from_empty"]["revision"] != HEAD or not result["upgrade_from_empty"]["schema_matches_models"] \
            or not result["re_upgrade"]["schema_matches_models"] or "geopolitical_anchor_registry" in result["downgrade"]["tables"]:
        raise StagingStop("Migration chain does not match the models; schema defect (stop and report)")
    return result


# ------------------------------------------------------------------ per-family result views

def submitted_keys(family, result):
    if family in ("macro", "treasury"):
        return set(result.get("keys", []))
    if family == "geopolitical":
        return {key for key in result.get("resolved", []) if key}
    if family == "fed":
        return set(result.get("fingerprints", []))
    if family == "sec":
        return {e["fingerprint"] for e in result.get("events", [])}
    return {k["identity_key"] for k in result.get("submitted_keys", [])}


def outcome_view(family, result):
    """What the collector did (never persistence details) for ON/OFF comparison."""
    if family == "sec":
        return {k: result.get(k) for k in sec_staging.PARITY_FIELDS + ("fetched",)}
    if family == "news":
        return dict({k: result.get(k) for k in news_staging.PARITY_FIELDS}, feeds=news_staging.feed_view(result),
                    fetched=result.get("fetched"))
    keep = ("stats", "processed_events", "events", "feed_entries", "per_source")
    return {k: result[k] for k in keep if k in result}


def redis_family_parity(on, off, family):
    return staging.comparable(on, REDIS_PREFIX[family]) == staging.comparable(off, REDIS_PREFIX[family])


def ops(family, *, all_sources=False, clock=None):
    if family == "geopolitical":
        return [("live", dict(label="live", all_sources=all_sources))]
    if family == "news":
        return [("live", dict(label="live", clock=clock))]
    return [("live", dict(label="live"))]


NOT_FOUND = {"event_found", "version_found", "version_match", "provenance_match", "score_match", "decision_match",
             "ai_match", "accession_match", "relevance_match", "relationship_match"}


def not_found(fields):
    """The reconciliation shape of a submission that was never written (nothing found, nothing contradicting)."""
    return "event_found" in fields and set(fields) <= NOT_FOUND


def outage_lost(label, fields):
    """Explained: a submission made while PostgreSQL was deliberately stopped was never written (no replay)."""
    return bool(label) and label.endswith("(DB stopped)") and not_found(fields)


def growth(before, after):
    kinds = ("events", "versions", "provenance", "score_history", "decision_history", "ai_history")
    return {family: {k: after[family][k] - before[family][k] for k in kinds} for family in FAMILIES}


def sanity(resources):
    """Generous leak sanity: report values; flag only runaway growth."""
    flags = []
    for label, entry in resources.items():
        if (entry.get("end_rss_kb") or 0) > RSS_LIMIT_KB or (entry.get("max_threads") or 0) > THREAD_LIMIT:
            flags.append(label)
    return flags


# ------------------------------------------------------------------ runbook

class Run:
    def __init__(self, workdir, contact, *, cycles, cycle_seconds, deadline, faults):
        self.workdir, self.contact, self.cycles, self.cycle_seconds = workdir, contact, cycles, cycle_seconds
        self.deadline, self.faults = deadline, faults
        self.evidence = dict(started_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), processes=[],
                             cycle_summaries=[], restart_matrix=[])
        self.workers, self.cli_text = [], []
        self.durable_keys = {family: set() for family in FAMILIES}
        self.unexplained_growth = []
        self.url = None

    # -- process helpers
    def worker(self, family, label, *, shadow=True, db=None, redis_which="staging"):
        switches = SWITCHES[family] if shadow else {}
        env = base_env(db_url=db or self.url, redis_port=REDIS[redis_which][1], **switches, **news_staging.CANARIES)
        w = AllWorker(COLLECTOR[family], env, label, self.workdir)
        w.contact = self.contact
        self.workers.append(w)
        self.evidence["processes"].append(dict(label=label, family=family, pid=w.pid, shadow=shadow, redis=redis_which,
                                               db="staging" if (db or self.url) == self.url else "unavailable",
                                               started_utc=datetime.now(timezone.utc).strftime("%H:%M:%S")))
        return w

    def finish(self, w, *, hard=False):
        result = w.finish(hard=hard)
        entry = next(p for p in self.evidence["processes"] if p["label"] == w.label)
        integrity = result["reconciliation"].get("integrity_mismatches") or []
        explained = [i for i in integrity if outage_lost(*i)]
        stats = result.get("shadow_stats") or {}
        # Submissions the bounded writer dropped because its queue was full (counted, never retried): "not found"
        # mismatches are explained by drops only up to the number of drops this process's writer counted.
        dropped = [i for i in integrity if i not in explained and not_found(i[1])]
        queue_full = dropped if len(dropped) <= stats.get("dropped_queue_full", 0) else []
        resources = [r["resources"] for r in w.results if "resources" in r]
        entry.update(ended_utc=datetime.now(timezone.utc).strftime("%H:%M:%S"), exit_mode=result["exit_mode"],
                     exit_code=result["exit_code"], shadow_stats=result.get("shadow_stats"),
                     shadow_timestamps=result.get("shadow_timestamps"),
                     reconciliation=dict(status=result["reconciliation"].get("status"),
                                         checked=result["reconciliation"].get("checked"),
                                         current_pointer_differs=result["reconciliation"].get("current_pointer_differs"),
                                         integrity_mismatches=[i for i in integrity if i not in explained + queue_full],
                                         explained_outage_lost=explained, explained_queue_full_drop=queue_full),
                     telegram_transport_calls=result["telegram_calls"], openai_calls=result["openai_calls"],
                     would_send_total=result.get("would_send_total"), ai_attempts_total=result.get("ai_attempts_total"),
                     submissions=result["submissions"], warnings=result["warnings"],
                     resources=dict(start_rss_kb=resources[0].get("rss_kb"), end_rss_kb=resources[-1].get("rss_kb"),
                                    max_threads=max(r.get("os_threads", 0) for r in resources)),
                     http=_http_view(result.get("http", {})), http_post=_http_view(result.get("http_post", {})),
                     shadow_capacity=result.get("shadow_capacity"))
        if result["reconciliation"].get("status") == "ok" and entry["reconciliation"]["integrity_mismatches"]:
            raise StagingStop(f"Unexplained reconciliation integrity mismatches in {w.label}: "
                              f"{entry['reconciliation']['integrity_mismatches'][:10]}")  # Labels and check names only.
        if entry["shadow_stats"] and (entry["shadow_stats"]["queue_depth"] or entry["shadow_stats"]["in_flight"]):
            raise StagingStop(f"Writer did not drain in {w.label}")
        return result

    def one(self, family, label, *, hard=False, extra=(), db_up=True, **kwargs):
        """One fresh process: operations, drain, exit; records a restart-matrix row and explains growth."""
        before = counts(self.url) if db_up else None
        w = self.worker(family, label, **kwargs)
        results = [w.send(op, **args) for op, args in list(extra) or ops(family)]
        self.finish(w, hard=hard)
        if not db_up or not kwargs.get("shadow", True):
            return results
        after = counts(self.url)
        keys = set().union(*(submitted_keys(family, r) for r in results))
        stored = existing_keys(self.url, family, keys)
        new_keys = stored - self.durable_keys[family]
        unwritten = keys - stored
        entry = next(p for p in self.evidence["processes"] if p["label"] == label)
        delta = {k: after[family][k] - before[family][k] for k in ("events", "versions", "provenance", "score_history",
                                                                    "decision_history", "ai_history")}
        # Every new durable event is a newly stored identity; identities never stored must be covered by counted drops.
        explained = delta["events"] == len(new_keys) and len(unwritten) <= (
            entry["shadow_stats"]["dropped_queue_full"] + entry["shadow_stats"]["failed"])
        if not explained:
            self.unexplained_growth.append(dict(process=label, family=family, events_added=delta["events"],
                                                newly_stored_identities=len(new_keys), unwritten_identities=len(unwritten)))
        self.durable_keys[family] |= stored
        self.evidence["restart_matrix"].append(dict(
            process=label, family=family, pid=entry["pid"], exit_mode=entry["exit_mode"],
            pre=dict(events=before[family]["events"], versions=before[family]["versions"]),
            post=dict(events=after[family]["events"], versions=after[family]["versions"]), added=delta,
            submissions=entry["submissions"], duplicates=entry["shadow_stats"]["duplicate"],
            failed=entry["shadow_stats"]["failed"], dropped_queue_full=entry["shadow_stats"]["dropped_queue_full"],
            newly_observed_identities=len(new_keys), identities_not_written=len(unwritten),
            growth_explained=explained, reconciliation=entry["reconciliation"]["status"]))
        return results

    # -- stages
    def run(self):
        ev = self.evidence
        info = verify_services()
        for which in REDIS:
            if redis_client(which).dbsize():
                raise StagingStop("Disposable Redis must be empty at start")
        admin = make_engine(DatabaseSettings(url=database_url(PG_ADMIN_DB)))
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            if connection.execute(sa.text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": STAGING_DB}).scalar_one():
                raise StagingStop("Staging database already exists; refusing to reuse unknown state")
            connection.execute(sa.text(f'CREATE DATABASE "{STAGING_DB}"'))
            ev["postgresql_version"] = connection.execute(sa.text("SHOW server_version")).scalar_one()
        admin.dispose()
        self.url = database_url(STAGING_DB)
        staging_redis, control_redis = redis_client("staging"), redis_client("control")
        ev["redis_version"] = staging_redis.info("server")["redis_version"]
        ev["environment"] = dict(
            postgresql=dict(container=PG_CONTAINER, image_digest=info["Image"][:19] + "…", binding=f"127.0.0.1:{PG_PORT}",
                            storage="private anonymous volume", database=STAGING_DB),
            redis=dict(staging=f"{REDIS['staging'][0]} 127.0.0.1:{REDIS['staging'][1]} tmpfs, RDB/AOF off",
                       control=f"{REDIS['control'][0]} 127.0.0.1:{REDIS['control'][1]} tmpfs, RDB/AOF off"),
            untouched=["mias-redis", "primary local PostgreSQL"], configuration="explicit process environment only; .env never read",
            canaries="workers receive test-only canary Telegram/OpenAI values; artifacts are scanned for them and for the SEC contact")
        ev["migration"] = migration_check(self.url)
        ev["migration_head"] = ev["migration"]["re_upgrade"]["revision"]
        ev["resources_start"] = dict(postgresql=pg_resources(self.url), redis=redis_resources(staging_redis))
        clock = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        # ---- Parity: paired ON/OFF processes per family (geopolitical API/RSS subset).
        parity, live = {}, {}
        for family in FAMILIES:
            on = self.one(family, f"P-{family}-on", extra=ops(family, clock=clock))
            off_w = self.worker(family, f"P-{family}-off-control", shadow=False, redis_which="control")
            off = [off_w.send(op, **args) for op, args in ops(family, clock=clock)]
            self.finish(off_w)
            same = [outcome_view(family, a) == outcome_view(family, b) for a, b in zip(on, off)]
            parity[family] = dict(identical=all(same), redis_identical=redis_family_parity(
                redis_snapshot(staging_redis), redis_snapshot(control_redis), family))
            live[family] = on[0]
            if family in ("sec", "news") and not all(same) and on[0].get("fetched") != off[0].get("fetched"):
                parity[family]["note"] = "live input changed between paired reads; replay parity used"
                parity[family]["identical"] = self.replay_parity(family, on[0], clock)
        ev["parity"] = parity
        ev["live"] = {family: _live_view(family, result) for family, result in live.items()}
        if not all(p["identical"] for p in parity.values()):
            raise StagingStop(f"Collector-visible behavior differs with persistence on: {parity}")
        if not all(p["redis_identical"] for f, p in parity.items() if f not in ("sec", "news")) \
                or not all(p["redis_identical"] for f, p in parity.items() if f in ("sec", "news") and "note" not in p):
            raise StagingStop("Redis state differs with persistence on")
        baseline = counts(self.url)
        ev["counts_after_parity"] = baseline

        # ---- Soak cycles: a fresh process per family per cycle; one abrupt exit per family (cycle 2).
        cycle = 0
        while not STOP["requested"]:
            cycle += 1
            started = monotonic()
            summary = dict(cycle=cycle, started_utc=datetime.now(timezone.utc).strftime("%H:%M:%S"), families={})
            for family in FAMILIES:
                if STOP["requested"]:
                    break
                results = self.one(family, f"C{cycle}-{family}", hard=cycle == 2,
                                   extra=ops(family, all_sources=cycle == 1, clock=datetime.now(timezone.utc)
                                             .replace(microsecond=0).isoformat()))
                row = ev["restart_matrix"][-1]
                summary["families"][family] = dict(submissions=row["submissions"], added=row["added"],
                                                   growth_explained=row["growth_explained"],
                                                   stats=results[0].get("stats") if family != "news" else
                                                   {f["label"]: f["stats"] for f in results[0]["feeds"]})
            ev["cycle_summaries"].append(summary)
            print(json.dumps(dict(cycle=cycle, families={f: dict(submissions=v["submissions"], events_added=v["added"]["events"])
                                                         for f, v in summary["families"].items()})), flush=True)
            if cycle >= self.cycles and (self.deadline is None or monotonic() >= self.deadline):
                break
            remaining = self.cycle_seconds - (monotonic() - started)
            while remaining > 0 and not STOP["requested"]:
                sleep(min(1.0, remaining))
                remaining -= 1.0
        ev["soak"] = dict(cycles_completed=cycle, interrupted=STOP["requested"],
                          label="bounded Phase 2S validation" if self.deadline is None else "configured-duration soak")
        ev["history_growth_soak"] = growth(baseline, counts(self.url))

        if self.faults and not STOP["requested"]:
            self.cross_family(clock)
            self.outage(clock)
            self.redis_loss(clock, live)
        self.audits()
        self.finalize(staging_redis, control_redis)
        return ev

    def replay_parity(self, family, live_result, clock):
        """Both processes replay the ON process's fetched live items (no network) against empty control Redis states."""
        if family == "sec":
            op = ("replay", dict(label="replay live filings", filings=live_result["fetched"]))
        else:
            op = ("replay", dict(label="replay live items", clock=clock,
                                 feeds=[dict(label=k, entries=v) for k, v in live_result["fetched"].items()]))
        results = []
        for shadow in (True, False):  # ON then OFF, each against a freshly cleared control keyspace for this family.
            client = redis_client("control")
            for key in list(client.scan_iter(match=REDIS_PREFIX[family] + "*")):
                client.delete(key)
            w = self.worker(family, f"P-{family}-replay-{'on' if shadow else 'off'}", shadow=shadow, redis_which="control")
            results.append(w.send(*op[:1], **op[1]))
            self.finish(w)
        return outcome_view(family, results[0]) == outcome_view(family, results[1])

    def cross_family(self, clock):
        """Look-alike inputs across families (same URL, similar headline, same day) stay separate durable events."""
        news_item = news_staging.item("Federal Reserve issues FOMC statement as Nvidia rallies", FED_FIXTURE_URL,
                                      datetime.fromisoformat(FED_CLOCK), publisher="Reuters")
        meta_news = news_staging.item("META filed SEC Form 8-K", SEC_FIXTURE_URL, datetime.fromisoformat(FED_CLOCK),
                                      publisher="Reuters")
        self.one("fed", "X-fed-lookalike", extra=[("controlled", dict(label="fixture: FOMC statement", docs=["policy_statement"],
                                                                     clock=FED_CLOCK))])
        self.one("news", "X-news-lookalike", extra=[("controlled", dict(
            label="fixture: Fed-like and SEC-like news items", clock=FED_CLOCK,
            feeds=[dict(label="Google News META", entries=[news_item, meta_news])]))])
        self.one("sec", "X-sec-lookalike", extra=[("controlled", dict(label="fixture: META 8-K",
                                                                     filings=sec_staging.filings("meta_8k")))])
        self.one("geopolitical", "X-geo-lookalike", extra=[("controlled", dict(
            label="fixture: BIS release", docs=["bis_final"], clock=GEO_CLOCK))])
        engine = make_engine(DatabaseSettings(url=self.url))
        try:
            with engine.connect() as connection:
                shared_urls = connection.execute(sa.text(
                    "SELECT v.canonical_url, count(DISTINCT e.source_family) AS families, count(DISTINCT e.id) AS events "
                    "FROM events e JOIN event_versions v ON v.event_id = e.id WHERE v.canonical_url IN (:a, :b) "
                    "GROUP BY v.canonical_url"), dict(a=FED_FIXTURE_URL, b=meta_news["link"])).all()
                shared_keys = connection.execute(sa.text(
                    "SELECT count(*) FROM (SELECT event_key FROM events GROUP BY event_key HAVING count(DISTINCT source_family) > 1) t")).scalar_one()
        finally:
            engine.dispose()
        check = audit(self.url)
        self.evidence["cross_family"] = dict(
            shared_urls=[dict(url_kind="fed fixture URL" if r[0] == FED_FIXTURE_URL else "SEC archive URL", families=r[1],
                              events=r[2]) for r in shared_urls],
            event_keys_shared_across_families=shared_keys, integrity_findings=check["integrity_findings"],
            foreign_current_pointer=check["integrity"]["foreign_current_pointer"]["count"])
        if any(r[1] != r[2] for r in shared_urls) or len(shared_urls) != 2 or shared_keys or check["integrity_findings"]:
            raise StagingStop(f"Cross-family identity isolation failed: {self.evidence['cross_family']}")

    def outage(self, clock):
        """Stop PostgreSQL between cycles inside long-lived processes; fresh processes after recovery; no replay."""
        items = dict(fed=lambda n, note: ("controlled", dict(label=f"fixture: {n} ({note})", docs=[n], clock=FED_CLOCK)),
                     sec=lambda n, note: ("controlled", dict(label=f"synthetic {n} ({note})", filings=sec_staging.filings(n))),
                     news=lambda n, note: ("controlled", dict(label=f"synthetic outage item {n} ({note})",
                                                               feeds=news_staging.outage_item(n, datetime.fromisoformat(FED_CLOCK)),
                                                               clock=FED_CLOCK)))
        names = dict(fed=("economic_projections", "minutes", "policy_communication"),
                     sec=("outage_a", "outage_b", "outage_c"), news=("A", "B", "C"))
        on = {f: self.worker(f, f"O-{f}-outage") for f in items}
        off = {f: self.worker(f, f"O-{f}-control", shadow=False, redis_which="control") for f in items}
        live_on = {f: self.worker(f, f"O-{f}-live-outage") for f in ("macro", "treasury", "geopolitical")}
        pairs = {f: [] for f in items}
        for index, note in enumerate(("DB up", "DB stopped", "DB recovered")):
            if note == "DB stopped":
                for w in list(on.values()) + list(live_on.values()):
                    if not w.send("drain_wait")["drained"]:
                        raise StagingStop("Writer did not drain before the planned outage")
                staging.docker("stop", "--time", "5", PG_CONTAINER)
            try:
                for f in items:
                    op, args = items[f](names[f][index], note)
                    pairs[f].append((args["label"], on[f].send(op, **args), off[f].send(op, **args)))
                if note == "DB stopped":
                    for f, w in live_on.items():
                        w.send("live", label=f"live ({note})")
            finally:
                if note == "DB stopped":
                    staging.docker("start", PG_CONTAINER)
                    staging.pg_ready(timeout=60, container=PG_CONTAINER, database=PG_ADMIN_DB)
        results = {}
        for w in list(on.values()) + list(off.values()) + list(live_on.values()):
            results[w.label] = self.finish(w)
        differing = {f: [label for label, a, b in pairs[f] if outcome_view(f, a) != outcome_view(f, b)] for f in items}
        failed = {w.label: results[w.label]["shadow_stats"]["failed"] for w in list(on.values()) + list(live_on.values())}
        stopped_keys = {f: submitted_keys(f, pairs[f][1][1]) for f in items}
        recovered = {}
        for f in items:  # Fresh processes after recovery: stopped-window items again (Redis skips them) + new items.
            new_item = dict(fed=("controlled", dict(label="fixture: symbol mention (after recovery)", docs=["symbol_mention"],
                                                    clock=FED_CLOCK)),
                            sec=("controlled", dict(label="synthetic after recovery", filings=sec_staging.filings("outage_b",
                                                                                                                   "after_recovery_e"))),
                            news=("controlled", dict(label="synthetic after recovery", clock=FED_CLOCK,
                                                     feeds=[dict(label="Google News NVDA", entries=news_staging.outage_item("B",
                                                              datetime.fromisoformat(FED_CLOCK))[0]["entries"]
                                                              + news_staging.outage_item("E", datetime.fromisoformat(FED_CLOCK))[0]["entries"])])))
            recovered[f] = self.one(f, f"R-{f}-after-recovery", extra=[new_item[f]])
        engine = make_engine(DatabaseSettings(url=self.url))
        try:
            with engine.connect() as connection:
                replayed = {f: connection.execute(sa.text("SELECT count(*) FROM events WHERE source_family = :f AND "
                                                          "event_key IN :k").bindparams(sa.bindparam("k", expanding=True)),
                                                  dict(f=f, k=sorted(stopped_keys[f]) or ["-"])).scalar_one() for f in items}
        finally:
            engine.dispose()
        self.evidence["outage"] = dict(collector_differing=differing, failures_counted=failed,
                                       outage_only_items_persisted=replayed,
                                       after_recovery_submissions={f: sum(r.get("submitted", 0) for r in recovered[f]) for f in items})
        if any(differing.values()) or any(replayed.values()) or not all(failed[f"O-{f}-outage"] >= 1 for f in items):
            raise StagingStop(f"PostgreSQL outage was not non-critical or replayed work: {self.evidence['outage']}")

    def redis_loss(self, clock, live):
        """Replace Redis with empty state; fresh processes replay known items; durable identities hold."""
        replace_redis("staging")
        before = counts(self.url)
        replays = dict(
            sec=[("replay", dict(label="replay parity filings after Redis loss", filings=live["sec"]["fetched"]))],
            news=[("replay", dict(label="replay parity items after Redis loss", clock=clock,
                                  feeds=[dict(label=k, entries=v) for k, v in live["news"]["fetched"].items()]))],
            geopolitical=[("controlled", dict(label="fixture: BIS release after Redis loss (durable lookup)", docs=["bis_final"],
                                              clock=GEO_CLOCK))],
            fed=[("controlled", dict(label="fixture: FOMC statement after Redis loss", docs=["policy_statement"], clock=FED_CLOCK))],
            macro=ops("macro"), treasury=ops("treasury"))
        rows = {}
        for family in FAMILIES:
            results = self.one(family, f"L-{family}-after-redis-loss", extra=replays[family])
            rows[family] = dict(ev_row=self.evidence["restart_matrix"][-1],
                                would_send=sum(r.get("would_send", 0) for r in results),
                                ai_attempts=sum(r.get("ai_attempts", 0) for r in results))
        after = counts(self.url)
        self.evidence["redis_loss"] = {f: dict(submissions=rows[f]["ev_row"]["submissions"], added=rows[f]["ev_row"]["added"],
                                               duplicates=rows[f]["ev_row"]["duplicates"],
                                               growth_explained=rows[f]["ev_row"]["growth_explained"],
                                               would_send_repeat=rows[f]["would_send"], ai_attempts_repeat=rows[f]["ai_attempts"])
                                       for f in FAMILIES}
        for family in ("sec", "news", "geopolitical", "fed"):  # Replays of known identities must add no logical events.
            if after[family]["events"] != before[family]["events"]:
                raise StagingStop(f"Redis loss created duplicate durable {family} events")

    def audits(self):
        env = base_env(db_url=self.url, redis_port=REDIS["staging"][1])
        def cli(module, *argv):
            result = subprocess.run([sys.executable, "-m", module, *argv], cwd=staging.ROOT, env=env, capture_output=True,
                                    text=True, timeout=300)
            text = result.stdout + result.stderr
            self.cli_text.append(text)
            if credential_hits(text, self.contact):
                raise StagingStop(f"Credential-like content in {module} output")
            body = result.stdout.strip()
            return result.returncode, json.loads(body) if body.startswith("{") else body
        geo = dict(status=cli("persistence.geopolitical_tools", "status", "--json"),
                   audit=cli("persistence.geopolitical_tools", "audit", "--json"),
                   disclosure=cli("persistence.geopolitical_tools", "disclosure-audit", "--json"),
                   conflicts=cli("persistence.geopolitical_tools", "conflicts", "--json"))
        sec = dict(shared=cli("persistence.sec_audit", "shared-accessions", "--json"), repeats=cli("persistence.sec_audit", "repeats", "--json"))
        news = dict(variants=cli("persistence.news_audit", "url-variants", "--json"), repeats=cli("persistence.news_audit", "repeats", "--json"))
        family = cli("persistence.family_audit", "--json")
        codes = {k: v[0] for group in (geo, sec, news) for k, v in group.items()}
        codes["family"] = family[0]
        geo_audit = geo["audit"][1]
        self.evidence["audits"] = dict(
            exit_codes=codes,
            geopolitical=dict(status_healthy=geo["status"][1].get("healthy"), audit_summary=geo_audit.get("summary"),
                              disclosure_flagged=geo["disclosure"][1].get("flagged_count"),
                              conflicts=len(geo["conflicts"][1].get("conflicts", []))),
            sec=dict(shared_accessions=sec["shared"][1]["shared_accession_count"],
                     shared_synthetic=[g["accession"] for g in sec["shared"][1]["groups"] if g["accession"].startswith("0000000000-")],
                     repeated_events=sec["repeats"][1]["repeated_events"], note=sec["repeats"][1]["note"]),
            news=dict(variant_groups=news["variants"][1]["variant_group_count"],
                      variant_hosts=sorted({g["host"] for g in news["variants"][1]["groups"]}),
                      repeated_events=news["repeats"][1]["repeated_events"], note=news["repeats"][1]["note"]),
            family=dict(integrity_findings=family[1]["integrity_findings"],
                        findings={k: v["count"] for k, v in family[1]["integrity"].items() if v["count"]},
                        pointer_rule=dict((k, family[1]["pointer_rule"][k]) for k in (
                            "multi_version_events", "versions_evaluated", "violation_count")),
                        violations=family[1]["pointer_rule"]["violations"][:10]),
            families=family[1]["families"])
        audits = self.evidence["audits"]
        if any(codes.values()):
            raise StagingStop(f"An audit command failed: {codes}")
        if audits["family"]["integrity_findings"] or audits["family"]["pointer_rule"]["violation_count"] \
                or audits["geopolitical"]["disclosure_flagged"]:
            raise StagingStop(f"Current-pointer or integrity findings: {audits['family']}")

    def finalize(self, staging_redis, control_redis):
        ev = self.evidence
        snapshot = redis_snapshot(staging_redis)
        namespaces = namespace_counts(snapshot)
        known = tuple(REDIS_PREFIX.values())
        ev["redis_namespaces"] = dict(staging=namespaces, unexpected=sorted(n for n in namespaces if not n.startswith(known)),
                                      ttl_ranges={p: _ttl_range(snapshot, p) for p in known})
        if ev["redis_namespaces"]["unexpected"]:
            raise StagingStop(f"Unexpected Redis namespaces: {ev['redis_namespaces']['unexpected']}")
        processes = ev["processes"]
        ev["writer_health"] = {p["label"]: p["shadow_stats"] for p in processes if p.get("shadow_stats")}
        unexplained_failures = [p["label"] for p in processes if p.get("shadow_stats") and p["shadow_stats"]["failed"]
                                and not (p["label"].startswith("O-") and p["label"].endswith(("-outage", "-live-outage")))]
        drops = {p["label"]: p["shadow_stats"]["dropped_queue_full"] for p in processes
                 if p.get("shadow_stats") and p["shadow_stats"]["dropped_queue_full"]}
        ev["writer_summary"] = dict(unexplained_failures=unexplained_failures, queue_full_drops=drops,
                                    queue_full_drops_by_family={f: sum(n for label, n in drops.items()
                                                                       if next(p for p in processes if p["label"] == label)["family"] == f)
                                                                for f in FAMILIES})
        ev["reconciliation"] = dict(
            processes=len([p for p in processes if p.get("reconciliation")]),
            unexplained=sum(len(p["reconciliation"]["integrity_mismatches"]) for p in processes if p.get("reconciliation")),
            explained_outage_lost=sum(len(p["reconciliation"]["explained_outage_lost"]) for p in processes if p.get("reconciliation")),
            explained_queue_full_drop=sum(len(p["reconciliation"]["explained_queue_full_drop"]) for p in processes
                                          if p.get("reconciliation")),
            database_unavailable=[p["label"] for p in processes if (p.get("reconciliation") or {}).get("status") == "database_unavailable"])
        ev["resources"] = {p["label"]: p["resources"] for p in processes if p.get("resources")}
        ev["resources_end"] = dict(postgresql=pg_resources(self.url), redis=redis_resources(staging_redis))
        ev["resource_flags"] = sanity(ev["resources"])
        ev["history_growth_total"] = growth({f: dict.fromkeys(("events", "versions", "provenance", "score_history",
                                                                "decision_history", "ai_history"), 0) for f in FAMILIES},
                                            counts(self.url))
        ev["unexplained_growth"] = self.unexplained_growth
        ev["guards"] = dict(telegram_transport_calls=sum(p.get("telegram_transport_calls") or 0 for p in processes),
                            openai_calls=sum(p.get("openai_calls") or 0 for p in processes),
                            would_send_stub_total=sum(p.get("would_send_total") or 0 for p in processes),
                            ai_stub_attempts_total=sum(p.get("ai_attempts_total") or 0 for p in processes))
        artifacts = {"worker protocol lines": json.dumps([w.results for w in self.workers], default=str),
                     "worker stderr logs": "".join(w.logfile.read_text() for w in self.workers),
                     "audit CLI output": "".join(self.cli_text), "stored rows": news_staging.dump_rows(self.url),
                     "Redis keys and values": json.dumps([redis_snapshot(staging_redis), redis_snapshot(control_redis)]),
                     "evidence": json.dumps(ev, default=str)}
        ev["credential_scan"] = dict(patterns=sorted(news_staging.CREDENTIAL_PATTERNS) + ["canaries", "sec_contact"],
                                     artifacts={n: dict(bytes=len(t), hits=sum(credential_hits(t, self.contact).values()))
                                                for n, t in artifacts.items()})
        capacities = {family: sorted({q["shadow_capacity"] for q in processes if q["family"] == family
                                      and q.get("shadow_capacity")}) for family in FAMILIES}
        ev["writer_capacity"] = dict(observed_by_family=capacities, expected=EXPECTED_CAPACITY)
        problems = acceptance_problems(ev)
        if problems:
            raise StagingStop("Acceptance failed: " + "; ".join(problems))
        ev["acceptance"] = "PASS"


def acceptance_problems(ev):
    """Readiness acceptance over collected evidence (diagnostic classifications never make a problem acceptable)."""
    problems = []
    if ev["unexplained_growth"]:
        problems.append("unexplained durable growth")
    if ev["writer_summary"]["unexplained_failures"]:
        problems.append("unexplained persistence failures")
    # Phase 2S-A: queue-full drops are still classified diagnostically (they explain "not found" rows), but no
    # healthy cycle may drop anything. This runner injects no deliberate overflow, so every drop fails readiness.
    if ev["writer_summary"]["queue_full_drops"]:
        problems.append(f"queue-full drops in healthy cycles: {ev['writer_summary']['queue_full_drops']}")
    wrong = {f: c for f, c in ev["writer_capacity"]["observed_by_family"].items() if c and c != [EXPECTED_CAPACITY[f]]}
    if wrong:
        problems.append(f"unexpected writer capacities: {wrong}")
    if ev["reconciliation"]["unexplained"]:
        problems.append("unexplained reconciliation mismatches")
    if ev["resource_flags"]:
        problems.append("resource sanity flags")
    if ev["guards"]["telegram_transport_calls"] or ev["guards"]["openai_calls"]:
        problems.append("Telegram/OpenAI tripwire")
    if any(a["hits"] for a in ev["credential_scan"]["artifacts"].values()):
        problems.append("credential-like content")
    return problems


def _ttl_range(snapshot, prefix):
    ttls = [v["ttl"] for k, v in snapshot.items() if k.startswith(prefix)]
    return [min(ttls), max(ttls)] if ttls else None


def _http_view(http):
    """Aggregate endpoints to host + first path segment (no document identifiers)."""
    view = {}
    for endpoint, entry in http.items():
        host, _, path = endpoint.partition("/")
        key = f"{host}/{path.split('/')[0]}/*" if path else host
        agg = view.setdefault(key, dict(requests=0, statuses={}, content_types=set()))
        agg["requests"] += entry["requests"]
        for status, n in entry["statuses"].items():
            agg["statuses"][status] = agg["statuses"].get(status, 0) + n
        agg["content_types"].update(entry["content_types"])
    return {k: dict(v, content_types=sorted(v["content_types"])) for k, v in sorted(view.items())}


def _live_view(family, result):
    """Safe live metadata only: counts, statuses, endpoint classes."""
    if family == "news":
        return {f["label"]: dict(http_status=f["http"]["http_status"], content_type=f["http"]["content_type"],
                                 fetched=f["stats"]["fetched"], relevant=f["stats"]["relevant"],
                                 duplicates=f["stats"]["duplicates"], processed=f["stats"]["processed"],
                                 alert_candidates=f["alert_candidates"], submissions=f["submissions"]) for f in result["feeds"]}
    if family == "sec":
        return dict(fetched=result["input_count"], processed=result["processed"], submissions=result["submitted"],
                    forms=sorted({e["form"] for e in result["events"]}), would_send=result["would_send"])
    stats = result.get("stats", {})
    view = {k: stats.get(k) for k in ("fetched", "relevant", "processed", "duplicates", "stale", "future", "missing_date",
                                      "invalid", "fetch_errors", "state_errors", "unresolved", "yield_observations")
            if k in stats}
    view.update(submissions=result.get("submitted"), historical_submissions=result.get("submitted_historical",
                                                                                       result.get("stale_or_undated_submitted")))
    if family == "geopolitical":
        view["per_source"] = result.get("per_source")
    return view


# ------------------------------------------------------------------ report

DECISIONS = [
    ("Repeat AI calls after Redis expiry/loss", "News re-analyzes ALERT candidates once Redis forgets them (Fed/macro/Treasury "
     "cache processed results; SEC has no AI).", "news_ai", "Extra OpenAI cost only; alerts stay correct.", "No"),
    ("Repeat Telegram sends after Redis expiry/loss", "News and SEC re-send ALERT items after 24 h or Redis loss.", "sends",
     "Duplicate notifications after expiry/loss.", "No"),
    ("Durable observation counts", "Only first/last observation times are durable; exact repeat counts are process-local.",
     "repeats", "Audits cannot quantify repeats exactly.", "No"),
    ("News URL query-string canonicalization", "Query/fragment variants are separate durable articles.", "variants",
     "Possible history fragmentation (audited).", "No"),
    ("SEC 10-filing retrieval window", "Only the 10 newest submissions per company are read.", "sec_forms",
     "Busy insider days can hide 8-K/10-Q/10-K filings.", "No"),
    ("SEC freshness/lookback", "No freshness filter; old filings in the window are processed until Redis dedup.", None,
     "Re-processing of old filings after expiry.", "No"),
    ("SEC amendment scoring", "8-K/A and other /A forms score the default (35, IGNORE).", None, "Amendments never alert.", "No"),
    ("SEC User-Agent configuration", "Contact is hard-coded in source; never logged or reported (scanned).", None,
     "Operational/privacy hygiene.", "No"),
    ("Outage-window durable replay/outbox", "Writes lost during a DB outage are not replayed. They persist when the "
     "collector next submits the item: Fed, macro and Treasury re-submit cached results on every re-observation; SEC "
     "and News only after Redis dedup expiry.", "outage", "History gap until re-observation (up to 24 h for SEC/News).",
     "No"),
    ("Single-process test ordering issue", "One-process runs hit a pre-existing interaction in test_geopolitical_identity_stats; "
     "suites run one process per module.", None, "CI must keep per-module processes.", "No"),
    ("Fed AI-quality penalty policy", "The collector's AI quality adjustment can change Fed scores; persisted as computed.",
     None, "Scoring policy question, not persistence.", "No"),
    ("Shadow queue capacity vs Treasury live burst (resolved in Phase 2S-A)", "Writers are bounded and non-blocking. "
     "Treasury's healthy live burst (~115) exceeded the shared 64-slot queue; Phase 2S-A made Treasury's bounded "
     "capacity configurable (TREASURY_PERSISTENCE_QUEUE_SIZE, default 256). Other families keep 64.", "queue_full",
     "Pre-fix: the same 50 Treasury observations were dropped every cycle. Post-fix: see this run.",
     "No (resolved; keep monitoring dropped_queue_full)"),
]


PHASE2S_FINDING = [
    "**Pre-fix evidence (first bounded Phase 2S run, 2026-09-24 16:38 UTC; harness acceptance then classified the "
    "drops as explained):**",
    "",
    "- A healthy live Treasury cycle submitted 115 observations: 37 yield observations and 78 release/auction items,",
    "  105 of them historical.",
    "- The shared shadow queue holds 64. Every Treasury process accepted 65 (64 queued plus 1 in flight) and dropped",
    "  50 as `dropped_queue_full`.",
    "- Submission order is deterministic, so the same tail observations were dropped every cycle and never reached",
    "  durable history.",
    "- Six Treasury processes dropped 300 observations in total. Reconciliation explained 250 of the missing rows as",
    "  queue-full drops; the outage-window process accounted for the rest. Every other family dropped 0.",
    "- Alerts and collector output were unaffected (non-blocking by design).",
    "",
    "**Phase 2S-A corrective action:** Treasury's writer takes a bounded, validated, configurable capacity,",
    "`TREASURY_PERSISTENCE_QUEUE_SIZE` (default 256, range 64..4096; `shared/queue_settings.py`). Every other family",
    "keeps the shared 64. Submission stays non-blocking, overflow is still dropped and counted, and nothing is retried.",
    "Readiness acceptance now requires zero queue-full drops in healthy cycles. The diagnostic classification remains",
    "only to explain missing rows.",
    "",
]


def render(ev):
    def block(value):
        return "```json\n" + json.dumps(value, sort_keys=True, indent=2, default=str) + "\n```"
    loss, outage, audits = ev.get("redis_loss", {}), ev.get("outage", {}), ev["audits"]
    news_live = ev["live"]["news"]
    evidence_notes = dict(
        news_ai=f"after Redis loss: news AI attempts {loss.get('news', {}).get('ai_attempts_repeat')}; stub total "
                f"{ev['guards']['ai_stub_attempts_total']}",
        sends=f"after Redis loss: would-be sends news {loss.get('news', {}).get('would_send_repeat')}, SEC "
              f"{loss.get('sec', {}).get('would_send_repeat')}",
        repeats=f"re-observed events: SEC {audits['sec']['repeated_events']}, News {audits['news']['repeated_events']}",
        variants=f"variant groups: {audits['news']['variant_groups']}",
        sec_forms=f"live SEC forms in window: {ev['live']['sec']['forms']}",
        outage=f"outage-only items persisted afterwards: {outage.get('outage_only_items_persisted')}",
        queue_full=f"queue-full drops by family: {ev['writer_summary']['queue_full_drops_by_family']}; per process: "
                   f"{ev['writer_summary']['queue_full_drops']}")
    rows = [
        ("Families exercised", ", ".join(FAMILIES)),
        ("Migration", f"head `{ev['migration_head']}`; empty → head, downgrade to `{EARLIER}`, re-upgrade; schema matches models"),
        ("ON/OFF parity", ", ".join(f"{f}: {'identical' if p['identical'] else 'DIFFERS'}" + (" (replay)" if 'note' in p else "")
                                    for f, p in ev["parity"].items())),
        ("Bounded soak", f"{ev['soak']['cycles_completed']} cycles × 6 families, fresh process per family per cycle "
                         f"({ev['soak']['label']}); growth explained: {not ev['unexplained_growth']}"),
        ("Restart matrix", f"{len(ev['restart_matrix'])} fresh processes with durable counts before/after; abrupt exits in cycle 2"),
        ("PostgreSQL outage", f"collector behavior unchanged: {not any(outage.get('collector_differing', {}).values())}; "
                              f"failures counted {outage.get('failures_counted')}; outage-only items replayed: "
                              f"{outage.get('outage_only_items_persisted')}"),
        ("Redis loss", "; ".join(f"{f}: +{v['added']['events']} events" for f, v in loss.items())),
        ("Cross-family isolation", f"{ev.get('cross_family', {}).get('shared_urls')}; event keys shared across families: "
                                   f"{ev.get('cross_family', {}).get('event_keys_shared_across_families')}"),
        ("Redis namespaces", f"{sorted(ev['redis_namespaces']['staging'])}; unexpected: {ev['redis_namespaces']['unexpected']}"),
        ("Family audit", f"integrity findings {audits['family']['integrity_findings']}; pointer-rule violations "
                         f"{audits['family']['pointer_rule']['violation_count']} of {audits['family']['pointer_rule']['versions_evaluated']} evaluated"),
        ("Reconciliation", f"{ev['reconciliation']['unexplained']} unexplained; {ev['reconciliation']['explained_outage_lost']} "
                           f"explained (outage window); {ev['reconciliation']['explained_queue_full_drop']} explained "
                           "(writer queue full, counted drops)"),
        ("Writer queue-full drops (finding)", f"{ev['writer_summary']['queue_full_drops_by_family']}"),
        ("Credential scan", f"{sum(a['hits'] for a in ev['credential_scan']['artifacts'].values())} hits (patterns, canaries, SEC contact)"),
        ("Telegram / OpenAI", f"{ev['guards']['telegram_transport_calls']} / {ev['guards']['openai_calls']} real calls; "
                              f"stubs: {ev['guards']['would_send_stub_total']} would-be sends, {ev['guards']['ai_stub_attempts_total']} AI attempts"),
    ]
    lines = ["# Persistence Phase 2S: all-source readiness report", "",
             f"Run: {ev['started_utc']}. Generated by `python -m tests.real_staging_all_sources`. This is a **bounded Phase 2S "
             "validation** (minutes, not days); a future multi-day shared-staging soak is described in "
             "[the soak runbook](persistence-phase2s-soak-runbook.md) and has **not** been performed.", "",
             f"- PostgreSQL: `{ev['postgresql_version']}`", f"- Redis: `{ev['redis_version']}`",
             f"- Migration head: `{ev['migration_head']}`", f"- Acceptance: **{ev['acceptance']}**", "",
             "## Executive summary", "", "| Check | Evidence |", "|---|---|", *[f"| {c} | {v} |" for c, v in rows], ""]
    sections = [("1. Environment", "environment"), ("4. Migration validation", "migration"),
                ("6. Live-source results by family (counts only)", "live"), ("ON/OFF parity", "parity"),
                ("7. Combined cycles", ("soak", "cycle_summaries")), ("8. Restart matrix", "restart_matrix"),
                ("9. PostgreSQL outage", "outage"), ("10. Redis loss", "redis_loss"), ("11. Family isolation", "cross_family"),
                ("12. Redis namespace isolation", "redis_namespaces"),
                ("13-14. Current-pointer integrity and reconciliation", ("reconciliation",)),
                ("15-17. Audits (geopolitical, SEC, News, all-family)", "audits"),
                ("18. Writer/counter health (process-local)", ("writer_summary", "writer_health")),
                ("19. Resource sanity", ("resources_start", "resources_end", "resource_flags", "resources")),
                ("20. History growth", ("history_growth_soak", "history_growth_total", "unexplained_growth")),
                ("21. Credential scan (hit counts only)", "credential_scan"), ("Guards", "guards"),
                ("Process lifecycle", "processes")]
    for title, key in sections:
        value = {k: ev.get(k) for k in key} if isinstance(key, tuple) else ev.get(key)
        lines += [f"## {title}", block(value)]
    lines += ["## Phase 2S finding and Phase 2S-A corrective action", "", *PHASE2S_FINDING,
              f"**Post-fix evidence (this run):** queue-full drops by family "
              f"{ev['writer_summary']['queue_full_drops_by_family']}; writer capacities by family "
              f"{ev.get('writer_capacity', {}).get('observed_by_family')}.", ""]
    lines += ["## 24. Open product decisions (not implemented)", "",
              "| # | Decision | Current behavior | Evidence (this run) | Operational impact | Blocks Phase 3 |",
              "|---|---|---|---|---|---|"]
    lines += [f"| {i} | {name} | {behavior} | {evidence_notes.get(key, 'see earlier phase reports') if key else 'see earlier phase reports'} "
              f"| {impact} | {blocks} |" for i, (name, behavior, key, impact, blocks) in enumerate(DECISIONS, 1)]
    lines += ["", "None of these must be solved before Phase 3; each is an explicit, separately approved product decision.", ""]
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--json-report", help="write raw evidence JSON (counts only)")
    parser.add_argument("--cycles", type=int, default=3, help="minimum soak cycles (default 3)")
    parser.add_argument("--duration-minutes", type=float, help="keep cycling until this much time has passed")
    parser.add_argument("--cycle-seconds", type=int, default=60, help="staging cadence between cycle starts (10..3600)")
    parser.add_argument("--no-faults", action="store_true", help="skip cross-family, outage and Redis-loss scenarios")
    parser.add_argument("--use-existing-containers", action="store_true", help="verify labelled containers instead of creating them")
    args = parser.parse_args(argv)
    if not 1 <= args.cycles <= 10_000 or not 10 <= args.cycle_seconds <= 3600:
        parser.error("cycles must be 1..10000 and cycle-seconds 10..3600")
    deadline = monotonic() + args.duration_minutes * 60 if args.duration_minutes else None
    def stop(signum, frame):
        STOP["requested"] = True  # Finish the current process step, then clean up; never leave workers behind.
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    contact = sec_staging.load_contact()
    run, evidence, error, created = None, None, None, False
    try:
        if not args.use_existing_containers:
            ensure_absent()
            created = True  # From here on, anything named phase2s was created by this invocation.
            start_containers()
        with tempfile.TemporaryDirectory(prefix="mias-phase2s-") as workdir:
            run = Run(workdir, contact, cycles=args.cycles, cycle_seconds=args.cycle_seconds, deadline=deadline,
                      faults=not args.no_faults)
            try:
                evidence = run.run()
            finally:
                for w in run.workers:
                    if w.process.poll() is None:
                        w.process.kill()  # Only this runner's own worker processes.
                        w.process.wait(timeout=10)
    except StagingStop as stop_error:
        error = stop_error
    finally:
        if created:
            removed = remove_containers()
            if evidence is not None:
                evidence["cleanup"] = dict(removed_containers=removed, on="success")
    if error is not None:
        print(f"STAGING STOP: {error}", file=sys.stderr)
        return 1
    text = render(evidence)
    if credential_hits(text, contact):
        print("STAGING STOP: credential-like content would appear in the report", file=sys.stderr)
        return 1
    if args.json_report:
        Path(args.json_report).write_text(json.dumps(evidence, sort_keys=True, indent=2, default=str))
    Path(args.report).write_text(text)
    print(f"acceptance: {evidence['acceptance']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
