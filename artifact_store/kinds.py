"""The closed artifact kinds (``artifact-store-v1``) and their mapping to existing domain contracts.

- ``market-intelligence``: ``trade_setup.validation.validated_market_intelligence``; id ``intelligence_id``;
  symbol ``synthesis_ref.symbol``; sealed as_of ``synthesis_ref.as_of``.
- ``options-intelligence``: ``trade_setup.validation.validated_options_intelligence``; id
  ``options_intelligence_id``; symbol ``snapshot_ref.underlying``; sealed as_of ``snapshot_ref.as_of``.
- ``trade-setup``: ``trade_setup.validation.validated_assessment``; id ``assessment_id``; symbol ``inputs.symbol``;
  sealed as_of ``inputs.assessment_as_of``.
- ``invalidation-check``: ``trade_setup.invalidation.validated_invalidation``; id ``invalidation_id``; symbol
  ``symbol``; sealed as_of ``market_intelligence_ref.as_of``.
- ``alert``: ``alert_engine.validation.validated_alert``; id ``alert_id``; symbol ``subject.symbol``; sealed as_of
  ``as_of``.

Every validator is the existing pure, structural, fail-closed check of the sealed contract, and each recomputes the
content id from the body. MarketIntelligence and OptionsIntelligence use the Phase 10 upstream-contract validators:
they're standalone (no evidence-layer, options_data or provider imports). Each kind's canonical serialization is the
shared MIAS form (sorted keys, compact separators, ASCII, no NaN) plus one trailing newline, as every runner writes.
"""
from dataclasses import dataclass
from typing import Callable

from alert_engine.canonical import canonical_json as alert_canonical_json
from alert_engine.validation import validated_alert
from trade_setup.canonical import canonical_json as setup_canonical_json
from trade_setup.invalidation import validated_invalidation
from trade_setup.validation import validated_assessment, validated_market_intelligence, validated_options_intelligence

STORE_FORMAT_VERSION = "artifact-store-v1"


@dataclass(frozen=True)
class Kind:
    name: str
    validate: Callable[[dict], dict]
    id_field: str
    symbol: Callable[[dict], str]
    as_of: Callable[[dict], str]
    canonical_json: Callable[[dict], str]

    def canonical_bytes(self, data):
        return (self.canonical_json(data) + "\n").encode("ascii")


KINDS = {k.name: k for k in (
    Kind("market-intelligence", validated_market_intelligence, "intelligence_id",
         lambda d: d["synthesis_ref"]["symbol"], lambda d: d["synthesis_ref"]["as_of"], setup_canonical_json),
    Kind("options-intelligence", validated_options_intelligence, "options_intelligence_id",
         lambda d: d["snapshot_ref"]["underlying"], lambda d: d["snapshot_ref"]["as_of"], setup_canonical_json),
    Kind("trade-setup", validated_assessment, "assessment_id",
         lambda d: d["inputs"]["symbol"], lambda d: d["inputs"]["assessment_as_of"], setup_canonical_json),
    Kind("invalidation-check", validated_invalidation, "invalidation_id",
         lambda d: d["symbol"], lambda d: d["market_intelligence_ref"]["as_of"], setup_canonical_json),
    Kind("alert", validated_alert, "alert_id",
         lambda d: d["subject"]["symbol"], lambda d: d["as_of"], alert_canonical_json),
)}
KIND_NAMES = tuple(KINDS)
