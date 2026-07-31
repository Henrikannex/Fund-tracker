"""Tests for the backtest alignment, which decides which day an estimate scores against."""

from __future__ import annotations

import pandas as pd
import pytest

from fundtracker.backtest import align_lag

# Deliberately spans a weekend: Fri 31 July, then Mon 3 August.
DATES = pd.to_datetime(["2026-07-29", "2026-07-30", "2026-07-31", "2026-08-03"])


def series(values):
    return pd.Series(values, index=DATES, dtype="float64")


def test_lag_zero_pairs_the_same_day():
    frame = align_lag(series([1.0, 2.0, 3.0, 4.0]), series([1.1, 2.1, 3.1, 4.1]), lag=0)

    assert len(frame) == 4
    assert frame.loc[DATES[0], "actual_pct"] == pytest.approx(1.1)
    assert frame.loc[DATES[0], "error_pct"] == pytest.approx(-0.1)


def test_lag_one_pairs_with_the_next_published_nav():
    """A valuation point before the US close means our day T lands on NAV T+1."""
    frame = align_lag(series([1.0, 2.0, 3.0, 4.0]), series([9.0, 1.0, 2.0, 3.0]), lag=1)

    # Estimate for 29 July is scored against the NAV published on 30 July.
    assert frame.loc[DATES[0], "actual_pct"] == pytest.approx(1.0)
    assert frame.loc[DATES[0], "error_pct"] == pytest.approx(0.0)
    # The last date has no following NAV yet, so it drops out rather than pairing
    # with something wrong.
    assert DATES[-1] not in frame.index


def test_lag_shifts_by_trading_day_not_calendar_day():
    """Friday's estimate must pair with Monday's NAV, not with a missing Saturday."""
    frame = align_lag(series([1.0, 2.0, 3.0, 4.0]), series([0.0, 0.0, 0.0, 7.0]), lag=1)

    friday = pd.Timestamp("2026-07-31")
    assert frame.loc[friday, "actual_pct"] == pytest.approx(7.0)


def test_non_overlapping_dates_produce_an_empty_frame_not_bad_pairs():
    estimated = pd.Series([1.0], index=pd.to_datetime(["2026-07-29"]), dtype="float64")
    actual = pd.Series([2.0], index=pd.to_datetime(["2025-01-02"]), dtype="float64")

    assert align_lag(estimated, actual, lag=0).empty
