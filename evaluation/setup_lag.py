"""Setup-lag research metrics (Phase 4D, evaluation-only; no live snapshot schema change).

**Hypothesis under test:** setup states may appear late. Confirmed pivots need
``pivot_window`` future bars, so a setup may identify structure after much of the
move has already happened.

For each **setup occurrence** (the first bar of a ``bullish_setup`` or
``bearish_setup`` run), the *structure-completing pivot* is the more recently
confirmed of the snapshot's ``significant_high`` / ``significant_low``. From it:

| Field | Meaning |
|---|---|
| ``pivot_candidate_timestamp`` | the pivot bar |
| ``pivot_confirmed_timestamp`` | the bar at which the pivot became known (candidate + ``pivot_window``) |
| ``confirmation_lag_bars`` | confirmed index - candidate index (equals ``pivot_window`` by construction) |
| ``setup_delay_bars`` | setup bar index - candidate index |
| ``setup_after_confirmation_bars`` | setup bar index - confirmed index |
| ``move_before_setup`` | close at the setup bar / pivot price - 1 (signed): how far price had already travelled from the pivot |
| ``move_before_confirmation`` | close at the pivot-confirmation bar / pivot price - 1 (signed; Phase 5, H2/H3) |

All values come from snapshots computed causally; nothing here feeds back into
state generation. The minimum-sample rule applies: fewer than 30 occurrences
report the count and ``INSUFFICIENT_SAMPLE`` only.
"""
from statistics import mean, median

from evaluation.statistics import MIN_SAMPLE, sample_label

SETUPS = ("bullish_setup", "bearish_setup")


def _completing_pivot(snapshot):
    pivots = [p for p in (snapshot.significant_high, snapshot.significant_low) if p]
    return max(pivots, key=lambda p: (p["confirmed_index"], p["index"])) if pivots else None


def setup_events(bars, snapshots):
    events = []
    for i, snapshot in enumerate(snapshots):
        state = snapshot.signal.state
        if state not in SETUPS or (i and snapshots[i - 1].signal.state == state):
            continue
        pivot = _completing_pivot(snapshot)
        if pivot is None:
            continue
        candidate, confirmed = pivot["index"], pivot["confirmed_index"]
        events.append(dict(
            state=state, setup_timestamp=bars[i].timestamp.isoformat(), pivot_kind=pivot["kind"],
            pivot_label=pivot["label"], pivot_candidate_timestamp=bars[candidate].timestamp.isoformat(),
            pivot_confirmed_timestamp=bars[confirmed].timestamp.isoformat(),
            confirmation_lag_bars=confirmed - candidate, setup_delay_bars=i - candidate,
            setup_after_confirmation_bars=i - confirmed,
            move_before_setup=round(float(bars[i].close) / pivot["price"] - 1.0, 8),
            move_before_confirmation=round(float(bars[confirmed].close) / pivot["price"] - 1.0, 8)))
    return events


def summarize(events, *, seed=None, key="setup_lag"):
    """Per setup state; with ``seed``, also a 95% bootstrap CI of the mean move before confirmation (Phase 5)."""
    from evaluation.statistics import bootstrap
    out = {}
    for state in SETUPS:
        rows = [e for e in events if e["state"] == state]
        summary = dict(occurrences=len(rows), sample_label=sample_label(len(rows)))
        if len(rows) < MIN_SAMPLE:
            summary["status"] = "INSUFFICIENT_SAMPLE"  # Minimum-sample rule: count only.
        else:
            summary["status"] = "OK"
            summary.update(
                mean_confirmation_lag_bars=round(mean(e["confirmation_lag_bars"] for e in rows), 4),
                mean_setup_delay_bars=round(mean(e["setup_delay_bars"] for e in rows), 4),
                median_setup_delay_bars=median(e["setup_delay_bars"] for e in rows),
                mean_setup_after_confirmation_bars=round(mean(e["setup_after_confirmation_bars"] for e in rows), 4),
                mean_move_before_setup=round(mean(e["move_before_setup"] for e in rows), 8),
                median_move_before_setup=round(median(e["move_before_setup"] for e in rows), 8))
            if "move_before_confirmation" in rows[0]:
                moves = [e["move_before_confirmation"] for e in rows]
                summary.update(mean_move_before_confirmation=round(mean(moves), 8),
                               median_move_before_confirmation=round(median(moves), 8),
                               mean_abs_move_before_confirmation=round(mean(abs(x) for x in moves), 8))
                if seed is not None:
                    summary["ci95_mean_move_before_confirmation"] = bootstrap(moves, seed=seed,
                                                                              key=f"{key}:{state}:mbc")["mean"]
        out[state] = summary
    return out
