"""Read-only all-family persistence audit (Phase 2S); never repairs, merges or replays.

    python -m persistence.family_audit [--json] [--max-events N]

Reports, per source family (macro, treasury, geopolitical, fed, sec, news):
events, versions, current pointers, provenance, score/decision/AI history,
first/last observation and repeat indications. It then checks cross-family
integrity:

- missing or foreign current pointers;
- unknown family labels, identity versions or version event types;
- event types appearing under more than one family, and provenance roles that
  are not among the roles the family's adapter writes;
- orphan versions, provenance and history.

A single current pointer per event is structural (one column, composite foreign
key), so "multiple current pointers" cannot occur and is reported as such.

**Current-pointer rule check:** for every multi-version event, each non-current
version is evaluated against the current one with the family's own promotion
function. Any version the rule would promote now is reported as a rule
violation. Versions that were submitted only as historical (``make_current=False``,
e.g. stale observations) are never evaluated by the writer; callers classify
those using their own evidence.

The database comes from the process environment (``DatabaseSettings.from_env``);
``.env`` is never read. Every query runs in a read-only transaction on
PostgreSQL. Exit codes: 0 ok, 2 usage, 3 database unavailable or not configured.
"""
import argparse
from contextlib import contextmanager
from dataclasses import replace
import json
import sys

import sqlalchemy as sa

from persistence.adapters.fed import fed_promotion
from persistence.adapters.geopolitical import geopolitical_promotion
from persistence.adapters.macro import macro_promotion
from persistence.adapters.news import news_promotion
from persistence.adapters.sec import sec_promotion
from persistence.adapters.treasury import EVENT_TYPES as TREASURY_EVENT_TYPES, treasury_promotion
from persistence.config import ConfigurationError, DatabaseSettings
from persistence.database import make_engine, transaction
from persistence.models import events, event_versions, event_provenance, event_history

FAMILIES = ("macro", "treasury", "geopolitical", "fed", "sec", "news")
IDENTITY_VERSIONS = dict(macro={"macro-v1"}, treasury={"treasury-v1"}, geopolitical={"geopolitical-v1"},
                         fed={"fed-v1"}, sec={"sec-v1"}, news={"news-url-v1", "news-fingerprint-v1"})
EVENT_TYPES = dict(macro={"macro_release"}, treasury=set(TREASURY_EVENT_TYPES),
                   geopolitical={"policy_action", "sanctions_action", "trade_action", "regulatory_action",
                                 "operational_disruption"},
                   fed={"fed_policy"}, sec={"sec_filing"}, news={None})
# Provenance ``role`` values each adapter writes (geopolitical records a ``relation`` instead of a role).
PROVENANCE_ROLES = dict(macro={"release", "structured_data", "feed"},
                        treasury={"release", "auction_record", "yield_dataset", "debt_limit_letter"},
                        geopolitical={None}, fed={"fed_monetary_policy_release"}, sec={"sec_filing"},
                        news={"news_rss_item"})
PROMOTION = dict(macro=macro_promotion, treasury=treasury_promotion, geopolitical=geopolitical_promotion,
                 fed=fed_promotion, sec=sec_promotion, news=news_promotion)
SAMPLE = 10


def _bounded(name, value, upper):
    if type(value) is not int or not 1 <= value <= upper:
        raise ValueError(f"{name} must be between 1 and {upper}")


def _as_text(value):
    return None if value is None else value.isoformat() if hasattr(value, "isoformat") else str(value)


def _attrs(value):
    return value if isinstance(value, dict) else json.loads(value) if value else {}


