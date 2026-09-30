"""Pure assembly: explicit, provider-neutral source records -> one sealed OptionsSnapshot.

``assemble(...)`` takes everything explicitly (``as_of`` included); it never reads the clock, environment, files or
network. A later adapter (Phase 9C) maps provider responses to these records; nothing provider-specific appears
here.

**Source record** (a dict; unknown keys make the record malformed)::

    provider_symbol   OCC-style option symbol (optionally "O:"-prefixed); required
    underlying        optional cross-check against the snapshot underlying
    option_type       optional cross-check ("call" | "put")
    expiration        optional cross-check (ISO date or date)
    strike            optional cross-check (Decimal, int or numeric string)
    terms             optional {exercise_style, shares_per_contract, deliverables: [{kind, symbol, amount}]}
    quote             optional {bid, ask, bid_size, ask_size, observed_at}
    trade             optional {price, size, observed_at}
    day               optional {open, high, low, close, previous_close, change, change_percent, volume, vwap,
                                observed_at}
    open_interest     optional {value, as_of_date, observed_at}
    implied_volatility optional {value, observed_at}
    greeks            optional {delta, gamma, theta, vega, rho, observed_at}

Numbers are Decimal, int or numeric strings (never floats); times are tz-aware datetimes or ISO strings.

**Fact status:** a group listed in ``unavailable_groups`` (the source or entitlement cannot provide it) is
``unavailable`` for every contract. A group that is absent, or has no values, is ``missing``. A zero value is
present. A group observed after ``as_of`` (or open interest dated after the ``as_of`` exchange date) becomes
``excluded_after_as_of``; the contract's identity and terms stay. Each such group counts once as
``fact_after_as_of``.

**Time basis:** ``observed_at`` when the source supplied an instant; ``provider_as_of_date`` for open interest with
a source date; otherwise ``provider_snapshot_unverified``. No date or time is ever invented.

**Record exclusions**, checked in this order:

1. identity: ``malformed_record`` (not a dict, unknown keys, no or unparseable symbol, unparseable cross-check
   fields), ``unsupported_option_type``, ``invalid_strike``, ``identity_mismatch`` (a cross-check field disagrees
   with the symbol, or the record names another underlying);
2. terms and fact values: ``malformed_record`` (bad terms, floats, non-finite, negative or non-whole numbers where
   not allowed, unknown group keys, naive times);
3. ``expired_before_as_of`` (expiration before the ``as_of`` exchange date).

Then duplicates:
identical copies keep one (``duplicate_identical``); conflicting copies of one ``contract_id`` are all excluded
(``identity_conflict``).

Caller errors (a bad ``as_of``, underlying, scope, provenance or underlying price, or data supplied for a group
declared unavailable) raise ``SnapshotAssemblyError``. The result is validated before it is returned.
"""
from collections import Counter
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from market_data.models import EXCHANGE_TZ, SYMBOL, format_decimal
from options_data import identity as ident
from options_data import model as m
from options_data.canonical import content_id
from options_data.validation import validated_snapshot

PROVENANCE_KEYS = ("provider", "adapter_version", "endpoint_families", "configured_delay_seconds", "pages_fetched",
                   "requests_made", "truncated")
SCOPE_KEYS = ("contract_types", "expiration_from", "expiration_through", "provider_page_limit",
              "provider_result_limit")
RECORD_KEYS = ("provider_symbol", "underlying", "option_type", "expiration", "strike", "terms") + m.FACT_GROUPS


class SnapshotAssemblyError(ValueError):
    """The caller supplied invalid assembly inputs (not a provider-data problem)."""


