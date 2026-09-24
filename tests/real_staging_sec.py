"""Phase 2P real-process SEC operational validation (test utility; never a deployment tool).

Drives ``tests.real_staging_worker --collector sec`` processes (separate OS
processes, real restarts), the separate-process ``persistence.sec_audit`` CLI and
disposable, labelled ``phase2p`` PostgreSQL/Redis containers, then writes a
credential- and contact-free report. Reuses the Phase 2M harness helpers; the
geopolitical/Fed runbook in ``tests.real_staging`` is untouched.

Safety: refuses unless every container carries the ``phase2p`` label with
loopback-only bindings and private storage, both Redis instances are empty, and
the staging database is a new loopback ``mias_test_phase2p*`` database. Never
touches ``mias-redis``, never reads ``.env``, never sends Telegram (recording stub;
the transport is a tripwire), never calls OpenAI (tripwire). The SEC contact in
the configured User-Agent is held in memory only to scan every artifact for it.

    python -m tests.real_staging_sec --report docs/persistence-phase2p-sec-operational-report.md
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from time import monotonic, sleep
from unittest.mock import patch

import redis
import sqlalchemy as sa
from alembic import command

from persistence.config import DatabaseSettings
from persistence.database import make_engine
from tests import real_staging as staging
from tests.real_staging import StagingStop, Worker, base_env, clean, db_counts, event_exists, namespace_counts, redis_snapshot
from tests.test_persistence import migration_config

LABEL = "phase2p"
PG_CONTAINER, PG_PORT, PG_ADMIN_DB = "mias-test-phase2p-postgres", 55432, "mias_test_phase2p"
STAGING_DB = "mias_test_phase2p_sec"
REDIS = dict(staging=("mias-test-phase2p-redis", 56379), control=("mias-test-phase2p-redis-control", 56380))
DEDUP_TTL = 86400
TTL_SLACK = 900  # Seconds a key may age during the run before TTL comparison.

# Clearly labelled synthetic filings: accession prefix 0000000000 (CIK 0 never files), EDGAR-shaped documents.
def _filing(symbol, form, sequence, filed, document):
    return dict(symbol=symbol, form=form, accession_number=f"0000000000-26-{sequence:06d}", filing_date=filed,
                primary_document=document)


SYNTHETIC = dict(
    meta_8k=_filing("META", "8-K", 900101, "2026-09-21", "mias-staging-fixture-8k.htm"),
    meta_10q=_filing("META", "10-Q", 900102, "2026-07-30", "mias-staging-fixture-10q.htm"),
    meta_10k=_filing("META", "10-K", 900103, "2026-01-29", "mias-staging-fixture-10k.htm"),
    nvda_8k=_filing("NVDA", "8-K", 900104, "2026-09-21", "mias-staging-fixture-nvda-8k.htm"),
    meta_form4=_filing("META", "4", 900105, "2026-09-18", "xslF345X05/mias-staging-fixture-form4.xml"),
    meta_npx=_filing("META", "N-PX", 900106, "2026-08-28", "xslN-PX_X01/mias-staging-fixture-npx.xml"),
    meta_8ka=_filing("META", "8-K/A", 900107, "2026-09-22", "mias-staging-fixture-8ka.htm"),
    joint_form4_nvda=_filing("NVDA", "4", 900105, "2026-09-18", "xslF345X05/mias-staging-fixture-form4.xml"),
    outage_d1=_filing("META", "8-K", 900201, "2026-09-23", "mias-staging-fixture-d1.htm"),
    outage_a=_filing("META", "4", 900202, "2026-09-23", "xslF345X05/mias-staging-fixture-a.xml"),
    outage_b=_filing("META", "8-K", 900203, "2026-09-23", "mias-staging-fixture-b.htm"),
    outage_c=_filing("NVDA", "144", 900204, "2026-09-23", "xsl144X01/mias-staging-fixture-c.xml"),
    after_recovery_e=_filing("NVDA", "10-Q", 900205, "2026-09-23", "mias-staging-fixture-e.htm"),
)
BASE_SET = ["meta_8k", "meta_10q", "meta_10k", "nvda_8k", "meta_form4", "meta_npx", "meta_8ka", "joint_form4_nvda"]
PARITY_FIELDS = ("input_count", "processed", "events", "events_digest", "stdout_digest", "stdout_lines",
                 "collector_logs", "would_send", "would_send_digests")


def filings(*names):
    return [dict(SYNTHETIC[name]) for name in names]


# ------------------------------------------------------------------ safety

def load_contact():
    """The configured SEC contact, kept in memory only for scanning (never printed or stored)."""
    with patch("dotenv.load_dotenv"):
        from collector.sec_collector import SEC_HEADERS
    agent = SEC_HEADERS["User-Agent"]
    return tuple(sorted({agent, *(part for part in agent.split() if "@" in part)}))


def contact_hits(text, contact):
    return sum(text.count(token) for token in contact)


def verify_services():
    info = staging.verify_container(PG_CONTAINER, PG_PORT, volume=True, label=LABEL)
    for container, port in REDIS.values():
        staging.verify_container(container, port, volume=False, label=LABEL)
    return info


def redis_client(which):
    return redis.Redis(host="127.0.0.1", port=REDIS[which][1], decode_responses=True, socket_timeout=5)


def database_url(name):
    return staging.database_url(name, prefix="mias_test_phase2p", port=PG_PORT)


def pg_ready():
    staging.pg_ready(container=PG_CONTAINER, database=PG_ADMIN_DB)


# ------------------------------------------------------------------ evidence helpers

def sec_redis(snapshot):
    keys = {k: v for k, v in snapshot.items() if k.startswith("mias:sec:")}
    ttls = [v["ttl"] for v in keys.values()]
    return dict(keys=len(keys), namespaces=namespace_counts(keys), values=sorted({v["value"] for v in keys.values()}),
                ttl_min=min(ttls) if ttls else None, ttl_max=max(ttls) if ttls else None,
                ttl_within_contract=all(DEDUP_TTL - TTL_SLACK <= t <= DEDUP_TTL for t in ttls),
                other_keys=sorted(k for k in snapshot if not k.startswith("mias:sec:")))


def redis_parity(on, off, fingerprints=None):
    """Keys and values identical (optionally restricted); TTLs within the unchanged 24 h contract."""
    select = (lambda s: {k: v["value"] for k, v in s.items() if k.startswith("mias:sec:")
                         and (fingerprints is None or k.rsplit(":", 1)[-1] in fingerprints)})
    return dict(identical_keys_and_values=select(on) == select(off), keys=len(select(on)),
                ttl_within_contract=sec_redis(on)["ttl_within_contract"] and sec_redis(off)["ttl_within_contract"])


def cycle_parity(on, off):
    differing = [field for field in PARITY_FIELDS if on.get(field) != off.get(field)]
    if on.get("fetched") is not None or off.get("fetched") is not None:
        differing += [] if on.get("fetched") == off.get("fetched") else ["fetched"]
    return differing


def http_view(http):
    return {endpoint: dict(requests=e["requests"], statuses=e["statuses"], content_types=e["content_types"])
            for endpoint, e in http.items()}


def sec_db_checks(url):
    """Read-only: every SEC event has exactly one version and a current pointer."""
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with engine.connect() as connection:
            row = connection.execute(sa.text(
                "SELECT count(*) AS events, coalesce(max(n), 0) AS max_versions, coalesce(min(n), 0) AS min_versions, "
                "sum(CASE WHEN current_version_id IS NULL THEN 1 ELSE 0 END) AS without_current FROM ("
                "SELECT e.id, e.current_version_id, count(v.id) AS n FROM events e JOIN event_versions v "
                "ON v.event_id = e.id WHERE e.source_family = 'sec' GROUP BY e.id, e.current_version_id) t")).one()
            return dict(events=row.events, max_versions=row.max_versions, min_versions=row.min_versions,
                        without_current=int(row.without_current or 0))
    finally:
        engine.dispose()


def dump_rows(url):
    engine = make_engine(DatabaseSettings(url=url))
    try:
        with engine.connect() as connection:
            return json.dumps([[dict(r) for r in connection.execute(sa.text(f"SELECT * FROM {table}")).mappings()]
                               for table in ("events", "event_versions", "event_provenance", "event_history")],
                              default=str)
    finally:
        engine.dispose()


def audit_cli(url, *argv):
    env = base_env(db_url=url, redis_port=REDIS["staging"][1])
    result = subprocess.run([sys.executable, "-m", "persistence.sec_audit", *argv, "--json"], cwd=staging.ROOT, env=env,
                            capture_output=True, text=True, timeout=120)
    clean(result.stdout + result.stderr)
    if result.returncode:
        raise StagingStop(f"sec_audit {argv[0]} failed with exit code {result.returncode}")
    return json.loads(result.stdout), result.stdout + result.stderr


def expire_keys(client, fingerprints, timeout=10):
    """Real Redis expiry of selected dedup keys (test-time PEXPIRE; product TTL untouched)."""
    keys = [f"mias:sec:event:{f}" for f in fingerprints]
    before = {key: client.ttl(key) for key in keys}
    for key in keys:
        if not client.pexpire(key, 50):
            raise StagingStop("Expected SEC dedup key missing before expiry")
    deadline = monotonic() + timeout
    while monotonic() < deadline and any(client.exists(key) for key in keys):
        sleep(0.02)
    if any(client.exists(key) for key in keys):
        raise StagingStop("Redis did not expire the selected keys")
    return dict(keys=len(keys), ttl_before=sorted(set(before.values())), exist_after=0)


def replace_redis(which):
    """Redis loss: restart the tmpfs, persistence-free container (all state discarded), then verify it."""
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


# ------------------------------------------------------------------ runbook

def run(workdir, contact):
    evidence = dict(started_utc=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    info = verify_services()
    staging_redis, control_redis = redis_client("staging"), redis_client("control")
    for client in (staging_redis, control_redis):
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
    evidence["redis_version"] = staging_redis.info("server")["redis_version"]
    engine = make_engine(DatabaseSettings(url=url))
    with engine.begin() as connection:
        command.upgrade(migration_config(connection), "head")
        evidence["migration_revision"] = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
    engine.dispose()
    evidence["environment"] = dict(
        postgresql=dict(container=PG_CONTAINER, image_digest=info["Image"][:19] + "…", binding=f"127.0.0.1:{PG_PORT}",
                        storage="private anonymous volume", database=STAGING_DB),
        redis=dict(staging=f"{REDIS['staging'][0]} 127.0.0.1:{REDIS['staging'][1]} tmpfs, RDB/AOF off",
                   control=f"{REDIS['control'][0]} 127.0.0.1:{REDIS['control'][1]} tmpfs, RDB/AOF off"),
        untouched=["mias-redis", "primary local PostgreSQL"], configuration="explicit process environment only; .env never read",
        synthetic_filings="accession prefix 0000000000 and mias-staging-fixture-* documents (cannot match real filings)")

    processes, texts, workers = [], [], []
    evidence["processes"] = processes

    def worker(label, *, shadow=True, db=url, redis_which="staging"):
        env = base_env(db_url=db, redis_port=REDIS[redis_which][1], SEC_PERSISTENCE_SHADOW_ENABLED=shadow)
        w = Worker("sec", env, label, workdir)
        workers.append(w)
        processes.append(dict(label=label, pid=w.pid, shadow=shadow, redis=redis_which,
                              db="staging" if db == url else "unavailable (reserved closed port)",
                              started_utc=datetime.now(timezone.utc).strftime("%H:%M:%S")))
        return w

    def finish(w, *, hard=False):
        result = w.finish(hard=hard)
        entry = next(p for p in processes if p["label"] == w.label)
        entry.update(ended_utc=datetime.now(timezone.utc).strftime("%H:%M:%S"), exit_mode=result["exit_mode"],
                     exit_code=result["exit_code"], cycles=[_cycle(r) for r in w.results[:-1]],
                     shadow_stats=result["shadow_stats"], shadow_timestamps=result["shadow_timestamps"],
                     reconciliation=result["reconciliation"], http=http_view(result["http"]),
                     telegram_transport_calls=result["telegram_calls"], openai_calls=result["openai_calls"],
                     would_send_total=result["would_send_total"], submissions=result["submissions"],
                     warnings=result["warnings"])
        integrity = result["reconciliation"].get("integrity_mismatches") or []
        explained = [item for item in integrity if outage_lost(*item)]
        entry["reconciliation"]["explained_outage_lost"] = explained
        entry["reconciliation"]["integrity_mismatches"] = [item for item in integrity if item not in explained]
        if result["reconciliation"]["status"] == "ok" and entry["reconciliation"]["integrity_mismatches"]:
            raise StagingStop(f"Unexplained reconciliation integrity mismatches in {w.label}")
        return result

    def fingerprints(result):
        return [e["fingerprint"] for e in result["events"]]

    # ---- S1. Persistence ON/OFF parity: live watchlist, synthetic mix, replay within TTL.
    s1, s1c = worker("S1-sec-on"), worker("S1c-sec-off-control", shadow=False, redis_which="control")
    live_on, live_off = s1.send("live", label="live watchlist"), s1c.send("live", label="live watchlist")
    live_filings = live_on["fetched"]
    ops = [("controlled", dict(label="synthetic: form mix (ALERT/DISPLAY_ONLY/IGNORE, joint accession)",
                               filings=filings(*BASE_SET))),
           ("replay", dict(label="replay live filings within TTL", filings=live_filings))]
    pairs = [("live watchlist", live_on, live_off)]
    for op, kwargs in ops:
        pairs.append((kwargs["label"], s1.send(op, **kwargs), s1c.send(op, **kwargs)))
    s1_result, s1c_result = finish(s1), finish(s1c)
    differing = {label: cycle_parity(on, off) for label, on, off in pairs}
    snapshot_on, snapshot_off = redis_snapshot(staging_redis), redis_snapshot(control_redis)
    evidence["parity"] = dict(
        cycles={label: dict(differing=d, processed=on["processed"], would_send=on["would_send"],
                            decisions=_decisions(on)) for (label, on, _), d in zip(pairs, differing.values())},
        http=dict(on=http_view(s1_result["http"]), off=http_view(s1c_result["http"]),
                  identical=http_view(s1_result["http"]) == http_view(s1c_result["http"])),
        redis=redis_parity(snapshot_on, snapshot_off), redis_on=sec_redis(snapshot_on), redis_off=sec_redis(snapshot_off),
        shadow_logs_only_when_on=dict(on=sum(len(c["shadow_logs"]) for _, c, _ in pairs),
                                      off=sum(len(c["shadow_logs"]) for _, _, c in pairs)))
    if any(differing.values()):
        if differing["live watchlist"] == ["fetched"] or "fetched" in differing["live watchlist"]:
            raise StagingStop("Live SEC data changed between the paired reads; rerun the staging run")
        raise StagingStop(f"Collector-visible behavior differs with persistence on: {differing}")
    if not (evidence["parity"]["http"]["identical"] and evidence["parity"]["redis"]["identical_keys_and_values"]
            and evidence["parity"]["redis"]["ttl_within_contract"]):
        raise StagingStop("HTTP or Redis behavior differs with persistence on")
    live_events = live_on["events"]
    evidence["live"] = _live_summary(live_on, s1_result["http"])
    if not live_filings or any(status != "200" for e in s1_result["http"].values() for status in e["statuses"]):
        raise StagingStop("Live SEC read did not succeed")
    if live_on["processed"] != len(live_filings) or s1_result["shadow_stats"]["failed"]:
        raise StagingStop("Live SEC filings did not all process and persist")
    synthetic_on = pairs[1][1]
    evidence["db_after_S1"], evidence["sec_db_after_S1"] = db_counts(url), sec_db_checks(url)
    baseline = evidence["db_after_S1"]

    # ---- S2. Real restart with Redis intact: fresh process, same filings, collector skips, DB unchanged.
    s2 = worker("S2-sec-restart-redis-intact")
    s2_cycles = [s2.send("replay", label="replay live filings", filings=live_filings),
                 s2.send("controlled", label="synthetic form mix again", filings=filings(*BASE_SET))]
    s2_result = finish(s2, hard=True)
    evidence["restart_redis_intact"] = dict(processed=[c["processed"] for c in s2_cycles],
                                            submitted=[c["submitted"] for c in s2_cycles],
                                            shadow=s2_result["shadow_stats"], db=db_counts(url))
    if any(c["processed"] or c["submitted"] for c in s2_cycles) or evidence["restart_redis_intact"]["db"] != baseline:
        raise StagingStop("Restart with Redis intact changed collector or durable state")

    # ---- S3. Real Redis TTL expiry (test-time PEXPIRE on selected keys), then a fresh process.
    expire_set = [live_events[0]["fingerprint"]] + [e["fingerprint"] for e in synthetic_on["events"]
                                                    if e["accession"] in (SYNTHETIC["meta_8k"]["accession_number"],)
                                                    or e["form"] == "4" and e["symbol"] == "META"]
    evidence["ttl_expiry"] = dict(expired=expire_keys(staging_redis, expire_set))
    s3 = worker("S3-sec-after-ttl-expiry")
    s3_cycles = [s3.send("replay", label="replay live filings after expiry", filings=live_filings),
                 s3.send("controlled", label="synthetic form mix after expiry", filings=filings(*BASE_SET))]
    s3_result = finish(s3, hard=True)
    reprocessed = sorted(f for c in s3_cycles for f in fingerprints(c))
    evidence["ttl_expiry"].update(
        reprocessed=len(reprocessed), reprocessed_matches_expired=reprocessed == sorted(expire_set),
        would_send_repeat=sum(c["would_send"] for c in s3_cycles), shadow=s3_result["shadow_stats"],
        db=db_counts(url), redis_after=sec_redis(redis_snapshot(staging_redis)))
    if not evidence["ttl_expiry"]["reprocessed_matches_expired"] or s3_result["shadow_stats"]["duplicate"] != len(expire_set) \
            or evidence["ttl_expiry"]["db"] != baseline or not evidence["ttl_expiry"]["redis_after"]["ttl_within_contract"]:
        raise StagingStop("Redis expiry did not reprocess exactly the expired filings as durable duplicates")

    # ---- S4. Redis loss: the staging Redis instance is replaced (tmpfs restart), then a fresh process.
    evidence["redis_loss"] = dict(replacement=replace_redis("staging"))
    staging_redis = redis_client("staging")
    s4 = worker("S4-sec-after-redis-loss")
    s4_cycles = [s4.send("replay", label="replay live filings after Redis loss", filings=live_filings),
                 s4.send("controlled", label="synthetic form mix after Redis loss", filings=filings(*BASE_SET))]
    s4_result = finish(s4)
    expected = len(live_filings) + len(BASE_SET)
    evidence["redis_loss"].update(
        reprocessed=sum(c["processed"] for c in s4_cycles), expected=expected,
        would_send_repeat=sum(c["would_send"] for c in s4_cycles), shadow=s4_result["shadow_stats"], db=db_counts(url),
        redis_after=sec_redis(redis_snapshot(staging_redis)))
    if evidence["redis_loss"]["reprocessed"] != expected or s4_result["shadow_stats"]["duplicate"] != expected \
            or evidence["redis_loss"]["db"] != baseline:
        raise StagingStop("Redis loss did not reprocess every filing as a durable duplicate")

    # ---- S5. PostgreSQL outage across process boundaries.
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))  # Bound, never listening: cannot be any database.
    down_url = url.replace(f":{PG_PORT}/", f":{holder.getsockname()[1]}/")
    try:
        d1, d1c = worker("D1-sec-db-unavailable-at-start", db=down_url), worker("D1c-sec-control", shadow=False,
                                                                               redis_which="control")
        d1_on = d1.send("controlled", label="synthetic 8-K (DB unavailable)", filings=filings("outage_d1"))
        d1_off = d1c.send("controlled", label="synthetic 8-K (DB unavailable)", filings=filings("outage_d1"))
        d1_result, _ = finish(d1), finish(d1c)
    finally:
        holder.close()
    d1_keys = set(fingerprints(d1_on))
    d2, d2c = worker("D2-sec-midrun-outage"), worker("D2c-sec-control", shadow=False, redis_which="control")
    d2_pairs = []
    for name, note in (("outage_a", "DB up"), ("outage_b", "DB stopped"), ("outage_c", "DB recovered")):
        if name == "outage_b":
            if not d2.send("drain_wait")["drained"]:
                raise StagingStop("Writer did not drain before the planned outage")
            staging.docker("stop", "--time", "5", PG_CONTAINER)
        try:
            label = f"synthetic {SYNTHETIC[name]['form']} ({note})"
            d2_pairs.append((label, d2.send("controlled", label=label, filings=filings(name)),
                             d2c.send("controlled", label=label, filings=filings(name))))
        finally:
            if name == "outage_b":
                staging.docker("start", PG_CONTAINER)
                pg_ready()
    d2_result, _ = finish(d2), finish(d2c)
    d3 = worker("D3-sec-restart-after-recovery")
    d3_cycle = d3.send("controlled", label="outage-window filing again + new filing after recovery",
                       filings=filings("outage_b", "after_recovery_e"))
    d3_result = finish(d3)
    key = {name: fingerprints(pair[1])[0] for name, pair in zip(("outage_a", "outage_b", "outage_c"), d2_pairs)}
    outage_differing = {label: cycle_parity(on, off) for label, on, off in [("D1", d1_on, d1_off)] + d2_pairs}
    evidence["db_outage"] = dict(
        collector_parity_differing=outage_differing,
        redis_parity=redis_parity(redis_snapshot(staging_redis), redis_snapshot(redis_client("control")),
                                  d1_keys | set(key.values())),
        unavailable_at_start=dict(shadow=d1_result["shadow_stats"], reconciliation=d1_result["reconciliation"]["status"]),
        midrun=dict(shadow=d2_result["shadow_stats"], cycles=[dict(label=l, processed=on["processed"], submitted=on["submitted"])
                                                             for l, on, _ in d2_pairs]),
        after_recovery=dict(processed=d3_cycle["processed"], shadow=d3_result["shadow_stats"]),
        durable=dict(d1_filing=any(event_exists(url, k) for k in d1_keys), a_before_outage=event_exists(url, key["outage_a"]),
                     b_during_outage=event_exists(url, key["outage_b"]), c_after_recovery=event_exists(url, key["outage_c"]),
                     e_new_after_restart=event_exists(url, fingerprints(d3_cycle)[0]) if d3_cycle["events"] else False))
    outage = evidence["db_outage"]
    if any(outage_differing.values()) or not outage["redis_parity"]["identical_keys_and_values"]:
        raise StagingStop("DB outage changed collector-visible behavior")
    if d1_result["shadow_stats"]["failed"] < 1 or d2_result["shadow_stats"]["failed"] < 1:
        raise StagingStop("Outage did not register persistence failures")
    if outage["durable"] != dict(d1_filing=False, a_before_outage=True, b_during_outage=False, c_after_recovery=True,
                                 e_new_after_restart=True) or d3_cycle["processed"] != 1:
        raise StagingStop(f"Unexpected durable state around the outage: {outage['durable']}")

    # ---- S6. Read-only audits (separate CLI processes).
    shared, shared_text = audit_cli(url, "shared-accessions")
    repeats, repeats_text = audit_cli(url, "repeats")
    texts += [shared_text, repeats_text]
    joint = SYNTHETIC["joint_form4_nvda"]["accession_number"]
    unexpected = [g["accession"] for g in shared["groups"]
                  if g["accession"] != joint or any(len(m["symbols"]) != 1 for m in g["members"])
                  or len({s for m in g["members"] for s in m["symbols"]}) != g["count"]]
    evidence["shared_accessions"] = dict(count=shared["shared_accession_count"], truncated=shared["truncated"],
                                         groups=[{k: g[k] for k in ("accession", "count", "event_ids", "symbols", "urls",
                                                                    "forms")} for g in shared["groups"]],
                                         expected_synthetic=joint, unexpected=unexpected)
    if unexpected or shared["shared_accession_count"] != 1:
        raise StagingStop(f"Unexpected shared SEC accessions: {unexpected}")
    tally = _observation_tally(processes, workers)
    re_observed = {fp for fp, t in tally.items() if t["durable_observations"] > 1}
    audited = {e["event_key"] for e in repeats["events"]}
    evidence["repeat_processing"] = dict(
        durable_audit=dict(events_scanned=repeats["events_scanned"], repeated_events=repeats["repeated_events"],
                           note=repeats["note"],
                           events=[{k: e[k] for k in ("accession", "event_row_id", "forms", "versions", "first_observed",
                                                      "last_observed", "span_seconds", "observation_count",
                                                      "duplicate_count")} for e in repeats["events"]]),
        process_evidence=[dict(accession=t["accession"], symbol=t["symbol"], form=t["form"],
                               observations=t["observations"], durable_observations=t["durable_observations"],
                               duplicate_observations=max(t["durable_observations"] - 1, 0), processes=t["processes"])
                          for t in sorted(tally.values(), key=lambda t: (-t["observations"], t["accession"], t["symbol"]))],
        re_observed_all_audited=re_observed <= audited, audited_without_evidence=sorted(audited - re_observed))
    if not re_observed <= audited or audited - re_observed:
        raise StagingStop("Repeat-processing audit disagrees with process evidence")

    # ---- S7. Final state, one-version check, reconciliation, contact scan.
    evidence["sec_db_final"], evidence["db_final"] = sec_db_checks(url), db_counts(url)
    if evidence["sec_db_final"]["max_versions"] != 1 or evidence["sec_db_final"]["without_current"]:
        raise StagingStop("An SEC event does not have exactly one current version")
    evidence["redis_final"] = dict(staging=sec_redis(redis_snapshot(staging_redis)),
                                   control=sec_redis(redis_snapshot(redis_client("control"))))
    evidence["reconciliation"] = [dict(process=p["label"], **{k: p["reconciliation"].get(k) for k in (
        "status", "checked", "integrity_mismatches", "explained_outage_lost", "current_pointer_differs")})
        for p in processes]
    guard_total = sum(p["telegram_transport_calls"] + p["openai_calls"] for p in processes)
    evidence["guards"] = dict(telegram_transport_calls=sum(p["telegram_transport_calls"] for p in processes),
                              openai_calls=sum(p["openai_calls"] for p in processes),
                              would_send_stub_total=sum(p["would_send_total"] for p in processes))
    if guard_total:
        raise StagingStop("Telegram/OpenAI tripwire reached")
    artifacts = {"worker protocol lines": json.dumps([w.results for w in workers], default=str),
                 "worker stderr logs": "".join(w.logfile.read_text() for w in workers),
                 "audit CLI output": "".join(texts), "stored rows": dump_rows(url),
                 "Redis keys and values": json.dumps([redis_snapshot(staging_redis), redis_snapshot(redis_client("control"))]),
                 "evidence": json.dumps(evidence, default=str)}
    evidence["contact_scan"] = dict(artifacts={name: dict(bytes=len(text), hits=contact_hits(text, contact))
                                               for name, text in artifacts.items()})
    if any(a["hits"] for a in evidence["contact_scan"]["artifacts"].values()):
        raise StagingStop("SEC contact string found in an operational artifact")
    evidence["acceptance"] = "PASS"
    return evidence


def outage_lost(label, fields):
    """Explained: a submission made while PostgreSQL was deliberately stopped was never written (no replay)."""
    return bool(label) and label.endswith("(DB stopped)") and "event_found" in fields and set(fields) <= {
        "event_found", "version_found", "version_match", "provenance_match", "accession_match", "score_match",
        "decision_match"}


def _decisions(cycle):
    counts = {}
    for event in cycle["events"]:
        counts[event["decision"]] = counts.get(event["decision"], 0) + 1
    return dict(sorted(counts.items()))


def _cycle(result):
    keep = ("op", "label", "input_count", "processed", "submitted", "would_send", "drained")
    summary = {k: result[k] for k in keep if k in result}
    if "events" in result:
        summary["decisions"] = _decisions(result)
    return summary


def _live_summary(live, http):
    by_symbol = {}
    for filing in live["fetched"]:
        entry = by_symbol.setdefault(filing["symbol"], dict(window=0, forms=set(), path_style_documents=0,
                                                            empty_documents=0, representative=[]))
        entry["window"] += 1
        entry["forms"].add(filing["form"])
        entry["path_style_documents"] += "/" in (filing["primary_document"] or "")
        entry["empty_documents"] += not filing["primary_document"]
        if len(entry["representative"]) < 3:
            entry["representative"].append({k: filing[k] for k in ("form", "accession_number", "filing_date")})
    processed = {}
    for event in live["events"]:
        processed.setdefault(event["symbol"], {}).setdefault(event["decision"], 0)
        processed[event["symbol"]][event["decision"]] += 1
    for symbol, entry in by_symbol.items():
        entry["forms"] = sorted(entry["forms"])
        entry["processed"] = processed.get(symbol, {})
        entry["newest_filing_date"] = max(f["filing_date"] for f in live["fetched"] if f["symbol"] == symbol)
    alert_worthy = sum(e["decision"] == "ALERT" for e in live["events"])
    return dict(endpoints=http_view(http), companies=by_symbol, processed=live["processed"],
                accessions_extracted=sum(1 for e in live["events"] if e["accession"]), alert_decisions=alert_worthy,
                note=None if alert_worthy else "no alert-worthy (ALERT) forms in the live window at run time")


def _observation_tally(processes, workers):
    """Per filing (fingerprint): observations submitted by shadow-on processes, and those with the DB reachable."""
    tally = {}
    for entry, w in zip(processes, workers):
        if not entry["shadow"]:
            continue
        for result in w.results[:-1]:
            for event in result.get("events", []):
                t = tally.setdefault(event["fingerprint"], dict(accession=event["accession"], symbol=event["symbol"],
                                                                form=event["form"], observations=0,
                                                                durable_observations=0, processes=[]))
                t["observations"] += 1
                db_down = entry["db"] != "staging" or result.get("label", "").endswith("(DB stopped)")
                t["durable_observations"] += 0 if db_down else 1
                t["processes"].append(entry["label"])
    return tally


# ------------------------------------------------------------------ report

RISKS = [
    "- The collector re-sends `ALERT` filings after Redis TTL expiry or Redis loss (existing, unchanged behavior;",
    "  measured above as would-be repeat sends). Persistence records these as durable duplicates only.",
    "- A filing whose shadow write is lost during a DB outage stays unpersisted until the collector observes it",
    "  again, which Redis dedup prevents for 24 h (no replay by design).",
    "- The exact number of repeat observations is not stored durably (first/last observation times only); per-run",
    "  counts come from process-local counters, which reset on restart.",
    "- One filing under both watchlist issuers is two durable events (collector identity); the audit surfaces it.",
    "- Time-boxed local staging with one live read per paired process; not a multi-day soak.",
]


def summarize(evidence):
    parity, live = evidence["parity"], evidence["live"]
    ttl, loss, outage = evidence["ttl_expiry"], evidence["redis_loss"], evidence["db_outage"]
    mismatches = sum(len(r["integrity_mismatches"] or []) for r in evidence["reconciliation"])
    rows = [
        ("Live SEC read", f"{live['processed']} filings processed from {len(live['companies'])} companies; HTTP "
         + ", ".join(f"`{e}` {', '.join(f'{k}×{v}' for k, v in d['statuses'].items())} {', '.join(d['content_types'])}"
                     for e, d in live["endpoints"].items())),
        ("Path-style primary documents", f"{sum(c['path_style_documents'] for c in live['companies'].values())} of "
         f"{live['processed']} live filings; all accepted and persisted"),
        ("Alert-worthy live forms", str(live["alert_decisions"]) + ("" if live["alert_decisions"] else
                                                                     " (live window held only insider forms; synthetic ALERT forms exercised)")),
        ("Persistence ON/OFF parity", f"{len(parity['cycles'])} paired cycles identical (events, scores, decisions, stdout, "
         f"collector logs, would-be Telegram sends); HTTP identical: {parity['http']['identical']}; Redis keys/values "
         f"identical: {parity['redis']['identical_keys_and_values']}, TTL within 24 h contract: {parity['redis']['ttl_within_contract']}"),
        ("Restart with Redis intact (S2)", f"processed {evidence['restart_redis_intact']['processed']}, durable rows unchanged"),
        ("Redis TTL expiry (S3)", f"{ttl['expired']['keys']} keys really expired; {ttl['reprocessed']} filings reprocessed; "
         f"shadow duplicate={ttl['shadow']['duplicate']}; durable rows unchanged; would-be repeat sends={ttl['would_send_repeat']}"),
        ("Redis loss (S4)", f"instance replaced (0 keys); {loss['reprocessed']} filings reprocessed; shadow duplicate="
         f"{loss['shadow']['duplicate']}; durable rows unchanged; would-be repeat sends={loss['would_send_repeat']}"),
        ("DB unavailable at start (D1)", f"shadow failed={outage['unavailable_at_start']['shadow']['failed']}; collector output "
         "and Redis identical to control"),
        ("DB stopped between cycles, recovered (D2/D3)", f"shadow failed={outage['midrun']['shadow']['failed']}; durable: "
         + ", ".join(f"{k}={v}" for k, v in outage["durable"].items())),
        ("Shared-accession audit", f"{evidence['shared_accessions']['count']} (the labelled synthetic joint filing); "
         f"unexpected: {len(evidence['shared_accessions']['unexpected'])}"),
        ("Repeat-processing audit", f"{evidence['repeat_processing']['durable_audit']['repeated_events']} durable events "
         "re-observed; matches process evidence"),
        ("One version per SEC event", f"{evidence['sec_db_final']['events']} events, max versions "
         f"{evidence['sec_db_final']['max_versions']}, without current {evidence['sec_db_final']['without_current']}"),
        ("Reconciliation integrity mismatches", f"{mismatches} unexplained; "
         f"{sum(len(r.get('explained_outage_lost') or []) for r in evidence['reconciliation'])} explained "
         "(submitted while PostgreSQL was deliberately stopped; never written, not replayed)"),
        ("SEC contact in any artifact", str(sum(a["hits"] for a in evidence["contact_scan"]["artifacts"].values()))),
        ("Telegram transport / OpenAI calls", f"{evidence['guards']['telegram_transport_calls']} / {evidence['guards']['openai_calls']} "
         f"(would-be sends recorded by the stub: {evidence['guards']['would_send_stub_total']})"),
    ]
    lines = ["## Executive summary", "", "| Check | Evidence |", "|---|---|"]
    return lines + [f"| {check} | {value} |" for check, value in rows] + [""]


def render(evidence, contact):
    def block(value):
        return "```json\n" + json.dumps(value, sort_keys=True, indent=2, default=str) + "\n```"
    lines = ["# Persistence Phase 2P: SEC operational validation report", "",
             f"Run: {evidence['started_utc']}. Generated by `python -m tests.real_staging_sec`.",
             "Separate OS processes (`tests.real_staging_worker --collector sec`), real restarts, the live SEC",
             "submissions endpoint plus clearly labelled synthetic filings, disposable PostgreSQL/Redis, separate-process",
             "audit CLI. No Telegram sends and no OpenAI calls. Credential- and contact-free.", "",
             f"- PostgreSQL: `{evidence['postgresql_version']}`", f"- Redis: `{evidence['redis_version']}`",
             f"- Migration revision: `{evidence['migration_revision']}`", f"- Acceptance: **{evidence['acceptance']}**", ""]
    lines += summarize(evidence)
    sections = [("1. Environment", "environment"), ("5. Live SEC source results", "live"),
                ("6. Persistence ON/OFF parity", "parity"), ("7. Real restart (Redis intact)", "restart_redis_intact"),
                ("8. Redis TTL expiry", "ttl_expiry"), ("9. Redis loss", "redis_loss"),
                ("10. PostgreSQL outage and recovery", "db_outage"), ("11. Shared-accession audit", "shared_accessions"),
                ("12. Repeat-processing audit", "repeat_processing"), ("13. Reconciliation (read-only, per process)", "reconciliation"),
                ("14. Contact scan (hit counts only)", "contact_scan"), ("Guards", "guards"),
                ("15. Process lifecycle and counters (process-local)", "processes"),
                ("Durable state", ("db_after_S1", "sec_db_after_S1", "db_final", "sec_db_final", "redis_final"))]
    for title, key in sections:
        value = {k: evidence[k] for k in key} if isinstance(key, tuple) else evidence[key]
        lines += [f"## {title}", block(value)]
    lines += ["## 18. Remaining risks", "", *RISKS, ""]
    text = clean("\n".join(lines) + "\n")
    if contact_hits(text, contact):
        raise StagingStop("SEC contact string would appear in the report")
    return text


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True)
    parser.add_argument("--evidence", help="also write raw evidence JSON here (local scratch)")
    parser.add_argument("--render-from", help="render the report from a saved evidence JSON (no services touched)")
    args = parser.parse_args(argv)
    contact = load_contact()
    if args.render_from:
        evidence = json.loads(Path(args.render_from).read_text())
    else:
        with tempfile.TemporaryDirectory(prefix="mias-phase2p-") as workdir:
            try:
                evidence = run(workdir, contact)
            except StagingStop as error:
                print(f"STAGING STOP: {error}", file=sys.stderr)
                return 1
    try:
        report = render(evidence, contact)
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
