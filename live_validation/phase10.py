"""Phase 10E live-validation helpers: local files in, canonical files or metadata out.

These fill two integration gaps without changing any frozen package:
- the Phase 9E runner emits only ``phase9-v1``;
- there is no MarketIntelligence CLI.

    python -m live_validation.phase10 options-intelligence --snapshot SNAP.json --output OI.json [--overwrite]
    python -m live_validation.phase10 market-intelligence --synthesis SYN.json --output MI.json [--overwrite]
    python -m live_validation.phase10 snapshot-summary --snapshot SNAP.json

**options-intelligence** builds ``phase9-v2`` OptionsIntelligence with the unchanged Phase 9D builder, using the XNYS
calendar as the Phase 9E runner does. It verifies the result against the snapshot by full re-derivation, then writes
it atomically with the Phase 9E writer. Its summary adds the phase9-v2 fact counts.

**market-intelligence** builds Phase 8 MarketIntelligence from an EvidenceSynthesis file with the unchanged builder,
validates it, and writes it atomically.

**snapshot-summary** reports quote readiness and freshness from an OptionsSnapshot:
- quote status and time-basis counts;
- quote age (snapshot ``as_of`` minus quote ``observed_at``) as quantiles and buckets;
- multiplier presence, underlying-price status and provenance counts.

This is characterization only. There is no freshness threshold and no policy.

Every output is metadata only: counts, ids, statuses and ages. Prices, strikes, contract ids or lists, payloads,
environment contents and credentials are never printed. The module never reads the environment and never touches the
network, a provider or a database; collection stays with ``options_data.runner``, run by the operator.

Exit codes: 0 done; 1 invalid or unreadable input, or a verification failure; 2 usage error; 3 output exists or
cannot be written. These are the Phase 9E runner's codes, whose writer this module reuses.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
import sys

from options_intelligence.canonical import canonical_json
from options_intelligence.runner import InputFileError, OutputError, read_json, summary as oi_summary, write_atomic

OK, INVALID, USAGE, OUTPUT = 0, 1, 2, 3
AGE_BUCKETS = (60, 120, 900, 3600)


def _quantiles(values):
    if not values:
        return None
    ordered = sorted(values)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]  # noqa: E731
    return dict(min=ordered[0], p50=pick(0.5), p90=pick(0.9), max=ordered[-1])


def snapshot_summary(snapshot):
    """Quote readiness and freshness metadata for an OptionsSnapshot dict (no values, no contract ids)."""
    as_of = datetime.fromisoformat(snapshot["as_of"])
    contracts = snapshot["contracts"]
    quotes = [c["quote"] for c in contracts]
    ages = [int((as_of - datetime.fromisoformat(q["observed_at"])).total_seconds())
            for q in quotes if q["status"] == "present" and q["observed_at"]]
    buckets = Counter(next((f"<= {b}s" for b in AGE_BUCKETS if age <= b), f"> {AGE_BUCKETS[-1]}s") for age in ages)
    two_sided = sum(q["status"] == "present" and q["bid"] is not None and q["ask"] is not None for q in quotes)
    provenance = snapshot["provenance"]
    return dict(
        snapshot_id=snapshot["snapshot_id"], symbol=snapshot["underlying"], as_of=snapshot["as_of"],
        session=snapshot["session"], contract_count=len(contracts), truncated=provenance["truncated"],
        pages_fetched=provenance["pages_fetched"], requests_made=provenance["requests_made"],
        records_received=provenance["records_received"],
        source_capabilities={c["group"]: c["status"] for c in provenance["source_capabilities"]},
        quote_status_counts=dict(sorted(Counter(q["status"] for q in quotes).items())),
        quote_time_basis_counts=dict(sorted(Counter(q["time_basis"] for q in quotes).items())),
        two_sided_quote_count=two_sided, timestamped_quote_count=len(ages),
        quote_age_seconds=_quantiles(ages), quote_age_buckets=dict(sorted(buckets.items())),
        shares_per_contract_present_count=sum(c["terms"]["shares_per_contract"] is not None for c in contracts),
        underlying_price_status=snapshot["underlying_price"]["status"]
        if isinstance(snapshot["underlying_price"], dict) and "status" in snapshot["underlying_price"] else None,
        exclusion_counts={e["reason"]: e["count"] for e in snapshot["exclusions"]})


def v2_fact_counts(intelligence):
    """phase9-v2 numeric-fact availability counts for an OptionsIntelligence dict."""
    contracts = intelligence["contracts"]
    return dict(
        current_session_volume_present_count=sum(c["current_session_volume"] is not None for c in contracts),
        open_interest_value_present_count=sum(c["open_interest_value"] is not None for c in contracts),
        open_interest_time_basis_counts=dict(sorted(Counter(c["open_interest_time_basis"] for c in contracts).items())),
        shares_per_contract_present_count=sum(c["shares_per_contract"] is not None for c in contracts))


def build_options_intelligence(snapshot, calendar=None):
    from options_intelligence.builder import build
    from options_intelligence.validation import verify_against_snapshot
    data = build(snapshot, calendar=calendar, format="phase9-v2").to_dict()
    verify_against_snapshot(data, snapshot, calendar=calendar)
    return data


def build_market_intelligence(synthesis):
    from market_intelligence.builder import build
    return build(synthesis).to_dict()


def main(argv=None, *, out=None, err=None, calendar=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m live_validation.phase10")
    commands = parser.add_subparsers(dest="command", required=True)
    oi = commands.add_parser("options-intelligence")
    oi.add_argument("--snapshot", required=True)
    oi.add_argument("--output", required=True)
    oi.add_argument("--overwrite", action="store_true")
    mi = commands.add_parser("market-intelligence")
    mi.add_argument("--synthesis", required=True)
    mi.add_argument("--output", required=True)
    mi.add_argument("--overwrite", action="store_true")
    snap = commands.add_parser("snapshot-summary")
    snap.add_argument("--snapshot", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return OK if exit_.code == 0 else USAGE

    def fail(code, message):
        print(json.dumps(dict(result="FAILED", exit_code=code, error=message), sort_keys=True), file=err)
        return code

    try:
        if args.command == "snapshot-summary":
            report = snapshot_summary(read_json(args.snapshot, "snapshot"))
        elif args.command == "options-intelligence":
            data = build_options_intelligence(read_json(args.snapshot, "snapshot"), calendar)
            size = write_atomic(args.output, canonical_json(data) + "\n", overwrite=args.overwrite)
            report = dict(oi_summary(data, output=args.output, size=size, build_seconds=None,
                                     verification_seconds=None),
                          options_intelligence_format_version=data["options_intelligence_format_version"],
                          **v2_fact_counts(data))
        else:
            data = build_market_intelligence(read_json(args.synthesis, "synthesis"))
            size = write_atomic(args.output, canonical_json(data) + "\n", overwrite=args.overwrite)
            report = dict(result="WRITTEN", intelligence_id=data["intelligence_id"],
                          symbol=data["synthesis_ref"]["symbol"], as_of=data["synthesis_ref"]["as_of"],
                          pattern=data["timeframe_structure"]["pattern"],
                          technical_status=data["evidence_coverage"]["technical_status"], output=args.output,
                          output_bytes=size)
    except InputFileError as error:
        return fail(INVALID, str(error))
    except OutputError as error:
        return fail(OUTPUT, str(error))
    except (ValueError, KeyError, TypeError) as error:   # validation errors of the frozen builders are ValueErrors
        return fail(INVALID, f"{type(error).__name__}: {error}")
    print(json.dumps(report, sort_keys=True), file=out)
    return OK


if __name__ == "__main__":
    sys.exit(main())
