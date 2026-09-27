"""Phase 7D runner: build one Phase 7C evidence packet from durable MIAS data. Read-only and zero-network.

    python -m evidence_packet.runner --symbol META --as-of 2026-09-29T13:00:00Z \
        --market-context ctx.json [--news-lookback-hours 72] [--allow-partial] [--pretty] \
        [--output FILE] [--dry-run]

- ``--as-of`` is mandatory, ISO 8601 with a timezone, and normalized to UTC.
  There is no clock default and no implicit "latest".
- ``--market-context`` is a file produced by ``market_context.runner`` (that is
  where market data is fetched; this runner never fetches).
- Without ``--allow-partial``, a missing MarketContext file or a missing technical
  interval is a **source-incomplete** failure. With it, those domains are carried
  as Phase 7C ``unavailable/not_supplied``. Partial is never the default.
- **Output:** canonical Phase 7C JSON on stdout (or ``--output FILE``), UTF-8 with
  a trailing newline. ``--pretty`` is presentation only; ``packet_id`` always comes
  from the canonical serialization.
- **Diagnostics:** one JSON object on stderr (source counts, per-interval status,
  exclusion reasons). It never contains credentials, URLs of the database, raw
  bars, payloads or ``ai_*`` fields.
- **``--dry-run``:** prints the plan and performs no database or network access.

The database comes from the existing MIAS configuration (``DATABASE_URL``; never
``.env``). It is read in one read-only transaction (``SET TRANSACTION ... READ
ONLY`` on PostgreSQL).

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | packet built (or dry run) |
| 2 | invalid CLI or input (including an inconsistent MarketContext file) |
| 3 | source incomplete |
| 4 | integrity or hash verification failure |
| 5 | database configuration or read failure |
"""
import argparse
from datetime import datetime, timezone
import json
import os
import sys

from evidence_packet import models as m
from evidence_packet.loader import (MAX_LOOKBACK_HOURS, DatabaseFailure, InputError, LoadError, SourceIncomplete,
                                    load_market_context, load_sources)
from market_data.models import SYMBOL

DEFAULT_LOOKBACK_HOURS = 72
OK, INPUT, INCOMPLETE, INTEGRITY, DATABASE = 0, 2, 3, 4, 5