def family_counts(session):
    """Per-family counts; families without rows are reported with zeros."""
    counts = {family: dict(events=0, versions=0, current_pointers=0, provenance=0, score_history=0,
                           decision_history=0, ai_history=0, repeated_events=0, first_observed=None,
                           last_observed=None) for family in FAMILIES}
    for row in session.execute(sa.select(
            events.c.source_family, sa.func.count(), sa.func.count(events.c.current_version_id),
            sa.func.sum(sa.case((events.c.last_seen_at > events.c.first_seen_at, 1), else_=0)),
            sa.func.min(events.c.first_seen_at), sa.func.max(events.c.last_seen_at)).group_by(events.c.source_family)):
        entry = counts.setdefault(row[0], dict.fromkeys(counts["macro"], 0))
        entry.update(events=row[1], current_pointers=row[2], repeated_events=int(row[3] or 0),
                     first_observed=_as_text(row[4]), last_observed=_as_text(row[5]))
    version_join = event_versions.join(events, events.c.id == event_versions.c.event_id)
    for family, n in session.execute(sa.select(events.c.source_family, sa.func.count())
                                     .select_from(version_join).group_by(events.c.source_family)):
        counts.setdefault(family, dict.fromkeys(counts["macro"], 0))["versions"] = n
    for family, n in session.execute(sa.select(events.c.source_family, sa.func.count()).select_from(
            event_provenance.join(version_join, event_versions.c.id == event_provenance.c.event_version_id))
            .group_by(events.c.source_family)):
        counts[family]["provenance"] = n
    for family, kind, n in session.execute(sa.select(events.c.source_family, event_history.c.kind, sa.func.count())
                                           .select_from(event_history.join(version_join,
                                                        event_versions.c.id == event_history.c.event_version_id))
                                           .group_by(events.c.source_family, event_history.c.kind)):
        counts[family][f"{kind}_history"] = n
    return counts


def _check(session, query):
    rows = session.execute(query).all()
    return dict(count=len(rows), sample=[[_as_text(v) for v in row] for row in rows[:SAMPLE]])


def integrity_checks(session):
    """Cross-family integrity findings (each: count plus a bounded sample)."""
    current = event_versions.alias("current_version")
    checks = dict(
        missing_current_pointer=_check(session, sa.select(events.c.source_family, events.c.event_key)
                                       .where(events.c.current_version_id.is_(None))),
        foreign_current_pointer=_check(session, sa.select(events.c.source_family, events.c.event_key)
                                       .join(current, current.c.id == events.c.current_version_id)
                                       .where(current.c.event_id != events.c.id)),
        unknown_family_label=_check(session, sa.select(events.c.source_family, events.c.event_key)
                                    .where(events.c.source_family.not_in(FAMILIES))),
        unexpected_identity_version=_check(session, sa.select(events.c.source_family, events.c.identity_version)
                                           .where(sa.or_(*(sa.and_(events.c.source_family == f,
                                                                   events.c.identity_version.not_in(v))
                                                           for f, v in IDENTITY_VERSIONS.items())))
                                           .distinct()),
        orphan_versions=_check(session, sa.select(event_versions.c.id).outerjoin(events, events.c.id == event_versions.c.event_id)
                               .where(events.c.id.is_(None))),
        orphan_provenance=_check(session, sa.select(event_provenance.c.id).outerjoin(
            event_versions, event_versions.c.id == event_provenance.c.event_version_id).where(event_versions.c.id.is_(None))),
        orphan_history=_check(session, sa.select(event_history.c.id).outerjoin(
            event_versions, event_versions.c.id == event_history.c.event_version_id).where(event_versions.c.id.is_(None))),
    )
    types = session.execute(sa.select(events.c.source_family, event_versions.c.event_type).select_from(
        event_versions.join(events, events.c.id == event_versions.c.event_id)).distinct()).all()
    unexpected = [[f, t] for f, t in types if f in EVENT_TYPES and t not in EVENT_TYPES[f]]
    checks["unexpected_event_type"] = dict(count=len(unexpected), sample=unexpected[:SAMPLE])
    by_type = {}
    for family, event_type in types:
        if event_type is not None:
            by_type.setdefault(event_type, set()).add(family)
    crossover = sorted([t, sorted(f)] for t, f in by_type.items() if len(f) > 1)
    checks["event_type_family_crossover"] = dict(count=len(crossover), sample=crossover[:SAMPLE])
    roles = set()
    for family, attributes in session.execute(sa.select(events.c.source_family, event_provenance.c.attributes).select_from(
            event_provenance.join(event_versions, event_versions.c.id == event_provenance.c.event_version_id)
            .join(events, events.c.id == event_versions.c.event_id))):
        role = _attrs(attributes).get("role")
        if family in PROVENANCE_ROLES and role not in PROVENANCE_ROLES[family]:
            roles.add((family, role))
    unexpected_roles = sorted([f, r] for f, r in roles)
    checks["provenance_role_not_of_family"] = dict(count=len(unexpected_roles), sample=unexpected_roles[:SAMPLE])
    checks["multiple_current_pointers"] = dict(count=0, sample=[],
                                               note="structurally impossible: one current_version_id column per event")
    return checks


