"""Provider-neutral options data interface (Phase 9C).

A provider retrieves one underlying's options chain and returns **provider-neutral source records** (the Phase 9B
record format documented in ``options_data.normalization``), plus the facts needed for provenance. It never builds
an OptionsSnapshot: canonicalization, identity, cutoff exclusion, hashing and validation stay in the pure Phase 9B
layer.

The provider takes no ``as_of``: retrieval is "what the source returns now", and the cutoff is applied by the pure
assembler to every timed fact. No provider-specific JSON appears above this boundary.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass


class OptionsProviderError(RuntimeError):
    """Retrieval failed; ``kind`` is auth, rate_limit, http, transport, redirect, payload, budget, pagination or
    scope. Messages name the host and path only, never keys or queries."""

    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind, self.status = kind, status


@dataclass(frozen=True)
class ChainScope:
    """An explicit retrieval scope: collection bounds only, never a trading filter."""
    contract_types: tuple = ("call", "put")   # sorted subset of call/put
    expiration_from: str = None               # ISO date, inclusive
    expiration_through: str = None            # ISO date, inclusive
    page_size: int = 250                      # results requested per provider page
    max_pages: int = 2                        # pages followed at most

    def as_snapshot_scope(self):
        """The Phase 9B ``scope`` block (part of the snapshot identity)."""
        return dict(contract_types=sorted(self.contract_types), expiration_from=self.expiration_from,
                    expiration_through=self.expiration_through, provider_page_limit=self.page_size,
                    provider_result_limit=self.page_size * self.max_pages)


@dataclass(frozen=True)
class ChainResult:
    """What a provider returns: normalized records plus provenance facts. No raw payloads, keys or URLs."""
    provider: str
    adapter_version: str
    endpoint_families: tuple
    records: tuple                            # provider-neutral source records (dicts)
    unavailable_groups: tuple                 # fact groups the source cannot supply
    underlying_price: dict                    # Phase 9B underlying_price input: {value, observed_at, source} or
    #                                           {reason} when unavailable
    pages_fetched: int
    requests_made: int
    truncated: bool                           # True when retrieval stopped before the source said it was complete


class OptionsDataProvider(ABC):
    @abstractmethod
    def get_chain(self, underlying, scope):
        """A ChainResult for ``underlying`` within ``scope``; raises OptionsProviderError."""


class FixtureOptionsProvider(OptionsDataProvider):
    """Serves pre-built provider-neutral records from memory (deterministic; tests, replay and examples)."""

    def __init__(self, records, *, unavailable_groups=("quote", "trade"), truncated=False, pages_fetched=1,
                 underlying_price=None, provider="fixture"):
        self._records = tuple(records)
        self._unavailable = tuple(unavailable_groups)
        self._truncated, self._pages = truncated, pages_fetched
        self._price = underlying_price or dict(reason="not_supplied")
        self._provider = provider
        self.calls = []

    def get_chain(self, underlying, scope):
        self.calls.append((underlying, scope))
        return ChainResult(provider=self._provider, adapter_version="fixture-v1",
                           endpoint_families=("fixture_chain",), records=self._records,
                           unavailable_groups=self._unavailable, underlying_price=self._price,
                           pages_fetched=self._pages, requests_made=self._pages, truncated=self._truncated)
