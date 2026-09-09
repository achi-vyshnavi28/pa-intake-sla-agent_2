"""
Small, explicit, documented statistical helpers.

Deliberately simple closed-form formulas rather than an opaque trained
model -- every score this agent produces should be explainable in one
sentence to a reviewer. Dataclasses are used deliberately for structured
results so downstream code gets typed fields instead of bare tuples.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MIN_SAMPLE_SIZE = 5  # documented minimum sample size for a HIGH-confidence finding


@dataclass(frozen=True)
class TrendResult:
    slope: float
    intercept: float
    r_squared: float
    n_points: int

    @property
    def direction(self) -> str:
        if self.n_points < 3:
            return "insufficient_data"
        if abs(self.slope) < 1e-9:
            return "flat"
        return "degrading" if self.slope > 0 else "improving"


def ols_trend_slope(x, y) -> TrendResult:
    """Ordinary least squares slope of y on x, closed form (no library
    black box): slope = cov(x, y) / var(x). Used for SLA-breach-rate trend
    detection (breach rate per week vs. week index)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 2 or np.all(x == x[0]):
        return TrendResult(slope=0.0, intercept=float(y.mean()) if n else 0.0, r_squared=0.0, n_points=n)
    x_mean, y_mean = x.mean(), y.mean()
    cov_xy = float(np.sum((x - x_mean) * (y - y_mean)))
    var_x = float(np.sum((x - x_mean) ** 2))
    slope = cov_xy / var_x if var_x > 0 else 0.0
    intercept = y_mean - slope * x_mean
    y_pred = slope * x + intercept
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y_mean) ** 2))
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return TrendResult(slope=slope, intercept=intercept, r_squared=r_squared, n_points=n)


def robust_zscore(values) -> np.ndarray:
    """Median/MAD-based robust z-score -- resistant to the outliers it's
    trying to detect, unlike a mean/stdev z-score which the outliers
    themselves inflate. 1.4826 is the standard constant making MAD a
    consistent estimator of the standard deviation under normality."""
    values = np.asarray(values, dtype=float)
    median = np.median(values)
    mad = np.median(np.abs(values - median))
    if mad == 0:
        mad = 1e-6  # degenerate near-constant series: avoid divide-by-zero
    return (values - median) / (1.4826 * mad)


def weighted_score(components: dict[str, tuple[float, float]]) -> float:
    """Explicit, documented weighted risk/confidence scoring. `components`
    maps a named signal -> (normalized_value_0_to_1, weight). Not a
    trained/opaque model -- every score is explainable by listing its
    components and weights."""
    if not components:
        return 0.0
    total_weight = sum(w for _, w in components.values())
    if total_weight == 0:
        return 0.0
    return sum(v * w for v, w in components.values()) / total_weight
