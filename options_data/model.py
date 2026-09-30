"""Immutable OptionsSnapshot model (``snapshot_format_version = "phase9-snapshot-v1"``). Source facts only.

An OptionsSnapshot records what a provider reported at or before ``as_of``, normalized and validated. It holds no
interpretation: no mid, spread, quote state, days to expiration, moneyness, liquidity label, score or selection.
Every number is a canonical Decimal string (never a float). Every fact group carries a closed ``status`` and a
closed ``time_basis``, so zero, missing and unavailable are never confused, and untimed facts are never presented
as cutoff-timestamped.
"""
from dataclasses import dataclass

from options_data.canonical import to_plain

SNAPSHOT_FORMAT_VERSION = "phase9-snapshot-v1"
TOP_LEVEL_FIELDS = ("snapshot_format_version", "snapshot_id", "underlying", "as_of", "session", "underlying_price",
                    "scope", "contracts", "exclusions", "provenance")

OPTION_TYPES = ("call", "put")
EXERCISE_STYLES = ("american", "european", "bermudan")
CALENDAR_STATES = ("regular", "pre", "post", "closed")  # market_data.models.Session values

# Fact status: present (a value was supplied), missing (the source omitted it), unavailable (the source or the
# entitlement cannot provide it), excluded_after_as_of (supplied, but observed after the snapshot cutoff).
PRESENT, MISSING, UNAVAILABLE, EXCLUDED_AFTER_AS_OF = "present", "missing", "unavailable", "excluded_after_as_of"
FACT_STATUSES = (PRESENT, MISSING, UNAVAILABLE, EXCLUDED_AFTER_AS_OF)

# Time basis of a present fact group:
# - observed_at: the source supplied an observation instant, and it is at or before as_of;
# - provider_as_of_date: the source supplied only a date (open interest), on or before the as_of exchange date;
# - provider_snapshot_unverified: part of the provider snapshot, with no trustworthy own time (not proven
#   cutoff-safe);
# - unavailable: no value (the group is not present).
OBSERVED_AT, PROVIDER_AS_OF_DATE = "observed_at", "provider_as_of_date"
PROVIDER_SNAPSHOT_UNVERIFIED, TIME_UNAVAILABLE = "provider_snapshot_unverified", "unavailable"
TIME_BASES = (OBSERVED_AT, PROVIDER_AS_OF_DATE, PROVIDER_SNAPSHOT_UNVERIFIED, TIME_UNAVAILABLE)

PROVIDER_SOURCE = "provider"  # IV and Greeks are copied from the provider, never computed.

FACT_GROUPS = ("quote", "trade", "day", "open_interest", "implied_volatility", "greeks")
GROUP_VALUE_FIELDS = {
    "quote": ("bid", "ask", "bid_size", "ask_size"),
    "trade": ("price", "size"),
    "day": ("open", "high", "low", "close", "previous_close", "change", "change_percent", "volume", "vwap"),
    "open_interest": ("value",),
    "implied_volatility": ("value",),
    "greeks": ("delta", "gamma", "theta", "vega", "rho"),
}
# Time bases a present group may have.
GROUP_TIME_BASES = {
    "quote": (OBSERVED_AT, PROVIDER_SNAPSHOT_UNVERIFIED),
    "trade": (OBSERVED_AT, PROVIDER_SNAPSHOT_UNVERIFIED),
    "day": (OBSERVED_AT, PROVIDER_SNAPSHOT_UNVERIFIED),
    "open_interest": (OBSERVED_AT, PROVIDER_AS_OF_DATE, PROVIDER_SNAPSHOT_UNVERIFIED),
    "implied_volatility": (OBSERVED_AT, PROVIDER_SNAPSHOT_UNVERIFIED),
    "greeks": (OBSERVED_AT, PROVIDER_SNAPSHOT_UNVERIFIED),
}
# Fields that must be >= 0 when present; fields that must also be whole numbers.
NON_NEGATIVE = {
    "quote": ("bid", "ask", "bid_size", "ask_size"),
    "trade": ("price", "size"),
    "day": ("open", "high", "low", "close", "previous_close", "volume", "vwap"),
    "open_interest": ("value",),
    "implied_volatility": ("value",),
    "greeks": (),
}
WHOLE_NUMBER = {"quote": ("bid_size", "ask_size"), "trade": ("size",), "day": ("volume",), "open_interest": ("value",)}

