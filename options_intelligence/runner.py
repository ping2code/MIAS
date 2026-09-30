"""Local OptionsIntelligence replay runner (Phase 9E): snapshot file (+ optional MarketIntelligence file) ->
canonical OptionsIntelligence file.

    python -m options_intelligence.runner --snapshot FILE --output FILE [--market-intelligence FILE] [--overwrite]

Steps:

1. Read the OptionsSnapshot JSON. Duplicate JSON keys are rejected, and nothing is repaired.
2. Read the optional MarketIntelligence JSON.
3. Build OptionsIntelligence with the pure Phase 9D builder. This validates the snapshot fully and the
   MarketIntelligence reference.
4. Verify the result against its inputs: a full re-derivation that must match byte for byte.
5. Write the canonical JSON atomically: a temporary file in the target directory, fsync, then a no-clobber hard
   link, or a replace with ``--overwrite``. An existing file is never overwritten silently, and a failed write
   leaves nothing behind.
6. Print a compact metadata summary. It contains no strikes, prices, IV or Greek values, contracts or payloads.

**Local replay only:** no network (Massive, Polygon or any price provider), database, AI, environment-driven
semantics or wall-clock reads. All timing comes from ``snapshot.as_of``. ``build_seconds`` and
``verification_seconds`` in the summary are measured with a monotonic performance counter and are never part of
the content-addressed object. Session classification uses the XNYS exchange calendar (``market_data.calendar``),
the same one the Phase 9D builder uses by default.

There are no selection or filtering flags; those belong to Phase 10.

Exit codes: 0 written; 1 invalid or unreadable input, or verification failure; 2 usage error; 3 output file
exists or cannot be written.
"""
import argparse
import json
import os
import sys
import tempfile
import time

from options_data.validation import OptionsSnapshotError
from options_intelligence.builder import build
from options_intelligence.canonical import canonical_json
from options_intelligence.validation import OptionsIntelligenceError, verify_against_snapshot

OK, INVALID, USAGE, OUTPUT = 0, 1, 2, 3


class InputFileError(Exception):
    pass


class OutputError(Exception):
    pass


def _no_duplicate_keys(pairs):
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate JSON key")
    return dict(pairs)


def read_json(path, name):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=_no_duplicate_keys)
    except FileNotFoundError:
        raise InputFileError(f"{name} file not found") from None
    except (OSError, UnicodeDecodeError, ValueError):
        raise InputFileError(f"{name} file is unreadable, not JSON, or has duplicate keys") from None


def write_atomic(path, text, *, overwrite=False):
    """Write ``text`` atomically; never clobber an existing file unless ``overwrite``. Returns the byte size."""
    path = os.path.abspath(path)
    if not overwrite and os.path.lexists(path):
        raise OutputError("output file already exists (use --overwrite to replace it)")
    try:
        fd, temp = tempfile.mkstemp(prefix=".options-intelligence-", suffix=".tmp", dir=os.path.dirname(path))
    except OSError:
        raise OutputError("cannot create a temporary file in the output directory") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            os.link(temp, path)
            os.unlink(temp)
    except FileExistsError:
        raise OutputError("output file already exists (use --overwrite to replace it)") from None
    except OSError:
        raise OutputError("cannot write the output file") from None
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return os.path.getsize(path)


def summary(data, *, output, size, build_seconds, verification_seconds):
    """Metadata only (kept outside the content-addressed object): counts, ids and timings, never values."""
    comp = data["chain_completeness"]
    quote_states = {}
    for expiration in data["expirations"]:
        for entry in expiration["quote_state_counts"]:
            quote_states[entry["key"]] = quote_states.get(entry["key"], 0) + entry["count"]
    with_group = {entry["key"]: entry["count"] for entry in comp["contracts_with"]}
    return dict(
        result="WRITTEN", options_intelligence_id=data["options_intelligence_id"],
        snapshot_id=data["snapshot_ref"]["snapshot_id"], symbol=data["snapshot_ref"]["underlying"],
        as_of=data["snapshot_ref"]["as_of"], contract_count=comp["contract_count"],
        expiration_count=comp["expiration_count"], truncated=comp["truncated"],
        market_intelligence_attached=data["market_intelligence_ref"] is not None,
        quote_state_counts=dict(sorted(quote_states.items())),
        day_session_relation_counts={e["key"]: e["count"] for e in comp["day_session_relation_counts"]},
        iv_available_count=with_group["implied_volatility"], greeks_available_count=with_group["greeks"],
        open_interest_present_count=with_group["open_interest"],
        attention_count=len(data["attention"]),
        attention_codes={a["code"]: a["count"] for a in data["attention"]},
        underlying_price_status=data["underlying"]["price_status"],
        output=output, output_bytes=size, build_seconds=build_seconds, verification_seconds=verification_seconds)


def main(argv=None, *, out=None, calendar=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m options_intelligence.runner")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--market-intelligence")
    parser.add_argument("--overwrite", action="store_true")

    def fail(code, **fields):
        print(json.dumps(dict(result="NOT WRITTEN", **fields), sort_keys=True), file=out)
        return code

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return fail(USAGE, reason="usage error")
    if not args.overwrite and os.path.lexists(args.output):
        return fail(OUTPUT, reason="output file already exists (use --overwrite to replace it)")
    try:
        snapshot = read_json(args.snapshot, "snapshot")
        market_intelligence = read_json(args.market_intelligence, "market intelligence") \
            if args.market_intelligence else None
    except InputFileError as error:
        return fail(INVALID, reason=str(error))
    if calendar is None:
        from market_data.calendar import default_calendar
        calendar = default_calendar()
    try:
        started = time.perf_counter()
        intelligence = build(snapshot, market_intelligence, calendar=calendar)
        build_seconds = round(time.perf_counter() - started, 3)
        started = time.perf_counter()
        data = verify_against_snapshot(intelligence, snapshot, market_intelligence, calendar=calendar)
        verification_seconds = round(time.perf_counter() - started, 3)
    except (OptionsSnapshotError, OptionsIntelligenceError) as error:
        return fail(INVALID, reason=str(error))
    try:
        size = write_atomic(args.output, canonical_json(data) + "\n", overwrite=args.overwrite)
    except OutputError as error:
        return fail(OUTPUT, reason=str(error))
    print(json.dumps(summary(data, output=os.path.abspath(args.output), size=size, build_seconds=build_seconds,
                             verification_seconds=verification_seconds), indent=2, sort_keys=True), file=out)
    return OK


if __name__ == "__main__":
    sys.exit(main())
