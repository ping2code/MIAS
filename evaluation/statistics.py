"""Phase 4D evaluation statistics: baselines, excess returns, bootstrap intervals, random entry, sample rules.

Everything here is **descriptive research**. None of it feeds back into technical
state generation, and none of it is a profitability claim.

**Sample-size rule and labels.** ``n`` is the number of labelled observations for a
state/horizon.

| n | Label | Statistics reported |
|---|---|---|
| < 30 | ``INSUFFICIENT`` | count only; status ``INSUFFICIENT_SAMPLE`` (no mean, excess, CI or percentile) |
| 30-99 | ``SMALL`` | full statistics |
| 100-499 | ``MODERATE`` | full statistics |
| >= 500 | ``LARGE`` | full statistics |

**All-bars baseline:** the forward-return statistics (count, mean, median, positive
share) of *every* bar with a valid label for that horizon, in the same run
(symbol, interval, period, session policy).

**Excess return (drift adjustment):**

- ``excess_mean = state_mean - baseline_mean``;
- ``excess_median = state_median - baseline_median``.

**Bootstrap confidence intervals:**

- Percentile bootstrap. The **resampling unit is one labelled observation** of the
  state: it is resampled with replacement to the state's own sample size, ``B``
  times (default 1000).
- The interval is the 2.5th-97.5th percentile of the resampled means (and positive
  shares).
- The excess-mean interval subtracts the baseline mean, which is treated as fixed.
- Observations overlap in time, so the intervals are **optimistic** (too narrow):
  they are indications of spread, not proof of anything.

**Random-entry baseline:**

- ``R`` times (default 1000), draw a random set of entry bars of the same size as
  the state's sample, uniformly **with replacement**, from the bars that have a
  valid label for the same horizon (same symbol, interval, period and session
  policy). Take each draw's mean forward return.
- Reported: the 2.5th, 50th and 97.5th percentiles of those means, and the state's
  percentile within them.

**Determinism:** every resampling stream uses a numpy PCG64 generator seeded with
``(seed, crc32(stream key))``. The key names symbol, interval, period, pivot
window, state, horizon and statistic, so results never depend on evaluation order.
The same data, configuration and seed give identical output.
"""
from statistics import mean, median
import zlib

import numpy as np

MIN_SAMPLE = 30
DEFAULT_SEED = 42042
DEFAULT_ITERATIONS = 1000
CONFIDENCE = 0.95


def sample_label(n):
    if n < 30:
        return "INSUFFICIENT"
    if n < 100:
        return "SMALL"
    if n < 500:
        return "MODERATE"
    return "LARGE"


def rng(seed, key):
    return np.random.default_rng([int(seed), zlib.crc32(key.encode())])


def _round(value):
    return None if value is None else round(float(value), 8)


def basic(values):
    if not values:
        return dict(count=0)
    return dict(count=len(values), mean=_round(mean(values)), median=_round(median(values)),
                positive_share=_round(sum(v > 0 for v in values) / len(values)))


CHUNK = 100  # Resamples per block: bounds memory on large intraday samples; the stream stays sequential.


def _resampled(data, n, seed, key, iterations):
    """(means, positive shares) of ``iterations`` resamples of size ``n`` drawn with replacement from ``data``."""
    generator, means, shares = rng(seed, key), [], []
    for start in range(0, iterations, CHUNK):
        draws = data[generator.integers(0, len(data), size=(min(CHUNK, iterations - start), n))]
        means.append(draws.mean(axis=1))
        shares.append((draws > 0).mean(axis=1))
    return np.concatenate(means), np.concatenate(shares)


def bootstrap(values, *, seed, key, iterations=DEFAULT_ITERATIONS):
    """95% percentile intervals for the mean and the positive share."""
    data = np.asarray(values, dtype=float)
    lo, hi = (1 - CONFIDENCE) / 2 * 100, (1 + CONFIDENCE) / 2 * 100
    means, shares = _resampled(data, len(data), seed, key, iterations)
    return dict(mean=[_round(np.percentile(means, lo)), _round(np.percentile(means, hi))],
                positive_share=[_round(np.percentile(shares, lo)), _round(np.percentile(shares, hi))])


def random_entry(pool, n, state_mean, *, seed, key, iterations=DEFAULT_ITERATIONS):
    """Distribution of mean forward returns of ``n`` random entries drawn from ``pool``."""
    means, _ = _resampled(np.asarray(pool, dtype=float), n, seed, key, iterations)
    return dict(iterations=iterations, p2_5=_round(np.percentile(means, 2.5)), p50=_round(np.percentile(means, 50)),
                p97_5=_round(np.percentile(means, 97.5)),
                state_percentile=_round((means <= state_mean).mean() * 100))


def state_horizon(values, pool, baseline, *, seed, key, iterations=DEFAULT_ITERATIONS, excursions=None):
    """Full statistics for one state/horizon, or the count and status only when the sample is too small."""
    n = len(values)
    result = dict(count=n, sample_label=sample_label(n))
    if n < MIN_SAMPLE:
        result["status"] = "INSUFFICIENT_SAMPLE"
        return result
    stats = basic(values)
    ci = bootstrap(values, seed=seed, key=f"{key}:bootstrap", iterations=iterations)
    result.update(
        status="OK",
        forward_return=dict(mean=stats["mean"], median=stats["median"], positive_share=stats["positive_share"],
                            ci95_mean=ci["mean"], ci95_positive_share=ci["positive_share"]),
        excess=dict(mean=_round(stats["mean"] - baseline["mean"]), median=_round(stats["median"] - baseline["median"]),
                    ci95_mean=[_round(ci["mean"][0] - baseline["mean"]), _round(ci["mean"][1] - baseline["mean"])]),
        random_entry=random_entry(pool, n, stats["mean"], seed=seed, key=f"{key}:random", iterations=iterations))
    if excursions:
        for name, series in excursions.items():
            result[name] = dict(mean=_round(mean(series)), median=_round(median(series)))
    return result
