"""Pure builder: one OptionsSnapshot (plus an optional MarketIntelligence reference) -> one OptionsIntelligence.

``build(snapshot, market_intelligence=None, *, calendar=None)``:

- the snapshot (typed or canonical dict) is fully validated with the Phase 9B sealed validation;
- an optional MarketIntelligence is validated and **referenced only**. Nothing is derived from it: no call or
  put preference, filtering, direction or ranking;
- ``calendar`` (an object with ``previous_trading_day(date)``, e.g. ``market_data.calendar.ExchangeCalendar``)
  is used only to classify day records by session. When omitted, the default XNYS calendar is created lazily.

Everything is descriptive and exact or quantized to fixed precision:

- **Per contract:** days to expiration; exact strike relation and distances (unavailable without an underlying
  price); quote state, with mid and spread only for complete or locked quotes; volume and open-interest states;
  copied IV and provider Greeks with availability and sanity predicates; day age and session relation.
- **Chain and expiration activity:** volume totals count **current-session day records only**, because day
  records can come from older sessions. Open-interest totals count every present value. Put/call ratios have
  explicit null reasons.
- **Also:** IV summaries (count, min, max, median), completeness counts, and closed attention flags (no severity,
  priority or score).

No clock, environment, network, files, database or AI.
"""
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from market_data.models import EXCHANGE_TZ
from options_data import model as sm
from options_data.validation import validated_snapshot
from options_intelligence import model as m
from options_intelligence import rules as r
from options_intelligence.canonical import content_id
from options_intelligence.validation import (OptionsIntelligenceError, market_intelligence_reference,
                                             validated_options_intelligence)


def _d(value):
    return None if value is None else Decimal(value)


def _counts(values):
    return tuple(m.Count(k, n) for k, n in sorted(Counter(values).items()))


def _session_relation(observed, as_of_day, previous_day):
    if observed is None:
        return "unavailable"
    day = observed.astimezone(EXCHANGE_TZ).date()
    if day >= as_of_day:
        return "current_session"
    return "previous_session" if day >= previous_day else "older_session"


