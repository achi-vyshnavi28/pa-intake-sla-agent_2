"""Hand-checkable math tests for stats_utils.py -- every expected value
below is computed by hand (or with a trivial closed-form check), not
copied from the module under test."""
import numpy as np
import pytest

from stats_utils import ols_trend_slope, robust_zscore, weighted_score


def test_ols_trend_slope_perfect_line():
    # y = 2x + 1 exactly -> slope=2, intercept=1, r_squared=1
    x = [0, 1, 2, 3, 4]
    y = [1, 3, 5, 7, 9]
    result = ols_trend_slope(x, y)
    assert result.slope == pytest.approx(2.0)
    assert result.intercept == pytest.approx(1.0)
    assert result.r_squared == pytest.approx(1.0)
    assert result.direction == "degrading"  # positive slope


def test_ols_trend_slope_flat_line():
    x = [0, 1, 2, 3]
    y = [5, 5, 5, 5]
    result = ols_trend_slope(x, y)
    assert result.slope == pytest.approx(0.0)
    assert result.direction == "flat"


def test_ols_trend_slope_negative_slope_is_improving():
    x = [0, 1, 2, 3]
    y = [10, 7, 4, 1]  # slope = -3
    result = ols_trend_slope(x, y)
    assert result.slope == pytest.approx(-3.0)
    assert result.direction == "improving"


def test_ols_trend_slope_insufficient_points():
    result = ols_trend_slope([0], [5])
    assert result.n_points == 1
    assert result.direction == "insufficient_data"


def test_robust_zscore_hand_computed():
    # values: 1,2,3,4,100 -- median=3, MAD=median(|1-3|,|2-3|,|3-3|,|4-3|,|100-3|)
    #        = median(2,1,0,1,97) = 1 -> scaled MAD = 1.4826
    # z(100) = (100-3)/1.4826 = 65.428...
    values = np.array([1, 2, 3, 4, 100])
    z = robust_zscore(values)
    expected_last = (100 - 3) / (1.4826 * 1)
    assert z[-1] == pytest.approx(expected_last, rel=1e-4)
    assert z[2] == pytest.approx(0.0)  # the median itself has z=0


def test_robust_zscore_constant_series_no_div_by_zero():
    values = np.array([5, 5, 5, 5])
    z = robust_zscore(values)
    assert np.all(np.isfinite(z))
    assert np.all(z == 0)


def test_weighted_score_hand_computed():
    # (0.8, weight 3) and (0.2, weight 1) -> (0.8*3 + 0.2*1) / 4 = 2.6/4 = 0.65
    score = weighted_score({"a": (0.8, 3.0), "b": (0.2, 1.0)})
    assert score == pytest.approx(0.65)


def test_weighted_score_empty_components():
    assert weighted_score({}) == 0.0


def test_weighted_score_zero_total_weight():
    assert weighted_score({"a": (0.9, 0.0)}) == 0.0
