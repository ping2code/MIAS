"""Phase 2J staging-style rollout validation (test utility; never a deployment tool).

Executes the Phase 2I runbook end to end against a disposable, test-only
PostgreSQL database and an isolated Redis (a disposable loopback Redis, or the
in-memory test double), using the real collector code paths: Redis-first
resolver, geopolitical shadow writer, bounded durable lookup, registry, audit
and CLI. Only HTTP, OpenAI, Telegram and the clock are stubbed; documents are
the synthetic Phase 2F corpus. Explicit process environment only; ``.env`` is
never read (``dotenv.load_dotenv`` is disabled before any config import).

    TEST_DATABASE_URL=postgresql://mias_test_user@127.0.0.1:55432/mias_test_phase2j \\
    MIAS_PHASE2J_REDIS_URL=redis://127.0.0.1:56379/0 \\
    python -m tests.staging_rollout --report docs/persistence-phase2j-staging-report.md

Collector "restarts" between phases are simulated by draining and resetting the
process-local shadow writer and durable-lookup singletons, so counters reset
exactly as they would across process restarts.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from datetime import datetime
import io
import json
import logging
import os
import socket
import sys
import tempfile
from unittest.mock import patch

with patch("dotenv.load_dotenv"):
    from tests import geopolitical_readiness_corpus as corpus
    from tests.test_geopolitical_pipeline import NOW, MemoryRedis, geo, sources

import sqlalchemy as sa
from alembic import command

from persistence import geopolitical_durable_identity as durable_identity
from persistence import geopolitical_shadow as shadow
from persistence import geopolitical_tools as tools
from persistence.config import DatabaseSettings, require_test_database
from persistence.database import make_engine
from tests.test_persistence import migration_config

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
FR_WITH_EO = dict(corpus.DOCS["fr_companion"], identity_anchors=["eo:99980"])
TRADE_WITH_EO = dict(corpus.DOCS["earlier_companion_disclosure"], identity_anchors=["eo:99990"])
LOGGERS = ("geopolitical_identity_stats", "geopolitical_durable_identity", "geopolitical_collector", "geopolitical_shadow")
FORBIDDEN = ("postgresql://", "redis://", "password", "secret", "mias_test_user@")


class StagingStop(RuntimeError):
    """Acceptance or safety failure: stop, report, never repair."""


# ------------------------------------------------------------------ safety

def verify_database_url(url):
    settings = require_test_database(DatabaseSettings(url=url))  # mias_test* only.
    if settings.url.host not in LOOPBACK:
        raise StagingStop("Staging database must be loopback-only")
    return settings


class MemoryRedisAdapter:
    """Uniform expiry/inspection helpers over the in-memory double or a real client."""

    def __init__(self, client):
        self.client = client

    def keys(self, pattern):
        if hasattr(self.client, "data"):
            prefix = pattern.rstrip("*")
            return sorted(k for k in self.client.data if k.startswith(prefix))
        return sorted(self.client.scan_iter(match=pattern, count=500))

    def delete(self, keys):
        for key in keys:
            if hasattr(self.client, "data"):
                self.client.data.pop(key, None)
            else:
                self.client.delete(key)

    def size(self):
        return len(self.client.data) if hasattr(self.client, "data") else self.client.dbsize()


def verify_redis(client, url=None):
    if url is not None:
        from urllib.parse import urlsplit
        if urlsplit(url).hostname not in LOOPBACK:
            raise StagingStop("Staging Redis must be loopback-only")
    if MemoryRedisAdapter(client).size() != 0:
        raise StagingStop("Staging Redis must be empty at start (refusing to touch unknown state)")


def expire_identity_state(client, mode="aliases"):
    """Simulate Redis TTL expiry of alias/policy (or all geopolitical) keys."""
    adapter = MemoryRedisAdapter(client)
    patterns = ("mias:geopolitical:*",) if mode == "all" else ("mias:geopolitical:alias:*", "mias:geopolitical:policy:*")
    keys = [k for p in patterns for k in adapter.keys(p)]
    adapter.delete(keys)
    return len(keys)


def reserved_unreachable_url(url):
    """A loopback port that is bound but never listening: cannot be any database."""
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    return holder, DatabaseSettings(url=url).url.set(host="127.0.0.1", port=holder.getsockname()[1]).render_as_string(hide_password=False)


# ------------------------------------------------------------ collector run

def restart_collector():
    """Drain and reset process-local singletons, exactly like a process restart."""
    result = shadow.shutdown(drain=True, timeout=15)
    drained = counters()  # After the writer drained: includes its registry writes.
    shadow._after_fork()
    durable_identity._close()
    durable_identity._reset()
    return dict(stopped=result["stopped"], persisted=result["stats"]["persisted"], failed=result["stats"]["failed"],
                counters=drained)


@contextmanager
def captured_logs():
    stream, handler = io.StringIO(), logging.StreamHandler()
    handler.setStream(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    loggers = [logging.getLogger(name) for name in LOGGERS]
    for logger in loggers:
        logger.addHandler(handler)
    try:
        yield stream
    finally:
        for logger in loggers:
            logger.removeHandler(handler)


def run_collector(docs, redis_client, settings, database_url):
    """One real collector cycle with explicit settings (the process environment of that run)."""
    env = {"DATABASE_URL": database_url, "DB_CONNECT_TIMEOUT_SECONDS": "2"}
    resolver_calls, resolved = [], []
    real_resolve = geo.resolve_identity
    def spy(*args, **kwargs):
        resolver_calls.append(kwargs.get("durable") is not None)
        event = real_resolve(*args, **kwargs)
        resolved.append(event.get("event_id"))
        return event
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env))
        stack.enter_context(patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP forbidden")))
        for name, value in settings.items():
            stack.enter_context(patch.object(geo, name, value))
        stack.enter_context(patch.object(geo.deduplicator, "redis_client", redis_client))
        stack.enter_context(patch.object(sources, "SOURCES", {"staging": "synthetic"}))
        stack.enter_context(patch.object(sources, "fetch_documents", side_effect=lambda _: deepcopy(docs)))
        stack.enter_context(patch.object(geo, "analyze_geopolitical_event", side_effect=corpus.enrich))
        send = stack.enter_context(patch.object(geo, "deliver_geopolitical_alert",
                                                return_value={"ok": True, "result": {"message_id": 1}}))
        stack.enter_context(patch.object(geo, "resolve_identity", side_effect=spy))
        clock = stack.enter_context(patch.object(geo, "datetime"))
        clock.now.side_effect = lambda *a: NOW
        clock.fromisoformat = datetime.fromisoformat
        events, stats = geo.collect_geopolitical_events(enable_ai=True, send_alerts=True)
    return dict(events=events, stats=stats, deliveries=send.call_count, durable_resolver_calls=resolver_calls,
                resolved_event_ids=resolved)


def settings(*, shadow_on, lookup_on, timeout_ms=250, stats_log=True, interval=10):
    return dict(GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED=shadow_on, GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED=lookup_on,
                GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS=timeout_ms,
                GEOPOLITICAL_IDENTITY_STATS_LOG_ENABLED=stats_log, GEOPOLITICAL_IDENTITY_STATS_LOG_INTERVAL_SECONDS=interval)


def counters():
    stats = durable_identity.get_durable_identity_stats()
    return {name: stats[name] for name in durable_identity.STATS_LOG_FIELDS}


def cli(database_url, *argv, env_extra=None):
    out, err = io.StringIO(), io.StringIO()
    with patch.dict(os.environ, {"DATABASE_URL": database_url, **(env_extra or {})}):
        code = tools.main([*argv, "--json"] if "--save-snapshot" not in argv else list(argv), out=out, err=err)
    body = out.getvalue()
    try:
        parsed = json.loads(body) if body else None
    except ValueError:
        parsed = body
    return code, parsed, err.getvalue()


def _clean(text):
    for token in FORBIDDEN:
        if token in text.lower():
            raise StagingStop("Credential-like content in operational output")
    return text


# ------------------------------------------------------------------ runbook

def run_staging(database_url, redis_client, redis_url=None, workdir=None):
    verify_database_url(database_url)
    verify_redis(redis_client, redis_url)
    workdir = workdir or tempfile.mkdtemp(prefix="mias-phase2j-")
    admin = make_engine(DatabaseSettings(url=database_url))
    evidence = dict(redis="disposable loopback Redis server" if redis_url else "in-memory Redis test double")
    try:
        with admin.connect() as connection:
            evidence["postgresql_version"] = connection.execute(sa.text("SHOW server_version")).scalar_one()

        def migrate(target):
            with admin.begin() as connection:
                command.upgrade(migration_config(connection), target)

        # Pre-2H history: schema at 0002, shadow persistence on, lookup off.
        migrate("0002_macro_shadow_history")
        pre = settings(shadow_on=True, lookup_on=False)
        run_collector([corpus.DOCS["bis_final"], FR_WITH_EO], redis_client, pre, database_url)
        run_collector([corpus.DOCS["trade"], TRADE_WITH_EO], redis_client, pre, database_url)
        expire_identity_state(redis_client)
        historical = run_collector([TRADE_WITH_EO], redis_client, pre, database_url)
        writer = restart_collector()
        evidence["pre_registry_history"] = dict(writer={k: writer[k] for k in ("stopped", "persisted", "failed")},
                                                counters=writer["counters"],
                                                historical_divergence_processed=historical["stats"]["processed"])

        # 1-2: reachable, migrate to head, status.
        migrate("head")
        code, status, _ = cli(database_url, "status")
        if code != tools.EXIT_OK or not status["revision_current"]:
            raise StagingStop("Status not healthy after migration")
        evidence["migration_revision"] = status["alembic_revision"]
        # 4-5: backfill once, then idempotent rerun; conflicts.
        code, backfill, _ = cli(database_url, "backfill")
        code2, rerun, _ = cli(database_url, "backfill")
        if code or code2 or rerun["inserted"] != 0:
            raise StagingStop("Backfill not idempotent")
        keys = ("anchors_seen", "inserted", "already_present", "conflicts", "skipped_non_authoritative")
        evidence["backfill"] = {k: backfill[k] for k in keys} | dict(events_scanned=backfill["events_scanned"])
        evidence["backfill_rerun"] = {k: rerun[k] for k in keys}
        _, conflicts, _ = cli(database_url, "conflicts")
        evidence["conflicts"] = [dict(anchor=f"{c['anchor_type']}:{c['anchor_value']}", stage=c["stage"]["policy_stage"],
                                      roots=1 + len(c["conflicting_policy_ids"])) for c in conflicts["conflicts"]]
        # 6: baseline snapshot.
        baseline = os.path.join(workdir, "baseline.json")
        code, _, _ = cli(database_url, "audit", "--save-snapshot", baseline)
        with open(baseline, encoding="utf-8") as handle:
            evidence["baseline_audit"] = json.load(handle)["summary"]

        # 7-9: enable lookup after Redis identity expiry; controlled traffic; counters.
        expire_identity_state(redis_client)
        rollout = settings(shadow_on=True, lookup_on=True)
        with captured_logs() as logs:
            known = run_collector([FR_WITH_EO], redis_client, rollout, database_url)        # miss -> durable hit
            redis_hit = run_collector([corpus.DOCS["bis_final"]], redis_client, rollout, database_url)  # Redis hit
            unknown = run_collector([corpus.DOCS["entity_list"]], redis_client, rollout, database_url)  # miss -> miss
        writer = restart_collector()
        healthy = writer.pop("counters")
        lines = _clean(logs.getvalue()).splitlines()
        stats_lines = [line.split(" ", 2)[2] for line in lines if "event=geopolitical_identity_stats" in line]
        # Reference identity of the joined action, computed on an isolated in-memory Redis.
        outputs, _ = corpus.run_collector([corpus.DOCS["bis_final"], FR_WITH_EO], MemoryRedis(), enabled=False)
        joined_event = outputs[0][0]["event_id"]  # The processed (first) event of the joined action.
        if known["resolved_event_ids"] != [joined_event]:
            raise StagingStop("Durable lookup did not reuse the existing root")
        expected = dict(lookup_attempted=2, lookup_hit=1, lookup_miss=1, lookup_timeout=0, lookup_error=0,
                        lookup_conflict=0, lookup_skipped_busy=0, redis_hit_bypass=1)
        if {k: healthy[k] for k in expected} != expected or writer["failed"]:
            raise StagingStop(f"Unhealthy rollout counters: {healthy}")
        evidence["rollout"] = dict(counters=healthy, writer=writer, stats_log_lines=stats_lines,
                                   warnings=[l for l in lines if l.startswith("WARNING")],
                                   known_root_reused=True,
                                   collector=dict(known=known["stats"]["duplicates"] + known["stats"]["processed"],
                                                  redis_hit=redis_hit["stats"]["duplicates"],
                                                  unknown_processed=unknown["stats"]["processed"],
                                                  deliveries=known["deliveries"] + redis_hit["deliveries"] + unknown["deliveries"]))
        # 10-11: post snapshot and comparison.
        code, comparison, _ = cli(database_url, "audit", "--compare-to", baseline)
        evidence["post_rollout_audit"] = dict(exit_code=code, **{k: comparison[k] for k in (
            "historical_groups", "new_exact_authoritative_anchor", "new_by_classification", "inconclusive", "accepted")})
        if code != tools.EXIT_OK or not comparison["accepted"]:
            raise StagingStop("NEW exact_authoritative_anchor divergence after rollout: stopping without repair")

        # Rollback: switch only. No downgrade, no data removal.
        _, before_status, _ = cli(database_url, "status")
        rollback = settings(shadow_on=True, lookup_on=False)
        with captured_logs():
            steady = run_collector([corpus.DOCS["bis_final"], corpus.DOCS["entity_list"]], redis_client, rollback, database_url)
        rollback_counters = counters()
        rollback_lookup_created = durable_identity._lookup is not None
        restart_collector()
        _, after_status, _ = cli(database_url, "status")
        code, after_compare, _ = cli(database_url, "audit", "--compare-to", baseline)
        evidence["rollback"] = dict(
            lookup_instances_created=rollback_lookup_created,
            durable_resolver_calls=sum(steady["durable_resolver_calls"]),
            lookup_attempted=rollback_counters["lookup_attempted"], redis_hit_bypass=rollback_counters["redis_hit_bypass"],
            registry_rows_before=before_status["registry"]["rows"], registry_rows_after=after_status["registry"]["rows"],
            conflicted_before=before_status["registry"]["conflicted"], conflicted_after=after_status["registry"]["conflicted"],
            revision=after_status["alembic_revision"], downgrade_required=False, accepted_after=after_compare["accepted"],
            duplicates=steady["stats"]["duplicates"])
        if rollback_lookup_created or evidence["rollback"]["durable_resolver_calls"] or not after_compare["accepted"] \
                or after_status["registry"]["rows"] < before_status["registry"]["rows"]:
            raise StagingStop("Rollback did not return to the pre-2H resolver path")

        # Failure injection (shadow off: nothing persisted, history untouched).
        evidence["failure_injection"] = failure_injection(database_url, redis_client, admin)
        code, final_compare, _ = cli(database_url, "audit", "--compare-to", baseline)
        evidence["final_audit"] = dict(exit_code=code, accepted=final_compare["accepted"],
                                       new_exact_authoritative_anchor=final_compare["new_exact_authoritative_anchor"])
        if not final_compare["accepted"]:
            raise StagingStop("Failure injection created divergence")
        evidence["acceptance"] = "PASS"
        return evidence
    finally:
        try:
            restart_collector()
        except Exception:
            pass
        admin.dispose()


def failure_injection(database_url, redis_client, admin):
    results = {}
    injected = settings(shadow_on=False, lookup_on=True)
    control_settings = settings(shadow_on=False, lookup_on=False, stats_log=False)

    def phase(name, docs, url, *, expire=True, lock=False, **overrides):
        # Control run: identical Redis state, lookup off -> today's collector result.
        if expire:
            expire_identity_state(redis_client, "all")
        control = run_collector(docs, redis_client, control_settings, url)
        if expire:
            expire_identity_state(redis_client, "all")
        with captured_logs() as logs, ExitStack() as stack:
            if lock:
                connection = stack.enter_context(admin.connect())
                transaction = connection.begin()
                connection.execute(sa.text("LOCK TABLE geopolitical_anchor_registry IN ACCESS EXCLUSIVE MODE"))
                stack.callback(transaction.rollback)
            outcome = run_collector(docs, redis_client, injected | overrides, url)
        observed = counters()
        restart_collector()
        text = _clean(logs.getvalue())
        results[name] = dict(
            counters={k: observed[k] for k in ("lookup_attempted", "lookup_hit", "lookup_miss", "lookup_timeout",
                                               "lookup_error", "lookup_conflict", "redis_hit_bypass")},
            warnings=sorted({line.split(" ", 2)[2] for line in text.splitlines() if line.startswith("WARNING")}),
            processed_matches_control=(outcome["stats"]["processed"], outcome["stats"]["duplicates"], outcome["deliveries"])
                == (control["stats"]["processed"], control["stats"]["duplicates"], control["deliveries"]),
            deliveries=outcome["deliveries"])

    holder, down = reserved_unreachable_url(database_url)
    try:
        phase("db_unavailable_redis_miss", [FR_WITH_EO], down)
        phase("lookup_timeout", [FR_WITH_EO], database_url, lock=True, GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS=50)
        phase("conflicted_anchor", [TRADE_WITH_EO], database_url)
        # Redis hit with the DB down: warm aliases first (lookup off), then no expiry.
        expire_identity_state(redis_client, "all")
        run_collector([TRADE_WITH_EO], redis_client, control_settings, database_url)
        phase("redis_hit_db_unavailable", [TRADE_WITH_EO], down, expire=False)
    finally:
        holder.close()
    return results


# ------------------------------------------------------------------ report

def render_report(evidence):
    """Deterministic, credential-free Markdown (no timestamps, URLs or secrets)."""
    def block(value):
        return "```json\n" + json.dumps(value, sort_keys=True, indent=2) + "\n```"
    parts = [
        "# Persistence Phase 2J: staging rollout validation report",
        "",
        "Generated by `python -m tests.staging_rollout` against a disposable, test-only,",
        "loopback PostgreSQL database and " + evidence["redis"] + ". Synthetic Phase 2F corpus;",
        "HTTP, OpenAI and Telegram stubbed; real resolver, shadow writer, durable lookup,",
        "registry, audit and CLI. No credentials, URLs or timestamps are recorded.",
        "",
        f"- PostgreSQL version: `{evidence['postgresql_version']}`",
        f"- Migration revision: `{evidence['migration_revision']}`",
        f"- Acceptance: **{evidence['acceptance']}**",
        "",
        "## Pre-registry history (schema 0002, shadow on, lookup off)",
        block(evidence["pre_registry_history"]),
        "## Backfill (first run, then idempotent rerun)",
        block(dict(first=evidence["backfill"], rerun=evidence["backfill_rerun"])),
        "## Conflicts (historical, preserved; lookups fail closed)",
        block(evidence["conflicts"]),
        "## Baseline audit summary",
        block(evidence["baseline_audit"]),
        "## Rollout: lookup enabled, healthy staging traffic",
        block(evidence["rollout"]),
        "## Post-rollout audit comparison",
        block(evidence["post_rollout_audit"]),
        "## Rollback: lookup switch off",
        block(evidence["rollback"]),
        "## Failure injection (shadow off; nothing persisted)",
        block(evidence["failure_injection"]),
        "## Final audit comparison (after rollback and failure injection)",
        block(evidence["final_audit"]),
    ]
    return _clean("\n".join(parts) + "\n")


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="Phase 2J staging rollout validation (test-only).")
    parser.add_argument("--report", help="write the credential-free Markdown report here")
    args = parser.parse_args(argv)
    database_url = os.environ.get("TEST_DATABASE_URL")
    redis_url = os.environ.get("MIAS_PHASE2J_REDIS_URL")
    if not database_url or not redis_url:
        print("TEST_DATABASE_URL and MIAS_PHASE2J_REDIS_URL are required", file=sys.stderr)
        return 2
    import redis
    client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
    try:
        evidence = run_staging(database_url, client, redis_url=redis_url)
    except StagingStop as error:
        print(f"STAGING STOP: {error}", file=sys.stderr)
        return 1
    finally:
        client.close()
    report = render_report(evidence)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            handle.write(report)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
