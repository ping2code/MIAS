"""Phase 6 prospective validation core (research only): the evaluation gate, evidence eligibility, and H1'/H2'.

Everything here implements the frozen registry (``evidence.registry``,
``phase6-v1``) literally. Nothing is tuned, and the hypotheses are never revised
in place.

**Gate (no-peek):** evaluation opens only when both hold:

- today's exchange date is on or after the pinned ``earliest_evaluation_session``;
- at least ``min_complete_sessions`` (120) complete prospective sessions exist.

A session is *complete* when accepted ledger evidence exists for every frozen
symbol × interval, with no conflicts. Before the gate opens, no bars are fetched
and no states or statistics are computed. ``--early-look-override`` runs anyway,
and every output is labelled ``NON_PREREGISTERED_EARLY_LOOK``.

**Eligibility (frozen evaluation_data_rule):**

- bars are re-fetched at evaluation time;
- per (symbol, interval), a session is eligible when it is complete **and** the
  re-fetched ``bar_content_hash`` equals the accepted ledger hash;
- sessions whose hash differs are excluded and reported;
- states use the full series, so pre-start history serves as warm-up;
- an observation counts only when its bar, and every bar its measurement reads,
  lie in eligible sessions.
"""
from statistics import mean, median

import numpy as np

from evaluation import metrics, nulls, statistics
from evaluation.setup_lag import setup_events
from evidence.registry import H1P, H2P, NULL_VERSION, PROTOCOL, SEED
from market_data.models import EXCHANGE_TZ

PREREGISTERED = "PREREGISTERED_PROSPECTIVE"
EARLY_LOOK = "NON_PREREGISTERED_EARLY_LOOK"
H1_HORIZON = H1P.horizon[0]
H1_MIN_SYMBOL, H1_MIN_POOLED, H1_MIN_SYMBOLS, H1_MIN_BPS = 5, 30, 4, 0.0010
H2_MIN_CELL, H2_MIN_CELLS, H2_MAGNITUDE = 30, 4, 0.25
ALIGNED = {"bullish_setup": "low", "bearish_setup": "high"}
EXPECTED_SIGN = {"low": 1, "high": -1}


def gate(pin, complete_sessions, today):
    """dict(open, earliest_evaluation_session, eligible_sessions, required_sessions, date_reached, sessions_reached)."""
    earliest = pin["earliest_evaluation_session"]
    required = PROTOCOL.min_complete_sessions
    date_ok, sessions_ok = today >= earliest, complete_sessions >= required
    return dict(open=date_ok and sessions_ok, earliest_evaluation_session=earliest.isoformat(),
                eligible_sessions=complete_sessions, required_sessions=required, date_reached=date_ok,
                sessions_reached=sessions_ok)


def locked_message(state):
    return (f"Evaluation locked. Earliest evaluation date: {state['earliest_evaluation_session']}. "
            f"Eligible sessions: {state['eligible_sessions']} / {state['required_sessions']}. "
            "No hypothesis statistics were computed.")


def session_of(bar):
    return bar.timestamp.astimezone(EXCHANGE_TZ).date()


def eligible_sessions(bars, accepted, complete, *, symbol, interval):
    """(eligible dates, excluded [{session, reason}]) for one re-fetched series against its accepted ledger rows."""
    from persistence.technical_evidence_ledger import bar_content_hash
    by_day = {}
    for bar in bars:
        if bar.regular:
            by_day.setdefault(session_of(bar), []).append(bar)
    eligible, excluded = set(), []
    for day in sorted(complete):
        row = accepted.get((day, symbol, interval))
        if row is None:
            continue
        fetched = by_day.get(day, [])
        digest = bar_content_hash(fetched, symbol=symbol, interval=interval, session_date=day) if fetched else None
        if digest == row["bar_content_hash"] and len(fetched) == row["bar_count"]:
            eligible.add(day)
        else:
            excluded.append(dict(session=day.isoformat(), symbol=symbol, interval=interval,
                                 reason="refetched_bar_hash_mismatch"))
    return eligible, excluded


def h1_symbol(bars, states, eligible):
    """Prospective observation window for one symbol: (states, 5-bar forward returns) of eligible bars."""
    labels = metrics.forward_labels(bars, (H1_HORIZON,), session_bounded=False)
    days = [session_of(b) for b in bars]
    obs_states, obs_returns = [], []
    for t in range(len(bars) - H1_HORIZON):
        if all(days[t + k] in eligible for k in range(H1_HORIZON + 1)) and labels[t][H1_HORIZON] is not None:
            obs_states.append(states[t])
            obs_returns.append(labels[t][H1_HORIZON]["forward_return"])
    return obs_states, obs_returns