def parse_as_of(text):
    value = (text or "").strip()
    if value.endswith(("Z", "z")):
        value = value[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise InputError("--as-of must be ISO 8601") from None
    if parsed.utcoffset() is None:
        raise InputError("--as-of must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_symbol(text):
    symbol = (text or "").strip().upper()
    if not SYMBOL.fullmatch(symbol):
        raise InputError("--symbol must be a ticker (A-Z, 0-9, '.', '-'; at most 10 characters)")
    return symbol


def packet_text(packet, pretty):
    from evidence_packet.serialization import canonical_json
    data = packet.to_dict()
    text = json.dumps(data, sort_keys=True, indent=2, allow_nan=False) if pretty else canonical_json(data)
    return text + "\n"


def build(args, environ, *, engine=None, calendar=None):
    """(packet, diagnostics) or LoadError. The only I/O: the MarketContext file and read-only database reads."""
    from evidence_packet.assembler import assemble
    symbol, as_of = args.symbol, args.as_of
    if calendar is None:
        from market_data.calendar import default_calendar
        calendar = default_calendar()
    diagnostics = dict(symbol=symbol, as_of=as_of.isoformat(), news_lookback_hours=args.news_lookback_hours,
                       allow_partial=args.allow_partial)
    context, problems = None, []
    if args.market_context:
        try:
            context = load_market_context(args.market_context, symbol=symbol, as_of=as_of)
            diagnostics["market_context"] = dict(status="validated", as_of=context.as_of.isoformat())
        except SourceIncomplete:
            diagnostics["market_context"] = dict(status="missing_file")
            problems.append("market context file not found")
    else:
        diagnostics["market_context"] = dict(status="not_supplied")
        problems.append("--market-context not supplied")
    owned = engine is None
    if owned:
        from persistence.config import ConfigurationError, DatabaseSettings
        from persistence.database import make_engine
        try:
            engine = make_engine(DatabaseSettings.from_env(environ))
        except ConfigurationError:
            raise DatabaseFailure("database is not configured (DATABASE_URL)") from None
    try:
        rows, technical, events = load_sources(engine, symbol=symbol, as_of=as_of,
                                               lookback_hours=args.news_lookback_hours, calendar=calendar)
    finally:
        if owned:
            engine.dispose()
    diagnostics["technical"] = technical
    diagnostics["news"], diagnostics["sec"] = dict(events.diagnostics["news"]), dict(events.diagnostics["sec"])
    missing = [interval for interval in m.TECHNICAL_INTERVALS if interval not in rows]
    if missing:
        diagnostics["missing_intervals"] = missing
        problems.append(f"technical snapshots missing for {', '.join(missing)}")
    if problems and not args.allow_partial:
        error = SourceIncomplete("; ".join(problems) + " (strict mode; --allow-partial carries them as unavailable)")
        error.diagnostics = diagnostics
        raise error
    packet = assemble(symbol, as_of, calendar=calendar, market_context=context, technical_rows=rows,
                      news=m.NewsCollection(succeeded=True, inputs=events.inputs))
    included = {family: sum(1 for i in packet.news.items if i.facts.family == family) for family in ("news", "sec")}
    diagnostics["news"] = dict(events.diagnostics["news"], included=included["news"])
    diagnostics["sec"] = dict(events.diagnostics["sec"], included=included["sec"])
    diagnostics["excluded"] = {e.reason: e.count for e in packet.news.excluded}
    diagnostics["availability"] = {name: getattr(packet, name).availability.status
                                   for name in ("market_context", "technical", "news")}
    diagnostics["packet_id"] = packet.packet_id
    return packet, diagnostics


def plan(args):
    return dict(mode="dry_run", network=False, database_reads=False, symbol=args.symbol, as_of=args.as_of.isoformat(),
                market_context=dict(path=args.market_context,
                                    status=("not_supplied" if not args.market_context else
                                            "present" if os.path.isfile(args.market_context) else "missing")),
                technical_intervals=list(m.TECHNICAL_INTERVALS), news_lookback_hours=args.news_lookback_hours,
                durable_sources=["technical_snapshots", "events/event_versions/event_history/event_provenance "
                                 "(families: news, sec)"],
                allow_partial=args.allow_partial, packet_format_version=m.FORMAT_VERSION)


def main(argv=None, environ=None, *, engine=None, calendar=None, out=None, err=None):
    environ = os.environ if environ is None else environ
    out, err = out or sys.stdout, err or sys.stderr
    parser = argparse.ArgumentParser(prog="python -m evidence_packet.runner")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--market-context")
    parser.add_argument("--news-lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--allow-partial", action="store_true")
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output")
    parser.add_argument("--dry-run", action="store_true")
    try:
        args = parser.parse_args(argv)
        args.symbol, args.as_of = parse_symbol(args.symbol), parse_as_of(args.as_of)
        if not 1 <= args.news_lookback_hours <= MAX_LOOKBACK_HOURS:
            raise InputError(f"--news-lookback-hours must be between 1 and {MAX_LOOKBACK_HOURS}")
    except SystemExit:
        print(json.dumps(dict(error="usage", exit_code=INPUT)), file=err)
        return INPUT
    except InputError as error:
        print(json.dumps(dict(error=str(error), exit_code=INPUT)), file=err)
        return INPUT
    if args.dry_run:
        print(json.dumps(plan(args), indent=2, sort_keys=True), file=out)
        return OK
    try:
        packet, diagnostics = build(args, environ, engine=engine, calendar=calendar)
    except LoadError as error:
        report = dict(error=str(error), exit_code=error.exit_code)
        if getattr(error, "diagnostics", None) is not None:
            report["diagnostics"] = error.diagnostics
        print(json.dumps(report, sort_keys=True), file=err)
        return error.exit_code
    text = packet_text(packet, args.pretty)
    if args.output:
        with open(args.output, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
    else:
        out.write(text)
    print(json.dumps(dict(diagnostics=diagnostics), sort_keys=True), file=err)
    return OK


if __name__ == "__main__":
    sys.exit(main())
