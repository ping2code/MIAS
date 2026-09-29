"""Read-only CLI: one local EvidencePacket JSON file → canonical EvidenceSynthesis JSON.

    python -m evidence_synthesis.runner --packet FILE [--output FILE] [--pretty]

- It reads exactly one local packet file (for example from ``python -m evidence_packet.runner``), validates it
  fail-closed and re-verifies ``packet_id``.
- It writes canonical synthesis JSON (UTF-8, trailing newline) to stdout or ``--output``. ``--pretty`` is
  presentation only; ``synthesis_id`` always comes from the canonical body.
- No database, network, clock, AI, ``.env`` or environment-dependent rules.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | synthesis produced |
| 2 | usage error, unreadable file, or an invalid packet |
"""
import argparse
import json
import sys

from evidence_synthesis.builder import synthesize
from evidence_synthesis.canonical import canonical_json
from evidence_synthesis.validation import PacketValidationError

OK, INVALID = 0, 2


def main(argv=None, *, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m evidence_synthesis.runner")
    parser.add_argument("--packet", required=True)
    parser.add_argument("--output")
    parser.add_argument("--pretty", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        print(json.dumps(dict(error="usage", exit_code=INVALID)), file=err)
        return INVALID
    try:
        with open(args.packet, encoding="utf-8") as handle:
            packet = json.load(handle)
        synthesis = synthesize(packet)
    except (OSError, UnicodeDecodeError, ValueError) as error:
        # PacketValidationError is a ValueError; JSON decode errors are ValueErrors too.
        kind = "invalid_packet" if isinstance(error, PacketValidationError) else "unreadable_packet"
        print(json.dumps(dict(error=kind, detail=str(error)[:200], exit_code=INVALID), sort_keys=True), file=err)
        return INVALID
    data = synthesis.to_dict()
    text = (json.dumps(data, sort_keys=True, indent=2, allow_nan=False) if args.pretty else canonical_json(data)) + "\n"
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        out.write(text)
    return OK


if __name__ == "__main__":
    sys.exit(main())
