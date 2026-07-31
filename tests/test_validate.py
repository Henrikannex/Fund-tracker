"""Tests for validation against the fund's published period returns."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fundtracker.validate import PERIOD_OFFSETS, _compound, _shift

ANCHOR = date(2026, 7, 29)


def offset(key):
    return PERIOD_OFFSETS[key][1]


def test_week_is_seven_calendar_days():
    assert _shift(ANCHOR, offset("1u")) == date(2026, 7, 22)


def test_months_step_back_by_calendar_month():
    assert _shift(ANCHOR, offset("1m")) == date(2026, 6, 29)
    assert _shift(ANCHOR, offset("3m")) == date(2026, 4, 29)
    assert _shift(ANCHOR, offset("1y")) == date(2025, 7, 29)


def test_year_to_date_starts_at_the_previous_new_year():
    assert _shift(ANCHOR, offset("ytd")) == date(2025, 12, 31)


def test_month_arithmetic_clamps_to_a_real_day():
    """31 March minus one month is 28 February, not 31 February."""
    assert _shift(date(2026, 3, 31), offset("1m")) == date(2026, 2, 28)


def test_month_arithmetic_crosses_the_year_boundary():
    assert _shift(date(2026, 2, 15), offset("3m")) == date(2025, 11, 15)


def test_returns_compound_rather_than_add():
    """Two +1 % days are +2.01 %. Adding instead would understate a year badly."""
    assert _compound(pd.Series([1.0, 1.0])) == pytest.approx(2.01)


def test_compounding_handles_losses():
    # +10 % then -10 % leaves you down 1 %, not flat.
    assert _compound(pd.Series([10.0, -10.0])) == pytest.approx(-1.0)


def test_empty_window_compounds_to_zero():
    assert _compound(pd.Series([], dtype="float64")) == pytest.approx(0.0)
