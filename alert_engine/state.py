"""Pure Phase 12C alert state: deduplication and terminal suppression around sealed Phase 12B AlertEvents.

``decide(alert, subject_state, already_seen)`` classifies one validated AlertEvent against explicit state. It applies
this precedence (documented and tested):

1. ``duplicate``: this exact ``alert_id`` was already seen. A no-op; state is unchanged.
2. ``terminal_suppressed``: the alert's subject is already terminal (a setup invalidated earlier by a different
   alert). A no-op. The earliest accepted invalidation stays authoritative, and a setup is never reactivated.
3. ``new``: otherwise. The next subject state is derived from the event alone:
   - ``setup_available`` records the accepted alert;
   - ``setup_invalidated`` makes the setup terminal;
   - ``market_pattern_changed`` records the transition's current pattern.

The state layer never re-decides eligibility, never compares source objects, and never invents a transition: Phase
12B already sealed what happened.

Subjects: a setup (``setup:<assessment_id>``, shared by ``setup_available`` and ``setup_invalidated`` for the same
assessment) or a symbol (``symbol:<SYMBOL>``). Separate assessments are separate subjects, even for the same symbol.

``replay(alerts)`` folds an explicit, caller-ordered sequence of sealed AlertEvents. The sequence is never re-sorted
by any clock. It returns per-event decisions and the final state. Replaying the same sequence always yields the same
decisions and state, so state lost from a cache is recoverable from sealed history.

No I/O, clock, environment, Redis, database or network. State never holds wall-clock time.
"""
from dataclasses import dataclass

from alert_engine import rules as r
from alert_engine.canonical import to_plain
from alert_engine.validation import _require, validated_alert

STATE_FORMAT_VERSION = "phase12-state-v1"
NEW, DUPLICATE, TERMINAL_SUPPRESSED = "new", "duplicate", "terminal_suppressed"
CLASSIFICATIONS = (NEW, DUPLICATE, TERMINAL_SUPPRESSED)
SUBJECT_STATE_FIELDS = ("state_format_version", "subject_key", "kind", "symbol", "assessment_id", "terminal",
                        "terminal_alert_id", "last_alert_id", "last_source_id", "last_as_of", "current_pattern")


class _Plain:
    def to_dict(self):
        return to_plain(self)


@dataclass(frozen=True)
class AlertSubjectState(_Plain):
    """Current state of one subject, derived only from accepted sealed AlertEvents (no wall-clock data)."""
    state_format_version: str
    subject_key: str                  # setup:<assessment_id> | symbol:<SYMBOL>
    kind: str                         # setup | symbol
    symbol: str
    assessment_id: str                # None for a symbol subject
    terminal: bool                    # True once a setup is invalidated (never reverts)
    terminal_alert_id: str            # the earliest accepted setup_invalidated alert; None until terminal
    last_alert_id: str                # the latest accepted (new) alert for this subject
    last_source_id: str               # that alert's current source id
    last_as_of: str                   # that alert's sealed as_of (not the wall clock)
    current_pattern: str              # symbol subjects: the latest accepted transition's current pattern; else None


@dataclass(frozen=True)
class StateDecision(_Plain):
    alert_id: str
    alert_code: str
    subject_key: str
    classification: str               # new | duplicate | terminal_suppressed
    state: AlertSubjectState          # the subject state after this decision (unchanged unless new)


def subject_key(alert):
    """The deterministic subject key of a validated AlertEvent dict."""
    subject = alert["subject"]
    return f"setup:{subject['assessment_id']}" if subject["kind"] == r.SETUP else f"symbol:{subject['symbol']}"