def _contract(c, as_of, as_of_day, previous_day, price, v2=False):
    i, q, day, oi, iv, g = (c[k] for k in ("identity", "quote", "day", "open_interest", "implied_volatility",
                                           "greeks"))
    cid, strike = i["contract_id"], Decimal(i["strike"])
    pointers = {f"contracts[{cid}].identity.strike", f"contracts[{cid}].identity.expiration", "as_of"}
    dte = (date.fromisoformat(i["expiration"]) - as_of_day).days
    relation, distance, relative = r.strike_relation(strike, price), None, None
    if price is not None:
        distance, relative = strike - price, r.ratio(strike - price, price)
        pointers.add("underlying_price.value")
    bid, ask = _d(q["bid"]), _d(q["ask"])
    state = r.quote_state(q["status"], bid, ask)
    mid = spread = spread_relative = None
    reason = r.QUOTE_NOT_TWO_SIDED
    if state in ("complete", "locked"):
        mid, spread = (bid + ask) / 2, ask - bid
        spread_relative, reason = (r.ratio(spread, mid), None) if mid > 0 else (None, r.ZERO_MID)
        pointers |= {f"contracts[{cid}].quote.bid", f"contracts[{cid}].quote.ask"}
    volume, oi_value = _d(day["volume"]), _d(oi["value"])
    volume_state, oi_state = r.activity_state(day["status"], volume), r.activity_state(oi["status"], oi_value)
    if volume is not None:
        pointers.add(f"contracts[{cid}].day.volume")
    if oi_value is not None:
        pointers.add(f"contracts[{cid}].open_interest.value")
    observed = datetime.fromisoformat(day["observed_at"]) if day["observed_at"] else None
    if observed:
        pointers.add(f"contracts[{cid}].day.observed_at")
    session = _session_relation(observed, as_of_day, previous_day)
    if iv["value"] is not None:
        pointers.add(f"contracts[{cid}].implied_volatility.value")
    greek_values = {name: _d(g[name]) for name in r.GREEK_FIELDS}
    available = tuple(name for name in r.GREEK_FIELDS if greek_values[name] is not None)
    pointers |= {f"contracts[{cid}].greeks.{name}" for name in available}
    fields = dict(
        contract_id=cid, provider_symbol=i["provider_symbol"], option_type=i["option_type"],
        expiration=i["expiration"], strike=i["strike"], dte_calendar_days=dte, expires_on_as_of_date=dte == 0,
        strike_relation=relation, strike_distance=r.text(distance), strike_distance_relative=r.text(relative),
        quote_state=state, mid=r.text(mid), spread_absolute=r.text(spread), spread_relative=r.text(spread_relative),
        spread_relative_reason=reason, volume_state=volume_state, open_interest_state=oi_state,
        volume_exceeds_open_interest=None if volume is None or oi_value is None else volume > oi_value,
        implied_volatility=m.ImpliedVolatility(iv["status"], iv["value"], iv["time_basis"]),
        greeks=m.Greeks(g["status"], g["delta"], g["gamma"], g["theta"], g["vega"], g["rho"], g["time_basis"],
                        available, tuple(n for n in r.GREEK_FIELDS if n not in available),
                        tuple(r.greeks_out_of_bounds(i["option_type"], greek_values))),
        day=m.Day(day["status"], day["observed_at"],
                  int((as_of - observed).total_seconds()) if observed else None, session),
        source_pointers=tuple(sorted(pointers)))
    if not v2:
        derived = m.Contract(**fields)
    else:
        current = r.text(volume) if volume is not None and session == "current_session" else None
        if current is not None:
            pointers.add(f"contracts[{cid}].day.volume")
        if c["terms"]["shares_per_contract"] is not None:
            pointers.add(f"contracts[{cid}].terms.shares_per_contract")
        fields["source_pointers"] = tuple(sorted(pointers | {f"contracts[{cid}].open_interest.time_basis"}))
        derived = m.ContractV2(**fields, current_session_volume=current, open_interest_value=oi["value"],
                               open_interest_time_basis=oi["time_basis"],
                               shares_per_contract=c["terms"]["shares_per_contract"])
    return derived, dict(volume=volume, oi=oi_value, iv=_d(iv["value"]))


def _activity(rows):
    """Totals and put/call ratios over (derived contract, raw values) rows."""
    fields = {}
    for kind in ("call", "put"):
        current = [raw["volume"] for c, raw in rows if c.option_type == kind and raw["volume"] is not None
                   and c.day.session_relation == "current_session"]
        interest = [raw["oi"] for c, raw in rows if c.option_type == kind and raw["oi"] is not None]
        fields[f"{kind}_volume_total"] = sum(current) if current else None
        fields[f"{kind}_volume_records"] = len(current)
        fields[f"{kind}_open_interest_total"] = sum(interest) if interest else None
        fields[f"{kind}_open_interest_records"] = len(interest)

    def put_call(put, call):
        if put is None or call is None:
            return None, r.TOTAL_UNAVAILABLE
        if call == 0:
            return None, r.ZERO_DENOMINATOR
        return r.text(r.ratio(put, call)), None

    volume_ratio, volume_reason = put_call(fields["put_volume_total"], fields["call_volume_total"])
    oi_ratio, oi_reason = put_call(fields["put_open_interest_total"], fields["call_open_interest_total"])
    return m.Activity(
        volume_basis=r.VOLUME_BASIS,
        call_volume_total=r.text(fields["call_volume_total"]), put_volume_total=r.text(fields["put_volume_total"]),
        call_volume_records=fields["call_volume_records"], put_volume_records=fields["put_volume_records"],
        volume_records_not_current_session=sum(1 for c, raw in rows if raw["volume"] is not None
                                               and c.day.session_relation != "current_session"),
        call_open_interest_total=r.text(fields["call_open_interest_total"]),
        put_open_interest_total=r.text(fields["put_open_interest_total"]),
        call_open_interest_records=fields["call_open_interest_records"],
        put_open_interest_records=fields["put_open_interest_records"],
        put_call_volume_ratio=volume_ratio, put_call_volume_ratio_reason=volume_reason,
        put_call_open_interest_ratio=oi_ratio, put_call_open_interest_ratio_reason=oi_reason)


