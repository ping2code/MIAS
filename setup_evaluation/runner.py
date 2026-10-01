"""Local Setup Evaluation replay runner (Phase 11): sealed files in, canonical files out. The only I/O module.

    python -m setup_evaluation.runner evaluate --assessment A.json --snapshot SNAP.json --schedule SCHED.json \\
        --protocol PROTOCOL.json --horizon session_1|session_5 --output EVAL.json \\
        [--invalidation-check CHECK.json ...] [--overwrite]
    python -m setup_evaluation.runner build-schedule --from YYYY-MM-DD --through YYYY-MM-DD --output SCHED.json
    python -m setup_evaluation.runner test-protocol --output PROTOCOL.json

- **evaluate** reads every input as JSON (duplicate keys rejected; nothing repaired). It builds the SetupEvaluation
  with the pure core and verifies it by full re-derivation **before** writing it atomically. The write goes to a
  temporary file in the target directory, is fsynced, then hard-linked into place (no-clobber) or replaces the target
  with ``--overwrite``. It prints a metadata-only summary: ids, horizon, counts and the relation status. It never
  prints quotes, marks, returns or contract lists.
- **build-schedule** writes a sealed SessionSchedule from ``market_data.calendar`` (XNYS). For a final evaluation,
  build it after the target date, so known ad-hoc closures and early closes are included.
- **test-protocol** writes the sealed *test* protocol. A production protocol does not exist yet; activation is a
  separate step.

Local files only. No network, provider, database, AI or environment-driven semantics, and evaluation reads no clock.
Only ``build-schedule`` imports a calendar, and it does so outside the pure core.

Exit codes: 0 written; 2 usage error, or an unreadable, invalid or incompatible input; 3 a built evaluation failed
its own re-derivation; 4 the output exists without ``--overwrite``, or cannot be written.
"""
import argparse
import json
import os
import sys
import tempfile

from setup_evaluation.builder import evaluate, verify_evaluation
from setup_evaluation.canonical import canonical_json
from setup_evaluation.protocol import make_test_protocol
from setup_evaluation.schedule import ScheduleError, make_schedule
from setup_evaluation.validation import SetupEvaluationInputError

OK, INVALID, INTEGRITY, OUTPUT = 0, 2, 3, 4


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
    path = os.path.abspath(path)
    if not overwrite and os.path.lexists(path):
        raise OutputError("output file already exists (use --overwrite to replace it)")
    try:
        fd, temp = tempfile.mkstemp(prefix=".setup-evaluation-", suffix=".tmp", dir=os.path.dirname(path))
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
    except FileExistsError:
        raise OutputError("output file already exists (use --overwrite to replace it)") from None
    except OSError:
        raise OutputError("cannot write the output file") from None
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return os.path.getsize(path)


def summary(data, size):
    """Metadata only: ids, horizon and counts. No quotes, marks, returns or contract lists."""
    return dict(result="WRITTEN", evaluation_id=data["evaluation_id"], assessment_id=data["setup_ref"]["assessment_id"],
                snapshot_id=data["observation_ref"]["snapshot_id"], schedule_id=data["schedule_ref"]["schedule_id"],
                protocol_id=data["protocol_ref"]["protocol_id"], protocol_purpose=data["protocol_ref"]["purpose"],
                horizon=data["horizon"]["name"], target_session_date=data["horizon"]["target_session_date"],
                observation_as_of=data["observation_ref"]["as_of"], truncated=data["observation_ref"]["truncated"],
                candidate_count=data["summary"]["candidate_count"],
                outcome_status_counts={c["key"]: c["count"] for c in data["summary"]["outcome_status_counts"]},
                invalidation_relation=data["invalidation_relation"]["status"], output_bytes=size)


def build_schedule(start, end):
    """A sealed SessionSchedule from the XNYS calendar (tooling; never called by the pure core)."""
    from market_data.calendar import default_calendar
    calendar = default_calendar()
    return make_schedule([(day, calendar.session_times(day).open, calendar.session_times(day).close)
                          for day in calendar.trading_days(start, end)])


def main(argv=None, *, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m setup_evaluation.runner")
    commands = parser.add_subparsers(dest="command", required=True)
    ev = commands.add_parser("evaluate")
    for flag in ("--assessment", "--snapshot", "--schedule", "--protocol", "--output"):
        ev.add_argument(flag, required=True)
    ev.add_argument("--horizon", required=True, choices=("session_1", "session_5"))
    ev.add_argument("--invalidation-check", action="append", default=[])
    ev.add_argument("--overwrite", action="store_true")
    sc = commands.add_parser("build-schedule")
    sc.add_argument("--from", dest="start", required=True)
    sc.add_argument("--through", dest="end", required=True)
    sc.add_argument("--output", required=True)
    sc.add_argument("--overwrite", action="store_true")
    tp = commands.add_parser("test-protocol")
    tp.add_argument("--output", required=True)
    tp.add_argument("--overwrite", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return OK if exit_.code == 0 else INVALID

    def fail(code, message):
        print(json.dumps(dict(result="FAILED", exit_code=code, error=message), sort_keys=True), file=err)
        return code

    try:
        if args.command == "evaluate":
            inputs = [args.assessment, args.snapshot, args.schedule, args.protocol, *args.invalidation_check]
            if os.path.realpath(args.output) in {os.path.realpath(p) for p in inputs}:
                return fail(INVALID, "the output path must differ from every input")
            files = [read_json(args.assessment, "assessment"), read_json(args.snapshot, "snapshot"),
                     read_json(args.schedule, "schedule"), read_json(args.protocol, "protocol")]
            checks = [read_json(p, "invalidation check") for p in args.invalidation_check]
            data = evaluate(*files, args.horizon, checks).to_dict()
            try:
                verify_evaluation(data, *files, checks)
            except SetupEvaluationInputError as error:
                return fail(INTEGRITY, str(error))
            size = write_atomic(args.output, canonical_json(data) + "\n", overwrite=args.overwrite)
            report = summary(data, size)
        elif args.command == "build-schedule":
            from datetime import date
            try:
                start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
            except ValueError:
                return fail(INVALID, "--from and --through must be YYYY-MM-DD dates")
            if start > end:
                return fail(INVALID, "--from must not be after --through")
            schedule = build_schedule(start, end).to_dict()
            size = write_atomic(args.output, canonical_json(schedule) + "\n", overwrite=args.overwrite)
            report = dict(result="WRITTEN", schedule_id=schedule["schedule_id"], session_count=len(schedule["sessions"]),
                          first_session=schedule["sessions"][0]["session_date"],
                          last_session=schedule["sessions"][-1]["session_date"], output_bytes=size)
        else:
            protocol = make_test_protocol().to_dict()
            size = write_atomic(args.output, canonical_json(protocol) + "\n", overwrite=args.overwrite)
            report = dict(result="WRITTEN", protocol_id=protocol["protocol_id"], purpose=protocol["purpose"],
                          output_bytes=size)
    except (InputFileError, SetupEvaluationInputError, ScheduleError) as error:
        return fail(INVALID, str(error))
    except OutputError as error:
        return fail(OUTPUT, str(error))
    print(json.dumps(report, sort_keys=True), file=out)
    return OK


if __name__ == "__main__":
    sys.exit(main())
