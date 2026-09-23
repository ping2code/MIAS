"""Phase 2M real-process staging validation orchestrator (test utility; never a deployment tool).

Drives ``tests.real_staging_worker`` collector processes (separate OS processes,
real restarts), the separate-process ``persistence.geopolitical_tools`` CLI,
disposable PostgreSQL/Redis containers, and writes a credential-free report.

Safety: refuses unless the PostgreSQL target is a loopback ``mias_test*``
database inside the verified disposable container, both Redis instances are
verified disposable loopback containers and empty at start, and no output
contains credential-like text. Never touches ``mias-redis``, never reads
``.env``, never sends Telegram, never calls OpenAI (worker guards must read 0).

    python -m tests.real_staging --report docs/persistence-phase2m-staging-report.md
"""
import argparse
from datetime import datetime, timezone
import json
import os
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

from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from tests.test_persistence import migration_config

ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
PG_CONTAINER, PG_PORT, PG_ADMIN_DB = "mias-test-phase2m-postgres", 55432, "mias_test_phase2m"
STAGING_DB = "mias_test_phase2m_staging"
REDIS = dict(staging=("mias-test-phase2m-redis", 56379), control=("mias-test-phase2m-redis-control", 56380))
LABEL = "phase2m"
GEO_CLOCK = "2026-09-22T14:00:00+00:00"
FED_CLOCK = "2026-09-17T18:00:00+00:00"
FORBIDDEN = ("postgresql://", "redis://", "password", "secret", "api_key", "token=")
FED_PROCESSED_TTL_MAX = max(86400, 48 * 3600) + 1


class StagingStop(RuntimeError):
    """Acceptance or safety failure: stop and report; nothing is repaired."""


# -------------------------------------------------------------- safety/services

def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise StagingStop("Disposable Docker operation failed")
    return result.stdout


def verify_container(name, port, *, volume):
    info = json.loads(docker("inspect", name))[0]
    if info["Config"]["Labels"].get("mias.disposable-test") != LABEL:
        raise StagingStop(f"{name} is not a {LABEL} disposable container")
    bindings = [b for ports in info["NetworkSettings"]["Ports"].values() for b in (ports or [])]
    if bindings != [{"HostIp": "127.0.0.1", "HostPort": str(port)}]:
        raise StagingStop(f"{name} is not loopback-only on {port}")
    if volume and not (len(info["Mounts"]) == 1 and info["Mounts"][0]["Type"] == "volume"):
        raise StagingStop(f"{name} storage is not a private volume")
    if not volume and "/data" not in (info["HostConfig"].get("Tmpfs") or {}):
        raise StagingStop(f"{name} storage is not tmpfs")
    return info


def database_url(name):
    settings = require_test_database(DatabaseSettings(url=f"postgresql://mias_test_user@127.0.0.1:{PG_PORT}/{name}"))
    if settings.url.host != "127.0.0.1" or not settings.url.database.startswith("mias_test_phase2m"):
        raise StagingStop("Unsafe staging database target")
    return settings.url.render_as_string(hide_password=False)


def pg_ready(timeout=30):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if subprocess.run(["docker", "exec", PG_CONTAINER, "pg_isready", "-U", "mias_test_user", "-d", PG_ADMIN_DB],
                          capture_output=True, timeout=10).returncode == 0:
            return
        sleep(0.2)
    raise StagingStop("Disposable PostgreSQL did not become ready")


def clean(text):
    lowered = text.lower()
    for token in FORBIDDEN:
        if token in lowered:
            raise StagingStop(f"Credential-like content in operational output (pattern {token!r})")
    return text


# ------------------------------------------------------------------ workers

def base_env(*, db_url, redis_port, **switches):
    env = dict(PATH=os.environ.get("PATH", "/usr/bin:/bin"), HOME=os.environ.get("HOME", "/tmp"), LANG="C.UTF-8",
               PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(ROOT), DATABASE_URL=db_url,
               DB_CONNECT_TIMEOUT_SECONDS="2", REDIS_HOST="127.0.0.1", REDIS_PORT=str(redis_port))
    env.update({k: ("true" if v is True else "false" if v is False else str(v)) for k, v in switches.items()})
    return env


