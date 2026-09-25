"""Operational CLI for the durable geopolitical identity registry (Phase 2I).

    python -m persistence.geopolitical_tools status    [--json]
    python -m persistence.geopolitical_tools backfill  [--json] [--max-events N]
    python -m persistence.geopolitical_tools conflicts [--json] [--limit N] [--after CURSOR]
    python -m persistence.geopolitical_tools audit     [--json] [--page-size N] [--after CURSOR]
                                                       [--max-events N] [--batch-size N]
                                                       [--save-snapshot FILE | --compare-to FILE]
    python -m persistence.geopolitical_tools disclosure-audit   [--json] [--max-events N]
    python -m persistence.geopolitical_tools disclosure-correct [--json] [--max-events N] [--apply]
                                                       [--changes-out FILE | --revert FILE]

The database comes from the process environment (DATABASE_URL and DB_* limits)
via ``DatabaseSettings.from_env``; ``.env`` is never read and ``shared.config``
is never imported. Nothing connects at import time. ``backfill`` is the only
command that writes (registry table only); every other command runs in a
read-only transaction on PostgreSQL. ``disclosure-correct --apply`` (Phase 2N) is the
only other writer and changes only ``events.current_version_id``; without ``--apply``
it is a read-only dry run. No command touches Redis, the collector,
or event history. Credentials and connection strings are never printed.

Exit codes: 0 ok, 1 check failed (unhealthy status / acceptance not met),
2 usage error, 3 database unavailable or not configured, 4 schema missing/behind.
"""
import argparse
import base64
from contextlib import contextmanager
from dataclasses import replace
import json
import os
import sys

import sqlalchemy as sa

from persistence.config import ConfigurationError, DatabaseSettings
from persistence.database import make_engine, transaction
from persistence import geopolitical_durable_identity as durable_identity
from persistence.geopolitical_audit import (
    audit_geopolitical_identity_divergence, audit_geopolitical_identity_divergence_page,
    capture_identity_audit_snapshot, compare_identity_audits,
)
from persistence.geopolitical_disclosure import (
    audit_disclosure_pointers, correct_disclosure_pointers, revert_disclosure_corrections,
)
from persistence.geopolitical_registry import AnchorRegistryRepository, KEY, backfill_geopolitical_anchor_registry

# The migration head this tool expects (tests pin it to the Alembic head; bump with every new migration).
EXPECTED_REVISION = "0006_technical_numeric_volume"
EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_DATABASE, EXIT_SCHEMA = 0, 1, 2, 3, 4
SWITCHES = ("GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_ENABLED", "GEOPOLITICAL_PERSISTENCE_SHADOW_ENABLED")


class ToolError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError(EXIT_USAGE, "usage error: " + message[:200])


def tool_engine():
    """Process-environment settings only; never .env, never logged."""
    return make_engine(replace(DatabaseSettings.from_env(), application_name="mias_geopolitical_tools"))


@contextmanager
def _session(engine, *, read_only):
    with transaction(engine) as session:
        if read_only and engine.dialect.name == "postgresql":
            session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        yield session


def _schema(session):
    inspector = sa.inspect(session.connection())
    revision = None
    if inspector.has_table("alembic_version"):
        revision = session.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
    return dict(alembic_revision=revision, expected_revision=EXPECTED_REVISION,
                revision_current=revision == EXPECTED_REVISION,
                history_tables=all(inspector.has_table(t) for t in ("events", "event_versions", "event_provenance")),
                registry_table=inspector.has_table("geopolitical_anchor_registry"))


def _require_registry(schema):
    if not schema["registry_table"] or not schema["revision_current"]:
        raise ToolError(EXIT_SCHEMA, "schema error: anchor registry unavailable (expected migration "
                        f"{EXPECTED_REVISION}, found {schema['alembic_revision'] or 'none'})")


def _positive(maximum):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("must be an integer") from None
        if not 1 <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between 1 and {maximum}")
        return number
    return parse


def _encode_cursor(values):
    return base64.urlsafe_b64encode(json.dumps(list(values)).encode()).decode().rstrip("=")


def _decode_cursor(cursor):
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except Exception:
        raise ToolError(EXIT_USAGE, "usage error: invalid conflict cursor") from None
    if not isinstance(values, list) or len(values) != len(KEY) or not all(isinstance(v, str) for v in values):
        raise ToolError(EXIT_USAGE, "usage error: invalid conflict cursor")
    return values


def switch_state(environ=None):
    environ = os.environ if environ is None else environ
    state = {name: environ.get(name, "false").lower() == "true" for name in SWITCHES}
    state["GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS"] = environ.get("GEOPOLITICAL_DURABLE_IDENTITY_LOOKUP_TIMEOUT_MS", "250")
    state["source"] = "tool process environment (.env not read)"
    return state


