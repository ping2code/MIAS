"""A compact, rebuildable read model of one OptionsIntelligence artifact, and its intraday comparison.

Derived only from fields the artifact already records (phase9-v2); nothing is persisted and the full artifact stays
the only source of truth. Pure: no I/O, no clock, no network. Descriptive only: no direction, recommendation,
contract pick, entry, exit, size, target or stop.

``summarize(data)`` (from a validated artifact) records:
- identity: format and rules versions, snapshot id, ``contract_count`` (= ``len(contracts)``), ``expiration_count``
  (= ``chain_completeness.expiration_count``);
- ``call_volume`` / ``put_volume``: ``activity.call_volume_total`` / ``activity.put_volume_total`` (Phase 9
  current-session day records only; ``None`` when the artifact has none);
- ``put_call_volume_ratio`` (+ reason): copied from ``activity``;
- ``iv_median``: ``volatility.overall.median`` (a decimal fraction, copied);
- ``volume_gt_oi_count``: contracts whose Phase 9 ``volume_exceeds_open_interest`` is true **and** whose day record is
  current-session (``day.session_relation``). Phase 9 compares whatever day record the contract has with its open
  interest, so before the open the relation describes the previous session; counting only current-session records
  keeps sessions apart, as the volume totals do;
- ``call_breadth`` / ``put_breadth``: distinct strikes of that type with a positive ``current_session_volume``;
- ``call_concentration`` / ``put_concentration``: where that type's current-session volume sits relative to the
  underlying, by Phase 9's own ``strike_relation`` (below / equal / above the underlying price): the relation holding
  the largest share (a tie is ``mixed``). ``unavailable`` when the artifact has no underlying price (phase9-v2 records
  none, so every contract's relation is ``unavailable``) or no current-session volume. No distance threshold is used,
  so there is no "near" or "far" category.

``compare(current, prior, prior_status)``: ``prior`` is the immediately previous valid report for the same symbol (the
index selects it); it is used only if it is from the same trading session, the America/New_York calendar date of
``as_of`` (Phase 9's session date). Otherwise every change is ``None`` and the comparison says why.
"""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("America/New_York")      # = market_data.models.EXCHANGE_TZ (Phase 9 session date)

CALL, PUT, BALANCED, UNAVAILABLE = "CALL", "PUT", "BALANCED", "UNAVAILABLE"
INSUFFICIENT_PRIOR = "INSUFFICIENT_PRIOR"

# Comparison status (closed)
COMPARABLE = "comparable"
NO_PRIOR = "no_prior_snapshot"                  # first report of this symbol in the store
PRIOR_OTHER_SESSION = "prior_from_other_session"
PRIOR_AMBIGUOUS = "prior_ambiguous"             # two reports share the previous as_of
COMPARISON_STATUSES = (COMPARABLE, NO_PRIOR, PRIOR_OTHER_SESSION, PRIOR_AMBIGUOUS)

# Change reasons (closed): why a change or percentage is null
NOT_COMPARABLE = "not_comparable"
VALUE_UNAVAILABLE = "value_unavailable"
PRIOR_ZERO = "prior_zero"

# Concentration (closed)
CONCENTRATION_LABELS = {"below": "below_spot", "equal": "at_spot", "above": "above_spot"}
MIXED = "mixed"
CONCENTRATION_UNAVAILABLE = "unavailable"

# Momentum extra state: a cumulative volume went down (a provider correction), so increments are not compared
VOLUME_CORRECTION = "VOLUME_CORRECTION"


@dataclass(frozen=True)
class Summary:
    symbol: str
    as_of: str
    session_date: str
    options_intelligence_format_version: str
    rules_version: str
    snapshot_id: str
    contract_count: int
    expiration_count: int
    call_volume: int | None
    put_volume: int | None
    put_call_volume_ratio: str | None
    put_call_volume_ratio_reason: str | None
    iv_median: str | None
    volume_gt_oi_count: int
    call_breadth: int
    put_breadth: int
    call_concentration: str
    put_concentration: str
    concentration_reason: str | None
    underlying_price_status: str


