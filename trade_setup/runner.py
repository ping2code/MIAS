"""Local Trade Setup replay runner (Phase 10E): sealed files in, canonical files out.

    python -m trade_setup.runner --market-intelligence MI.json --options-intelligence OI.json \\
        --policy POLICY.json --assessment-output ASSESSMENT.json \\
        [--later-market-intelligence LATER_MI.json --invalidation-output INVALIDATION.json] [--overwrite]

Every path is explicit, and there are no defaults: the policy is a sealed ``phase10-policy-v1`` file.

1. Read every input as JSON. Duplicate keys are rejected, and nothing is repaired.
2. Validate each input structurally:
   - MarketIntelligence (``phase8-v1``);
   - OptionsIntelligence (``phase9-v1`` or ``phase9-v2``);
   - the policy;
   - the optional later MarketIntelligence.
3. Build the TradeSetupAssessment (``trade_setup.builder.assess``), then validate and re-derive it.
4. If requested, build the InvalidationCheck (``check_invalidation``) against the later MarketIntelligence, then
   validate and re-derive it.
5. **Only after every requested output has been built and verified in memory**, write the files atomically. Each
   goes to a temporary file in the target directory and is fsynced, then:
   - without ``--overwrite``, it is hard-linked into place (no-clobber);
   - with ``--overwrite``, it replaces the target.

   Every target is checked for collisions before anything is written. A failure while building or verifying writes
   nothing. Without ``--overwrite``, a failure while committing removes any output this run already linked, so the
   run writes all requested outputs or none. With ``--overwrite``, an OS error after the first replace can leave the
   new assessment in place without the invalidation file. Both files are complete and valid, never partial.
6. Print one metadata-only JSON summary on stdout: ids, statuses, reason counts, quote readiness and timings. It
   never prints prices, strikes, contract lists or environment contents.

**Boundary:** local files only. No network, provider, database, AI, environment-driven semantics or wall-clock
reads. Timings come from a monotonic counter and never enter a content-addressed object. The pure core
(``trade_setup`` except this module) performs no I/O at all.

Exit codes: 0 written; 2 usage error, or an unreadable, invalid or incompatible input (the frozen contracts reject
it); 3 a built output failed its own validation or re-derivation (an internal integrity failure); 4 an output file
exists without ``--overwrite``, or cannot be written.
"""
import argparse
from collections import Counter
import json
import os
import sys
import tempfile
import time

from trade_setup.builder import assess
from trade_setup.canonical import canonical_json
from trade_setup.invalidation import check_invalidation, validated_invalidation, verify_invalidation
from trade_setup.policy import PolicyError, validated_policy
from trade_setup.validation import (TradeSetupInputError, validated_assessment, validated_market_intelligence,
                                    validated_options_intelligence, verify_assessment)

OK, INVALID, INTEGRITY, OUTPUT = 0, 2, 3, 4


class InputFileError(Exception):
    pass


class IntegrityError(Exception):
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


def canonical_text(data):
    return canonical_json(data) + "\n"


def build_outputs(mi, oi, policy, later_mi=None):
    """(assessment dict, invalidation dict or None), built, validated and re-derived in memory."""
    try:
        validated_market_intelligence(mi)
        validated_options_intelligence(oi)
        validated_policy(policy)
        if later_mi is not None:
            validated_market_intelligence(later_mi)
        assessment = assess(mi, oi, policy).to_dict()
        invalidation = check_invalidation(assessment, later_mi).to_dict() if later_mi is not None else None
    except PolicyError as error:
        raise TradeSetupInputError(f"policy is invalid: {error}") from None
    try:
        validated_assessment(assessment)
        verify_assessment(assessment, mi, oi)
        if invalidation is not None:
            validated_invalidation(invalidation)
            verify_invalidation(invalidation, assessment, later_mi)
    except TradeSetupInputError as error:
        raise IntegrityError(str(error)) from None
    return assessment, invalidation


def _temp(path, text):
    try:
        fd, temp = tempfile.mkstemp(prefix=".trade-setup-", suffix=".tmp", dir=os.path.dirname(path))
    except OSError:
        raise OutputError("cannot create a temporary file in the output directory") from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        os.unlink(temp)
        raise OutputError("cannot write the output file") from None
    return temp


def write_all(outputs, *, overwrite=False):
    """Write {path: text} as one unit: every target is checked first; without overwrite, a failure while committing
    removes the outputs this call already linked. Returns {path: bytes}."""
    paths = [os.path.abspath(p) for p in outputs]
    if not overwrite:
        for path in paths:
            if os.path.lexists(path):
                raise OutputError("output file already exists (use --overwrite to replace it)")
    temps, committed = {}, []
    try:
        for path, text in zip(paths, outputs.values()):
            temps[path] = _temp(path, text)
        for path, temp in temps.items():
            try:
                if overwrite:
                    os.replace(temp, path)
                else:
                    os.link(temp, path)
            except FileExistsError:
                raise OutputError("output file already exists (use --overwrite to replace it)") from None
            except OSError:
                raise OutputError("cannot write the output file") from None
            committed.append(path)
    except OutputError:
        if not overwrite:
            for path in committed:
                os.unlink(path)
        raise
    finally:
        for temp in temps.values():
            if os.path.exists(temp):
                os.unlink(temp)
    return {path: os.path.getsize(path) for path in paths}


