"""Generate the Phase 5 results report from actual matrix outputs (statistics only; research only).

    python -m evaluation.phase5_report --in /tmp/mias-5 --out docs/phase5-results.md [--json results.json]

- Hypothesis verdicts come **only** from the frozen rules in ``evaluation.hypotheses``.
- The report refuses to run if ``plan.json`` records hypothesis hashes that differ
  from the registered ones (the definitions changed after the run).
- Nothing here ranks configurations, recommends thresholds or claims profitability.
"""
import argparse
import glob
import json
import os
import sys
from statistics import median

from evaluation import hypotheses as hyp

SECTORS = dict(META="Communication services", NVDA="Information technology", MSFT="Information technology",
               SPY="Broad-market ETF", JPM="Financials", UNH="Health care", CAT="Industrials", XOM="Energy")
UNSEEN = ("JPM", "UNH", "CAT", "XOM")
PROFILES = (("5m", True), ("1h", True), ("1h", False), ("1d", False))


class ReportError(RuntimeError):
    pass


def load(directory):
    reports = {}
    for path in sorted(glob.glob(os.path.join(directory, "*_pw*.json"))):
        with open(path) as handle:
            reports[os.path.basename(path)[:-5]] = json.load(handle)
    with open(os.path.join(directory, "plan.json")) as handle:
        plan = json.load(handle)
    with open(os.path.join(directory, "summary.json")) as handle:
        summary = json.load(handle)
    if plan.get("hypothesis_hashes") != hyp.REGISTERED_HASHES:
        raise ReportError("plan.json hypothesis hashes differ from the registered hypotheses")
    return reports, plan, summary


def find(reports, symbol, interval, period, session_bound=None, pivot=2):
    for r in reports.values():
        m = r["metadata"]
        if (m["symbol"], m["interval"], m["period"]["label"], m["pivot_window"]) == (symbol, interval, period, pivot) \
                and (session_bound is None or m["session_bound"] == session_bound):
            return r
    return None


def bps(x):
    return "—" if x is None else f"{x * 1e4:+.1f}"


