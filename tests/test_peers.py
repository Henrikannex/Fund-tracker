"""The comparison block, and the promise that a peer can never break the mail."""

from __future__ import annotations

from datetime import date

import pytest

from fundtracker.models import Contribution, Estimate
from fundtracker.report import to_html, to_text


def make(fund_id: str, name: str, currency: str, return_pct: float) -> Estimate:
    return Estimate(
        fund_id=fund_id,
        fund_name=name,
        date=date(2026, 8, 10),
        currency=currency,
        return_pct=return_pct,
        equity_return_pct=return_pct,
        fx_contribution_pct=0.0,
        fee_drag_pct=0.0,
        coverage_pct=99.0,
        stale_weight_pct=0.0,
        cash_pct=1.0,
        snapshot_age_days=1,
        contributions=[
            Contribution("Amazon.com Inc", "AMZN", "USD", 5.0, 0.01, 0.0, 0.01, 0.05)
        ],
    )


PRIMARY = make("dnb-teknologi-a", "DNB Teknologi A", "NOK", -0.1728)
PEERS = [
    make("dnb-disruptive-opportunities", "DNB Disruptive Opportunities", "EUR", 0.84),
    make("seb-teknologifond-a", "SEB Teknologifond A", "SEK", 1.21),
]


@pytest.mark.parametrize("render", [to_text, to_html])
def test_every_fund_appears_with_its_own_return(render):
    out = render(PRIMARY, PEERS)
    # The fund the mail is about belongs in the comparison too, otherwise there
    # is nothing to compare the peers against.
    assert "-0,17" in out
    assert "+0,84" in out
    assert "+1,21" in out
    assert "Disruptive" in out
    assert "SEB Teknologifond" in out


@pytest.mark.parametrize("render", [to_text, to_html])
def test_currency_is_named_when_funds_disagree(render):
    out = render(PRIMARY, PEERS)
    for currency in ("NOK", "EUR", "SEK"):
        assert currency in out
    assert "ikke direkte sammenlignbare" in out


@pytest.mark.parametrize("render", [to_text, to_html])
def test_no_currency_caveat_when_every_fund_shares_one(render):
    same = [make("b", "Fond B", "NOK", 0.5)]
    assert "ikke direkte sammenlignbare" not in render(PRIMARY, same)


@pytest.mark.parametrize("render", [to_text, to_html])
def test_mail_is_unchanged_when_there_are_no_peers(render):
    out = render(PRIMARY)
    assert "Til sammenligning" not in out
    # The report still has to stand on its own, peers or not.
    assert "-0,17" in out
    assert "Amazon" in out


def test_peers_do_not_bring_their_own_contributor_tables():
    """Only the fund the mail is about gets the winners/losers breakdown.

    Three sets of contributor tables is the thing this feature was explicitly
    asked not to be.
    """
    out = to_html(PRIMARY, PEERS)
    assert out.count("Sterkeste bidrag") == 1
    assert out.count("Svakeste bidrag") == 1


def test_a_broken_peer_is_skipped_rather_than_raised(monkeypatch, caplog):
    """A peer that cannot be priced costs one row, not the whole mail."""
    from fundtracker import cli

    monkeypatch.setattr(
        cli, "load_fund", lambda fund_id: (_ for _ in ()).throw(RuntimeError("nope"))
    )

    class Fund:
        peers = ["does-not-exist"]

    assert cli._peer_estimates(Fund(), date(2026, 8, 10)) == []


def test_peer_currency_reaches_the_estimate():
    """The base currency has to survive the trip from config to report.

    It is the one field the comparison cannot fake: get it wrong and the mail
    labels a SEK return as NOK, which reads as a real number and is not one.
    """
    import pandas as pd

    from fundtracker.config import FundConfig, TickerMapping
    from fundtracker.estimate import estimate_return
    from fundtracker.models import Holding, HoldingsSnapshot

    fund = FundConfig(
        id="x", name="X", isin="", currency="SEK", total_cost_pct=0.0,
        trading_days_per_year=252, holdings_source={}, nav_source={},
        tickers={"A": TickerMapping("A", "SEK")},
    )
    snapshot = HoldingsSnapshot(
        fund_id="x", retrieved=date(2026, 8, 10), source="test",
        holdings=[Holding("A", 100.0)],
    )
    index = pd.to_datetime(["2026-08-07", "2026-08-10"])
    prices = pd.DataFrame({"A": [100.0, 101.0]}, index=index)
    fx = pd.DataFrame({"SEK": [1.0, 1.0]}, index=index)

    est = estimate_return(fund, snapshot, date(2026, 8, 10), prices, fx)
    assert est.currency == "SEK"
