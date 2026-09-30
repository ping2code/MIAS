"""Options snapshot runner (Phase 9C): live chain -> OptionsSnapshot -> canonical snapshot file.

    OPTIONS_DATA_PROVIDER=massive OPTIONS_DATA_API_KEY=... python -m options_data.runner --symbol META \
        --output /tmp/META-options-snapshot.json [--contract-type call|put|all] [--expiration-from YYYY-MM-DD] \
        [--expiration-through YYYY-MM-DD] [--page-limit N] [--max-pages N] [--max-requests N] [--overwrite]

Steps:

1. Validate the configuration (``OPTIONS_DATA_*`` only; ``MARKET_DATA_*`` is never read).
2. Read the clock **once**: ``checked_at``.
3. ``as_of = checked_at - OPTIONS_DATA_DELAY_SECONDS``. If the delay is unset, ``as_of = checked_at`` and
   ``configured_delay_seconds`` is null: no stock-market delay is assumed. Facts timed after ``as_of`` are still
   excluded by the pure assembler.
4. Fetch the chain through the provider (bounded; a truncated fetch is recorded, never hidden).
5. Assemble and validate the snapshot with the pure Phase 9B layer. The session comes from the exchange
   calendar at ``as_of``.
6. Write the canonical JSON atomically: a temporary file in the target directory, fsync, then a no-clobber link
   (or a replace with ``--overwrite``). An existing file is never overwritten silently.
7. Print a compact metadata summary. The chain itself is never printed.

Scope options are collection bounds, never trading filters. ``--page-limit`` is results per provider page
(1-250); ``--max-pages`` (1-200) defaults to ``OPTIONS_DATA_MAX_PAGES``.

Exit codes: 0 written; 1 provider or data failure; 2 configuration or usage error (nothing fetched); 3 output
file exists or cannot be written.
"""
import argparse
from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
import os
import sys
import tempfile
import time

from market_data.models import SYMBOL
from options_data import model as m
from options_data.canonical import canonical_json
from options_data.config import OptionsDataConfigError, load_options_data_settings
from options_data.normalization import SnapshotAssemblyError, assemble
from options_data.provider import ChainScope, OptionsProviderError

OK, FAILED, USAGE, OUTPUT = 0, 1, 2, 3
MAX_PAGES, MAX_REQUESTS, MAX_PAGE_LIMIT = 200, 250, 250


class OutputError(Exception):
    pass


def write_atomic(path, text, *, overwrite=False):
    """Write ``text`` to ``path`` atomically; never clobber an existing file unless ``overwrite``."""
    path = os.path.abspath(path)
    if not overwrite and os.path.lexists(path):
        raise OutputError("output file already exists (use --overwrite to replace it)")
    directory = os.path.dirname(path)
    try:
        fd, temp = tempfile.mkstemp(prefix=".options-snapshot-", suffix=".tmp", dir=directory)
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
            os.link(temp, path)  # Atomic and fails if the target appeared meanwhile.
            os.unlink(temp)
    except FileExistsError:
        raise OutputError("output file already exists (use --overwrite to replace it)") from None
    except OSError:
        raise OutputError("cannot write the output file") from None
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    return os.path.getsize(path)


def summary(snapshot, *, checked_at, output, size, timings, requests_made):
    statuses = {group: dict(sorted(Counter(getattr(c, group).status for c in snapshot.contracts).items()))
                for group in m.FACT_GROUPS}
    return dict(
        result="WRITTEN", snapshot_id=snapshot.snapshot_id, snapshot_format_version=snapshot.snapshot_format_version,
        symbol=snapshot.underlying, checked_at=checked_at.isoformat(), as_of=snapshot.as_of,
        session=snapshot.session.to_dict(), configured_delay_seconds=snapshot.provenance.configured_delay_seconds,
        contracts=len(snapshot.contracts), exclusions={e.reason: e.count for e in snapshot.exclusions},
        pages_fetched=snapshot.provenance.pages_fetched, requests_made=requests_made,
        truncated=snapshot.provenance.truncated, scope=snapshot.scope.to_dict(),
        underlying_price=dict(status=snapshot.underlying_price.status, reason=snapshot.underlying_price.reason),
        group_statuses=statuses, output=output, file_bytes=size, timings_seconds=timings)