def validated_subject_state(state):
    """A plain AlertSubjectState dict, validated (fail closed). None stays None."""
    if state is None:
        return None
    data = state.to_dict() if isinstance(state, AlertSubjectState) else state
    _require(isinstance(data, dict) and set(data) == set(SUBJECT_STATE_FIELDS)
             and data["state_format_version"] == STATE_FORMAT_VERSION and data["kind"] in (r.SETUP, r.SYMBOL)
             and isinstance(data["terminal"], bool) and isinstance(data["symbol"], str) and data["symbol"]
             and data["subject_key"] == (f"setup:{data['assessment_id']}" if data["kind"] == r.SETUP
                                         else f"symbol:{data['symbol']}")
             and (data["assessment_id"] is None) == (data["kind"] == r.SYMBOL)
             and (data["terminal_alert_id"] is not None) == data["terminal"]
             and (not data["terminal"] or data["kind"] == r.SETUP)
             and (data["current_pattern"] is None or (data["kind"] == r.SYMBOL and data["current_pattern"] in r.PATTERNS))
             and all(data[k] is None or (isinstance(data[k], str) and r.CONTENT_ID.fullmatch(data[k]))
                     for k in ("assessment_id", "terminal_alert_id", "last_alert_id", "last_source_id")),
             "alert subject state is malformed")
    return data


def _next_state(alert, previous):
    subject = alert["subject"]
    current = next(ref for ref in alert["source_refs"] if ref["role"] == "current")
    base = previous or dict(state_format_version=STATE_FORMAT_VERSION, subject_key=subject_key(alert),
                            kind=subject["kind"], symbol=subject["symbol"], assessment_id=subject["assessment_id"],
                            terminal=False, terminal_alert_id=None, current_pattern=None)
    state = dict(base, last_alert_id=alert["alert_id"], last_source_id=current["id"], last_as_of=alert["as_of"])
    if alert["alert_code"] == r.SETUP_INVALIDATED:
        state.update(terminal=True, terminal_alert_id=alert["alert_id"])
    elif alert["alert_code"] == r.MARKET_PATTERN_CHANGED:
        state.update(current_pattern=alert["transition"]["current"])
    return AlertSubjectState(**{k: state[k] for k in SUBJECT_STATE_FIELDS})


def decide(alert, subject_state, already_seen):
    """The StateDecision for one AlertEvent given explicit subject state and whether its alert_id was seen."""
    data = validated_alert(alert)
    previous = validated_subject_state(subject_state)
    key = subject_key(data)
    _require(previous is None or previous["subject_key"] == key, "subject state does not belong to the alert subject")
    _require(isinstance(already_seen, bool), "already_seen must be a boolean")
    unchanged = AlertSubjectState(**previous) if previous else None
    if already_seen:
        return StateDecision(data["alert_id"], data["alert_code"], key, DUPLICATE, unchanged)
    if previous and previous["terminal"]:
        return StateDecision(data["alert_id"], data["alert_code"], key, TERMINAL_SUPPRESSED, unchanged)
    return StateDecision(data["alert_id"], data["alert_code"], key, NEW, _next_state(data, previous))


@dataclass(frozen=True)
class ReplayState(_Plain):
    seen_alert_ids: tuple             # sorted
    subjects: tuple                   # AlertSubjectState, sorted by subject_key

    def subject(self, key):
        return next((s for s in self.subjects if s.subject_key == key), None)


@dataclass(frozen=True)
class ReplayResult(_Plain):
    decisions: tuple                  # StateDecision, in input order
    state: ReplayState


EMPTY = ReplayState((), ())


def replay(alerts, initial=EMPTY):
    """Fold an explicit, caller-ordered sequence of sealed AlertEvents into decisions and a final state."""
    _require(isinstance(initial, ReplayState), "initial state must be a ReplayState")
    seen, subjects = set(initial.seen_alert_ids), {s.subject_key: s for s in initial.subjects}
    decisions = []
    for alert in alerts:
        data = validated_alert(alert)
        key = subject_key(data)
        decision = decide(data, subjects.get(key), data["alert_id"] in seen)
        decisions.append(decision)
        if decision.classification == NEW:
            seen.add(data["alert_id"])
            subjects[key] = decision.state
    return ReplayResult(tuple(decisions), ReplayState(tuple(sorted(seen)),
                                                      tuple(subjects[k] for k in sorted(subjects))))