def session_date(as_of):
    """The trading-session date of a sealed as_of: its America/New_York calendar date."""
    return datetime.fromisoformat(as_of).astimezone(EXCHANGE_TZ).date().isoformat()


def _int(text):
    if text is None:
        return None
    value = Decimal(text)
    if value != value.to_integral_value() or value < 0:
        raise ValueError("volume total is not a non-negative integer")
    return int(value)


def _positive(text):
    if text is None:
        return False
    try:
        return Decimal(text) > 0
    except InvalidOperation:
        raise ValueError("current_session_volume is not a decimal") from None


def _concentration(contracts, option_type, price_available):
    if not price_available:
        return CONCENTRATION_UNAVAILABLE
    shares = {}
    for c in contracts:
        if c["option_type"] == option_type and _positive(c.get("current_session_volume")):
            relation = c["strike_relation"]
            if relation not in CONCENTRATION_LABELS:
                return CONCENTRATION_UNAVAILABLE
            shares[relation] = shares.get(relation, 0) + Decimal(c["current_session_volume"])
    if not shares:
        return CONCENTRATION_UNAVAILABLE
    top = max(shares.values())
    leaders = [relation for relation, volume in shares.items() if volume == top]
    return CONCENTRATION_LABELS[leaders[0]] if len(leaders) == 1 else MIXED


def summarize(data):
    """The compact summary of one validated OptionsIntelligence artifact (phase9-v2)."""
    ref, activity, contracts = data["snapshot_ref"], data["activity"], data["contracts"]
    price_available = data["underlying"]["price_status"] == "present"        # Phase 9: present | unavailable
    strikes = {"call": set(), "put": set()}
    for c in contracts:
        if _positive(c.get("current_session_volume")):
            strikes[c["option_type"]].add(Decimal(c["strike"]).normalize())
    reason = None
    if not price_available:
        reason = "underlying_price_unavailable"
    return Summary(
        symbol=ref["underlying"], as_of=ref["as_of"], session_date=session_date(ref["as_of"]),
        options_intelligence_format_version=data["options_intelligence_format_version"],
        rules_version=data["rules_version"], snapshot_id=ref["snapshot_id"], contract_count=len(contracts),
        expiration_count=data["chain_completeness"]["expiration_count"],
        call_volume=_int(activity["call_volume_total"]), put_volume=_int(activity["put_volume_total"]),
        put_call_volume_ratio=activity["put_call_volume_ratio"],
        put_call_volume_ratio_reason=activity["put_call_volume_ratio_reason"],
        iv_median=data["volatility"]["overall"]["median"],
        volume_gt_oi_count=sum(1 for c in contracts if c["volume_exceeds_open_interest"] is True
                               and c["day"]["session_relation"] == "current_session"),
        call_breadth=len(strikes["call"]), put_breadth=len(strikes["put"]),
        call_concentration=_concentration(contracts, "call", price_available),
        put_concentration=_concentration(contracts, "put", price_available),
        concentration_reason=reason, underlying_price_status=data["underlying"]["price_status"])


def _change(current, prior, comparable):
    """(absolute, percent text, reason): percent is relative to the prior value, two decimals."""
    if not comparable:
        return None, None, NOT_COMPARABLE
    if current is None or prior is None:
        return None, None, VALUE_UNAVAILABLE
    delta = current - prior
    if prior == 0:
        return delta, None, PRIOR_ZERO
    percent = (Decimal(delta) * 100 / Decimal(prior)).quantize(Decimal("0.01"))
    return delta, str(percent), None


def _side(call, put):
    if call is None or put is None:
        return UNAVAILABLE
    return CALL if call > put else PUT if put > call else BALANCED


def _momentum(call_delta, put_delta, comparable):
    if not comparable:
        return INSUFFICIENT_PRIOR
    if call_delta is None or put_delta is None:
        return UNAVAILABLE
    if call_delta < 0 or put_delta < 0:
        return VOLUME_CORRECTION
    return _side(call_delta, put_delta)