class Worker:
    """One real collector OS process driven over stdin/stdout."""

    def __init__(self, collector, env, label, logdir):
        self.label, self.collector = label, collector
        self.logfile = Path(logdir) / f"{label}.stderr.log"
        self.process = subprocess.Popen([PYTHON, "-m", "tests.real_staging_worker", "--collector", collector],
                                        cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=open(self.logfile, "w"), text=True)
        self.pid, self.results = self.process.pid, []

    def send(self, op, **kwargs):
        self.process.stdin.write(json.dumps(dict(op=op, **kwargs)) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise StagingStop(f"Worker {self.label} exited unexpectedly")
        result = json.loads(clean(line))
        if result["telegram_calls"] or result["openai_calls"]:
            raise StagingStop("Telegram/OpenAI guard tripped")
        self.results.append(result)
        return result

    def finish(self, *, hard=False):
        result = self.send("hard_exit" if hard else "exit")
        code = self.process.wait(timeout=60)
        logs = clean(self.logfile.read_text())
        result.update(exit_code=code, exit_mode="hard (os._exit after drain)" if hard else "graceful",
                      stats_lines=[line.split(" ", 3)[3] for line in logs.splitlines() if "event=geopolitical_identity_stats" in line],
                      warnings=sorted({line.split(" ", 3)[3] for line in logs.splitlines() if " WARNING " in line}))
        if code != 0:
            raise StagingStop(f"Worker {self.label} exit code {code}")
        return result


# ------------------------------------------------------------------ evidence

def redis_client(which):
    return redis.Redis(host="127.0.0.1", port=REDIS[which][1], decode_responses=True, socket_timeout=5)


def redis_snapshot(client):
    keys = {}
    for key in client.scan_iter(match="*", count=500):
        keys[key] = dict(type=client.type(key), ttl=client.ttl(key), value=client.get(key) if client.type(key) == "string" else None)
    return keys


def namespace_counts(snapshot):
    counts = {}
    for key in snapshot:
        namespace = re.sub(r":[0-9a-f]{64}$", ":<id>", key)
        counts[namespace] = counts.get(namespace, 0) + 1
    return dict(sorted(counts.items()))


def expire_geo_identity(client):
    keys = [k for pattern in ("mias:geopolitical:alias:*", "mias:geopolitical:policy:*") for k in client.scan_iter(match=pattern)]
    for key in keys:
        client.delete(key)
    return len(keys)


def db_counts(url):
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with engine.connect() as connection:
            rows = connection.execute(sa.text(
                "SELECT source_family, count(*) FROM events GROUP BY source_family ORDER BY 1")).all()
            counts = {family: n for family, n in rows}
            for table in ("event_versions", "event_provenance", "event_history"):
                counts[table] = connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
            if sa.inspect(connection).has_table("geopolitical_anchor_registry"):
                counts["registry_rows"] = connection.execute(sa.text("SELECT count(*) FROM geopolitical_anchor_registry")).scalar_one()
        return counts
    finally:
        engine.dispose()


def first_resolved(processes, label):
    entry = next(p for p in processes if p["label"] == label)
    fixture = next(c for c in entry["cycles"] if c["label"].startswith("fixture: FR companion"))
    return fixture["resolved"][0]


def disclosure_versions(url, key):
    """Read-only: the versions of one geopolitical event with disclosure time and current flag."""
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with engine.connect() as connection:
            rows = connection.execute(sa.text(
                "SELECT v.published_at, v.headline, (e.current_version_id = v.id) AS current "
                "FROM event_versions v JOIN events e ON e.id = v.event_id WHERE e.event_key = :k "
                "ORDER BY v.recorded_at"), {"k": key}).all()
        return [dict(published_at=str(r.published_at), analyzed_headline=r.headline, current=bool(r.current)) for r in rows]
    finally:
        engine.dispose()


def event_exists(url, key):
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with engine.connect() as connection:
            return connection.execute(sa.text("SELECT count(*) FROM events WHERE event_key = :k"), {"k": key}).scalar_one() > 0
    finally:
        engine.dispose()


def cli(url, *argv):
    env = base_env(db_url=url, redis_port=REDIS["staging"][1])
    result = subprocess.run([PYTHON, "-m", "persistence.geopolitical_tools", *argv], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=300)
    clean(result.stdout + result.stderr)
    body = result.stdout.strip()
    parsed = json.loads(body) if body.startswith("{") else body
    return result.returncode, parsed


def fed_redis_check(snapshot):
    fed = {k: v for k, v in snapshot.items() if k.startswith("mias:fed:")}
    unexpected = sorted(namespace for namespace in namespace_counts(fed)
                        if namespace not in ("mias:fed:event:<id>", "mias:fed:processed:<id>"))
    ttls = [v["ttl"] for k, v in fed.items() if k.startswith("mias:fed:processed:")]
    return dict(keys=namespace_counts(fed), delivered_markers=sum(k.startswith("mias:fed:delivered:") for k in fed),
                unexpected_namespaces=unexpected,
                processed_ttl_within_contract=all(0 < t <= FED_PROCESSED_TTL_MAX for t in ttls), processed_keys=len(ttls))


def comparable(snapshot, prefix):
    """Redis state minus random lease tokens and TTL drift (compared separately)."""
    return {k: (v["type"], v["value"] if ":event:" not in k or k.startswith("mias:fed:") else "<lease>")
            for k, v in snapshot.items() if k.startswith(prefix)}


# ------------------------------------------------------------------- runbook

def run(workdir):
    evidence = dict(started_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    info = verify_container(PG_CONTAINER, PG_PORT, volume=True)
    for container, port in REDIS.values():
        verify_container(container, port, volume=False)
    staging, control = redis_client("staging"), redis_client("control")
    for client in (staging, control):
        if client.dbsize():
            raise StagingStop("Disposable Redis must be empty at start")
    admin_url, url = database_url(PG_ADMIN_DB), database_url(STAGING_DB)
    admin = make_engine(DatabaseSettings(url=admin_url))
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if connection.execute(sa.text("SELECT count(*) FROM pg_database WHERE datname = :n"), {"n": STAGING_DB}).scalar_one():
            raise StagingStop("Staging database already exists; refusing to reuse unknown state")
        connection.execute(sa.text(f'CREATE DATABASE "{STAGING_DB}"'))
        evidence["postgresql_version"] = connection.execute(sa.text("SHOW server_version")).scalar_one()
    admin.dispose()
    evidence["redis_version"] = staging.info("server")["redis_version"]
    evidence["environment"] = dict(
        postgresql=dict(container=PG_CONTAINER, image_digest=info["Image"][:19] + "…", binding=f"127.0.0.1:{PG_PORT}",
                        storage="private anonymous volume", database=STAGING_DB),
        redis=dict(staging=f"{REDIS['staging'][0]} 127.0.0.1:{REDIS['staging'][1]} tmpfs, RDB/AOF off",
                   control=f"{REDIS['control'][0]} 127.0.0.1:{REDIS['control'][1]} tmpfs, RDB/AOF off"),
        untouched=["mias-redis"], configuration="explicit process environment only; .env never read")
    engine = make_engine(DatabaseSettings(url=url))
    def migrate(target):
        with engine.begin() as connection:
            command.upgrade(migration_config(connection), target)
    geo_on = dict(GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED=True, GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED=True,
                  GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS=10)
    processes = evidence["processes"] = []
    def worker(collector, label, *, db=url, redis_which="staging", **switches):
        w = Worker(collector, base_env(db_url=db, redis_port=REDIS[redis_which][1], **switches), label, workdir)
        processes.append(dict(label=label, collector=collector, pid=w.pid, redis=redis_which,
                              db="staging" if db == url else "unavailable (reserved closed port)",
                              switches={k: v for k, v in switches.items()}))
        return w
    def record(worker_, result):
        entry = next(p for p in processes if p["label"] == worker_.label)
        entry.update(cycles=[_cycle_summary(r) for r in worker_.results[:-1]], finish=_finish_summary(result))
        return result

    # ---- A. Pre-registry history (schema 0002), live + labelled controlled fixtures.
    migrate("0002_macro_shadow_history")
    a1 = worker("geo", "A1-geo-history", **geo_on)
    a1.send("live", label="live")
    a1.send("controlled", label="fixture: joined BIS+FR action", docs=["bis_final", "FR_WITH_EO"], clock=GEO_CLOCK)
    a1.send("controlled", label="fixture: joined USTR+FR action", docs=["trade", "TRADE_WITH_EO"], clock=GEO_CLOCK)
    record(a1, a1.finish())
    control_geo = worker("geo", "A1c-geo-live-control", redis_which="control")
    control_geo.send("live", label="live (control: shadow/lookup off)")
    record(control_geo, control_geo.finish())
    live_s, live_c = a1.results[0], control_geo.results[0]
    outcome_keys = ("relevant", "processed", "duplicates", "unresolved", "stale", "future", "missing_date", "invalid",
                    "state_errors", "fetch_errors")
    evidence["geo_live_parity"] = dict(
        staging=dict(events=live_s["events"], stats={k: live_s["stats"][k] for k in outcome_keys}),
        control=dict(events=live_c["events"], stats={k: live_c["stats"][k] for k in outcome_keys}),
        identical=(live_s["events"], {k: live_s["stats"][k] for k in outcome_keys})
        == (live_c["events"], {k: live_c["stats"][k] for k in outcome_keys}))
    if not evidence["geo_live_parity"]["identical"]:
        raise StagingStop("Live geopolitical outcomes differ between shadow-on and control processes")
    expire_geo_identity(staging)
    a2 = worker("geo", "A2-geo-history-divergence", **geo_on)
    a2.send("controlled", label="fixture: USTR companion alone after alias expiry (pre-2H divergence)",
            docs=["TRADE_WITH_EO"], clock=GEO_CLOCK)
    record(a2, a2.finish())
    fed_ops = [("live", {}), ("controlled", dict(label="fixture: FOMC statement dry run", docs=["policy_statement"], clock=FED_CLOCK)),
               ("live", {})]
    f1 = worker("fed", "F1-fed-shadow", FED_PERSISTENCE_SHADOW_ENABLED=True)
    f1c = worker("fed", "F1c-fed-control", redis_which="control")
    for op, kwargs in fed_ops:
        f1.send(op, **kwargs)
        f1c.send(op, **kwargs)
    record(f1, f1.finish())
    record(f1c, f1c.finish())
    snap_s, snap_c = redis_snapshot(staging), redis_snapshot(control)
    fed_parity = comparable(snap_s, "mias:fed:") == comparable(snap_c, "mias:fed:")
    evidence["fed_redis_parity_after_F1"] = dict(identical_keys_and_values=fed_parity, staging=fed_redis_check(snap_s),
                                                 control=fed_redis_check(snap_c))
    if not fed_parity or fed_redis_check(snap_s)["delivered_markers"] or fed_redis_check(snap_s)["unexpected_namespaces"]:
        raise StagingStop("Fed Redis state differs from shadow-off control")
    evidence["db_after_history"] = db_counts(url)

    # ---- B. Migrate, status, backfill, conflicts, baseline (separate CLI processes).
    migrate("head")
    code, status = cli(url, "status", "--json")
    evidence["migration_revision"] = status["alembic_revision"]
    if code or not status["revision_current"]:
        raise StagingStop("Status unhealthy after migration")
    _, backfill = cli(url, "backfill", "--json")
    _, rerun = cli(url, "backfill", "--json")
    keys = ("anchors_seen", "inserted", "already_present", "conflicts", "skipped_non_authoritative", "events_scanned")
    evidence["backfill"] = dict(first={k: backfill[k] for k in keys}, rerun={k: rerun[k] for k in keys})
    if rerun["inserted"]:
        raise StagingStop("Backfill not idempotent")
    _, conflicts = cli(url, "conflicts", "--json")
    evidence["conflicts_before"] = [f"{c['anchor_type']}:{c['anchor_value']} ({c['stage']['policy_stage']}, "
                                    f"{1 + len(c['conflicting_policy_ids'])} roots)" for c in conflicts["conflicts"]]
    baseline = str(Path(workdir) / "baseline.json")
    cli(url, "audit", "--save-snapshot", baseline)
    evidence["baseline_audit"] = json.loads(Path(baseline).read_text())["summary"]

    # ---- C. Lookup enabled; real restarts (graceful, hard exit after writes, fresh).
    evidence["expired_identity_keys_before_C"] = expire_geo_identity(staging)
    geo_lookup = dict(geo_on, GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED=True)
    geo_ops = [("live", dict(label="live")),
               ("controlled", dict(label="fixture: FR companion alone after expiry (Redis miss, known root)", docs=["FR_WITH_EO"], clock=GEO_CLOCK)),
               ("controlled", dict(label="fixture: BIS release (Redis hit)", docs=["bis_final"], clock=GEO_CLOCK)),
               ("controlled", dict(label="fixture: new Entity List action (Redis miss, unknown root)", docs=["entity_list"], clock=GEO_CLOCK))]
    before_c = db_counts(url)
    c_runs = {}
    for label, hard in (("C1-geo-lookup", False), ("C2-geo-restart", True), ("C3-geo-restart-after-hard-exit", False)):
        w = worker("geo", label, **geo_lookup)
        for op, kwargs in geo_ops:
            w.send(op, **kwargs)
        c_runs[label] = record(w, w.finish(hard=hard))
        c_runs[label + ":db"] = db_counts(url)
    evidence["db_before_C"], evidence["db_after_C"] = before_c, db_counts(url)
    versions = disclosure_versions(url, first_resolved(processes, "C1-geo-lookup"))
    earliest = min(versions, key=lambda v: v["published_at"])
    evidence["disclosure_promotion_check"] = dict(versions=versions, earliest_is_current=earliest["current"])
    if not earliest["current"]:  # Phase 2N acceptance: a later disclosure-only companion never replaces current.
        raise StagingStop("Disclosure-only companion replaced the earliest-disclosure current version")
    first = c_runs["C1-geo-lookup"]["durable_counters"]
    expected = dict(lookup_attempted=2, lookup_hit=1, lookup_miss=1, lookup_timeout=0, lookup_error=0, lookup_conflict=0,
                    redis_hit_bypass=1)
    if {k: first[k] for k in expected} != expected:
        raise StagingStop(f"Unexpected healthy lookup counters: {first}")
    for label in ("C2-geo-restart", "C3-geo-restart-after-hard-exit"):
        counters = c_runs[label]["durable_counters"]
        if counters["lookup_attempted"] or counters["redis_hit_bypass"] != 3:
            raise StagingStop("Restarted process consulted PostgreSQL on the Redis-hit path")
    if c_runs["C1-geo-lookup:db"]["geopolitical"] != c_runs["C3-geo-restart-after-hard-exit:db"]["geopolitical"]:
        raise StagingStop("Restart created new durable geopolitical rows")
    fed_before = db_counts(url)
    f_runs = {}
    for label, hard in (("F2-fed-restart", False), ("F3-fed-restart-hard-exit", True), ("F4-fed-restart-after-hard-exit", False)):
        w = worker("fed", label, FED_PERSISTENCE_SHADOW_ENABLED=True)
        for op, kwargs in fed_ops:
            w.send(op, **kwargs)
        f_runs[label] = record(w, w.finish(hard=hard))
    evidence["fed_restart_db"] = dict(before=fed_before.get("fed"), after=db_counts(url).get("fed"))
    if evidence["fed_restart_db"]["before"] != evidence["fed_restart_db"]["after"]:
        raise StagingStop("Fed restart created duplicate durable rows")
    evidence["redis_after_restarts"] = fed_redis_check(redis_snapshot(staging))

    # ---- D. Failure injection across process boundaries.
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))  # Bound, never listening: cannot be any database.
    down_url = database_url(STAGING_DB).replace(f":{PG_PORT}/", f":{holder.getsockname()[1]}/")
    try:
        d1g = worker("geo", "D1-geo-db-unavailable-at-start", db=down_url, **geo_lookup)
        d1g.send("live", label="live")
        d1g.send("controlled", label="fixture: new sanctions action (Redis miss, DB down)", docs=["sanctions"], clock=GEO_CLOCK)
        d1g_result = record(d1g, d1g.finish())
        d1f = worker("fed", "D1-fed-db-unavailable-at-start", db=down_url, FED_PERSISTENCE_SHADOW_ENABLED=True)
        d1fc = worker("fed", "D1c-fed-control", redis_which="control")
        for w in (d1f, d1fc):
            w.send("live")
            w.send("controlled", label="fixture: new economic projections (DB down)", docs=["economic_projections"], clock=FED_CLOCK)
        d1f_result, _ = record(d1f, d1f.finish()), record(d1fc, d1fc.finish())
    finally:
        holder.close()
    sanctions_key = d1g.results[1]["resolved"][0]
    projections_key = d1f.results[1]["fingerprints"][0] if d1f.results[1]["fingerprints"] else None
    parity_d1 = comparable(redis_snapshot(staging), "mias:fed:") == comparable(redis_snapshot(control), "mias:fed:")
    evidence["db_unavailable_at_start"] = dict(
        geo=dict(counters=d1g_result["durable_counters"], shadow=d1g_result["shadow_stats"],
                 reconciliation=d1g_result["reconciliation"]["status"], collector=d1g.results[1]["stats"]),
        fed=dict(shadow=d1f_result["shadow_stats"], collector=d1f.results[1]["stats"],
                 redis_identical_to_control=parity_d1))
    if d1g_result["durable_counters"]["lookup_error"] < 1 or d1g_result["shadow_stats"]["failed"] < 1 or not parity_d1:
        raise StagingStop("DB-unavailable run did not fail safe")
    # Mid-run outage inside long-lived processes, then recovery.
    d2g = worker("geo", "D2-geo-midrun-outage", **geo_lookup)
    d2f = worker("fed", "D2-fed-midrun-outage", FED_PERSISTENCE_SHADOW_ENABLED=True)
    d2g.send("controlled", label="fixture: MOEA disruption (DB up)", docs=["moea_disruption"], clock=GEO_CLOCK)
    d2f.send("controlled", label="fixture: minutes (DB up)", docs=["minutes"], clock=FED_CLOCK)
    for w in (d2g, d2f):  # "Between cycles": previous cycle's shadow work is durably written first.
        if not w.send("drain_wait")["drained"]:
            raise StagingStop("Writer did not drain before the planned outage")
    docker("stop", "--time", "5", PG_CONTAINER)
    try:
        d2g.send("controlled", label="fixture: FTC remedy (DB stopped)", docs=["platform_remedy"], clock=GEO_CLOCK)
        d2f.send("controlled", label="fixture: policy action (DB stopped)", docs=["policy_action"], clock=FED_CLOCK)
    finally:
        docker("start", PG_CONTAINER)
        pg_ready()
    d2g.send("controlled", label="fixture: FTC remedy repeat poll (DB recovered)", docs=["platform_remedy"], clock=GEO_CLOCK)
    d2f.send("controlled", label="fixture: policy action repeat poll (DB recovered)", docs=["policy_action"], clock=FED_CLOCK)
    d2g_result, d2f_result = record(d2g, d2g.finish()), record(d2f, d2f.finish())
    evidence["midrun_outage"] = dict(geo=dict(shadow=d2g_result["shadow_stats"], counters=d2g_result["durable_counters"],
                                              reconciliation=d2g_result["reconciliation"]),
                                     fed=dict(shadow=d2f_result["shadow_stats"], reconciliation=d2f_result["reconciliation"]))
    if d2g_result["shadow_stats"]["failed"] < 1 or d2f_result["shadow_stats"]["failed"] < 1:
        raise StagingStop("Mid-run outage did not register persistence failures")
    # Restart after recovery; outage-window-only work is not replayed.
    d3g = worker("geo", "D3-geo-restart-after-recovery", **geo_lookup)
    d3g.send("live", label="live")
    d3g.send("controlled", label="fixture: BIS release (Redis hit)", docs=["bis_final"], clock=GEO_CLOCK)
    d3f = worker("fed", "D3-fed-restart-after-recovery", FED_PERSISTENCE_SHADOW_ENABLED=True)
    d3f.send("live")
    d3g_result, d3f_result = record(d3g, d3g.finish()), record(d3f, d3f.finish())
    evidence["no_replay"] = dict(geo_sanctions_persisted=event_exists(url, sanctions_key),
                                 fed_projections_persisted=bool(projections_key) and event_exists(url, projections_key))
    if any(evidence["no_replay"].values()):
        raise StagingStop("Outage-window-only work appeared in PostgreSQL (unexpected replay)")

    # ---- E. Post-run audit, conflicts, final Redis/DB evidence.
    code, comparison = cli(url, "audit", "--json", "--compare-to", baseline)
    evidence["post_run_audit"] = dict(exit_code=code, **{k: comparison[k] for k in (
        "historical_groups", "new_exact_authoritative_anchor", "new_by_classification", "inconclusive", "accepted")})
    if code or not comparison["accepted"]:
        raise StagingStop("NEW exact_authoritative_anchor divergence after staging run")
    _, conflicts = cli(url, "conflicts", "--json")
    evidence["conflicts_after"] = [f"{c['anchor_type']}:{c['anchor_value']} ({c['stage']['policy_stage']}, "
                                   f"{1 + len(c['conflicting_policy_ids'])} roots)" for c in conflicts["conflicts"]]
    if evidence["conflicts_after"] != evidence["conflicts_before"]:
        raise StagingStop("New registry conflicts appeared")
    _, final_status = cli(url, "status", "--json")
    evidence["final_status"] = {k: final_status[k] for k in ("healthy", "alembic_revision", "registry", "audit")}
    final = redis_snapshot(staging)
    evidence["redis_final"] = dict(namespaces=namespace_counts(final), fed=fed_redis_check(final))
    evidence["db_final"] = db_counts(url)
    integrity = {p["label"]: p["finish"]["reconciliation"].get("integrity_mismatches") for p in processes
                 if p["finish"]["reconciliation"]["status"] == "ok" and p["finish"]["reconciliation"]["integrity_mismatches"]}
    if integrity:
        raise StagingStop(f"Reconciliation integrity mismatches: {sorted(integrity)}")
    evidence["reconciliation_summary"] = [dict(process=p["label"], **{k: p["finish"]["reconciliation"].get(k) for k in (
        "status", "checked", "integrity_mismatches", "current_pointer_differs", "current_pointer_differs_labels")})
        for p in processes]
    evidence["acceptance"] = "PASS"
    engine.dispose()
    return evidence


