"""Cross-symbol, cross-period and pivot-window research summaries over ``phase4d-v1`` reports (descriptive only).

Nothing here ranks states, selects a configuration or claims profitability. It uses
neutral robustness labels only:

| Label | Meaning |
|---|---|
| ``insufficient_evidence`` | fewer than two comparable results meet the 30-observation minimum |
| ``mixed_across_symbols`` | the sign of the excess mean return differs between symbols |
| ``mixed_across_periods`` | the sign of the excess mean return differs between Period A and Period B |
| ``directionally_consistent`` | every qualifying result has the same excess-mean sign (``direction`` says which) |

**Direction consistency** only means the *sign* of the excess return agrees. It
says nothing about size, reliability or tradeability.
"""
from collections import defaultdict


def _sign(value):
    return (value > 0) - (value < 0)


def _profile(report):
    m = report["metadata"]
    return (m["interval"], m["session_bound"], m["pivot_window"])


def cross_symbol(reports):
    """Rows per (period, interval, session policy, pivot window, state, horizon) across symbols."""
    groups = defaultdict(dict)
    for report in reports:
        m = report["metadata"]
        for state, entry in report["states"].items():
            for h, stats in entry["horizons"].items():
                key = (m["period"]["label"], *_profile(report), state, int(h))
                groups[key][m["symbol"]] = stats
    rows = []
    for (period, interval, bounded, pivot, state, h), by_symbol in sorted(groups.items(), key=lambda kv: tuple(
            "" if x is None else str(x) for x in kv[0])):
        ok = {s: v for s, v in by_symbol.items() if v["status"] == "OK"}
        row = dict(period=period, interval=interval, session_bound=bounded, pivot_window=pivot, state=state, horizon=h,
                   symbols=sorted(by_symbol), symbols_meeting_min_sample=sorted(ok))
        if len(ok) < 2:
            row.update(label="insufficient_evidence")
        else:
            means = [v["excess"]["mean"] for v in ok.values()]
            medians = [v["excess"]["median"] for v in ok.values()]
            signs = {_sign(x) for x in means}
            row.update(excess_mean_range=[min(means), max(means)], excess_median_range=[min(medians), max(medians)],
                       direction={1: "positive", -1: "negative", 0: "zero"}[signs.pop()] if len(signs) == 1 else "mixed")
            row["label"] = "mixed_across_symbols" if row["direction"] == "mixed" else "directionally_consistent"
        rows.append(row)
    return rows


def _overlap(a, b):
    return not (a[1] < b[0] or b[1] < a[0])


def cross_period(reports, first="A", second="B"):
    """Period A vs Period B per (symbol, interval, session policy, pivot window, state, horizon)."""
    index = {}
    for report in reports:
        m = report["metadata"]
        for state, entry in report["states"].items():
            for h, stats in entry["horizons"].items():
                index[(m["period"]["label"], m["symbol"], *_profile(report), state, int(h))] = stats
    rows = []
    for key in sorted(k for k in index if k[0] == first):
        other = (second, *key[1:])
        if other not in index:
            continue
        a, b = index[key], index[other]
        row = dict(symbol=key[1], interval=key[2], session_bound=key[3], pivot_window=key[4], state=key[5],
                   horizon=key[6], count_a=a["count"], count_b=b["count"])
        if a["status"] != "OK" or b["status"] != "OK":
            row["label"] = "insufficient_evidence"
        else:
            ea, eb = a["excess"]["mean"], b["excess"]["mean"]
            row.update(excess_mean_a=ea, excess_mean_b=eb, same_sign=_sign(ea) == _sign(eb),
                       magnitude_ratio_b_over_a=None if ea == 0 else round(eb / ea, 4),
                       ci95_overlap=_overlap(a["excess"]["ci95_mean"], b["excess"]["ci95_mean"]))
            row["label"] = "directionally_consistent" if row["same_sign"] else "mixed_across_periods"
        rows.append(row)
    return rows


def pivot_research(reports):
    """Setup-state counts, lag and excess returns per research pivot window (no winner is selected)."""
    rows = []
    for report in sorted(reports, key=lambda r: (r["metadata"]["symbol"], str(r["metadata"]["period"]["label"]),
                                                 r["metadata"]["interval"], r["metadata"]["pivot_window"])):
        m = report["metadata"]
        for state in ("bullish_setup", "bearish_setup"):
            lag = report["setup_lag"][state]
            horizons = report["states"].get(state, {}).get("horizons", {})
            rows.append(dict(
                symbol=m["symbol"], period=m["period"]["label"], interval=m["interval"], pivot_window=m["pivot_window"],
                production=not m["research_config"], state=state, bars_in_state=report["state_frequency"].get(
                    state, {}).get("count", 0), lag=lag,
                excess_mean_by_horizon={h: (v["excess"]["mean"] if v["status"] == "OK" else v["status"])
                                        for h, v in horizons.items()},
                mfe_mae_by_horizon={h: ([v["mfe"]["mean"], v["mae"]["mean"]] if v["status"] == "OK" else v["status"])
                                    for h, v in horizons.items()}))
    return rows