def summary(assessment, invalidation, *, sizes, build_seconds):
    """Metadata only (never part of a content-addressed object): ids, statuses and counts, never values."""
    readiness, outcome = assessment["execution_readiness"], assessment["outcome"]
    candidates = assessment["candidates"]
    data = dict(
        result="WRITTEN", assessment_id=assessment["assessment_id"], symbol=assessment["inputs"]["symbol"],
        market_intelligence_id=assessment["provenance"]["market_intelligence_id"],
        options_intelligence_id=assessment["provenance"]["options_intelligence_id"],
        options_intelligence_format_version=readiness["options_intelligence_format_version"],
        policy_id=assessment["policy"]["policy_id"],
        market_intelligence_as_of=assessment["inputs"]["market_intelligence_ref"]["as_of"],
        options_intelligence_as_of=assessment["inputs"]["options_intelligence_ref"]["as_of"],
        input_gap_seconds=assessment["inputs"]["input_gap_seconds"],
        max_input_gap_seconds=assessment["policy"]["max_input_gap_seconds"],
        outcome=outcome["status"], no_setup_reasons=outcome["no_setup_reasons"],
        market_bias=assessment["market_bias"]["state"], eligible_side=assessment["market_bias"]["side"],
        contract_count=readiness["contract_count"], truncated=readiness["truncated"],
        quote_state_counts={c["key"]: c["count"] for c in readiness["quote_state_counts"]},
        usable_two_sided_quote_count=readiness["usable_two_sided_quote_count"],
        shares_per_contract_present_count=readiness["shares_per_contract_present_count"],
        candidate_count=len(candidates),
        premium_risk_status_counts=dict(sorted(Counter(c["derived"]["premium_risk_status"] for c in candidates).items())),
        rejection_counts={x["reason_code"]: x["count"] for x in assessment["rejections"]},
        screening_step=assessment["decision_trace"][-1]["result"] + "/" + assessment["decision_trace"][-1]["reason"],
        build_seconds=round(build_seconds, 3), output_bytes=sizes)
    if invalidation is not None:
        data.update(invalidation_id=invalidation["invalidation_id"], invalidation_result=invalidation["result"],
                    invalidation_reason=invalidation["reason"],
                    later_market_intelligence_as_of=invalidation["market_intelligence_ref"]["as_of"])
    return data


def main(argv=None, *, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m trade_setup.runner",
                                     description="Local, deterministic Trade Setup replay from sealed files.")
    parser.add_argument("--market-intelligence", required=True)
    parser.add_argument("--options-intelligence", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--assessment-output", required=True)
    parser.add_argument("--later-market-intelligence")
    parser.add_argument("--invalidation-output")
    parser.add_argument("--overwrite", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return OK if exit_.code == 0 else INVALID

    def fail(code, message):
        print(json.dumps(dict(result="FAILED", exit_code=code, error=message), sort_keys=True), file=err)
        return code

    if (args.later_market_intelligence is None) != (args.invalidation_output is None):
        return fail(INVALID, "--later-market-intelligence and --invalidation-output must be given together")
    inputs = [args.market_intelligence, args.options_intelligence, args.policy, args.later_market_intelligence]
    targets = [args.assessment_output] + ([args.invalidation_output] if args.invalidation_output else [])
    real = lambda p: os.path.realpath(p)  # noqa: E731
    if len({real(t) for t in targets}) != len(targets) or {real(t) for t in targets} & {real(p) for p in inputs if p}:
        return fail(INVALID, "output paths must be distinct from each other and from every input")
    try:
        mi = read_json(args.market_intelligence, "market intelligence")
        oi = read_json(args.options_intelligence, "options intelligence")
        policy = read_json(args.policy, "policy")
        later = read_json(args.later_market_intelligence, "later market intelligence") \
            if args.later_market_intelligence else None
        start = time.perf_counter()
        assessment, invalidation = build_outputs(mi, oi, policy, later)
        build_seconds = time.perf_counter() - start
    except (InputFileError, TradeSetupInputError) as error:
        return fail(INVALID, str(error))
    except IntegrityError as error:
        return fail(INTEGRITY, str(error))
    outputs = {args.assessment_output: canonical_text(assessment)}
    if invalidation is not None:
        outputs[args.invalidation_output] = canonical_text(invalidation)
    try:
        sizes = write_all(outputs, overwrite=args.overwrite)
    except OutputError as error:
        return fail(OUTPUT, str(error))
    print(json.dumps(summary(assessment, invalidation, sizes=list(sizes.values()), build_seconds=build_seconds),
                     sort_keys=True), file=out)
    return OK


if __name__ == "__main__":
    sys.exit(main())
