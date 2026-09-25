"""Descriptive metrics over a sequence of technical states and forward-looking evaluation labels.

Forward labels are computed **after** the states and are never fed back into
signal calculation. The state at bar t comes from ``TechnicalEngine.replay``,
which uses data only through bar t.

For bar t and horizon h (in bars):

- ``forward_return = close[t+h] / close[t] - 1``
- ``max_up = max(high[t+1..t+h]) / close[t] - 1`` and
  ``max_down = min(low[t+1..t+h]) / close[t] - 1``
- For a bullish state, the favorable excursion (MFE, Maximum Favorable Excursion)
  is ``max_up`` and the adverse excursion (MAE, Maximum Adverse Excursion) is
  ``max_down``. For a bearish state they are ``-max_down`` and ``-max_up``.
  Non-directional states report the raw ``max_up``/``max_down`` only.
- A label is None when t+h is past the data, or, with ``session_bounded`` (default
  for intraday), when t+h falls in another trading session (no overnight hold is
  implied).
"""
from statistics import mean, median

from market_data.models import session_date
from technical.signals import DIRECTION


def state_frequency(states):
    total = len(states)
    counts = {}
    for state in states:
        counts[state] = counts.get(state, 0) + 1
    return {state: dict(count=n, share=round(n / total, 6)) for state, n in sorted(counts.items())}


def state_runs(states):
    """[(state, start_index, length)] for consecutive runs."""
    runs = []
    for i, state in enumerate(states):
        if runs and runs[-1][0] == state:
            runs[-1][2] += 1
        else:
            runs.append([state, i, 1])
    return [tuple(run) for run in runs]


def durations(states):
    by_state = {}
    for state, _, length in state_runs(states):
        by_state.setdefault(state, []).append(length)
    return {state: dict(runs=len(v), mean_bars=round(mean(v), 4), median_bars=median(v), max_bars=max(v), min_bars=min(v))
            for state, v in sorted(by_state.items())}


def transitions(states):
    counts = {}
    for before, after in zip(states, states[1:]):
        if before != after:
            key = f"{before}->{after}"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def forward_labels(bars, horizons, *, session_bounded=True):
    """Per bar: {h: dict(forward_return, max_up, max_down) or None}."""
    closes = [float(b.close) for b in bars]
    highs = [float(b.high) for b in bars]
    lows = [float(b.low) for b in bars]
    bounded = session_bounded and bool(bars) and bars[0].interval.intraday
    days = [session_date(b.timestamp) for b in bars]
    out = []
    for t in range(len(bars)):
        labels = {}
        for h in horizons:
            end = t + h
            if end >= len(bars) or (bounded and days[end] != days[t]):
                labels[h] = None
                continue
            base = closes[t]
            labels[h] = dict(forward_return=closes[end] / base - 1.0, max_up=max(highs[t + 1:end + 1]) / base - 1.0,
                             max_down=min(lows[t + 1:end + 1]) / base - 1.0)
        out.append(labels)
    return out


def _summary(values):
    if not values:
        return dict(count=0)
    return dict(count=len(values), mean=round(mean(values), 8), median=round(median(values), 8),
                min=round(min(values), 8), max=round(max(values), 8),
                positive_share=round(sum(v > 0 for v in values) / len(values), 6))


def forward_by_state(states, labels, horizons):
    report = {}
    for state in sorted(set(states)):
        direction = DIRECTION.get(state)
        per_h = {}
        for h in horizons:
            rows = [labels[t][h] for t, s in enumerate(states) if s == state and labels[t][h] is not None]
            entry = dict(forward_return=_summary([r["forward_return"] for r in rows]),
                         max_up=_summary([r["max_up"] for r in rows]),
                         max_down=_summary([r["max_down"] for r in rows]))
            if direction == 1:
                entry["mfe"] = _summary([r["max_up"] for r in rows])
                entry["mae"] = _summary([r["max_down"] for r in rows])
            elif direction == -1:
                entry["mfe"] = _summary([-r["max_down"] for r in rows])
                entry["mae"] = _summary([-r["max_up"] for r in rows])
            per_h[str(h)] = entry
        report[state] = dict(direction={1: "bullish", -1: "bearish"}.get(direction, "none"), horizons=per_h)
    return report


def transition_probabilities(states):
    """For each state, its exits and the share of those exits going to each next state (descriptive only)."""
    out = {}
    for key, count in transitions(states).items():
        before, after = key.split("->")
        entry = out.setdefault(before, dict(exits=0, to={}))
        entry["exits"] += count
        entry["to"][after] = dict(count=count)
    for entry in out.values():
        for target in entry["to"].values():
            target["share"] = round(target["count"] / entry["exits"], 6)
        entry["to"] = dict(sorted(entry["to"].items(), key=lambda kv: (-kv[1]["count"], kv[0])))
    return dict(sorted(out.items()))