def evaluate_h1(per_symbol, *, seed=SEED, iterations=nulls.ITERATIONS):
    """``per_symbol``: {symbol: (states, returns)} over prospective observations (frozen H1' rules)."""
    state = H1P.state[0]
    symbols, null_rows, details = [], [], {}
    for symbol in H1P.symbols:
        states, returns = per_symbol.get(symbol, ([], []))
        n_state = sum(s == state for s in states)
        detail = dict(observations=len(returns), state_observations=n_state, qualifying=n_state >= H1_MIN_SYMBOL)
        details[symbol] = detail
        if not detail["qualifying"]:
            continue
        values = np.array(returns, dtype=float)
        mask = np.array([s == state for s in states])
        all_mean = float(values.mean())
        detail.update(state_mean=round(float(values[mask].mean()), 8), all_bars_mean=round(all_mean, 8),
                      excess=round(float(values[mask].mean()) - all_mean, 8))
        shifts = nulls.draw_shifts(len(values), H1_HORIZON, seed=seed, key=f"H1'|{symbol}", iterations=iterations)
        if not len(shifts):
            detail["null"] = "NULL_UNAVAILABLE"
            continue
        null_rows.append(np.array([values[np.roll(mask, int(k))].mean() - all_mean for k in shifts]))
        symbols.append(symbol)
    pooled_n = sum(details[s]["state_observations"] for s in symbols)
    result = dict(id=H1P.id, hypothesis_hash=H1P.content_hash(), qualifying_symbols=symbols,
                  pooled_state_observations=pooled_n, per_symbol=details, seed=seed, iterations=iterations,
                  null_method=f"{NULL_VERSION} per symbol, equal-weight pooled")
    if len(symbols) < H1_MIN_SYMBOLS or pooled_n < H1_MIN_POOLED:
        return dict(result, verdict="INSUFFICIENT", reason="pooled sample rule not met")
    pooled = float(mean(details[s]["excess"] for s in symbols))
    null = np.vstack(null_rows).mean(axis=0)
    p97_5 = float(np.percentile(null, 97.5))
    positive = sum(details[s]["excess"] > 0 for s in symbols)
    result.update(pooled_excess=round(pooled, 8), pooled_excess_bps=round(pooled * 1e4, 2),
                  null_p2_5=round(float(np.percentile(null, 2.5)), 8), null_p50=round(float(np.percentile(null, 50)), 8),
                  null_p97_5=round(p97_5, 8), observed_percentile=round(float((null <= pooled).mean() * 100), 4),
                  positive_symbols=positive)
    if pooled <= 0 or abs(pooled) < H1_MIN_BPS or pooled <= p97_5:
        verdict = "UNSUPPORTED"
    elif positive * 3 >= 2 * len(symbols):
        verdict = "SUPPORTED"
    else:
        verdict = "MIXED"
    return dict(result, verdict=verdict)


def h2_cells(bars, snapshots, eligible, *, symbol, interval):
    """{(state, kind): [move_before_confirmation]}, lag list and the 2-bar magnitude reference over eligible bars."""
    days = [session_of(b) for b in bars]
    index = {b.timestamp.isoformat(): i for i, b in enumerate(bars)}
    cells, lags = {}, {}
    for event in setup_events(bars, snapshots):
        touched = [index[event["pivot_candidate_timestamp"]], index[event["pivot_confirmed_timestamp"]],
                   index[event["setup_timestamp"]]]
        if not all(days[i] in eligible for i in touched):
            continue
        key = (event["state"], event["pivot_kind"])
        cells.setdefault(key, []).append(event["move_before_confirmation"])
        lags.setdefault(key, []).append(event["confirmation_lag_bars"])
    closes = [float(b.close) for b in bars]
    two_bar = [abs(closes[t + 2] / closes[t] - 1) for t in range(len(bars) - 2)
               if days[t] in eligible and days[t + 2] in eligible]
    return cells, lags, (median(two_bar) if two_bar else None)


def evaluate_h2(per_series, *, seed=SEED, iterations=statistics.DEFAULT_ITERATIONS):
    """``per_series``: {(symbol, interval): (cells, lags, reference)} from ``h2_cells`` (frozen H2' rules)."""
    primary, descriptive = [], []
    for (symbol, interval), (cells, lags, reference) in sorted(per_series.items()):
        for (state, kind), moves in sorted(cells.items()):
            aligned = ALIGNED[state] == kind
            cell = dict(symbol=symbol, interval=interval, state=state, pivot_kind=kind, occurrences=len(moves),
                        mean_confirmation_lag_bars=round(mean(lags[(state, kind)]), 4), magnitude_reference=reference)
            if len(moves) >= H2_MIN_CELL:
                ci = statistics.bootstrap(moves, seed=seed, key=f"H2'|{symbol}|{interval}|{state}|{kind}",
                                          iterations=iterations)["mean"]
                abs_mean = mean(abs(m) for m in moves)
                sign = EXPECTED_SIGN[kind]
                expected_side = ci[0] > 0 if sign > 0 else ci[1] < 0
                opposite_side = ci[1] < 0 if sign > 0 else ci[0] > 0
                large = reference is not None and abs_mean >= H2_MAGNITUDE * reference
                cell.update(evaluable=aligned, mean_move_before_confirmation=round(mean(moves), 8),
                            mean_abs_move=round(abs_mean, 8), ci95_mean=ci, expected_sign="+" if sign > 0 else "-",
                            ci_excludes_zero_expected=expected_side, ci_excludes_zero_opposite=opposite_side,
                            magnitude_ok=large, consistent=bool(expected_side and large) if aligned else None)
            else:
                cell.update(evaluable=False, status="INSUFFICIENT_SAMPLE")
            (primary if aligned else descriptive).append(cell)
    evaluable = [c for c in primary if c["evaluable"]]
    result = dict(id=H2P.id, hypothesis_hash=H2P.content_hash(), seed=seed, iterations=iterations,
                  primary_cells=primary, counter_kind_cells_descriptive=descriptive,
                  evaluable_cells=len(evaluable), consistent_cells=sum(c["consistent"] for c in evaluable))
    if len(evaluable) < H2_MIN_CELLS:
        return dict(result, verdict="INSUFFICIENT", reason="fewer than 4 evaluable primary cells")
    share = result["consistent_cells"] / len(evaluable)
    verdict = "SUPPORTED" if share >= 0.75 else "UNSUPPORTED" if share < 0.25 else "MIXED"
    if verdict == "SUPPORTED" and any(c["ci_excludes_zero_opposite"] for c in evaluable):
        verdict, result["capped"] = "MIXED", "an evaluable cell is significant on the opposite side"
    return dict(result, consistent_share=round(share, 4), verdict=verdict)