def table(header, rows):
    return "\n".join(["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] +
                     ["| " + " | ".join(str(c) for c in row) + " |" for row in rows])


def excess_cell(report, state, horizon):
    """H1-style cell: counts, raw/baseline/excess, bootstrap CI and circular-shift null (None if unavailable)."""
    if report is None:
        return None
    stats = report["states"].get(state, {}).get("horizons", {}).get(str(horizon))
    if not stats:
        return dict(count=0)
    if stats["status"] != "OK":
        return dict(count=stats["count"])
    null = stats.get("circular_shift", {})
    if null.get("status") != "OK":
        return dict(count=stats["count"])
    return dict(count=stats["count"], raw=stats["forward_return"]["mean"], baseline=report["baseline"][str(horizon)]["mean"],
                excess=stats["excess"]["mean"], ci=stats["excess"]["ci95_mean"], null_p2_5=null["null_p2_5"],
                null_p97_5=null["null_p97_5"], percentile=null["observed_percentile"], p=null["p_two_sided"],
                excess_bps=stats["excess"]["mean"] * 1e4, null_p2_5_bps=null["null_p2_5"] * 1e4,
                null_p97_5_bps=null["null_p97_5"] * 1e4)


def excess_rows(cells):
    rows = []
    for symbol, c in cells.items():
        if c is None or "excess" not in c:
            rows.append([symbol, (c or {}).get("count", 0), "—", "—", "INSUFFICIENT / unavailable", "—", "—", "—",
                         hyp.classify_symbol_excess(c if c and "excess" in c else None)])
            continue
        rows.append([symbol, c["count"], bps(c["raw"]), bps(c["baseline"]),
                     f"{bps(c['excess'])} [{bps(c['ci'][0])}, {bps(c['ci'][1])}]",
                     f"[{bps(c['null_p2_5'])}, {bps(c['null_p97_5'])}]", f"{c['percentile']:.1f}", f"{c['p']:.3f}",
                     hyp.classify_symbol_excess(c)])
    return table(["Symbol", "n", "Raw mean (bps)", "Baseline (bps)", "Excess [95% bootstrap CI]",
                  "Circular-shift null 95%", "Null percentile", "Two-sided p (descriptive)", "Frozen class"], rows)


def build(reports, plan, summary):
    results, sections = {}, {}
    symbols = plan["symbols"]
    # H1 / H1X.
    h1_cells = {s: excess_cell(find(reports, s, "1d", "A"), "bearish_setup", 5) for s in hyp.H1.symbols}
    h1_status, h1_classes = hyp.classify_h1({s: (c if c and "excess" in c else None) for s, c in h1_cells.items()}, {})
    results["H1"] = dict(status=h1_status, per_symbol=h1_classes, cells=h1_cells)
    pseudo = {f"{s} {half}": excess_cell(find(reports, s, "1d", half), "bearish_setup", 5)
              for s in hyp.H1.symbols for half in ("A1", "A2")}
    h1x_cells = {s: excess_cell(find(reports, s, "1d", "A"), "bearish_setup", 5) for s in hyp.H1X.symbols}
    h1x_status, h1x_classes = hyp.classify_h1x({s: (c if c and "excess" in c else None) for s, c in h1x_cells.items()})
    results["H1X"] = dict(status=h1x_status, per_symbol=h1x_classes, cells=h1x_cells)
    sections.update(H1=excess_rows(h1_cells), H1_PSEUDO=excess_rows(pseudo), H1X=excess_rows(h1x_cells))
    # H2.
    cells, rows = [], []
    for s in symbols:
        for interval in ("5m", "1h"):
            for period in ("A", "B"):
                r = find(reports, s, interval, period, session_bound=True)
                if r is None:
                    continue
                typical = r["metadata"].get("median_abs_2bar_return")
                for state, direction in (("bullish_setup", "bullish"), ("bearish_setup", "bearish")):
                    lag = r["setup_lag"][state]
                    n = lag["occurrences"]
                    if lag["status"] == "OK":
                        ci = lag["ci95_mean_move_before_confirmation"]
                        cells.append(dict(direction=direction, count=n, mean=lag["mean_move_before_confirmation"],
                                          ci95=ci, symbol=s))
                        ratio = (abs(lag["mean_move_before_confirmation"]) / typical) if typical else None
                        rows.append([s, interval, period, direction, n, f"{lag['mean_confirmation_lag_bars']:.2f}",
                                     f"{lag['mean_move_before_confirmation'] * 100:+.3f}%",
                                     f"[{ci[0] * 100:+.3f}%, {ci[1] * 100:+.3f}%]",
                                     "—" if ratio is None else f"{ratio:.2f}"])
                    else:
                        cells.append(dict(direction=direction, count=n, mean=None, ci95=[0, 0], symbol=s))
                        rows.append([s, interval, period, direction, n, "—", "INSUFFICIENT", "—", "—"])
    h2_status, h2_detail = hyp.classify_h2(cells)
    h2_unseen = hyp.classify_h2([c for c in cells if c["symbol"] in UNSEEN])
    results["H2"] = dict(status=h2_status, detail=h2_detail, unseen_status=h2_unseen[0], unseen_detail=h2_unseen[1])
    sections["H2"] = table(["Symbol", "Interval", "Period", "Direction", "Setups", "Mean confirmation lag (bars)",
                            "Mean move before confirmation", "95% CI", "abs(move) ÷ median abs(2-bar return)"], rows)
    # H3.
    h3_cells, h3_rows = [], []
    for s in symbols:
        for state, direction in (("bullish_setup", "bullish"), ("bearish_setup", "bearish")):
            occ, move, cols = [], [], []
            for w in (1, 2, 3, 4):
                r = find(reports, s, "1h", "A", session_bound=True, pivot=w)
                lag = r["setup_lag"][state] if r else dict(occurrences=0, status="INSUFFICIENT_SAMPLE")
                occ.append(lag["occurrences"])
                move.append(lag.get("mean_abs_move_before_confirmation") if lag["status"] == "OK" else None)
                cols.append(f"{lag['occurrences']} / " + ("—" if move[-1] is None else f"{move[-1] * 100:.3f}%"))
            h3_cells.append(dict(occurrences=occ, move=move))
            h3_rows.append([s, direction, *cols])
    h3_status, h3_detail = hyp.classify_h3(h3_cells)
    results["H3"] = dict(status=h3_status, detail=h3_detail)
    sections["H3"] = table(["Symbol", "Direction"] + [f"Window {w}: setups / mean abs(move before confirmation)"
                                                      for w in (1, 2, 3, 4)], h3_rows)
    evaluable = [c for c in h3_cells if all(n >= hyp.MIN_SAMPLE for n in c["occurrences"]) and None not in c["move"]]
    results["H3"]["decomposition"] = dict(
        evaluable=len(evaluable),
        move_non_decreasing=sum(all(a <= b for a, b in zip(c["move"], c["move"][1:])) for c in evaluable),
        occurrences_non_increasing=sum(all(a >= b for a, b in zip(c["occurrences"], c["occurrences"][1:]))
                                       for c in evaluable),
        move_w1_to_w4_ratio_median=round(median(c["move"][3] / c["move"][0] for c in evaluable), 2) if evaluable else None,
        occurrences_w4_over_w1_median=round(median(c["occurrences"][3] / c["occurrences"][0] for c in evaluable), 2)
        if evaluable else None)
    # 1h pivot-window trade-offs: excess cells outside the circular null band, per window and policy.
    trade = []
    for w in (1, 2, 3, 4):
        for bound in (True, False):
            tested = outside = 0
            magnitudes = []
            for s in symbols:
                r = find(reports, s, "1h", "A", session_bound=bound, pivot=w)
                if not r:
                    continue
                for state in ("bullish_setup", "bearish_setup"):
                    for h, stats in r["states"].get(state, {}).get("horizons", {}).items():
                        null = stats.get("circular_shift", {})
                        if stats["status"] == "OK" and null.get("status") == "OK":
                            tested += 1
                            ex = stats["excess"]["mean"]
                            outside += not (null["null_p2_5"] <= ex <= null["null_p97_5"])
                            magnitudes.append(abs(ex) * 1e4)
            trade.append([w, "session-bound 1,3" if bound else "cross-session 1,3,5,10", tested, outside,
                          f"{outside / tested * 100:.1f}%" if tested else "—",
                          f"{median(magnitudes):.1f}" if magnitudes else "—"])
    sections["PIVOT"] = table(["Pivot window", "Horizons", "Setup state/horizon cells (n ≥ 30)",
                               "Outside circular null 95%", "Share", "Median abs(excess) (bps)"], trade)
    # Null comparison: random entry vs circular shift, per profile (production, all periods).
    comp = []
    totals = [0, 0, 0]
    for interval, bound in PROFILES:
        tested = rand = circ = 0
        for r in reports.values():
            m = r["metadata"]
            if m["research_config"] or m["period"].get("pseudo_holdout") or m["interval"] != interval \
                    or m["session_bound"] != (bound and interval != "1d"):
                continue
            for state, entry in r["states"].items():
                if state == "insufficient_data":
                    continue
                for stats in entry["horizons"].values():
                    null = stats.get("circular_shift", {})
                    if stats["status"] != "OK" or null.get("status") != "OK":
                        continue
                    tested += 1
                    p = stats["random_entry"]["state_percentile"]
                    rand += p < 2.5 or p > 97.5
                    circ += not (null["null_p2_5"] <= stats["excess"]["mean"] <= null["null_p97_5"])
        totals = [totals[0] + tested, totals[1] + rand, totals[2] + circ]
        if tested:
            comp.append([interval, "session-bound" if bound and interval != "1d" else
                         ("cross-session" if interval == "1h" else ("session-bound" if interval == "5m" else "daily")),
                         tested, f"{rand / tested * 100:.1f}%", f"{circ / tested * 100:.1f}%"])
    comp.append(["all", "", totals[0], f"{totals[1] / max(totals[0], 1) * 100:.1f}%",
                 f"{totals[2] / max(totals[0], 1) * 100:.1f}%"])
    results["null_comparison"] = dict(tested=totals[0], random_outside=totals[1], circular_outside=totals[2])
    sections["NULLS"] = table(["Interval", "Policy", "State/horizon results tested", "Outside independent random-entry 95%",
                               "Outside circular-shift 95%"], comp)
    # Clustering and overlap.
    clus = []
    for interval, bound in (("5m", True), ("1h", True), ("1d", False)):
        fractions, means = [], []
        for r in reports.values():
            m = r["metadata"]
            if m["interval"] == interval and not m["research_config"] and m["period"]["label"] == "A" \
                    and m["session_bound"] == (bound and interval != "1d"):
                for state, c in r["clustering"].items():
                    if state != "insufficient_data":
                        fractions.append(c["multi_bar_fraction"])
                        means.append(c["mean_run"])
        if not means:
            clus.append([interval, "—", "—", "—", "—"])
            continue
        clus.append([interval, f"{min(means):.2f} … {max(means):.2f}", f"{median(means):.2f}",
                     f"{min(fractions) * 100:.0f}% … {max(fractions) * 100:.0f}%", f"{median(fractions) * 100:.0f}%"])
    sections["CLUSTER"] = table(["Interval", "Mean run length (range over symbols × states)", "Median",
                                 "Share of bars in multi-bar runs (range)", "Median"], clus)
    over = []
    for interval, bound, horizons in (("5m", True, (1, 3, 5, 10)), ("1h", False, (1, 3, 5, 10)),
                                      ("1d", False, (1, 3, 5, 10))):
        cells_by_h = {h: [] for h in horizons}
        for r in reports.values():
            m = r["metadata"]
            if m["interval"] == interval and not m["research_config"] and m["period"]["label"] == "A" \
                    and m["session_bound"] == (bound and interval != "1d"):
                for h in horizons:
                    for state, o in r["overlap"].get(str(h), {}).items():
                        if state != "insufficient_data" and o["observations"] >= hyp.MIN_SAMPLE:
                            cells_by_h[h].append(o["overlapping_fraction"])
        over.append([interval + (" cross-session" if interval == "1h" else "")] +
                    [f"{median(v) * 100:.0f}%" if v else "—" for v in cells_by_h.values()])
    sections["OVERLAP"] = table(["Interval", "h=1", "h=3", "h=5", "h=10"], over)
    # Cross-sector (1d/1h/5m, period A, production): share of state/horizon results outside the circular band.
    sector = []
    for s in symbols:
        row = [s, SECTORS.get(s, "—"), "unseen in Phase 4D" if s in UNSEEN else "Phase 4D"]
        for interval, bound in PROFILES:
            r = find(reports, s, interval, "A", session_bound=bound and interval != "1d")
            tested = outside = 0
            for state, entry in (r or {}).get("states", {}).items():
                if state == "insufficient_data":
                    continue
                for stats in entry["horizons"].values():
                    null = stats.get("circular_shift", {})
                    if stats["status"] == "OK" and null.get("status") == "OK":
                        tested += 1
                        outside += not (null["null_p2_5"] <= stats["excess"]["mean"] <= null["null_p97_5"])
            row.append(f"{outside}/{tested}" if tested else "—")
        sector.append(row)
    sections["SECTOR"] = table(["Symbol", "Sector", "Status", "5m", "1h session-bound", "1h cross-session", "1d"], sector)
    # Effect sizes of results outside the circular band.
    eff = []
    for interval, bound in PROFILES:
        mags = []
        for r in reports.values():
            m = r["metadata"]
            if m["research_config"] or m["period"].get("pseudo_holdout") or m["interval"] != interval \
                    or m["session_bound"] != (bound and interval != "1d"):
                continue
            for state, entry in r["states"].items():
                for stats in entry["horizons"].values():
                    null = stats.get("circular_shift", {})
                    if state != "insufficient_data" and stats["status"] == "OK" and null.get("status") == "OK" \
                            and not (null["null_p2_5"] <= stats["excess"]["mean"] <= null["null_p97_5"]):
                        mags.append(abs(stats["excess"]["mean"]) * 1e4)
        eff.append([interval, "session-bound" if bound and interval != "1d" else
                    ("cross-session" if interval == "1h" else ("session-bound" if interval == "5m" else "daily")),
                    len(mags), f"{median(mags):.1f}" if mags else "—", f"{max(mags):.1f}" if mags else "—"])
    sections["EFFECT"] = table(["Interval", "Policy", "Results outside circular 95%", "Median abs(excess) (bps)",
                                "Max abs(excess) (bps)"], eff)
    # Cross-period and correlations.
    cp = [r for r in summary["cross_period"] if r["label"] != "insufficient_evidence" and r["state"] != "insufficient_data"]
    same = sum(r["label"] == "directionally_consistent" for r in cp)
    results["cross_period"] = dict(comparable=len(cp), same_sign=same)
    corr_rows = []
    for label, c in summary.get("correlations", {}).items():
        corr_rows.append([label, len(c["symbols"]), "—" if c["mean_pairwise"] is None else f"{c['mean_pairwise']:.2f}",
                          "—" if c["effective_independent_symbols"] is None else c["effective_independent_symbols"],
                          ", ".join(f"{k} {v:.2f}" for k, v in sorted(c["pairs"].items(), key=lambda kv: -kv[1])[:5])])
    sections["CORR"] = table(["Period", "Symbols", "Mean pairwise daily-return correlation",
                              "Rough effective independent symbols", "Most correlated pairs"], corr_rows)
    inv = []
    for s in symbols:
        for period in [p["label"] for p in plan["periods"]]:
            r5 = find(reports, s, "5m", period)
            r1 = find(reports, s, "1h", period, session_bound=True)
            rd = find(reports, s, "1d", period)
            inv.append([s, period] + [f"{r['metadata']['bar_count']:,}" if r else "missing" for r in (r5, r1, rd)])
    sections["INVENTORY"] = table(["Symbol", "Period", "5m bars", "1h bars", "1d bars"], inv)
    results["plan"] = dict(requests_made=plan.get("requests_made"), failures=plan.get("failures", []),
                           excluded_overnight_bars=plan.get("excluded_overnight_bars"), reports=len(reports))
    return results, sections


def render(results, sections, plan):
    statuses = {k: results[k]["status"] for k in ("H1", "H1X", "H2", "H3")}
    lines = [
        "# Phase 5: Results",
        "",
        "Generated by `python -m evaluation.phase5_report` from the actual Phase 5 matrix outputs. **Research and",
        "observational evidence only**: this is not a profitability backtest, and no production threshold, signal,",
        "schedule or alert changed. Hypotheses and verdict rules were frozen before the run",
        "(`docs/phase5-preregistered-hypotheses.md`); `plan.json` carries the matching hashes.",
        "",
        "## 0. Verdicts (frozen rules)",
        "",
        table(["Hypothesis", "Verdict"], [[k, v] for k, v in statuses.items()]),
        "",
        f"- H2 on unseen symbols only (JPM, UNH, CAT, XOM): **{results['H2']['unseen_status']}**.",
        "- Genuine time holdout: **not available** (see §4), so H1 cannot be SUPPORTED by construction.",
        "",
        "## 1. Run inventory",
        "",
        sections["INVENTORY"],
        "",
        f"- Reports: {results['plan']['reports']}. Requests: {results['plan']['requests_made']}. Failures: "
        f"{len(results['plan']['failures'])}. Overnight vendor bars excluded: {results['plan']['excluded_overnight_bars']}.",
        "",
        "## 2. H1: daily bearish_setup above drift (META, MSFT; 5-bar horizon; Period A, in-sample)",
        "",
        sections["H1"],
        "",
        f"**Verdict: {statuses['H1']}.**",
        "",
        "Pseudo-holdout (Period A halves; both in-sample; descriptive only):",
        "",
        sections["H1_PSEUDO"],
        "",
        "## 3. H1X: the same pattern on symbols unseen in Phase 4D",
        "",
        sections["H1X"],
        "",
        f"**Verdict: {statuses['H1X']}.**",
        "",
        "## 4. Holdout availability",
        "",
        "The free tier offers 4 unseen sessions before Period B and 1 after Period A. That is not enough for any",
        "time holdout. The genuinely unseen evidence is cross-sectional: JPM, UNH, CAT and XOM (H1X, H2 unseen",
        "cells, H3).",
        "",
        "## 5. H2: intraday confirmation lag",
        "",
        sections["H2"],
        "",
        f"**Verdict: {statuses['H2']}** ({results['H2']['detail']['consistent']} of "
        f"{results['H2']['detail']['evaluable']} evaluable cells consistent; "
        f"{results['H2']['detail']['opposite']} significant on the opposite side).",
        "",
        "## 6. H3: 1h pivot windows (research only; nothing selected)",
        "",
        sections["H3"],
        "",
        f"**Verdict: {statuses['H3']}** ({results['H3']['detail']['consistent']} of "
        f"{results['H3']['detail']['evaluable']} evaluable cells consistent).",
        "",
        "Setup-state forward returns by window, reported as trade-offs only:",
        "",
        sections["PIVOT"],
        "",
        "## 7. Null models: independent random entry vs circular shift",
        "",
        sections["NULLS"],
        "",
        "## 8. State clustering",
        "",
        sections["CLUSTER"],
        "",
        "## 9. Overlapping forward windows (median share of a state's observations that overlap another)",
        "",
        sections["OVERLAP"],
        "",
        "## 10. Cross-sector view (Period A: state/horizon results outside the circular-shift 95% band)",
        "",
        sections["SECTOR"],
        "",
        "## 11. Cross-period",
        "",
        f"Comparable state/horizon pairs (n ≥ 30 in both A and B): {results['cross_period']['comparable']}; "
        f"same excess sign: {results['cross_period']['same_sign']} "
        f"({results['cross_period']['same_sign'] / max(results['cross_period']['comparable'], 1) * 100:.0f}%; "
        "50% expected by chance).",
        "",
        "## 12. Effect sizes of results outside the circular-shift band",
        "",
        sections["EFFECT"],
        "",
        "## 13. Symbol dependence",
        "",
        sections["CORR"],
        "",
        "## 14. Interpretation notes (descriptive; verdicts above are final)",
        "",
        f"- **H1:** the excess is positive with bootstrap CIs excluding zero, as in Phase 4D, but it lies inside the",
        "  circular-shift null band for both symbols. Once state clustering and overlapping windows are respected,",
        "  the effect cannot be distinguished from chance alignment. The bootstrap CI (resampling observations",
        "  independently) is narrower than the cluster-preserving null band, which is why Phase 4D's CI",
        "  looked conclusive.",
        "- **H1X:** CAT has too few daily bearish setups, so the frozen rule (all four unseen symbols evaluable)",
        "  gives INSUFFICIENT. The evaluable unseen symbols show the same pattern as H1: positive excess, but inside",
        "  their circular nulls. A pooled cross-sectional test was **not** pre-registered and is not performed here.",
        "- **H2:** confirmation lag equals the pivot window (2 bars) by construction. The pre-registered",
        "  move-before-confirmation metric uses the structure-completing pivot, which for a bullish setup can be",
        "  a swing *high* (whose later closes lie mechanically below it) and for a bearish setup a swing *low*.",
        "  The metric therefore mixes two opposite mechanical effects. That plausibly explains the MIXED verdict",
        "  (no cell is significant in the opposite direction). A pivot-kind-specific metric would need a new",
        "  pre-registration; the frozen metric is reported as registered.",
        f"- **H3:** of {results['H3']['decomposition']['evaluable']} evaluable cells, the lag-magnitude half holds in "
        f"{results['H3']['decomposition']['move_non_decreasing']} (median window 4 ÷ window 1 move ratio "
        f"{results['H3']['decomposition']['move_w1_to_w4_ratio_median']}). The sample-availability half holds in "
        f"only {results['H3']['decomposition']['occurrences_non_increasing']} (median window 4 ÷ window 1 setup-count "
        f"ratio {results['H3']['decomposition']['occurrences_w4_over_w1_median']}). Setup counts are roughly flat",
        "  across windows. The frozen rule needs both, so H3 is UNSUPPORTED. The pivot-window return trade-offs",
        "  (§6) stay near the null rate: no window stands out, and none is selected.",
        f"- **Null models:** switching from independent random entries to the cluster-preserving circular shift",
        f"  lowers the share of results outside the 95% band from "
        f"{results['null_comparison']['random_outside'] / max(results['null_comparison']['tested'], 1) * 100:.1f}% to "
        f"{results['null_comparison']['circular_outside'] / max(results['null_comparison']['tested'], 1) * 100:.1f}%, "
        "close to the nominal 5%. That confirms Phase 4D's excess exceedance was mostly clustering. The results",
        "  still outside the band are tiny on 5m (§12). Those on 1h cross-session are larger in size but occur near",
        "  the null rate, and all are spread across symbols without a consistent pattern (§10).",
        "",
        "## 15. Limitations",
        "",
        "- No genuine time holdout (free-tier history). Periods A and B were both seen in Phase 4D. The pseudo-",
        "  holdout halves are in-sample and mostly below the sample minimum for daily setups.",
        "- The eight symbols are correlated (mean pairwise daily correlation above; roughly 3 effective independent",
        "  symbols), so cross-symbol agreement is weaker evidence than eight separate experiments.",
        "- Forward windows overlap heavily (§9) and states cluster (§8). The circular shift handles both for the",
        "  null, but bootstrap CIs remain optimistic.",
        "- The circular shift assumes the state/return relationship is time-invariant within a run, and wrap-point",
        "  effects exist in short series.",
        "- The descriptive tables contain hundreds of results. Only the four frozen hypotheses carry verdicts.",
        "",
        "## 16. Does any evidence justify a future threshold-calibration phase?",
        "",
    ]
    supported = [k for k, v in statuses.items() if v == "SUPPORTED"]
    if supported:
        lines.append(f"Hypotheses SUPPORTED under the frozen rules: {', '.join(supported)}. Any calibration would still "
                     "need its own pre-registration and genuinely unseen data before touching production.")
    else:
        lines.append("**No.** No pre-registered hypothesis is SUPPORTED. The daily `bearish_setup` pattern that motivated "
                     "H1 does not survive the cluster-preserving null, in-sample or on unseen symbols. Remaining "
                     "exceedances occur close to the null rate, and are tiny on 5m. No production threshold should "
                     "change on this evidence. Options analytics "
                     "and signal fusion remain out of scope.")
    lines.append("")
    return "\n".join(lines)


def main(argv=None, out=None):
    parser = argparse.ArgumentParser(prog="python -m evaluation.phase5_report")
    parser.add_argument("--in", dest="directory", required=True)
    parser.add_argument("--out")
    parser.add_argument("--json")
    args = parser.parse_args(argv)
    try:
        reports, plan, summary = load(args.directory)
    except (OSError, ValueError, ReportError) as error:
        print(f"report error: {error}", file=sys.stderr)
        return 1
    results, sections = build(reports, plan, summary)
    text = render(results, sections, plan)
    if args.out:
        with open(args.out, "w") as handle:
            handle.write(text.rstrip() + "\n")
    else:
        print(text, file=out or sys.stdout)
    if args.json:
        with open(args.json, "w") as handle:
            json.dump(results, handle, indent=2, sort_keys=True, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
