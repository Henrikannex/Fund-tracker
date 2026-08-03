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


def test_a_dead_source_is_not_treated_as_no_new_days(tmp_path, monkeypatch, caplog):
    fund = FakeFund(tmp_path, [("2026-07-30", 101.0)])
    monkeypatch.setattr(watch.investing, "fetch_nav",
                        lambda *a, **k: pd.Series(dtype="float64"))

    assert watch.poll(fund) == []
    assert any("Ingen kurser hentet" in r.message for r in caplog.records)
