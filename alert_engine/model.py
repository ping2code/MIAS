"""Immutable AlertEvent model (``phase12-v1``): the delivery-independent record that one closed v1 alert rule fired
on explicit sealed inputs.

Exactly 11 top-level fields. There is no generated_at, delivery state, channel field, severity, score, ranking,
recommendation, free text or AI field.
"""
from dataclasses import dataclass

from alert_engine.canonical import to_plain

ALERT_FIELDS = ("alert_format_version", "alert_id", "rules_version", "alert_code", "subject", "transition",
                "source_refs", "as_of", "facts", "decision_trace", "provenance")


class _Plain:
    def to_dict(self):
        return to_plain(self)


@dataclass(frozen=True)
class Subject(_Plain):
    kind: str                         # setup | symbol
    symbol: str
    assessment_id: str                # the setup's TradeSetupAssessment id; None for a symbol subject


@dataclass(frozen=True)
class Transition(_Plain):
    previous: str                     # Phase 8 pattern of the previous MarketIntelligence
    current: str                      # Phase 8 pattern of the current MarketIntelligence (always different)


@dataclass(frozen=True)
class SourceRef(_Plain):
    role: str                         # current | previous | setup
    object_kind: str
    id: str
    format_version: str
    rules_version: str
    as_of: str


@dataclass(frozen=True)
class TraceStep(_Plain):
    step: int
    rule: str
    result: str                       # always pass (an AlertEvent exists only when its rule fired)
    pointers: tuple


@dataclass(frozen=True)
class Provenance(_Plain):
    source_ids: tuple                 # sorted
    rules_version: str
    pointer_version: str


@dataclass(frozen=True)
class AlertEvent(_Plain):
    alert_format_version: str
    alert_id: str
    rules_version: str
    alert_code: str
    subject: Subject
    transition: Transition            # None unless the code is a transition
    source_refs: tuple                # SourceRef, sorted by role
    as_of: str                        # the current source object's as_of (never the wall clock)
    facts: dict                       # the code's closed fact set
    decision_trace: tuple
    provenance: Provenance

    def body(self):
        data = self.to_dict()
        data.pop("alert_id")
        return data
