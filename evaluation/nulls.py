"""Phase 5 cluster-preserving null and dependence diagnostics (research only).

**Circular-shift null.** The state sequence ``s[0..N-1]`` is rotated by an offset
``k`` against the *unchanged* forward-return series: ``s'[t] = s[(t - k) mod N]``.

- This preserves the exact state sequence, every run length and the clustering
  (the circular run structure is identical; only the one run spanning the wrap
  point is split or joined).
- It also preserves the exact return series and its autocorrelation, and every
  horizon's validity mask (session-bound labels stay ``None``).
- Only the *alignment* between states and returns is destroyed.

For each iteration, ``k`` is drawn uniformly from ``[m, N - m]`` with
``m = 2 × (h + 1)``. That excludes the zero shift and near-identity shifts
whose labels would still overlap the observed windows. The shifted state means
minus the unchanged all-bars mean form the null distribution of the excess mean.

Reported per state/horizon: ``null_mean``, ``null_p2_5``, ``null_p50``,
``null_p97_5``, the observed percentile (share of null values ≤ observed, × 100)
and a two-sided empirical p-value, ``min(1, 2 × min(P(null ≤ obs), P(null ≥ obs)))``
with the +1 correction. **The p-value is descriptive evidence only.**

- Seeded: numpy PCG64 with ``(seed, crc32(key))``, so it is deterministic.
- No look-ahead: states are computed causally before any shifting, and the null
  never changes a state.

**Dependence diagnostics (descriptive):**

- *Clustering:* per state, the run count, mean/median/max run length and the share
  of the state's bars that sit in multi-bar runs.
- *Overlap:* per state and horizon ``h``, the share of labelled observations whose
  forward window ``[t+1, t+h]`` overlaps another observation of the same state
  (``|t - t'| < h``).
"""
from statistics import mean, median
import zlib

import numpy as np

ITERATIONS = 1000


def rng(seed, key):
    return np.random.default_rng([int(seed), zlib.crc32(key.encode())])


def min_shift(horizon):
    return 2 * (horizon + 1)


def draw_shifts(n, horizon, *, seed, key, iterations=ITERATIONS):
    """Deterministic non-zero offsets in [m, n - m] (empty when the series is too short)."""
    m = min_shift(horizon)
    if n - 2 * m < 1:
        return np.array([], dtype=np.int64)
    return rng(seed, key).integers(m, n - m + 1, size=iterations)


def shifted(states, k):
    """``s'[t] = s[(t - k) mod N]``: the state sequence rotated by ``k`` against fixed returns."""
    return [states[(t - k) % len(states)] for t in range(len(states))]


def circular_runs(states):
    """Run lengths of the sequence read circularly (the invariant a circular shift preserves)."""
    n = len(states)
    start = next((i for i in range(n) if states[i] != states[i - 1]), None)
    if start is None:
        return [n]
    runs, length = [], 0
    for step in range(n):
        i = (start + step) % n
        if step and states[i] != states[i - 1]:
            runs.append(length)
            length = 0
        length += 1
    runs.append(length)
    return sorted(runs)


def circular_shift_null(states, returns, baseline_mean, *, seed, key, horizon, iterations=ITERATIONS):
    """{state: dict(observed_excess_mean, null_*, observed_percentile, p_two_sided, iterations, seed, min_shift)}.

    ``returns`` holds one float per bar or None where the horizon has no valid label.
    """
    names = sorted(set(states))
    code = {name: i for i, name in enumerate(names)}
    codes = np.array([code[s] for s in states], dtype=np.int64)
    values = np.array([np.nan if r is None else r for r in returns], dtype=float)
    valid = ~np.isnan(values)
    n, m = len(codes), min_shift(horizon)
    shifts = draw_shifts(n, horizon, seed=seed, key=key, iterations=iterations)
    if not len(shifts) or not valid.any():
        return {name: dict(status="NULL_UNAVAILABLE", iterations=0, seed=seed) for name in names}
    weights = values[valid]

    def excess(shifted):
        sums = np.bincount(shifted[valid], weights=weights, minlength=len(names))
        counts = np.bincount(shifted[valid], minlength=len(names))
        with np.errstate(invalid="ignore", divide="ignore"):
            return sums / counts - baseline_mean

    observed = excess(codes)
    null = np.vstack([excess(np.roll(codes, int(k))) for k in shifts])
    out = {}
    for name, i in code.items():
        column = null[:, i][~np.isnan(null[:, i])]
        obs = observed[i]
        if np.isnan(obs) or not len(column):
            out[name] = dict(status="NULL_UNAVAILABLE", iterations=iterations, seed=seed)
            continue
        below, above = (column <= obs).sum(), (column >= obs).sum()
        out[name] = dict(
            status="OK", iterations=iterations, seed=seed, min_shift=m, observed_excess_mean=_r(obs),
            null_mean=_r(column.mean()), null_p2_5=_r(np.percentile(column, 2.5)), null_p50=_r(np.percentile(column, 50)),
            null_p97_5=_r(np.percentile(column, 97.5)), observed_percentile=_r(below / len(column) * 100),
            p_two_sided=_r(min(1.0, 2 * min((1 + below) / (1 + len(column)), (1 + above) / (1 + len(column))))))
    return out


def _r(value):
    return round(float(value), 8)


def clustering(states):
    runs = {}
    for i, state in enumerate(states):
        if i and states[i - 1] == state:
            runs[state][-1] += 1
        else:
            runs.setdefault(state, []).append(1)
    return {state: dict(runs=len(v), mean_run=round(mean(v), 4), median_run=median(v), max_run=max(v),
                        multi_bar_fraction=round(sum(x for x in v if x > 1) / sum(v), 6))
            for state, v in sorted(runs.items())}


def overlap(states, returns, horizon):
    """{state: share of labelled observations whose forward window overlaps another same-state observation}."""
    positions = {}
    for t, (state, value) in enumerate(zip(states, returns)):
        if value is not None:
            positions.setdefault(state, []).append(t)
    out = {}
    for state, idx in sorted(positions.items()):
        overlapping = 0
        for j, t in enumerate(idx):
            near = (j > 0 and t - idx[j - 1] < horizon) or (j + 1 < len(idx) and idx[j + 1] - t < horizon)
            overlapping += near
        out[state] = dict(observations=len(idx), overlapping_fraction=round(overlapping / len(idx), 6))
    return out
