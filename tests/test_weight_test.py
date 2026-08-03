"""Tests for the single-holding weight test.

Built with synthetic data where the true weight error is known, because the
previous attempt at this produced confident nonsense and nothing caught it.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest


def fit(actual, estimated, holding):
    """Same three-parameter fit the real code does, on arrays."""
    X = np.column_stack([np.ones(len(actual)), estimated, holding])
    beta, *_ = np.linalg.lstsq(X, np.asarray(actual), rcond=None)
    return beta[2] * 100.0


def synthetic(n=120, excess_weight=0.0, seed=3):
    """A fund whose true weight on one holding differs from the one we assume."""
    rng = np.random.default_rng(seed)
    holding = rng.normal(0, 2.0, n)          # the holding's daily return, %
    rest = rng.normal(0, 1.0, n)             # everything else, %
    # We believe the weight is 9 %; the truth is 9 % minus the excess.
    estimated = 0.09 * holding + rest
    actual = (0.09 - excess_weight) * holding + rest
    return actual, estimated, holding


def test_no_weight_error_gives_a_slope_of_zero():
    actual, estimated, holding = synthetic(excess_weight=0.0)
    assert fit(actual, estimated, holding) == pytest.approx(0.0, abs=0.2)


def test_carrying_too_much_shows_up_as_a_negative_slope():
    """Weight believed 9 %, truth 3 % - the fit should recover about -6 points."""
    actual, estimated, holding = synthetic(excess_weight=0.06)
    assert fit(actual, estimated, holding) == pytest.approx(-6.0, abs=0.3)


def test_carrying_too_little_shows_up_as_positive():
    actual, estimated, holding = synthetic(excess_weight=-0.03)
    assert fit(actual, estimated, holding) == pytest.approx(3.0, abs=0.3)


def test_the_fit_is_not_fooled_by_a_correlated_market():
    """Tech names move together; the control must absorb that, not the holding."""
    rng = np.random.default_rng(11)
    n = 120
    market = rng.normal(0, 1.5, n)
    holding = market + rng.normal(0, 1.0, n)      # heavily correlated with market
    estimated = 0.09 * holding + 0.5 * market
    actual = 0.09 * holding + 0.5 * market        # weights are correct
    assert fit(actual, estimated, holding) == pytest.approx(0.0, abs=0.2)
