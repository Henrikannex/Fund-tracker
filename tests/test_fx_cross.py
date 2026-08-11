"""Deriving the exotic FX crosses Yahoo does not quote.

KRWSEK=X does not exist. Without a cross, every Korean holding in a Swedish
fund is dropped for want of a quote — a sixth of SEB Teknologifond, silently
normalised away as if it were not there.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fundtracker.sources import prices as price_source

INDEX = pd.to_datetime(["2026-08-07", "2026-08-10"])


def fake_closing_prices(series: dict[str, list[float]]):
    """Stand in for Yahoo, answering only for the pairs it is given."""
    def _download(tickers, start, end):
        available = {t: series[t] for t in tickers if t in series}
        if not available:
            return pd.DataFrame()
        return pd.DataFrame(available, index=INDEX)

    return _download


def test_direct_pair_is_preferred_and_nothing_extra_is_fetched(monkeypatch):
    """A fund whose pairs Yahoo quotes must not change behaviour at all."""
    calls = []

    def _download(tickers, start, end):
        calls.append(list(tickers))
        return pd.DataFrame({"USDNOK=X": [10.0, 10.1]}, index=INDEX)

    monkeypatch.setattr(price_source, "closing_prices", _download)
    out = price_source.fx_to_base(["USD", "NOK"], "NOK", date(2026, 8, 1), date(2026, 8, 10))

    assert out["USD"].tolist() == [10.0, 10.1]
    assert (out["NOK"] == 1.0).all()
    assert len(calls) == 1, "a working direct pair should not trigger a second fetch"


def test_missing_cross_is_derived_from_the_two_usd_legs(monkeypatch):
    # 1 USD buys 1300 KRW and 10 SEK, so 1 KRW is 10/1300 SEK.
    monkeypatch.setattr(price_source, "closing_prices", fake_closing_prices({
        "USDKRW=X": [1300.0, 1300.0],
        "USDSEK=X": [10.0, 11.0],
    }))
    out = price_source.fx_to_base(["KRW"], "SEK", date(2026, 8, 1), date(2026, 8, 10))

    assert out["KRW"].tolist() == pytest.approx([10.0 / 1300.0, 11.0 / 1300.0])


def test_cross_into_usd_base_needs_only_one_leg(monkeypatch):
    monkeypatch.setattr(price_source, "closing_prices", fake_closing_prices({
        "USDTWD=X": [32.0, 32.0],
    }))
    out = price_source.fx_to_base(["TWD"], "USD", date(2026, 8, 1), date(2026, 8, 10))

    assert out["TWD"].tolist() == pytest.approx([1 / 32.0, 1 / 32.0])


def test_an_all_nan_direct_pair_still_falls_through_to_the_cross(monkeypatch):
    """Yahoo answering with an empty column is the failure mode seen in practice.

    It returns the column rather than omitting it, so checking only for the
    column's presence would accept a series of nothing and skip the cross.
    """
    monkeypatch.setattr(price_source, "closing_prices", fake_closing_prices({
        "KRWSEK=X": [float("nan"), float("nan")],
        "USDKRW=X": [1300.0, 1300.0],
        "USDSEK=X": [10.0, 10.0],
    }))
    out = price_source.fx_to_base(["KRW"], "SEK", date(2026, 8, 1), date(2026, 8, 10))

    assert out["KRW"].notna().all()
    assert out["KRW"].tolist() == pytest.approx([10.0 / 1300.0, 10.0 / 1300.0])


def test_currency_stays_missing_when_neither_route_answers(monkeypatch):
    """Better an honest gap than a fabricated rate; the estimator warns on it."""
    monkeypatch.setattr(price_source, "closing_prices", fake_closing_prices({
        "USDSEK=X": [10.0, 10.0],
    }))
    out = price_source.fx_to_base(["XYZ"], "SEK", date(2026, 8, 1), date(2026, 8, 10))

    assert out["XYZ"].isna().all()


def test_a_zero_quote_does_not_become_an_infinite_rate(monkeypatch):
    monkeypatch.setattr(price_source, "closing_prices", fake_closing_prices({
        "USDKRW=X": [0.0, 1300.0],
        "USDSEK=X": [10.0, 10.0],
    }))
    out = price_source.fx_to_base(["KRW"], "SEK", date(2026, 8, 1), date(2026, 8, 10))

    assert pd.isna(out["KRW"].iloc[0])
    assert out["KRW"].iloc[1] == pytest.approx(10.0 / 1300.0)
