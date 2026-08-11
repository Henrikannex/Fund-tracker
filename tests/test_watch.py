"""Tests for detecting newly published days.

The failure that matters here is a job that runs every few hours and mails
every few hours, so "nothing new" has to stay genuinely silent.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fundtracker import watch


class FakeFund:
    id = "test"
    name = "Test Fund"
    currency = "NOK"
    # Empty, so the remote fallback in poll() has nothing to reach for and the
    # tests exercise Investing.com's result on its own.
    holdings_source: dict = {}

    def __init__(self, tmp_path, nav_rows, estimates=None):
        self.nav = tmp_path / "nav.csv"
        self.est = tmp_path / "est.csv"
        self.nav_source = {
            "investing_url": "https://example.invalid/x",
            "manual_file": str(self.nav),
        }
        self.nav.write_text(
            "date,nav\n" + "".join(f"{d},{v}\n" for d, v in nav_rows), encoding="utf-8"
        )
        if estimates:
            self.est.write_text(
                "date,estimated_pct\n"
                + "".join(f"{d},{v}\n" for d, v in estimates),
                encoding="utf-8",
            )

    def estimates_file(self):
        return self.est


def series(rows):
    return pd.Series(
        {pd.Timestamp(d): v for d, v in rows}, dtype="float64"
    ).sort_index()


def test_a_newly_published_day_is_reported(tmp_path, monkeypatch):
    fund = FakeFund(tmp_path, [("2026-07-29", 100.0), ("2026-07-30", 101.0)],
                    estimates=[("2026-07-31", 1.50)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: series([("2026-07-30", 101.0),
                                                ("2026-07-31", 102.0)]))

    days = watch.poll(fund)

    assert len(days) == 1
    assert days[0].date == date(2026, 7, 31)
    assert days[0].actual_pct == pytest.approx(0.990, abs=0.01)
    assert days[0].estimated_pct == pytest.approx(1.50)
    assert days[0].error_pct == pytest.approx(0.51, abs=0.01)


def test_nothing_new_reports_nothing(tmp_path, monkeypatch):
    """A four-times-a-day job must be silent on the three quiet runs."""
    fund = FakeFund(tmp_path, [("2026-07-29", 100.0), ("2026-07-30", 101.0)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: series([("2026-07-29", 100.0),
                                                ("2026-07-30", 101.0)]))

    assert watch.poll(fund) == []


def test_a_new_day_is_written_into_the_history(tmp_path, monkeypatch):
    fund = FakeFund(tmp_path, [("2026-07-30", 101.0)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: series([("2026-07-31", 102.0)]))

    watch.poll(fund)

    written = fund.nav.read_text(encoding="utf-8")
    assert "2026-07-31,102.000" in written
    assert "2026-07-30,101.000" in written  # existing rows survive


def test_a_day_without_an_estimate_is_still_reported(tmp_path, monkeypatch):
    fund = FakeFund(tmp_path, [("2026-07-30", 101.0)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: series([("2026-07-31", 102.0)]))

    days = watch.poll(fund)

    assert days[0].estimated_pct is None
    assert days[0].error_pct is None
    assert "ikke noe estimat" in watch.to_text(fund, days)


def test_a_dead_source_raises_rather_than_reporting_a_quiet_day(tmp_path, monkeypatch):
    """Every source refusing must not look like "nothing was published".

    This used to return [] and log a warning. In production that meant nine days
    of 403s from Investing.com reported as green runs saying "ingen nye kurser",
    while the NAV history silently stopped growing and the backtest kept
    measuring against ever older data. A checking mechanism that fails quietly
    also removes the reason to go looking.
    """
    fund = FakeFund(tmp_path, [("2026-07-30", 101.0)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: pd.Series(dtype="float64"))

    with pytest.raises(watch.NoSourceAvailable) as caught:
        watch.poll(fund)
    # The message has to name what was tried, or the next person debugging this
    # learns only that something, somewhere, said no.
    assert "example.invalid" in str(caught.value)


def test_the_remote_fallback_rescues_a_blocked_investing_com(tmp_path, monkeypatch):
    """Investing.com 403s every datacentre request, so CI lives on the fallback."""
    fund = FakeFund(tmp_path, [("2026-07-30", 101.0)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: pd.Series(dtype="float64"))
    monkeypatch.setattr(watch.nav_source, "load_nav_remote",
                        lambda *a, **k: series([("2026-07-30", 101.0),
                                                ("2026-07-31", 103.02)]))

    days = watch.poll(fund)
    assert [d.date for d in days] == [date(2026, 7, 31)]
    assert days[0].actual_pct == pytest.approx(2.0)


def test_the_manual_file_is_never_the_fallback_for_polling(tmp_path, monkeypatch):
    """Reading our own file back would turn a dead source into a quiet day."""
    fund = FakeFund(tmp_path, [("2026-07-30", 101.0)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: pd.Series(dtype="float64"))
    monkeypatch.setattr(watch.nav_source, "load_nav_remote",
                        lambda *a, **k: pd.Series(dtype="float64"))

    with pytest.raises(watch.NoSourceAvailable):
        watch.poll(fund)