class _Excluded(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _number(value):
    """A finite Decimal from Decimal/int/numeric string; ValueError for floats, booleans or non-numbers."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ValueError("not a Decimal, int or numeric string")
    try:
        number = Decimal(value.strip() if isinstance(value, str) else value)
    except InvalidOperation:
        raise ValueError("not a number") from None
    if not number.is_finite():
        raise ValueError("not finite")
    return number


def _instant(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    else:
        raise ValueError("not an instant")
    if parsed.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _day(value):
    if isinstance(value, datetime):
        raise ValueError("a date, not a datetime, is required")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value.strip())
    raise ValueError("not a date")


def _group(name, raw, as_of, as_of_day, unavailable):
    """(group model, excluded_after_as_of?) for one fact group; raises _Excluded(malformed_record)."""
    fields = m.GROUP_VALUE_FIELDS[name]
    allowed = set(fields) | {"observed_at"} | ({"as_of_date"} if name == "open_interest" else set())
    empty = dict.fromkeys(fields)
    extras = dict(observed_at=None, time_basis=m.TIME_UNAVAILABLE)
    if name == "open_interest":
        extras["as_of_date"] = None
    if name in ("implied_volatility", "greeks"):
        extras["source"] = None
    model = {"quote": m.Quote, "trade": m.Trade, "day": m.Day, "open_interest": m.OpenInterest,
             "implied_volatility": m.ImpliedVolatility, "greeks": m.Greeks}[name]

    def blank(status):
        return model(status=status, **empty, **extras)

    if name in unavailable:
        if raw is not None:
            raise SnapshotAssemblyError(f"{name} was supplied for a group declared unavailable")
        return blank(m.UNAVAILABLE), False
    if raw is None:
        return blank(m.MISSING), False
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise _Excluded(m.MALFORMED_RECORD)
    values = {}
    try:
        for key in fields:
            if raw.get(key) is None:
                values[key] = None
                continue
            number = _number(raw[key])
            if key in m.NON_NEGATIVE[name] and number < 0:
                raise ValueError("negative")
            if key in m.WHOLE_NUMBER.get(name, ()) and number != number.to_integral_value():
                raise ValueError("not whole")
            values[key] = format_decimal(number)
        observed = _instant(raw["observed_at"]) if raw.get("observed_at") is not None else None
        oi_day = _day(raw["as_of_date"]) if name == "open_interest" and raw.get("as_of_date") is not None else None
    except (ValueError, TypeError):
        raise _Excluded(m.MALFORMED_RECORD) from None
    if all(v is None for v in values.values()):
        return blank(m.MISSING), False
    if (observed is not None and observed > as_of) or (oi_day is not None and oi_day > as_of_day):
        return blank(m.EXCLUDED_AFTER_AS_OF), True
    basis = m.OBSERVED_AT if observed else m.PROVIDER_AS_OF_DATE if oi_day else m.PROVIDER_SNAPSHOT_UNVERIFIED
    extras = dict(observed_at=observed.isoformat() if observed else None, time_basis=basis)
    if name == "open_interest":
        extras["as_of_date"] = oi_day.isoformat() if oi_day else None
    if name in ("implied_volatility", "greeks"):
        extras["source"] = m.PROVIDER_SOURCE
    return model(status=m.PRESENT, **values, **extras), False


def _terms(raw):
    if raw is None:
        return m.Terms(None, None, ())
    if not isinstance(raw, dict) or set(raw) - {"exercise_style", "shares_per_contract", "deliverables"}:
        raise _Excluded(m.MALFORMED_RECORD)
    try:
        style = raw.get("exercise_style")
        style = style.strip().lower() if isinstance(style, str) else style
        if style is not None and style not in m.EXERCISE_STYLES:
            raise ValueError("exercise style")
        shares = raw.get("shares_per_contract")
        shares = None if shares is None else _number(shares)
        if shares is not None and shares <= 0:
            raise ValueError("shares")
        deliverables = []
        for item in raw.get("deliverables") or ():
            if not isinstance(item, dict) or set(item) != {"kind", "symbol", "amount"} \
                    or not all(isinstance(item[k], str) and item[k] for k in ("kind", "symbol")):
                raise ValueError("deliverable")
            deliverables.append(m.Deliverable(item["kind"], item["symbol"], format_decimal(_number(item["amount"]))))
    except (ValueError, TypeError, AttributeError):
        raise _Excluded(m.MALFORMED_RECORD) from None
    if len(set(deliverables)) != len(deliverables):
        raise _Excluded(m.MALFORMED_RECORD)
    return m.Terms(style, None if shares is None else format_decimal(shares),
                   tuple(sorted(deliverables, key=lambda d: (d.kind, d.symbol, d.amount))))


def _contract(record, underlying, as_of, as_of_day, unavailable):
    """(Contract, excluded fact groups) for one source record; raises _Excluded(reason)."""
    if not isinstance(record, dict) or set(record) - set(RECORD_KEYS) or "provider_symbol" not in record:
        raise _Excluded(m.MALFORMED_RECORD)
    try:
        root, expiration, option_type, strike = ident.parse(record["provider_symbol"])
    except ident.IdentityError:
        raise _Excluded(m.MALFORMED_RECORD) from None
    stated_type = record.get("option_type")
    if stated_type is not None:
        if not isinstance(stated_type, str) or stated_type.strip().lower() not in m.OPTION_TYPES:
            raise _Excluded(m.UNSUPPORTED_OPTION_TYPE)
        if stated_type.strip().lower() != option_type:
            raise _Excluded(m.IDENTITY_MISMATCH)
    try:
        stated_strike = None if record.get("strike") is None else _number(record["strike"])
        stated_expiration = None if record.get("expiration") is None else _day(record["expiration"])
    except (ValueError, TypeError):
        raise _Excluded(m.MALFORMED_RECORD) from None
    if strike <= 0 or (stated_strike is not None and stated_strike <= 0):
        raise _Excluded(m.INVALID_STRIKE)
    if (stated_strike is not None and stated_strike != strike) \
            or (stated_expiration is not None and stated_expiration != expiration) \
            or (record.get("underlying") is not None and record["underlying"] != underlying):
        raise _Excluded(m.IDENTITY_MISMATCH)
    terms = _terms(record.get("terms"))
    groups, excluded = {}, 0
    for name in m.FACT_GROUPS:
        groups[name], after = _group(name, record.get(name), as_of, as_of_day, unavailable)
        excluded += after
    if expiration < as_of_day:
        raise _Excluded(m.EXPIRED_BEFORE_AS_OF)
    identity = m.Identity(contract_id=ident.contract_id(root, expiration, option_type, strike),
                          provider_symbol=record["provider_symbol"], root=root,
                          root_matches_underlying=root == underlying, option_type=option_type,
                          expiration=expiration.isoformat(), strike=format_decimal(strike))
    return m.Contract(identity=identity, terms=terms, **groups), excluded


def _scope(raw):
    raw = dict(raw or {})
    if set(raw) - set(SCOPE_KEYS):
        raise SnapshotAssemblyError("scope has unexpected keys")
    types = raw.get("contract_types") or ()
    if isinstance(types, str) or set(types) - set(m.OPTION_TYPES):
        raise SnapshotAssemblyError("scope.contract_types must be a collection of call/put")
    try:
        start = None if raw.get("expiration_from") is None else _day(raw["expiration_from"])
        end = None if raw.get("expiration_through") is None else _day(raw["expiration_through"])
    except (ValueError, TypeError):
        raise SnapshotAssemblyError("scope expiration bounds must be dates") from None
    for key in ("provider_page_limit", "provider_result_limit"):
        value = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise SnapshotAssemblyError(f"scope.{key} must be a non-negative integer")
    return m.Scope(tuple(sorted(set(types))), start and start.isoformat(), end and end.isoformat(),
                   raw.get("provider_page_limit"), raw.get("provider_result_limit"))


def _underlying_price(raw, as_of):
    if raw is None:
        return m.UnderlyingPrice("unavailable", None, None, None, "not_supplied")
    if not isinstance(raw, dict) or set(raw) - {"value", "observed_at", "source", "reason"}:
        raise SnapshotAssemblyError("underlying_price has unexpected keys")
    if raw.get("value") is None:
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason:
            raise SnapshotAssemblyError("an unavailable underlying_price needs a reason")
        return m.UnderlyingPrice("unavailable", None, None, None, reason)
    try:
        value, observed = _number(raw["value"]), _instant(raw.get("observed_at"))
    except (ValueError, TypeError):
        raise SnapshotAssemblyError("underlying_price needs a numeric value and a timezone-aware observed_at") \
            from None
    source = raw.get("source")
    if value <= 0 or not isinstance(source, str) or not source:
        raise SnapshotAssemblyError("underlying_price needs a positive value and a source")
    if observed > as_of:
        return m.UnderlyingPrice("unavailable", None, None, None, "observed_after_as_of")
    return m.UnderlyingPrice("present", format_decimal(value), observed.isoformat(), source, None)


def _provenance(raw, records_received, unavailable):
    raw = dict(raw or {})
    if set(raw) - set(PROVENANCE_KEYS) or not isinstance(raw.get("provider"), str) or not raw["provider"]:
        raise SnapshotAssemblyError("provenance needs a provider and only the documented keys")
    families = raw.get("endpoint_families") or ()
    if isinstance(families, str) or not all(isinstance(f, str) and f for f in families):
        raise SnapshotAssemblyError("provenance.endpoint_families must be strings")
    for key in ("configured_delay_seconds", "pages_fetched", "requests_made"):
        value = raw.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise SnapshotAssemblyError(f"provenance.{key} must be a non-negative integer")
    if not isinstance(raw.get("truncated", False), bool):
        raise SnapshotAssemblyError("provenance.truncated must be a boolean")
    return m.Provenance(
        provider=raw["provider"], adapter_version=raw.get("adapter_version"),
        endpoint_families=tuple(sorted(set(families))), configured_delay_seconds=raw.get("configured_delay_seconds"),
        pages_fetched=raw.get("pages_fetched"), requests_made=raw.get("requests_made"),
        truncated=raw.get("truncated", False), records_received=records_received,
        source_capabilities=tuple(m.Capability(g, "unavailable" if g in unavailable else "available")
                                  for g in m.FACT_GROUPS))


def _sort_key(contract):
    i = contract.identity
    return (i.expiration, i.option_type, Decimal(i.strike), i.contract_id)


def assemble(underlying, as_of, records, *, provenance, calendar_state=None, underlying_price=None, scope=None,
             unavailable_groups=()):
    """One sealed, validated OptionsSnapshot from explicit inputs (pure)."""
    if not isinstance(underlying, str) or not SYMBOL.fullmatch(underlying):
        raise SnapshotAssemblyError("underlying must be an upper-case ticker")
    if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
        raise SnapshotAssemblyError("as_of must be a timezone-aware datetime")
    if calendar_state is not None and calendar_state not in m.CALENDAR_STATES:
        raise SnapshotAssemblyError("calendar_state is not supported")
    unavailable = set(unavailable_groups)
    if unavailable - set(m.FACT_GROUPS):
        raise SnapshotAssemblyError("unavailable_groups must name fact groups")
    as_of = as_of.astimezone(timezone.utc)
    as_of_day = as_of.astimezone(EXCHANGE_TZ).date()
    records = list(records)
    exclusions, by_id = Counter(), {}
    for record in records:
        try:
            contract, after = _contract(record, underlying, as_of, as_of_day, unavailable)
        except _Excluded as excluded:
            exclusions[excluded.reason] += 1
            continue
        by_id.setdefault(contract.identity.contract_id, []).append((contract, after))
    contracts = []
    for copies in by_id.values():
        first = copies[0][0].to_dict()
        if all(c.to_dict() == first for c, _ in copies):
            contracts.append(copies[0][0])
            exclusions[m.FACT_AFTER_AS_OF] += copies[0][1]
            exclusions[m.DUPLICATE_IDENTICAL] += len(copies) - 1
        else:
            exclusions[m.IDENTITY_CONFLICT] += len(copies)
    draft = m.OptionsSnapshot(
        snapshot_format_version=m.SNAPSHOT_FORMAT_VERSION, snapshot_id="", underlying=underlying,
        as_of=as_of.isoformat(), session=m.Session(as_of_day.isoformat(), calendar_state),
        underlying_price=_underlying_price(underlying_price, as_of), scope=_scope(scope),
        contracts=tuple(sorted(contracts, key=_sort_key)),
        exclusions=tuple(m.Exclusion(r, c) for r, c in sorted(exclusions.items()) if c),
        provenance=_provenance(provenance, len(records), unavailable))
    snapshot = replace(draft, snapshot_id=content_id(draft.body()))
    validated_snapshot(snapshot)  # Self-check: an assembled snapshot always passes the sealed validation.
    return snapshot