def _cycle_summary(result):
    keep = ("op", "label", "stats", "per_source", "feed_entries", "processed_events", "submitted", "submitted_current",
            "stale_or_undated_submitted", "undated_submitted", "durable_counters", "resolved", "drained")
    return {k: result[k] for k in keep if k in result}


def _finish_summary(result):
    keep = ("exit_mode", "exit_code", "shadow_stopped", "shadow_stats", "durable_counters", "reconciliation", "http",
            "telegram_calls", "openai_calls", "submissions", "stats_lines", "warnings")
    return {k: result[k] for k in keep if k in result}


def summarize(evidence):
    """Generated executive summary: every value below is read from the run's evidence."""
    procs = {p["label"]: p for p in evidence["processes"]}
    geo_live = next(c for c in procs["A1-geo-history"]["cycles"] if c["op"] == "live")
    fed_live = next(c for c in procs["F1-fed-shadow"]["cycles"] if c["op"] == "live")
    http = {}
    for p in evidence["processes"]:
        for endpoint, record in p["finish"].get("http", {}).items():
            entry = http.setdefault(endpoint, dict(requests=0, statuses={}, content_types=set()))
            entry["requests"] += record["requests"]
            for status, n in record["statuses"].items():
                entry["statuses"][status] = entry["statuses"].get(status, 0) + n
            entry["content_types"].update(record["content_types"])
    c1 = procs["C1-geo-lookup"]["finish"]["durable_counters"]
    guards = sum(p["finish"]["telegram_calls"] + p["finish"]["openai_calls"] for p in evidence["processes"])
    rows = [
        ("Live geopolitical cycle (FR, FR public inspection, FTC, MOEA)",
         f"{geo_live['stats']['fetched']} documents fetched, {geo_live['stats']['relevant']} relevant, "
         f"{geo_live['stats']['fetch_errors']} fetch errors; identical outcome in shadow-on and control processes"),
        ("Live Fed feed", f"{fed_live['feed_entries']} feed entries, {fed_live['stats']['fetched']} evaluated "
         f"(RSS_ENTRY_LIMIT), {fed_live['stats']['relevant']} relevant, {fed_live['stats']['processed']} fresh; "
         f"{fed_live['submitted']} stale entries shadowed non-current"),
        ("Real process restarts", f"{len(evidence['processes'])} collector processes; graceful and hard (os._exit) exits; "
         "restarts deduplicated from PostgreSQL only"),
        ("Geopolitical lookup, healthy (C1)", ", ".join(f"{k}={c1[k]}" for k in (
            "lookup_attempted", "lookup_hit", "lookup_miss", "lookup_timeout", "lookup_error", "lookup_conflict", "redis_hit_bypass"))),
        ("Redis-hit path after restarts (C2/C3)", "lookup_attempted=0, redis_hit_bypass=3 in each process"),
        ("New exact_authoritative_anchor groups", str(evidence["post_run_audit"]["new_exact_authoritative_anchor"])),
        ("Registry conflicts before/after", f"{evidence['conflicts_before']} / {evidence['conflicts_after']}"),
        ("Fed Redis vs shadow-off control", f"identical keys and values: {evidence['fed_redis_parity_after_F1']['identical_keys_and_values']}; "
         f"delivered markers: {evidence['redis_final']['fed']['delivered_markers']}; processed TTL within contract: "
         f"{evidence['redis_final']['fed']['processed_ttl_within_contract']}"),
        ("Fed durable rows across 3 restarts", f"{evidence['fed_restart_db']['before']} -> {evidence['fed_restart_db']['after']}"),
        ("DB unavailable at start", f"geo lookup_error={evidence['db_unavailable_at_start']['geo']['counters']['lookup_error']}, "
         f"geo shadow failed={evidence['db_unavailable_at_start']['geo']['shadow']['failed']}, fed shadow failed="
         f"{evidence['db_unavailable_at_start']['fed']['shadow']['failed']}, Fed Redis identical to control: "
         f"{evidence['db_unavailable_at_start']['fed']['redis_identical_to_control']}"),
        ("Mid-run outage (between cycles) and recovery", f"geo failed={evidence['midrun_outage']['geo']['shadow']['failed']}, "
         f"fed failed={evidence['midrun_outage']['fed']['shadow']['failed']}; repeat poll after recovery persisted; "
         f"outage-only work replayed: {any(evidence['no_replay'].values())}"),
        ("Reconciliation integrity mismatches", str(sum(len(r["integrity_mismatches"] or []) for r in evidence["reconciliation_summary"]))),
        ("Telegram sends / OpenAI calls (guards, all processes)", str(guards)),
    ]
    lines = ["## Executive summary", "", "| Check | Evidence |", "|---|---|"]
    lines += [f"| {check} | {value} |" for check, value in rows]
    lines += ["", "Live official endpoints contacted (aggregated over all processes; no bodies stored):", "",
              "| Endpoint | Requests | HTTP status | Content type |", "|---|---:|---|---|"]
    lines += [f"| `{endpoint}` | {e['requests']} | {', '.join(f'{k}×{v}' for k, v in sorted(e['statuses'].items()))} | "
              f"{', '.join(sorted(e['content_types']))} |" for endpoint, e in sorted(http.items())]
    check = evidence["disclosure_promotion_check"]
    lines += ["", "### Disclosure-only promotion check (Phase 2N acceptance)", "",
              "After alias/policy expiry, the durable hit reuses the existing event and the companion arrives with a",
              "later disclosure. Under the Phase 2N rule a disclosure-only observation is retained in history but",
              f"never replaces current. Earliest-disclosure version is current: **{check['earliest_is_current']}**.", ""]
    lines += [f"- `{v['published_at']}` current={v['current']}" for v in check["versions"]]
    if evidence.get("title"):  # Later reruns (e.g. Phase 2N): the Phase 2M harness notes do not apply.
        return lines + [""]
    lines += ["", "### Harness corrections made during this phase (not product changes)", "",
              "- Controlled Fed fixtures originally used `monetary20260916a`-style URLs that exactly matched the real",
              "  2026-09-16 FOMC statement (same collector fingerprint). Persistence held the synthetic version as",
              "  `ambiguous` and kept the real one current; fixtures now use `mias-staging-fixture-*` URLs.",
              "- Fixture modules clear `os.environ` while importing; they are now imported before any writer thread",
              "  starts (a mid-run import had made a DB-down writer report configuration failures).",
              "- The planned mid-run outage now stops PostgreSQL only after the writer drained (between cycles).", ""]
    return lines