# ------------------------------------------------------------------ commands

class _Repo:
    """Minimal repository shim: the audit only needs ``.session``."""
    def __init__(self, session):
        self.session = session


def cmd_status(engine, args):
    with _session(engine, read_only=True) as session:
        schema = _schema(session)
        registry = AnchorRegistryRepository(session).status() if schema["registry_table"] else None
        audit = None
        if schema["history_tables"]:
            report = audit_geopolitical_identity_divergence(_Repo(session), max_events=args.max_events)
            audit = dict(summary=report["summary"], events_scanned=report["events_scanned"], truncated=report["truncated"])
    issues = []
    if not schema["revision_current"]:
        issues.append(f"migration revision {schema['alembic_revision'] or 'none'} != {EXPECTED_REVISION}")
    if not schema["registry_table"]:
        issues.append("anchor registry table missing")
    if not schema["history_tables"]:
        issues.append("persistence history tables missing")
    warnings = []
    if registry and registry["conflicted"]:
        warnings.append(f"{registry['conflicted']} conflicted registry anchor(s): lookups on them fail closed")
    if audit and audit["truncated"]:
        warnings.append("audit summary truncated; raise --max-events or use paged audit")
    result = dict(database="reachable", **schema, registry=registry, switches=switch_state(),
                  durable_lookup_counters=dict(scope="this tool process only (collector counters are process-local)",
                                               **durable_identity.get_durable_identity_stats()),
                  audit=audit, healthy=not issues, issues=issues, warnings=warnings)
    return (EXIT_OK if not issues else EXIT_FAILED), result


def cmd_backfill(engine, args):
    with _session(engine, read_only=True) as session:
        _require_registry(_schema(session))
    with _session(engine, read_only=False) as session:
        summary = backfill_geopolitical_anchor_registry(session, max_events=args.max_events)
    return EXIT_OK, summary


def cmd_conflicts(engine, args):
    after = _decode_cursor(args.after) if args.after else None
    with _session(engine, read_only=True) as session:
        _require_registry(_schema(session))
        rows = AnchorRegistryRepository(session).conflicts(limit=args.limit + 1, after=after)
    more, rows = len(rows) > args.limit, rows[:args.limit]
    conflicts = [dict(
        anchor_type=r["anchor_type"], anchor_value=r["anchor_value"],
        stage=dict(event_type=r["event_type"], policy_stage=r["policy_stage"], revision_id=r["revision_id"]),
        current_root=dict(policy_id=r["policy_id"], event_key=r["event_key"]),
        conflicting_policy_ids=list((r["attributes"] or {}).get("conflicting_policy_ids", [])),
        conflicting_event_keys=list((r["attributes"] or {}).get("conflicting_event_keys", [])),
        source_document_id=r["source_document_id"], first_seen_at=r["first_seen_at"],
        last_seen_at=r["last_seen_at"], updated_at=r["updated_at"]) for r in rows]
    cursor = _encode_cursor([rows[-1][k] for k in KEY]) if rows and more else None
    return EXIT_OK, dict(conflicts=conflicts, count=len(conflicts), limit=args.limit, next_cursor=cursor,
                         note="read-only listing; no winner is chosen and nothing is repaired")


def cmd_audit(engine, args):
    if args.save_snapshot and args.compare_to:
        raise ToolError(EXIT_USAGE, "usage error: --save-snapshot and --compare-to are exclusive")
    baseline = None
    if args.compare_to:
        try:
            with open(args.compare_to, encoding="utf-8") as handle:
                baseline = json.load(handle)
        except (OSError, ValueError):
            raise ToolError(EXIT_USAGE, "usage error: baseline snapshot unreadable") from None
    with _session(engine, read_only=True) as session:
        repo = _Repo(session)
        if args.save_snapshot or baseline is not None:
            snapshot = capture_identity_audit_snapshot(repo, max_events=args.max_events, batch_size=args.batch_size)
        else:
            try:
                page = audit_geopolitical_identity_divergence_page(
                    repo, page_size=args.page_size, after=args.after, max_events=args.max_events,
                    batch_size=args.batch_size)
            except ValueError as error:
                raise ToolError(EXIT_USAGE, "usage error: " + str(error)[:200]) from None
            return EXIT_OK, page
    if args.save_snapshot:
        try:
            with open(args.save_snapshot, "x", encoding="utf-8") as handle:
                json.dump(snapshot, handle, sort_keys=True, indent=2, default=str)
        except OSError:
            raise ToolError(EXIT_USAGE, "usage error: snapshot file exists or is not writable") from None
        return EXIT_OK, dict(saved=args.save_snapshot, events_scanned=snapshot["events_scanned"],
                             truncated=snapshot["truncated"], summary=snapshot["summary"])
    try:
        comparison = compare_identity_audits(baseline, snapshot)
    except (ValueError, KeyError, TypeError):
        raise ToolError(EXIT_USAGE, "usage error: unsupported baseline snapshot") from None
    return (EXIT_OK if comparison["accepted"] else EXIT_FAILED), comparison


