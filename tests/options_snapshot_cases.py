"""Synthetic, provider-neutral source records for Phase 9B tests (test-only; no live API).

The shapes follow what the Phase 9A live META/NVDA checks proved for the Massive chain snapshot: contract details
(type, exercise style, expiration, shares per contract, strike), day OHLC/change/volume/VWAP, open interest, and
IV and Greeks when available (no rho, and no timestamps on IV, Greeks or open interest). Dedicated quotes and
trades were not entitled (HTTP 403), so the live-like plan marks ``quote`` and ``trade`` unavailable.
"""
from datetime import datetime, timedelta, timezone

from options_data.normalization import assemble

AS_OF = datetime(2026, 9, 30, 14, 45, tzinfo=timezone.utc)   # 10:45 ET, regular session
BEFORE = AS_OF - timedelta(minutes=1)
AFTER = AS_OF + timedelta(seconds=1)
LIVE_UNAVAILABLE = ("quote", "trade")                        # Phase 9A: 403 on quotes and trades
PROVENANCE = dict(provider="massive", adapter_version="synthetic-9b", endpoint_families=["chain_snapshot"],
                  configured_delay_seconds=None, pages_fetched=2, requests_made=2, truncated=True)


def symbol(root, expiration, option_type, strike):
    return f"O:{root}{expiration.replace('-', '')[2:]}{'C' if option_type == 'call' else 'P'}{int(strike * 1000):08d}"


def chain_record(root="META", expiration="2026-10-16", option_type="call", strike=700, *, underlying=None, iv=True,
                 greeks=True, oi=True, day=True, shares=100, **extra):
    """A live-like chain-snapshot record: details, day, open interest, IV, Greeks (no rho), no timestamps."""
    record = dict(provider_symbol=symbol(root, expiration, option_type, strike), underlying=underlying or root,
                  option_type=option_type, expiration=expiration, strike=str(strike),
                  terms=dict(exercise_style="american", shares_per_contract=shares))
    defaults = dict(
        day=dict(open="10", high="11.2", low="9.5", close="10.25", previous_close="9.8", change="0.45",
                 change_percent="4.59", volume=1200, vwap="10.4"),
        open_interest=dict(value=15000), implied_volatility=dict(value="0.4123"),
        greeks=dict(delta="0.55" if option_type == "call" else "-0.45", gamma="0.012", theta="-0.31", vega="0.92"))
    # True: the live-like default; a dict: that exact group; False: the group is absent (missing).
    for group, flag in (("day", day), ("open_interest", oi), ("implied_volatility", iv), ("greeks", greeks)):
        if isinstance(flag, dict):
            record[group] = flag
        elif flag:
            record[group] = defaults[group]
    record.update(extra)  # Any other group (quote, trade, open_interest=..., ...) given by its record name.
    return record


def meta_chain():
    """META-like: calls and puts over two expirations, every live-observed field present."""
    return [chain_record("META", exp, kind, strike) for exp in ("2026-10-16", "2026-10-23")
            for kind in ("call", "put") for strike in (690, 700, 710)]


def nvda_chain():
    """NVDA-like: only some contracts carry IV and Greeks (the rest omit them: missing)."""
    return [chain_record("NVDA", "2026-10-16", kind, strike, iv=strike % 10 == 0, greeks=strike % 10 == 0)
            for kind in ("call", "put") for strike in (185, 187.5, 190, 192.5)]


def live_snapshot(records, underlying="META", **kw):
    kw.setdefault("provenance", PROVENANCE)
    kw.setdefault("unavailable_groups", LIVE_UNAVAILABLE)
    kw.setdefault("calendar_state", "regular")
    return assemble(underlying, AS_OF, records, **kw)


def full_snapshot(records, underlying="META", **kw):
    """Every group available (for quote/trade semantics beyond the current plan)."""
    kw.setdefault("provenance", PROVENANCE)
    return assemble(underlying, AS_OF, records, **kw)
