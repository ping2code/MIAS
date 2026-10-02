"""Operator CLI for the artifact store. The only way artifacts enter the store; the HTTP API never takes a path.

    python -m artifact_store.runner publish --kind KIND --file FILE [--root DIR]
    python -m artifact_store.runner verify [--root DIR]

``--root`` defaults to the ``MIAS_ARTIFACT_ROOT`` process environment variable (never ``.env``); the root must
already exist. Kinds: ``market-intelligence``, ``options-intelligence``, ``trade-setup``, ``invalidation-check``,
``alert``.

- **publish** reads the source file (a regular, non-symlink file; never modified), validates it with the kind's domain
  validator, and stores its canonical bytes at ``<root>/<kind>/<hex>.json`` atomically and no-clobber. Publishing
  identical content again is ``ALREADY_PRESENT`` (exit 0). ``canonicalized`` reports whether the source bytes differed
  from the stored canonical form (for example, pretty-printed input).
- **verify** builds the full validated index read-only and prints per-kind counts. It writes nothing.

There is no delete, replace or purge. No network, Redis, PostgreSQL or provider access.

Output: one JSON line on stdout (metadata only); errors go to stderr as ``{"result": "FAILED", "exit_code", "error"}``.

Exit codes: 0 published, already present, or verified; 2 usage or invalid input (unknown kind, unreadable source,
not JSON, duplicate keys, too large); 3 store unavailable or conflict (missing or unsafe root, a different body under
the same id, a write failure); 4 artifact validation failure (domain validation, or a non-canonical/mismatched stored
file during verify).
"""
import argparse
import json
import os
import sys

from artifact_store import store
from artifact_store.errors import ArtifactInvalid, InvalidInput, InvalidQuery, StoreConflict, StoreUnavailable
from artifact_store.index import build_index
from artifact_store.kinds import KIND_NAMES, STORE_FORMAT_VERSION

OK, INVALID, STORE, VALIDATION = 0, 2, 3, 4
CODES = ((InvalidInput, INVALID), (InvalidQuery, INVALID), (ArtifactInvalid, VALIDATION),
         (StoreConflict, STORE), (StoreUnavailable, STORE))


def _root(args, environ):
    root = args.root or environ.get("MIAS_ARTIFACT_ROOT")
    if not root:
        raise StoreUnavailable("no artifact root: pass --root or set MIAS_ARTIFACT_ROOT")
    return root


def cmd_publish(args, environ):
    raw = store.read_source(args.file)
    result, parsed = store.publish(_root(args, environ), args.kind, raw)
    return dict(result=result.upper(), store_format_version=STORE_FORMAT_VERSION, kind=parsed.kind,
                artifact_id=parsed.artifact_id, symbol=parsed.symbol, as_of=parsed.as_of,
                bytes=len(parsed.canonical), canonicalized=raw != parsed.canonical)


def cmd_verify(args, environ):
    index = build_index(_root(args, environ))
    return dict(result="VERIFIED", store_format_version=STORE_FORMAT_VERSION, counts=index.counts())


def main(argv=None, *, out=None, err=None, environ=None):
    out, err = out or sys.stdout, err or sys.stderr
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python -m artifact_store.runner")
    commands = parser.add_subparsers(dest="command", required=True)
    publish = commands.add_parser("publish")
    publish.add_argument("--kind", required=True, choices=KIND_NAMES)
    publish.add_argument("--file", required=True)
    publish.add_argument("--root")
    verify = commands.add_parser("verify")
    verify.add_argument("--root")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return INVALID if exit_.code else OK
    try:
        summary = (cmd_publish if args.command == "publish" else cmd_verify)(args, environ)
    except tuple(error for error, _ in CODES) as error:
        code = next(c for e, c in CODES if isinstance(error, e))
        print(json.dumps(dict(command=args.command, result="FAILED", exit_code=code, error=str(error)),
                         sort_keys=True), file=err)
        return code
    print(json.dumps(dict(command=args.command, exit_code=OK, **summary), sort_keys=True), file=out)
    return OK


if __name__ == "__main__":
    sys.exit(main())
