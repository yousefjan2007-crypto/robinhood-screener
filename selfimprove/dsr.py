"""
The Deflated Sharpe ratio (Bailey & López de Prado 2014) — numpy + statistics.NormalDist only.

Vendored from entry_bot/stats.py, itself ported from signal_lab/evaluation.py, so the Sunday
gates (improve.py, entry_lab/improve_bands.py, weekly_summary.py, scorecard.py, evaluate.py)
run on the Actions runner: no sibling repo on sys.path, no scipy. It is the SAME function, not a
re-derivation — the differences from the sibling's copy are exactly two library swaps that are
numerically identical:

  * scipy.stats.skew(r) / kurtosis(r, fisher=False) with the default bias=True ARE the plain
    moment ratios m3 / m2^1.5 and m4 / m2^2 over the population moments;
  * scipy.stats.norm.ppf / norm.cdf ARE statistics.NormalDist().inv_cdf / .cdf (they agree to
    ~1e-15; verify.py section Q cross-pins the whole function against the sibling's on the Mac,
    where both are present, and runs the planted / zero-mean / n < 8 checks on both partitions).

WHAT IT MEASURES. P(true Sharpe > 0) after deflating the observed Sharpe by the expected maximum
of `n_trials` zero-skill trials (the search you did) and by the non-normality of the series
(skew g, Pearson kurtosis k). The callers feed it DAY MEANS, never rows: the z-score scales by
sqrt(n − 1), so 639 rows where the honest cluster count is 41 days inflates the statistic ~4x
(measured false-pass rate of DSR >= 0.95 under a zero-mean day-clustered null: 17.0% over rows,
0.0% over day means). `n_trials` is the family's cumulative count from selfimprove/trials.json —
you cannot un-look at a result.

Edge cases, kept as the sibling has them: fewer than 8 finite observations, a zero sample
standard deviation, or a Sharpe of exactly zero return 0.0 (never NaN, never a pass).
No wall-clock, no randomness, no I/O. Not financial advice.
"""
from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np

_ND = NormalDist()
EULER_MASCHERONI = 0.5772156649


def norm_ppf(p: float) -> float:
    """The standard normal quantile (scipy.stats.norm.ppf) via statistics.NormalDist."""
    return float(_ND.inv_cdf(float(p)))


def norm_cdf(x: float) -> float:
    """The standard normal CDF (scipy.stats.norm.cdf) via statistics.NormalDist."""
    return float(_ND.cdf(float(x)))


def deflated_sharpe_ratio(returns, n_trials: int, trial_sr_std: float | None = None) -> float:
    """P(true Sharpe > 0) after deflating for `n_trials` and for non-normal returns.

    Vendored from entry_bot/stats.py (ported from signal_lab/evaluation.py); Bailey & López de
    Prado 2014. `trial_sr_std` defaults to 1/sqrt(n), the standard error of a Sharpe estimate
    under the null. Returns 0.0 for n < 8, a constant series or a zero Sharpe.
    """
    r = np.asarray(list(returns), dtype=float)
    r = r[np.isfinite(r)]
    n = int(r.size)
    if n < 8 or r.std(ddof=1) == 0:
        return 0.0
    sr = float(r.mean() / r.std(ddof=1))
    if sr == 0:
        return 0.0
    # scipy's skew / kurtosis(fisher=False) with bias=True: population central moments
    d = r - r.mean()
    m2 = float(np.mean(d ** 2))
    g = float(np.mean(d ** 3) / m2 ** 1.5)
    k = float(np.mean(d ** 4) / m2 ** 2)
    if trial_sr_std is None:
        trial_sr_std = 1.0 / math.sqrt(n)
    e = EULER_MASCHERONI
    z = max(int(n_trials), 1)
    if z == 1:
        expected_max = 0.0
    else:
        expected_max = trial_sr_std * ((1 - e) * norm_ppf(1 - 1.0 / z)
                                       + e * norm_ppf(1 - 1.0 / (z * math.e)))
    denom = math.sqrt(max(1e-12, 1 - g * sr + (k - 1) / 4.0 * sr ** 2))
    return float(norm_cdf((sr - expected_max) * math.sqrt(n - 1) / denom))


if __name__ == "__main__":
    # Offline smoke test: a planted positive series clears 0.95 at 16 trials, a zero-mean series
    # does not, n < 8 and a constant series are 0.0, and the normal helpers invert each other.
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config
    rng = np.random.default_rng(config.SEED)
    planted = rng.normal(0.5, 1.0, 60)
    zero = rng.normal(0.0, 1.0, 60)
    d_p, d_z = deflated_sharpe_ratio(planted, 16), deflated_sharpe_ratio(zero, 16)
    print(f"DSR at 16 trials: planted (Sharpe 0.5, n=60) {d_p:.4f}   zero-mean {d_z:.4f}   "
          f"n<8 {deflated_sharpe_ratio(planted[:7], 16):.1f}   constant {deflated_sharpe_ratio([1.0] * 20, 16):.1f}")
    assert d_p >= 0.95 and d_z < 0.95
    assert deflated_sharpe_ratio(planted[:7], 16) == 0.0 and deflated_sharpe_ratio([1.0] * 20, 16) == 0.0
    assert deflated_sharpe_ratio(planted, 1) > d_p, "one trial deflates less than sixteen"
    assert abs(norm_cdf(norm_ppf(0.3)) - 0.3) < 1e-12 and abs(norm_ppf(0.975) - 1.959963984540054) < 1e-9
    assert deflated_sharpe_ratio([1.0, float("nan"), 2.0, 1.5, 0.5, 1.2, 0.8, 1.1, 0.9, float("inf")], 3) > 0.0
    print("OK — dsr.py assertions hold (numpy + statistics.NormalDist; no scipy, no sibling repo).")