def _iv_summary(rows, expiration=None, option_type=None):
    values = [raw["iv"] for c, raw in rows if raw["iv"] is not None]
    return m.IvSummary(expiration, option_type, len(values), len(rows) - len(values),
                       r.text(min(values)) if values else None, r.text(max(values)) if values else None,
                       r.text(r.median(values)) if values else None)


def _expiration(expiration, rows):
    strikes = defaultdict(set)
    for c, _ in rows:
        strikes[Decimal(c.strike)].add(c.option_type)
    return m.Expiration(
        expiration=expiration, dte_calendar_days=rows[0][0].dte_calendar_days, contract_count=len(rows),
        call_count=sum(c.option_type == "call" for c, _ in rows), put_count=sum(c.option_type == "put" for c, _ in rows),
        distinct_strike_count=len(strikes), strike_min=r.text(min(strikes)), strike_max=r.text(max(strikes)),
        paired_strike_count=sum(1 for kinds in strikes.values() if kinds == {"call", "put"}),
        quote_state_counts=_counts(c.quote_state for c, _ in rows),
        iv_available_count=sum(raw["iv"] is not None for _, raw in rows),
        greeks_available_count=sum(c.greeks.status == "present" for c, _ in rows),
        volume_present_count=sum(raw["volume"] is not None for _, raw in rows),
        open_interest_present_count=sum(raw["oi"] is not None for _, raw in rows),
        activity=_activity(rows))


def _attention(snapshot, contracts):
    flags = []

    def add(code, ids):
        ids = tuple(sorted(set(ids)))
        if ids:
            flags.append(m.Attention(r.ATTENTION_CODES[code], code, r.CONTRACT_SCOPE, len(ids), ids))

    add("quote_incomplete", (c.contract_id for c in contracts if c.quote_state not in ("complete", "locked", "crossed")))
    add("crossed_quote", (c.contract_id for c in contracts if c.quote_state == "crossed"))
    add("locked_quote", (c.contract_id for c in contracts if c.quote_state == "locked"))
    add("iv_unavailable", (c.contract_id for c in contracts if c.implied_volatility.status != "present"))
    add("greeks_unavailable", (c.contract_id for c in contracts if c.greeks.status != "present"))
    add("greeks_out_of_bounds", (c.contract_id for c in contracts if c.greeks.out_of_bounds_fields))
    add("no_volume", (c.contract_id for c in contracts if c.volume_state != "positive"))
    add("no_open_interest", (c.contract_id for c in contracts if c.open_interest_state != "positive"))
    add("day_not_current_session", (c.contract_id for c in contracts if c.day.session_relation != "current_session"))
    raw = snapshot["contracts"]
    add("facts_excluded_after_as_of", (c["identity"]["contract_id"] for c in raw
                                       if any(c[g]["status"] == sm.EXCLUDED_AFTER_AS_OF for g in sm.FACT_GROUPS)))
    add("time_basis_unverified", (c["identity"]["contract_id"] for c in raw
                                  if any(c[g]["time_basis"] == sm.PROVIDER_SNAPSHOT_UNVERIFIED for g in sm.FACT_GROUPS)))
    if snapshot["underlying_price"]["status"] != "present":
        flags.append(m.Attention(r.ATTENTION_CODES["underlying_price_unavailable"], "underlying_price_unavailable",
                                 r.CHAIN_SCOPE, 1, ()))
    record_exclusions = sum(e["count"] for e in snapshot["exclusions"] if e["reason"] in sm.RECORD_EXCLUSIONS)
    if record_exclusions:
        flags.append(m.Attention(r.ATTENTION_CODES["records_excluded"], "records_excluded", r.CHAIN_SCOPE,
                                 record_exclusions, ()))
    return tuple(sorted(flags, key=lambda a: (a.category, a.code)))