def _scope(args, settings):
    types = ("call", "put") if args.contract_type == "all" else (args.contract_type,)
    for value in (args.expiration_from, args.expiration_through):
        if value is not None:
            date.fromisoformat(value)
    if args.expiration_from and args.expiration_through and args.expiration_from > args.expiration_through:
        raise ValueError("expiration range is inverted")
    return ChainScope(contract_types=types, expiration_from=args.expiration_from,
                      expiration_through=args.expiration_through, page_size=args.page_limit,
                      max_pages=args.max_pages or settings.max_pages)


def main(argv=None, environ=None, *, provider=None, clock=None, calendar=None, out=None, session=None, sleep=None):
    environ = os.environ if environ is None else environ
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m options_data.runner")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--contract-type", choices=("call", "put", "all"), default="all")
    parser.add_argument("--expiration-from")
    parser.add_argument("--expiration-through")
    parser.add_argument("--page-limit", type=int, default=250)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--overwrite", action="store_true")

    def fail(code, **fields):
        print(json.dumps(dict(result="NOT WRITTEN", **fields), sort_keys=True), file=out)
        return code

    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return fail(USAGE, reason="usage error")
    symbol = args.symbol.strip().upper()
    if not SYMBOL.fullmatch(symbol) or not 1 <= args.page_limit <= MAX_PAGE_LIMIT \
            or (args.max_pages is not None and not 1 <= args.max_pages <= MAX_PAGES) \
            or (args.max_requests is not None and not 1 <= args.max_requests <= MAX_REQUESTS):
        return fail(USAGE, reason="usage error: symbol, --page-limit (1-250), --max-pages (1-200) or "
                                  "--max-requests (1-250)")
    try:
        settings = load_options_data_settings(environ)
        scope = _scope(args, settings)
    except OptionsDataConfigError as error:
        return fail(USAGE, reason=str(error))
    except ValueError:
        return fail(USAGE, reason="usage error: expiration bounds must be ISO dates in order")
    if provider is None:
        if settings.provider != "massive":
            return fail(USAGE, reason="OPTIONS_DATA_PROVIDER must be massive")
        from options_data.massive import MassiveOptionsProvider
        provider = MassiveOptionsProvider(settings, session=session, sleep=sleep,
                                          max_requests=args.max_requests or scope.max_pages * 3)
    if calendar is None:
        from market_data.calendar import default_calendar
        calendar = default_calendar()
    if not args.overwrite and os.path.lexists(args.output):
        return fail(OUTPUT, reason="output file already exists (use --overwrite to replace it)")

    checked_at = (clock or (lambda: datetime.now(timezone.utc)))()  # The only clock read.
    delay = settings.delay_seconds
    as_of = checked_at - timedelta(seconds=delay) if delay is not None else checked_at
    timings = {}
    started = time.perf_counter()
    try:
        result = provider.get_chain(symbol, scope)
    except OptionsProviderError as error:
        return fail(FAILED, kind=error.kind, error=str(error))
    timings["fetch"] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    try:
        snapshot = assemble(symbol, as_of, result.records, calendar_state=calendar.classify(as_of).value,
                            underlying_price=result.underlying_price, scope=scope.as_snapshot_scope(),
                            unavailable_groups=result.unavailable_groups,
                            provenance=dict(provider=result.provider, adapter_version=result.adapter_version,
                                            endpoint_families=list(result.endpoint_families),
                                            configured_delay_seconds=delay, pages_fetched=result.pages_fetched,
                                            requests_made=result.requests_made, truncated=result.truncated))
    except (SnapshotAssemblyError, ValueError) as error:
        return fail(FAILED, kind="assembly", error=str(error))
    text = canonical_json(snapshot.to_dict()) + "\n"
    timings["normalize_assemble_validate"] = round(time.perf_counter() - started, 3)
    started = time.perf_counter()
    try:
        size = write_atomic(args.output, text, overwrite=args.overwrite)
    except OutputError as error:
        return fail(OUTPUT, reason=str(error))
    timings["write"] = round(time.perf_counter() - started, 3)
    print(json.dumps(summary(snapshot, checked_at=checked_at, output=os.path.abspath(args.output), size=size,
                             timings=timings, requests_made=result.requests_made), indent=2, sort_keys=True),
          file=out)
    return OK


if __name__ == "__main__":
    sys.exit(main())