TREND_INSUFFICIENT = "Insufficient prior same-session snapshot for intraday trend."
TREND_UNAVAILABLE = "Current-session volume unavailable."
TREND_CORRECTION = "Cumulative volume decreased since the prior snapshot (provider correction); intraday trend not assessed."
_NOUN = {CALL: ("Calls", "call"), PUT: ("Puts", "put")}


def trend_summary(bias, momentum, call_breadth_change, put_breadth_change, new_volume=True):
    """A deterministic sentence from a closed rule set (no direction words, no prediction)."""
    if momentum == INSUFFICIENT_PRIOR:
        return TREND_INSUFFICIENT
    if bias == UNAVAILABLE or momentum == UNAVAILABLE:
        return TREND_UNAVAILABLE
    if momentum == VOLUME_CORRECTION:
        return TREND_CORRECTION
    if not new_volume and bias in _NOUN:
        text = f"{_NOUN[bias][0]} remain dominant; no new volume since the prior snapshot."
    elif not new_volume:
        text = "Activity broadly balanced; no new volume since the prior snapshot."
    elif bias == BALANCED and momentum == BALANCED:
        text = "Activity broadly balanced."
    elif bias == BALANCED:
        text = f"Activity balanced overall, while {_NOUN[momentum][1]} activity is accelerating."
    elif momentum == BALANCED:
        text = f"{_NOUN[bias][0]} remain dominant; new activity is balanced."
    elif bias == momentum:
        text = f"{_NOUN[bias][1].capitalize()} activity strengthening."
    else:
        text = f"{_NOUN[bias][0]} remain dominant, while {_NOUN[momentum][1]} activity is accelerating."
    widened = [name for name, change in (("Call", call_breadth_change), ("Put", put_breadth_change))
               if change is not None and change > 0]
    if widened:
        text += " " + " and ".join(widened) + " activity broadening across more strikes."
    return text


def compare(current, prior, prior_status):
    """The activity view fields for ``current`` (a Summary) against ``prior`` (Summary or None).

    ``prior_status`` is COMPARABLE when the index found exactly one immediately previous report, else NO_PRIOR or
    PRIOR_AMBIGUOUS. A prior from another session is downgraded here to PRIOR_OTHER_SESSION.
    """
    if prior_status not in COMPARISON_STATUSES:
        raise ValueError("unknown prior status")
    status = prior_status
    if status == COMPARABLE and (prior is None or prior.symbol != current.symbol):
        raise ValueError("a comparable prior must be a report of the same symbol")
    if status == COMPARABLE and prior.session_date != current.session_date:
        status = PRIOR_OTHER_SESSION
    if status == COMPARABLE and datetime.fromisoformat(prior.as_of) >= datetime.fromisoformat(current.as_of):
        raise ValueError("the prior report must be earlier (no lookahead)")
    ok = status == COMPARABLE
    p = prior if ok else None
    call_delta, call_pct, call_reason = _change(current.call_volume, p and p.call_volume, ok)
    put_delta, put_pct, put_reason = _change(current.put_volume, p and p.put_volume, ok)
    oi_delta = current.volume_gt_oi_count - p.volume_gt_oi_count if ok else None
    call_breadth_change = current.call_breadth - p.call_breadth if ok else None
    put_breadth_change = current.put_breadth - p.put_breadth if ok else None
    bias = _side(current.call_volume, current.put_volume)
    momentum = _momentum(call_delta, put_delta, ok)
    return dict(
        comparison=dict(status=status, prior_as_of=p.as_of if ok else None, session_date=current.session_date),
        call_volume_change=call_delta, call_volume_change_pct=call_pct, call_volume_change_reason=call_reason,
        put_volume_change=put_delta, put_volume_change_pct=put_pct, put_volume_change_reason=put_reason,
        volume_gt_oi_change=oi_delta, call_breadth_change=call_breadth_change, put_breadth_change=put_breadth_change,
        activity_bias=bias, momentum_15m=momentum,
        trend_summary=trend_summary(bias, momentum, call_breadth_change, put_breadth_change,
                                    new_volume=not (call_delta == 0 and put_delta == 0)))
