"""Fail-closed validation of a sealed OptionsSnapshot (pure). Nothing is ever repaired.

Accepts a typed ``OptionsSnapshot`` (via ``to_dict()``) or its canonical dict, and checks:

- the version, the exact 10 top-level keys and exact keys at every level;
- ``snapshot_id`` recomputes from the canonical body;
- the underlying symbol, and a canonical UTC ``as_of``; the session date is the ``as_of`` exchange date;
- the underlying price: present (value > 0, observed at or before ``as_of``) or unavailable (with a reason);
- the scope, provenance and source capabilities;
- every contract:
  - its identity re-derives from the provider symbol;
  - it is unique, in canonical order, and not expired before the ``as_of`` exchange date;
  - terms, and every fact group's status/value/time-basis combination; numbers are canonical Decimal strings,
    non-negative where required and whole where required; observed times and dates are not after the cutoff;
- the exclusions: sorted, unique and positive; ``fact_after_as_of`` equals the number of excluded fact groups,
  and ``records_received`` equals the contracts plus the record-level exclusions.

Incomplete data (missing or unavailable groups, partial quotes, no IV or Greeks, no underlying price) is valid.
Crossed and locked quotes are source facts and are valid.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re

from market_data.models import EXCHANGE_TZ, SYMBOL, format_decimal
from options_data import identity as ident
from options_data import model as m
from options_data.canonical import content_id

CONTENT_ID = re.compile(r"sha256:[0-9a-f]{64}")


class OptionsSnapshotError(ValueError):
    """The input is not a valid phase9-snapshot-v1 OptionsSnapshot (the message names the problem)."""


def _require(condition, message):
    if not condition:
        raise OptionsSnapshotError(message)


def _keys(value, expected, name):
    _require(isinstance(value, dict) and set(value) == set(expected), f"{name} has unexpected keys")


def canonical_instant(value, name):
    """A tz-aware ISO instant that is already in canonical UTC form; returns the datetime."""
    _require(isinstance(value, str), f"{name} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise OptionsSnapshotError(f"{name} is not ISO 8601") from None
    _require(parsed.utcoffset() is not None, f"{name} must be timezone-aware")
    _require(parsed.astimezone(timezone.utc).isoformat() == value, f"{name} must be canonical UTC")
    return parsed


def iso_day(value, name):
    _require(isinstance(value, str), f"{name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise OptionsSnapshotError(f"{name} is not an ISO date") from None
    _require(parsed.isoformat() == value, f"{name} must be a canonical ISO date")
    return parsed


def decimal_text(value, name):
    """A canonical Decimal string (as rendered by market_data.models.format_decimal)."""
    _require(isinstance(value, str), f"{name} must be a Decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise OptionsSnapshotError(f"{name} is not a number") from None
    _require(number.is_finite() and format_decimal(number) == value, f"{name} must be a canonical Decimal string")
    return number


def _optional_int(value, name):
    _require(value is None or (isinstance(value, int) and not isinstance(value, bool) and value >= 0),
             f"{name} must be a non-negative integer or null")


def _group(contract, group, as_of, as_of_day, capability):
    data = contract[group]
    fields = m.GROUP_VALUE_FIELDS[group]
    extra = ("observed_at", "time_basis") + (("as_of_date",) if group == "open_interest" else ()) \
        + (("source",) if group in ("implied_volatility", "greeks") else ())
    _keys(data, ("status",) + fields + extra, f"{group}")
    status = data["status"]
    _require(status in m.FACT_STATUSES, f"{group}.status is not supported")
    _require((status == m.UNAVAILABLE) == (capability == "unavailable"),
             f"{group}.status is inconsistent with the source capability")
    if status != m.PRESENT:
        _require(all(data[k] is None for k in fields + extra if k != "time_basis")
                 and data["time_basis"] == m.TIME_UNAVAILABLE, f"{group} is {status} but carries values")
        return 1 if status == m.EXCLUDED_AFTER_AS_OF else 0
    _require(any(data[k] is not None for k in fields), f"{group} is present but has no value")
    for key in fields:
        if data[key] is None:
            continue
        number = decimal_text(data[key], f"{group}.{key}")
        if key in m.NON_NEGATIVE[group]:
            _require(number >= 0, f"{group}.{key} must be non-negative")
        if key in m.WHOLE_NUMBER.get(group, ()):
            _require(number == number.to_integral_value(), f"{group}.{key} must be a whole number")
    basis = data["time_basis"]
    _require(basis in m.GROUP_TIME_BASES[group], f"{group}.time_basis is not supported for this group")
    _require((data["observed_at"] is not None) == (basis == m.OBSERVED_AT),
             f"{group}.observed_at is inconsistent with its time_basis")
    if data["observed_at"] is not None:
        _require(canonical_instant(data["observed_at"], f"{group}.observed_at") <= as_of,
                 f"{group}.observed_at is later than as_of")
    if group == "open_interest":
        # provider_as_of_date requires the source date; provider_snapshot_unverified never carries one; with an
        # observed timestamp, a date the source also supplied is kept (never invented).
        _require(data["as_of_date"] is not None if basis == m.PROVIDER_AS_OF_DATE
                 else data["as_of_date"] is None if basis == m.PROVIDER_SNAPSHOT_UNVERIFIED else True,
                 "open_interest.as_of_date is inconsistent with its time_basis")
        if data["as_of_date"] is not None:
            _require(iso_day(data["as_of_date"], "open_interest.as_of_date") <= as_of_day,
                     "open_interest.as_of_date is later than the as_of date")
    if group in ("implied_volatility", "greeks"):
        _require(data["source"] == m.PROVIDER_SOURCE, f"{group}.source must be provider")
    return 0


def _contract(contract, underlying, as_of, as_of_day, capabilities):
    _keys(contract, ("identity", "terms") + m.FACT_GROUPS, "contract")
    identity = contract["identity"]
    _keys(identity, ("contract_id", "provider_symbol", "root", "root_matches_underlying", "option_type",
                     "expiration", "strike"), "contract identity")
    try:
        root, expiration, option_type, strike = ident.parse(identity["provider_symbol"])
        derived = ident.contract_id(root, expiration, option_type, strike)
    except ident.IdentityError:
        raise OptionsSnapshotError("contract provider_symbol is not a valid OCC-style symbol") from None
    _require((identity["contract_id"], identity["root"], identity["option_type"], identity["expiration"],
              identity["root_matches_underlying"])
             == (derived, root, option_type, expiration.isoformat(), root == underlying),
             "contract identity does not match its provider_symbol")
    _require(identity["option_type"] in m.OPTION_TYPES, "contract option_type is not supported")
    _require(decimal_text(identity["strike"], "contract strike") == strike, "contract strike does not match its symbol")
    _require(strike > 0, "contract strike must be positive")
    _require(expiration >= as_of_day, "contract expired before the as_of date")
    terms = contract["terms"]
    _keys(terms, ("exercise_style", "shares_per_contract", "deliverables"), "contract terms")
    _require(terms["exercise_style"] is None or terms["exercise_style"] in m.EXERCISE_STYLES,
             "contract exercise_style is not supported")
    if terms["shares_per_contract"] is not None:
        _require(decimal_text(terms["shares_per_contract"], "shares_per_contract") > 0,
                 "shares_per_contract must be positive")
    deliverables = terms["deliverables"]
    _require(isinstance(deliverables, list), "contract deliverables must be a list")
    keys = []
    for item in deliverables:
        _keys(item, ("kind", "symbol", "amount"), "deliverable")
        _require(all(isinstance(item[k], str) and item[k] for k in ("kind", "symbol")), "deliverable is malformed")
        decimal_text(item["amount"], "deliverable amount")
        keys.append((item["kind"], item["symbol"], item["amount"]))
    _require(keys == sorted(set(keys)), "contract deliverables must be sorted and unique")
    return sum(_group(contract, g, as_of, as_of_day, capabilities[g]) for g in m.FACT_GROUPS)


def _sort_key(contract):
    i = contract["identity"]
    return (i["expiration"], i["option_type"], Decimal(i["strike"]), i["contract_id"])


def validated_snapshot(snapshot):
    """The snapshot as a plain dict, after every check. Typed snapshots use their own to_dict()."""
    if isinstance(snapshot, m.OptionsSnapshot):
        data = snapshot.to_dict()
    elif isinstance(snapshot, dict):
        data = snapshot
    else:
        raise OptionsSnapshotError("input must be an OptionsSnapshot or its canonical dict")
    _require(set(data) == set(m.TOP_LEVEL_FIELDS), "snapshot must have exactly the 10 phase9-snapshot-v1 keys")
    _require(data["snapshot_format_version"] == m.SNAPSHOT_FORMAT_VERSION, "unsupported snapshot format version")
    _require(isinstance(data["snapshot_id"], str) and CONTENT_ID.fullmatch(data["snapshot_id"]),
             "snapshot_id is malformed")
    try:
        recomputed = content_id({k: v for k, v in data.items() if k != "snapshot_id"})
    except (TypeError, ValueError):
        raise OptionsSnapshotError("snapshot body is not canonical JSON") from None
    _require(recomputed == data["snapshot_id"], "snapshot_id does not match the snapshot body (tampered or corrupt)")
    underlying = data["underlying"]
    _require(isinstance(underlying, str) and SYMBOL.fullmatch(underlying), "underlying is malformed")
    as_of = canonical_instant(data["as_of"], "as_of")
    as_of_day = as_of.astimezone(EXCHANGE_TZ).date()

    session = data["session"]
    _keys(session, ("session_date", "calendar_state"), "session")
    _require(iso_day(session["session_date"], "session.session_date") == as_of_day,
             "session.session_date must be the as_of exchange date")
    _require(session["calendar_state"] is None or session["calendar_state"] in m.CALENDAR_STATES,
             "session.calendar_state is not supported")

    price = data["underlying_price"]
    _keys(price, ("status", "value", "observed_at", "source", "reason"), "underlying_price")
    if price["status"] == "present":
        _require(decimal_text(price["value"], "underlying_price.value") > 0, "underlying_price.value must be positive")
        _require(canonical_instant(price["observed_at"], "underlying_price.observed_at") <= as_of,
                 "underlying_price.observed_at is later than as_of")
        _require(isinstance(price["source"], str) and price["source"] and price["reason"] is None,
                 "underlying_price source/reason is inconsistent with present")
    else:
        _require(price["status"] == "unavailable", "underlying_price.status is not supported")
        _require(price["value"] is None and price["observed_at"] is None and price["source"] is None
                 and isinstance(price["reason"], str) and price["reason"],
                 "unavailable underlying_price must carry only a reason")

    scope = data["scope"]
    _keys(scope, ("contract_types", "expiration_from", "expiration_through", "provider_page_limit",
                  "provider_result_limit"), "scope")
    types = scope["contract_types"]
    _require(isinstance(types, list) and types == sorted(set(types)) and set(types) <= set(m.OPTION_TYPES),
             "scope.contract_types must be a sorted subset of call/put")
    bounds = [iso_day(scope[k], f"scope.{k}") if scope[k] is not None else None
              for k in ("expiration_from", "expiration_through")]
    _require(None in bounds or bounds[0] <= bounds[1], "scope expiration range is inverted")
    for key in ("provider_page_limit", "provider_result_limit"):
        _optional_int(scope[key], f"scope.{key}")

    provenance = data["provenance"]
    _keys(provenance, ("provider", "adapter_version", "endpoint_families", "configured_delay_seconds",
                       "pages_fetched", "requests_made", "truncated", "records_received", "source_capabilities"),
          "provenance")
    _require(isinstance(provenance["provider"], str) and provenance["provider"], "provenance.provider is required")
    _require(provenance["adapter_version"] is None or isinstance(provenance["adapter_version"], str),
             "provenance.adapter_version is malformed")
    families = provenance["endpoint_families"]
    _require(isinstance(families, list) and all(isinstance(f, str) and f for f in families)
             and families == sorted(set(families)), "provenance.endpoint_families must be sorted and unique")
    for key in ("configured_delay_seconds", "pages_fetched", "requests_made"):
        _optional_int(provenance[key], f"provenance.{key}")
    _require(isinstance(provenance["truncated"], bool), "provenance.truncated must be a boolean")
    _optional_int(provenance["records_received"], "provenance.records_received")
    _require(provenance["records_received"] is not None, "provenance.records_received is required")
    capabilities = provenance["source_capabilities"]
    _require(isinstance(capabilities, list) and [c.get("group") if isinstance(c, dict) else None
                                                 for c in capabilities] == list(m.FACT_GROUPS),
             "provenance.source_capabilities must list every fact group in order")
    for c in capabilities:
        _keys(c, ("group", "status"), "source capability")
        _require(c["status"] in ("available", "unavailable"), "source capability status is not supported")
    capability = {c["group"]: c["status"] for c in capabilities}

    contracts = data["contracts"]
    _require(isinstance(contracts, list), "contracts must be a list")
    excluded_groups = sum(_contract(c, underlying, as_of, as_of_day, capability) for c in contracts)
    ids = [c["identity"]["contract_id"] for c in contracts]
    _require(len(ids) == len(set(ids)), "duplicate contract_id")
    keys = [_sort_key(c) for c in contracts]
    _require(keys == sorted(keys), "contracts are not in canonical order")

    exclusions = data["exclusions"]
    _require(isinstance(exclusions, list), "exclusions must be a list")
    reasons = []
    for e in exclusions:
        _keys(e, ("reason", "count"), "exclusion")
        _require(e["reason"] in m.EXCLUSION_REASONS, "exclusion reason is not supported")
        _require(isinstance(e["count"], int) and not isinstance(e["count"], bool) and e["count"] >= 1,
                 "exclusion count must be a positive integer")
        reasons.append(e["reason"])
    _require(reasons == sorted(set(reasons)), "exclusions must be sorted and unique")
    counts = {e["reason"]: e["count"] for e in exclusions}
    _require(counts.get(m.FACT_AFTER_AS_OF, 0) == excluded_groups,
             "fact_after_as_of count is inconsistent with the excluded fact groups")
    _require(provenance["records_received"] == len(contracts) + sum(counts.get(r, 0) for r in m.RECORD_EXCLUSIONS),
             "provenance.records_received is inconsistent with contracts and exclusions")
    return data