def cmd_disclosure_audit(engine, args):
    with _session(engine, read_only=True) as session:
        return EXIT_OK, audit_disclosure_pointers(session, max_events=args.max_events)


def cmd_disclosure_correct(engine, args):
    if args.revert and args.changes_out:
        raise ToolError(EXIT_USAGE, "usage error: --revert and --changes-out are exclusive")
    changes = None
    if args.revert:
        try:
            with open(args.revert, encoding="utf-8") as handle:
                changes = json.load(handle)["changes"]
        except (OSError, ValueError, KeyError, TypeError):
            raise ToolError(EXIT_USAGE, "usage error: change report unreadable") from None
    with _session(engine, read_only=not args.apply) as session:
        report = (revert_disclosure_corrections(session, changes, apply=args.apply) if changes is not None
                  else correct_disclosure_pointers(session, apply=args.apply, max_events=args.max_events))
    if args.changes_out:
        try:
            with open(args.changes_out, "x", encoding="utf-8") as handle:
                json.dump(report, handle, sort_keys=True, indent=2, default=str)
        except OSError:
            raise ToolError(EXIT_USAGE, "usage error: change report exists or is not writable") from None
    report["note"] = ("dry run: nothing changed; rerun with --apply to change current_version_id only"
                      if not args.apply else "applied: only events.current_version_id changed (guarded)")
    return EXIT_OK, report


COMMANDS = {"status": cmd_status, "backfill": cmd_backfill, "conflicts": cmd_conflicts, "audit": cmd_audit,
            "disclosure-audit": cmd_disclosure_audit, "disclosure-correct": cmd_disclosure_correct}


def build_parser():
    parser = _Parser(prog="python -m persistence.geopolitical_tools",
                     description="Durable geopolitical identity registry tooling (read-only except backfill).")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    for name in COMMANDS:
        command = sub.add_parser(name)
        command.add_argument("--json", action="store_true", help="machine-readable output")
        if name in ("status", "backfill", "audit", "disclosure-audit", "disclosure-correct"):
            command.add_argument("--max-events", type=_positive(100_000 if name != "backfill" else 1_000_000),
                                 default=10_000 if name != "backfill" else 100_000)
    sub.choices["conflicts"].add_argument("--limit", type=_positive(1_000), default=50)
    sub.choices["conflicts"].add_argument("--after", help="opaque cursor from a previous page")
    audit = sub.choices["audit"]
    audit.add_argument("--page-size", type=_positive(1_000), default=50)
    audit.add_argument("--after", help="group_key cursor from a previous page")
    audit.add_argument("--batch-size", type=_positive(5_000), default=500)
    audit.add_argument("--save-snapshot", metavar="FILE", help="write a full acceptance snapshot (refuses to overwrite)")
    audit.add_argument("--compare-to", metavar="FILE", help="compare current history with a saved snapshot")
    correct = sub.choices["disclosure-correct"]
    correct.add_argument("--apply", action="store_true", help="write: change only current_version_id (guarded)")
    correct.add_argument("--changes-out", metavar="FILE", help="write the change report (refuses to overwrite)")
    correct.add_argument("--revert", metavar="FILE", help="revert a previous change report (with --apply)")
    return parser


def _render(command, result, as_json):
    if as_json:
        return json.dumps(result, sort_keys=True, indent=2, default=str)
    lines = []
    def emit(prefix, value):
        if isinstance(value, dict):
            for key in sorted(value):
                emit(f"{prefix}.{key}" if prefix else key, value[key])
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{prefix}: {len(value)} item(s)")
            for index, item in enumerate(value, 1):
                emit(f"{prefix}[{index}]", item)
        else:
            lines.append(f"{prefix}: {', '.join(map(str, value)) if isinstance(value, list) else value}")
    emit("", result)
    return f"[{command}]\n" + "\n".join(lines)


def main(argv=None, *, engine=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
        owned = engine is None
        try:
            if owned:
                engine = tool_engine()
            code, result = COMMANDS[args.command](engine, args)
        finally:
            if owned and engine is not None:
                engine.dispose()
    except ToolError as error:
        print(str(error), file=err)
        return error.code
    except ConfigurationError as error:
        # ConfigurationError messages are credential-free by construction.
        print("database not configured: " + str(error)[:200], file=err)
        return EXIT_DATABASE
    except Exception as error:
        print(f"database unavailable or operation failed ({type(error).__name__})", file=err)
        return EXIT_DATABASE
    print(_render(args.command, result, args.json), file=out)
    return code


if __name__ == "__main__":
    sys.exit(main())