# Record-level exclusions (a provider record never becomes a contract) and the group-level cutoff exclusion.
MALFORMED_RECORD, UNSUPPORTED_OPTION_TYPE, INVALID_STRIKE = "malformed_record", "unsupported_option_type", "invalid_strike"
EXPIRED_BEFORE_AS_OF, IDENTITY_MISMATCH, IDENTITY_CONFLICT = "expired_before_as_of", "identity_mismatch", "identity_conflict"
DUPLICATE_IDENTICAL, FACT_AFTER_AS_OF = "duplicate_identical", "fact_after_as_of"
RECORD_EXCLUSIONS = (MALFORMED_RECORD, UNSUPPORTED_OPTION_TYPE, INVALID_STRIKE, EXPIRED_BEFORE_AS_OF,
                     IDENTITY_MISMATCH, IDENTITY_CONFLICT, DUPLICATE_IDENTICAL)
EXCLUSION_REASONS = RECORD_EXCLUSIONS + (FACT_AFTER_AS_OF,)


class _Plain:
    def to_dict(self):
        return to_plain(self)


@dataclass(frozen=True)
class Session(_Plain):
    session_date: str                  # the as_of America/New_York date
    calendar_state: str                # regular | pre | post | closed, or None if the caller did not classify


@dataclass(frozen=True)
class UnderlyingPrice(_Plain):
    status: str                        # present | unavailable
    value: str                         # > 0 when present, else None
    observed_at: str                   # <= as_of when present, else None
    source: str                        # where the price came from (e.g. a stocks provider), else None
    reason: str                        # required when unavailable, else None


@dataclass(frozen=True)
class Scope(_Plain):
    """What was requested or collected. Provenance, never a trading filter."""
    contract_types: tuple              # sorted subset of OPTION_TYPES
    expiration_from: str
    expiration_through: str
    provider_page_limit: int
    provider_result_limit: int


@dataclass(frozen=True)
class Identity(_Plain):
    contract_id: str                   # normalized OCC-style id: ROOT + YYMMDD + C|P + strike*1000 (8 digits)
    provider_symbol: str               # exactly as supplied
    root: str                          # OCC root; may differ from the underlying for adjusted contracts
    root_matches_underlying: bool
    option_type: str                   # call | put
    expiration: str                    # ISO date
    strike: str                        # Decimal string


@dataclass(frozen=True)
class Deliverable(_Plain):
    kind: str
    symbol: str
    amount: str


@dataclass(frozen=True)
class Terms(_Plain):
    exercise_style: str                # american | european | bermudan, or None
    shares_per_contract: str           # Decimal string > 0, or None
    deliverables: tuple                # Deliverable: adjustment metadata, only if supplied


@dataclass(frozen=True)
class Quote(_Plain):
    status: str
    bid: str
    ask: str
    bid_size: str
    ask_size: str
    observed_at: str
    time_basis: str


@dataclass(frozen=True)
class Trade(_Plain):
    status: str
    price: str
    size: str
    observed_at: str
    time_basis: str


@dataclass(frozen=True)
class Day(_Plain):
    status: str
    open: str
    high: str
    low: str
    close: str
    previous_close: str
    change: str
    change_percent: str
    volume: str
    vwap: str
    observed_at: str
    time_basis: str


@dataclass(frozen=True)
class OpenInterest(_Plain):
    status: str
    value: str
    as_of_date: str                    # never invented: None unless the source supplied a date
    observed_at: str
    time_basis: str


@dataclass(frozen=True)
class ImpliedVolatility(_Plain):
    status: str
    value: str
    observed_at: str
    time_basis: str
    source: str                        # "provider" when present


@dataclass(frozen=True)
class Greeks(_Plain):
    status: str
    delta: str
    gamma: str
    theta: str
    vega: str
    rho: str                           # optional: not observed in the Phase 9A samples
    observed_at: str
    time_basis: str
    source: str


@dataclass(frozen=True)
class Contract(_Plain):
    identity: Identity
    terms: Terms
    quote: Quote
    trade: Trade
    day: Day
    open_interest: OpenInterest
    implied_volatility: ImpliedVolatility
    greeks: Greeks


@dataclass(frozen=True)
class Exclusion(_Plain):
    reason: str
    count: int


@dataclass(frozen=True)
class Provenance(_Plain):
    provider: str
    adapter_version: str
    endpoint_families: tuple           # sorted
    configured_delay_seconds: int
    pages_fetched: int
    requests_made: int
    truncated: bool
    records_received: int              # = contracts + record-level exclusions
    source_capabilities: tuple         # Capability per fact group (sorted by group)


@dataclass(frozen=True)
class Capability(_Plain):
    group: str
    status: str                        # available | unavailable


@dataclass(frozen=True)
class OptionsSnapshot(_Plain):
    snapshot_format_version: str
    snapshot_id: str
    underlying: str
    as_of: str
    session: Session
    underlying_price: UnderlyingPrice
    scope: Scope
    contracts: tuple
    exclusions: tuple
    provenance: Provenance

    def body(self):
        data = self.to_dict()
        data.pop("snapshot_id")
        return data