def build(snapshot, market_intelligence=None, *, calendar=None, format=r.OPTIONS_INTELLIGENCE_FORMAT_VERSION):
    """OptionsIntelligence for one valid OptionsSnapshot; raises OptionsSnapshotError / OptionsIntelligenceError.

    ``format`` is ``phase9-v1`` (default, unchanged) or ``phase9-v2`` (the Phase 10 compatibility amendment).
    """
    if format not in r.FORMATS:
        raise OptionsIntelligenceError("unsupported options intelligence format version")
    rules_version, v2 = r.FORMATS[format], format == r.OPTIONS_INTELLIGENCE_FORMAT_V2
    data = validated_snapshot(snapshot)  # Also enforces the supported snapshot format version.
    mi_ref = market_intelligence_reference(market_intelligence, data)
    if calendar is None:
        from market_data.calendar import default_calendar
        calendar = default_calendar()
    as_of = datetime.fromisoformat(data["as_of"])
    as_of_day = as_of.astimezone(EXCHANGE_TZ).date()
    previous_day = calendar.previous_trading_day(as_of_day)
    price_block = data["underlying_price"]
    price = Decimal(price_block["value"]) if price_block["status"] == "present" else None
    rows = [_contract(c, as_of, as_of_day, previous_day, price, v2) for c in data["contracts"]]
    contracts = tuple(c for c, _ in rows)
    by_expiration = defaultdict(list)
    by_type = defaultdict(list)
    for row in rows:
        by_expiration[row[0].expiration].append(row)
        by_type[(row[0].expiration, row[0].option_type)].append(row)
    observed = datetime.fromisoformat(price_block["observed_at"]) if price_block["observed_at"] else None
    provenance = data["provenance"]
    fields = dict(
        options_intelligence_format_version=format, rules_version=rules_version,
        snapshot_ref=m.SnapshotRef(data["snapshot_id"], data["snapshot_format_version"], data["underlying"],
                                   data["as_of"]),
        market_intelligence_ref=mi_ref,
        underlying=m.Underlying(data["underlying"], price_block["status"], price_block["value"],
                                price_block["observed_at"], int((as_of - observed).total_seconds()) if observed else None,
                                price_block["source"], price_block["reason"]),
        chain_completeness=m.Completeness(
            contract_count=len(contracts), expiration_count=len(by_expiration),
            records_received=provenance["records_received"], truncated=provenance["truncated"],
            exclusions=tuple(m.Count(e["reason"], e["count"]) for e in data["exclusions"]),
            status_counts=tuple(m.GroupCounts(g, _counts(c[g]["status"] for c in data["contracts"]))
                                for g in sm.FACT_GROUPS),
            contracts_with=tuple(m.Count(g, sum(c[g]["status"] == sm.PRESENT for c in data["contracts"]))
                                 for g in sm.FACT_GROUPS),
            day_session_relation_counts=_counts(c.day.session_relation for c in contracts)),
        expirations=tuple(_expiration(e, by_expiration[e]) for e in sorted(by_expiration)),
        contracts=contracts, activity=_activity(rows),
        volatility=m.Volatility(_iv_summary(rows), tuple(_iv_summary(by_type[k], *k) for k in sorted(by_type))),
        attention=_attention(data, contracts),
        provenance=m.Provenance(data["snapshot_id"], mi_ref.intelligence_id if mi_ref else None, rules_version,
                                r.POINTER_VERSION))
    draft = m.OptionsIntelligence(options_intelligence_id="", **fields)
    intelligence = replace(draft, options_intelligence_id=content_id(draft.body()))
    validated_options_intelligence(intelligence)  # Structural self-check.
    return intelligence
