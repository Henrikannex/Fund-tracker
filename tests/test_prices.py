"""Tests for the price download layer.

The interesting behaviour here is not the happy path but the recovery: Yahoo
regularly returns a batch where some tickers are silently missing their most
recent close, and every one of those looks like a closed market to the rest of
the pipeline.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fundtracker.sources import prices


DAYS = pd.to_datetime(["2026-08-01", "2026-08-04"])


def frame_for(tickers, last_row) -> pd.DataFrame:
    """A two-day frame where the final day is whatever ``last_row`` says."""
    return pd.DataFrame(
        {t: [100.0, last_row.get(t)] for t in tickers},
        index=DAYS,
    ).astype("float64")


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    """The retry pause is real in production and pointless in a test."""
    monkeypatch.setattr(prices.time, "sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def no_stooq(monkeypatch):
    """Default Stooq to "no data" so existing tests stay network-free.

    Tests about the Stooq fallback itself override this explicitly.
    """
    monkeypatch.setattr(prices, "_download_stooq_symbol", lambda *_a, **_k: pd.DataFrame())


def test_missing_last_row_is_refetched_individually(monkeypatch):
    calls: list[list[str]] = []

    def fake_download(tickers, start, end):
        calls.append(list(tickers))
        if len(tickers) > 1:
            # The batch: SAP.DE came back without a close for the 4th.
            return frame_for(tickers, {"MSFT": 101.0, "SAP.DE": None})
        return frame_for(tickers, {tickers[0]: 202.0})

    monkeypatch.setattr(prices, "_download", fake_download)
    out = prices.closing_prices(["MSFT", "SAP.DE"], date(2026, 8, 1), date(2026, 8, 4))

    assert out.at[DAYS[-1], "SAP.DE"] == 202.0
    # Microsoft was fine, so it must not have been fetched a second time.
    assert calls == [["MSFT", "SAP.DE"], ["SAP.DE"]]


def test_good_batch_values_are_not_overwritten(monkeypatch):
    def fake_download(tickers, start, end):
        if len(tickers) > 1:
            return frame_for(tickers, {"MSFT": 101.0, "SAP.DE": None})
        # A retry that disagrees about an earlier day must not win.
        return pd.DataFrame({"SAP.DE": [999.0, 202.0]}, index=DAYS).astype("float64")

    monkeypatch.setattr(prices, "_download", fake_download)
    out = prices.closing_prices(["MSFT", "SAP.DE"], date(2026, 8, 1), date(2026, 8, 4))

    assert out.at[DAYS[0], "SAP.DE"] == 100.0
    assert out.at[DAYS[-1], "SAP.DE"] == 202.0


def test_whole_chunk_missing_is_recovered(monkeypatch):
    """The 4 August failure: every European name dropped at once."""
    european = ["ADYEN.AS", "ASML.AS", "SAP.DE", "STMPA.PA", "TELIA.ST"]
    wanted = ["MSFT", *european]

    def fake_download(tickers, start, end):
        if len(tickers) > 1:
            return frame_for(tickers, {"MSFT": 101.0})
        return frame_for(tickers, {tickers[0]: 202.0})

    monkeypatch.setattr(prices, "_download", fake_download)
    out = prices.closing_prices(wanted, date(2026, 8, 1), date(2026, 8, 4))

    assert out.loc[DAYS[-1], european].tolist() == [202.0] * len(european)


def test_a_genuinely_dead_ticker_still_reads_as_missing(monkeypatch):
    """Recovery must not invent data — Asmodee really does return nothing."""

    def fake_download(tickers, start, end):
        if len(tickers) > 1:
            return frame_for(tickers, {"MSFT": 101.0})
        return pd.DataFrame()

    monkeypatch.setattr(prices, "_download", fake_download)
    out = prices.closing_prices(
        ["MSFT", "ASMODEE-B.ST"], date(2026, 8, 1), date(2026, 8, 4)
    )

    assert pd.isna(out.at[DAYS[-1], "ASMODEE-B.ST"])
    assert out.at[DAYS[-1], "MSFT"] == 101.0


def test_empty_batch_is_passed_through(monkeypatch):
    monkeypatch.setattr(prices, "_download", lambda *_a, **_k: pd.DataFrame())
    assert prices.closing_prices(["MSFT"], date(2026, 8, 1), date(2026, 8, 4)).empty


def test_stooq_rescues_what_yahoo_could_not(monkeypatch):
    """The whole point of the fallback: SAP.DE dead on Yahoo, alive on Stooq."""

    def fake_download(tickers, start, end):
        return frame_for(tickers, {"MSFT": 101.0})  # SAP.DE stays null everywhere

    def fake_stooq(symbol, start, end):
        if symbol == "sap.de":
            return pd.DataFrame({"Close": [202.0]}, index=[DAYS[-1]])
        return pd.DataFrame()

    monkeypatch.setattr(prices, "_download", fake_download)
    monkeypatch.setattr(prices, "_download_stooq_symbol", fake_stooq)

    out = prices.closing_prices(["MSFT", "SAP.DE"], date(2026, 8, 1), date(2026, 8, 4))

    assert out.at[DAYS[-1], "SAP.DE"] == 202.0


def test_stooq_wrong_date_does_not_count_as_a_rescue(monkeypatch):
    """A Stooq symbol that resolves but has no row for our exact date must not
    be treated as found - that would carry forward a stale, mismatched price
    silently."""

    def fake_download(tickers, start, end):
        return frame_for(tickers, {"MSFT": 101.0})

    def fake_stooq(symbol, start, end):
        # Data exists, but only for an earlier day than the one we need.
        return pd.DataFrame({"Close": [150.0]}, index=[DAYS[0]])

    monkeypatch.setattr(prices, "_download", fake_download)
    monkeypatch.setattr(prices, "_download_stooq_symbol", fake_stooq)

    out = prices.closing_prices(["MSFT", "SAP.DE"], date(2026, 8, 1), date(2026, 8, 4))

    assert pd.isna(out.at[DAYS[-1], "SAP.DE"])


def test_unmapped_exchange_never_calls_stooq(monkeypatch):
    """No candidate suffix for this exchange -> no HTTP attempt at all."""
    calls = []

    def fake_download(tickers, start, end):
        return frame_for(tickers, {"MSFT": 101.0})

    def fake_stooq(symbol, start, end):
        calls.append(symbol)
        return pd.DataFrame()

    monkeypatch.setattr(prices, "_download", fake_download)
    monkeypatch.setattr(prices, "_download_stooq_symbol", fake_stooq)

    prices.closing_prices(["MSFT", "WEIRD.XX"], date(2026, 8, 1), date(2026, 8, 4))

    assert calls == []


def test_stooq_symbol_candidates():
    assert prices._stooq_candidates("AAPL") == ["aapl.us"]
    assert prices._stooq_candidates("SAP.DE") == ["sap.de"]
    assert prices._stooq_candidates("ERIC-B.ST") == ["eric-b.st"]
    assert prices._stooq_candidates("SMSN.IL") == ["smsn.uk"]
    assert prices._stooq_candidates("WEIRD.XX") == []
