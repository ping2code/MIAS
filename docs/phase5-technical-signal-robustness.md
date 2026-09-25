# Phase 5: Technical Signal Robustness and Validation (Methodology)

Phase 5 asks whether any technical pattern that looked interesting in Phase 4D
survives stronger validation. It is **research only**:

- production thresholds and signal semantics are unchanged, and so are the
  scheduler, alerting and persistence;
- there are no options, trading or fusion features.

The results are in `docs/phase5-results.md`, generated from the actual matrix
outputs. The hypotheses were frozen first, in
`docs/phase5-preregistered-hypotheses.md`.

| Code | Purpose |
|---|---|
| `evaluation/hypotheses.py` | frozen H1, H1X, H2 and H3 definitions, content hashes and verdict rules |
| `evaluation/nulls.py` | circular-shift null; clustering and overlap diagnostics |
| `evaluation/technical_replay.py` | evaluation output `phase5-v1` |
| `evaluation/setup_lag.py` | adds `move_before_confirmation` with bootstrap CIs (confidence intervals) |
| `evaluation/matrix.py` | Phase 5 run set, pseudo-holdout split, progress lines, `--resume`, correlations |
| `evaluation/phase5_report.py` | results report generator (frozen rules only; refuses mismatched hypothesis hashes) |

## 1. Pre-registered hypotheses

H1, H1X, H2 and H3 were frozen (with SHA-256 hashes pinned by tests) before any
Phase 5 analysis code or data existed:

- **H1:** daily `bearish_setup` above drift for META and MSFT, at a fixed 5-bar
  horizon.
- **H1X:** the same pattern on JPM, UNH, CAT and XOM, which were never evaluated
  in Phase 4D.
- **H2:** intraday setups confirm after part of the move.
- **H3:** 1h pivot windows change setup lag and sample availability.

The matrix records the hashes in `plan.json`, and the report generator refuses to
run if they differ from the registered ones.

## 2. Cluster-preserving null (circular shift)

Phase 4D's random-entry baseline drew independent bars. That ignores:

- **state clustering:** a state's bars come in runs;
- **overlapping forward windows:** consecutive observations share returns.

Both make a state's sample mean far more variable than independent draws, which
inflated Phase 4D's exceedance rate (13.9% vs 5%).

**Method.** The state sequence is rotated by an offset `k` against the unchanged
forward returns: `s'[t] = s[(t − k) mod N]`.

- **Preserved:** the exact state sequence, its circular run structure (every run
  length), the exact return series and its autocorrelation, and every horizon's
  validity mask.
- **Destroyed:** only the alignment between states and returns.
- **Shifts:** `k` is uniform in `[m, N − m]` with `m = 2 × (h + 1)`. That excludes
  zero and near-identity shifts whose windows would still overlap the observed
  ones.
- **Iterations and seed:** 1,000 shifts per run and horizon; seed 42042, via numpy
  PCG64 with `(seed, crc32(key))`.

**Output per state/horizon:**

- `observed_excess_mean`;
- `null_mean`, `null_p2_5`, `null_p50`, `null_p97_5`;
- `observed_percentile`;
- `p_two_sided` = `min(1, 2 × min(P(null ≤ obs), P(null ≥ obs)))` with the +1
  correction. **This is descriptive only.**

The null never changes a state, since states are computed causally first. Results
under 30 observations carry no null claim.

**Block bootstrap: not implemented.** The circular shift already preserves the
return dependence exactly, because the return series is untouched. A block
bootstrap would add a second tunable choice (block length) without addressing a
gap. It remains optional for a later phase.

## 3. Dependence diagnostics

- **Clustering,** per state: runs, mean/median/max run length, and the share of the
  state's bars that sit in multi-bar runs.
- **Overlap,** per state and horizon: the share of labelled observations whose
  window `[t+1, t+h]` overlaps another observation of the same state
  (`|t − t'| < h`).
- **Symbol correlation:** pairwise Pearson correlation of daily close-to-close
  returns per period, plus a rough effective count of independent symbols,
  `n / (1 + (n − 1) × mean ρ)`. It describes dependence only.

## 4. Universe

| Symbol | Sector | Phase 4D |
|---|---|---|
| META | Communication services | evaluated |
| NVDA | Information technology | evaluated |
| MSFT | Information technology | evaluated |
| SPY | Broad-market ETF | evaluated |
| JPM | Financials | **unseen** |
| UNH | Health care | **unseen** |
| CAT | Industrials | **unseen** |
| XOM | Energy | **unseen** |

All are liquid large caps. Symbols are plain configuration: no code path is
symbol-specific, and the sector labels exist only in the report.

## 5. Periods and holdout

- **Period A:** 2025-01-02 → 2026-09-23. **Period B:** 2024-10-01 → 2024-12-31.
  Both were seen in Phase 4D.
- **Genuinely unseen time:** 4 sessions earlier and 1 later, which is **not enough
  for any holdout**. So:
  - reused periods are never called holdouts;
  - H1 cannot be SUPPORTED by construction (its frozen rule requires a holdout);
  - the unseen evidence that does exist is cross-sectional: JPM, UNH, CAT and XOM.
- **Pseudo-holdout:** Period A split at 2025-11-03 into A1 and A2, evaluated
  separately from the same fetched bars.
  - It is labelled `pseudo_holdout: true`, excluded from production summaries, and
    descriptive only.
  - Both halves are in-sample for H1.

## 6. Matrix run set (production pivot window 2 unless stated)

| Interval | Session policy | Horizons | Pivot windows |
|---|---|---|---|
| 5m | session-bound | 1, 3, 5, 10 | 2 |
| 1h | session-bound | 1, 3 | 1, 2, 3, 4 (research except 2) |
| 1h | cross-session | 1, 3, 5, 10 | 1, 2, 3, 4 |
| 1d | daily | 1, 3, 5, 10 | 2, plus the A1/A2 pseudo split |

That is 176 reports for 8 symbols × 2 periods, with at most about 176 paced
requests (about 35 minutes; Phase 4D used about 73% of its estimate).

- **Progress:** one stderr line per fetch and per report, with elapsed time and an
  ETA (estimated time remaining). Never keys, bars or payloads.
- **Resume (`--resume`):** each existing report is validated before it is skipped
  (JSON, format version, and metadata matching the run). Invalid reports are
  recomputed, and only symbol-periods that still need work are fetched. The
  summary is always regenerated.

## 7. Effect size first

Every hypothesis result shows the raw mean, the baseline mean, the excess (bps),
the bootstrap CI, the circular-null band, the percentile and n. A result outside
the null band but only a few bps wide is described as **tiny**.

## 8. Minimum sample

Unchanged: n < 30 is `INSUFFICIENT_SAMPLE`, with no statistics and no null. For
hypothesis reporting, n ≥ 100 is described as stronger. Neither threshold is a
production setting.

## 9. Multiple comparisons

Phase 5 limits the formal claims to four frozen hypotheses. The many descriptive
tables in the results report carry no verdicts and must not be mined for new
claims. Any new idea found there would need its own pre-registration and fresh
data.

## 10. Reproducibility

The same data, configuration, seed and code produce byte-identical JSON. This is
tested for a full synthetic matrix, and for resume restoring corrupted files
byte-identically.

## 11. No tuning, no production change

- Production pivot window 2 and every other threshold are untouched.
- Research pivot windows exist only in labelled research runs. The live runner
  never imports the evaluation package, which a clean-subprocess test verifies.
- No migration, and no scheduler or alert change.