def pointer_rule_check(session, *, max_events=10_000):
    """Evaluate each non-current version against current with the family's promotion rule (read-only)."""
    _bounded("max_events", max_events, 100_000)
    multi = session.execute(sa.select(events.c.id, events.c.source_family, events.c.event_key, events.c.current_version_id)
                            .where(events.c.id.in_(sa.select(event_versions.c.event_id).group_by(event_versions.c.event_id)
                                                   .having(sa.func.count() > 1)))
                            .order_by(events.c.source_family, events.c.event_key).limit(max_events + 1)).all()
    truncated = len(multi) > max_events
    multi = multi[:max_events]
    violations, evaluated, per_family = [], 0, {}
    for event in multi:
        per_family[event.source_family] = per_family.get(event.source_family, 0) + 1
        versions = [dict(r) for r in session.execute(sa.select(event_versions).where(
            event_versions.c.event_id == event.id).order_by(event_versions.c.recorded_at, event_versions.c.id)).mappings()]
        current = next((v for v in versions if v["id"] == event.current_version_id), None)
        if current is None or event.source_family not in PROMOTION:
            continue
        for version in versions:
            if version["id"] == current["id"]:
                continue
            evaluated += 1
            candidate = dict(version, attributes=_attrs(version["attributes"]))
            promote, reason = PROMOTION[event.source_family](dict(current, attributes=_attrs(current["attributes"])),
                                                             candidate)
            if promote:
                violations.append(dict(family=event.source_family, event_key=event.event_key,
                                       current_version_id=current["id"], version_id=version["id"], reason=reason))
    violations.sort(key=lambda v: (v["family"], v["event_key"], v["version_id"]))
    return dict(multi_version_events=per_family, versions_evaluated=evaluated, truncated=truncated,
                violation_count=len(violations), violations=violations)


def audit_families(session, *, max_events=10_000):
    checks = integrity_checks(session)
    rule = pointer_rule_check(session, max_events=max_events)
    findings = sum(c["count"] for c in checks.values())
    return dict(families=family_counts(session), integrity=checks, integrity_findings=findings, pointer_rule=rule)


@contextmanager
def read_only(engine):
    with transaction(engine) as session:
        if engine.dialect.name == "postgresql":
            session.execute(sa.text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        yield session


def main(argv=None, *, engine=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m persistence.family_audit", description="Read-only all-family audit.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--max-events", type=int, default=10_000)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 2 if exit_.code else 0
    owned = engine is None
    try:
        if owned:
            engine = make_engine(replace(DatabaseSettings.from_env(), application_name="mias_family_audit"))
        with read_only(engine) as session:
            result = audit_families(session, max_events=args.max_events)
    except ConfigurationError as error:  # A ValueError subclass: must be handled first.
        print("database not configured: " + str(error)[:200], file=err)
        return 3
    except ValueError as error:
        print("usage error: " + str(error)[:200], file=err)
        return 2
    except Exception as error:
        print(f"database unavailable or operation failed ({type(error).__name__})", file=err)
        return 3
    finally:
        if owned and engine is not None:
            engine.dispose()
    if args.json:
        print(json.dumps(result, sort_keys=True, indent=2, default=str), file=out)
    else:
        print(f"[family-audit] integrity findings: {result['integrity_findings']}; "
              f"pointer rule violations: {result['pointer_rule']['violation_count']}", file=out)
        for family, counts in result["families"].items():
            print(f"{family}: " + json.dumps(counts, sort_keys=True), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
