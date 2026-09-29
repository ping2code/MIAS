"""Phase 7H read-only replay: re-run synthesis on local packet files and check that the output is byte-identical.

    python -m evidence_synthesis.replay --packet FILE [--repeat N]
    python -m evidence_synthesis.replay --corpus DIR [--repeat N]

- ``--packet``: one packet file.
- ``--corpus``: every ``*.packet.json`` in ``DIR``, in name order. If ``<name>.synthesis.json`` exists next to a
  packet, the replayed bytes are also compared with it.
- ``--repeat`` (default 100): the file text is re-parsed and re-synthesized N times. Every canonical synthesis must
  be byte-identical.

**Output:** one canonical JSON document on stdout. It has no timings, absolute paths, host or process details, so the
report itself is reproducible:

    {"replay_format_version": "phase7h-v1", "rules_version": ..., "synthesis_format_version": ...,
     "results": [{"file", "packet_id", "synthesis_id", "synthesis_sha256", "repeat_count", "identical",
                  "expected"}], "all_identical": ..., "all_expected_match": ...}

``expected`` is ``match``, ``mismatch`` or ``absent`` (no stored synthesis). ``all_expected_match`` ignores absent
entries.

**Exit codes:** 0 when everything is identical and matches, 1 on any difference, 2 on invalid or unreadable input or
bad usage (a JSON error on stderr).

Local files only. No database, network, clock or environment is used, and nothing is written.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from evidence_synthesis import rules
from evidence_synthesis.builder import synthesize
from evidence_synthesis.canonical import canonical_json
from evidence_synthesis.validation import PacketValidationError

REPLAY_FORMAT_VERSION = "phase7h-v1"
PACKET_SUFFIX = ".packet.json"
SYNTHESIS_SUFFIX = ".synthesis.json"
OK, DIFFERENT, INPUT = 0, 1, 2


class ReplayInputError(Exception):
    def __init__(self, code, file):
        super().__init__(code)
        self.code, self.file = code, file


def synthesis_text(packet_text):
    """Canonical synthesis JSON (plus newline) for one packet's JSON text."""
    return canonical_json(synthesize(json.loads(packet_text)).to_dict()) + "\n"


def replay_file(path, repeat):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
        first = synthesis_text(text)
        identical = all(synthesis_text(text) == first for _ in range(repeat - 1))
    except PacketValidationError:
        raise ReplayInputError("invalid_packet", path.name) from None
    except (OSError, UnicodeDecodeError, ValueError):
        raise ReplayInputError("unreadable_packet", path.name) from None
    data = json.loads(first)
    expected = "absent"
    if path.name.endswith(PACKET_SUFFIX):
        stored = path.with_name(path.name[:-len(PACKET_SUFFIX)] + SYNTHESIS_SUFFIX)
        if stored.is_file():
            expected = "match" if stored.read_text(encoding="utf-8") == first else "mismatch"
    return dict(file=path.name, packet_id=data["packet_ref"]["packet_id"], synthesis_id=data["synthesis_id"],
                synthesis_sha256=hashlib.sha256(first.encode("utf-8")).hexdigest(), repeat_count=repeat,
                identical=identical, expected=expected)


def report(results):
    return dict(replay_format_version=REPLAY_FORMAT_VERSION, synthesis_format_version=rules.SYNTHESIS_FORMAT_VERSION,
                rules_version=rules.RULES_VERSION, results=results,
                all_identical=all(r["identical"] for r in results),
                all_expected_match=all(r["expected"] != "mismatch" for r in results))


def main(argv=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m evidence_synthesis.replay")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--packet")
    source.add_argument("--corpus")
    parser.add_argument("--repeat", type=int, default=100)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return INPUT
    if args.repeat < 1:
        err.write(canonical_json(dict(error="usage", detail="--repeat must be >= 1")) + "\n")
        return INPUT
    try:
        if args.packet:
            paths = [Path(args.packet)]
        else:
            corpus = Path(args.corpus)
            if not corpus.is_dir():
                raise ReplayInputError("unreadable_corpus", corpus.name)
            paths = sorted(corpus.glob("*" + PACKET_SUFFIX))
            if not paths:
                raise ReplayInputError("empty_corpus", corpus.name)
        results = [replay_file(path, args.repeat) for path in paths]
    except ReplayInputError as exc:
        err.write(canonical_json(dict(error=exc.code, file=exc.file)) + "\n")
        return INPUT
    document = report(results)
    out.write(canonical_json(document) + "\n")
    return OK if document["all_identical"] and document["all_expected_match"] else DIFFERENT


if __name__ == "__main__":
    sys.exit(main())