def render(evidence):
    def block(value):
        return "```json\n" + json.dumps(value, sort_keys=True, indent=2, default=str) + "\n```"
    lines = [f"# {evidence.get('title', 'Persistence Phase 2M: real-process staging report')}", "",
             f"Run: {evidence['started_utc']}. Generated by `python -m tests.real_staging`.",
             "Separate OS processes (`tests.real_staging_worker`), real restarts, live official sources plus",
             "clearly labelled controlled fixtures, disposable PostgreSQL/Redis, separate-process CLI.",
             "No Telegram sends and no OpenAI calls (worker guards count 0 in every process). Credential-free.", "",
             f"- PostgreSQL: `{evidence['postgresql_version']}`", f"- Redis: `{evidence['redis_version']}`",
             f"- Migration revision: `{evidence['migration_revision']}`", f"- Acceptance: **{evidence['acceptance']}**", ""]
    lines += summarize(evidence)
    lines += ["## Raw evidence", "",
              "Rerun: start the three labelled `phase2m` containers (see Phase 2M notes in docs), then",
              "`python -m tests.real_staging --report <file>`. The orchestrator refuses unsafe targets.", ""]
    sections = [("Environment", "environment"), ("Geopolitical live parity (staging vs control Redis)", "geo_live_parity"),
                ("Fed Redis parity after first processes (staging vs shadow-off control)", "fed_redis_parity_after_F1"),
                ("DB after pre-registry history", "db_after_history"), ("Backfill", "backfill"),
                ("Conflicts before enablement", "conflicts_before"), ("Baseline audit", "baseline_audit"),
                ("Identity keys expired before lookup enablement (TTL simulation)", "expired_identity_keys_before_C"),
                ("DB before/after lookup-enabled restarts", ("db_before_C", "db_after_C")),
                ("Fed restart durability", "fed_restart_db"), ("Redis after restarts (Fed)", "redis_after_restarts"),
                ("DB unavailable at process start", "db_unavailable_at_start"), ("Mid-run PostgreSQL outage and recovery", "midrun_outage"),
                ("No replay of outage-window-only work", "no_replay"), ("Post-run audit comparison", "post_run_audit"),
                ("Conflicts after", "conflicts_after"), ("Final tool status", "final_status"),
                ("Final Redis state", "redis_final"), ("Final DB counts", "db_final"),
                ("Reconciliation summary (read-only, per process)", "reconciliation_summary"),
                ("Disclosure-only promotion check (Phase 2N)", "disclosure_promotion_check"),
                ("Process lifecycle and per-process evidence", "processes")]
    for title, key in sections:
        value = {k: evidence[k] for k in key} if isinstance(key, tuple) else evidence[key]
        lines += [f"## {title}", block(value)]
    return clean("\n".join(lines) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--evidence", help="also write raw evidence JSON here (local scratch)")
    parser.add_argument("--render-from", help="render the report from a saved evidence JSON (no services touched)")
    parser.add_argument("--title", help="report title (default: Phase 2M report title)")
    args = parser.parse_args(argv)
    if args.render_from:
        evidence = json.loads(Path(args.render_from).read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="mias-phase2m-") as workdir:
            try:
                evidence = run(workdir)
            except StagingStop as error:
                print(f"STAGING STOP: {error}", file=sys.stderr)
                return 1
    if args.title:
        evidence["title"] = args.title
    if args.evidence and not args.render_from:
        Path(args.evidence).write_text(json.dumps(evidence, sort_keys=True, indent=2, default=str))
    try:
        Path(args.report).write_text(render(evidence))
    except StagingStop as error:
        print(f"STAGING STOP: {error}", file=sys.stderr)
        return 1
    print(f"acceptance: {evidence['acceptance']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
