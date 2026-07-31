"""Unit tests for the return model, using synthetic prices so no network is needed."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from fundtracker.config import FundConfig, TickerMapping
from fundtracker.estimate import estimate_return, priced_tickers, resolve_holding
from fundtracker.models import Holding, HoldingsSnapshot

DAY0, DAY1 = pd.Timestamp("2026-07-29"), pd.Timestamp("2026-07-30")


def make_fund(**overrides) -> FundConfig:
    defaults = dict(
        id="test",
        name="Test Fund",
        isin="NO0000000000",
        currency="NOK",
        total_cost_pct=1.26,  # 1.26 / 252 = exactly 0.005 % a day
        trading_days_per_year=252,
        holdings_source={},
        nav_source={},
        tickers={
            "Alpha": TickerMapping("ALPHA", "USD"),
            "Beta": TickerMapping("BETA", "NOK"),
        },
        ignore=["Kontanter"],
    )
    defaults.update(overrides)
    return FundConfig(**defaults)


def make_snapshot(holdings, **kwargs) -> HoldingsSnapshot:
    return HoldingsSnapshot(
        fund_id="test",
        retrieved=date(2026, 7, 30),
        source="test",
        holdings=[Holding(name, weight) for name, weight in holdings],
        **kwargs,
    )


def frames(alpha=(100.0, 110.0), beta=(50.0, 50.0), usdnok=(10.0, 10.0)):
    prices = pd.DataFrame({"ALPHA": alpha, "BETA": beta}, index=[DAY0, DAY1])
    fx = pd.DataFrame({"USD": usdnok, "NOK": [1.0, 1.0]}, index=[DAY0, DAY1])
    return prices, fx


def test_single_holding_local_return_only():
    """A NOK holding at 100 % weight passes its own return straight through."""
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 100.0)])
    prices, fx = frames(beta=(50.0, 51.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.equity_return_pct == pytest.approx(2.0)
    assert est.return_pct == pytest.approx(2.0 - 0.005)
    assert est.fx_contribution_pct == pytest.approx(0.0)


def test_currency_move_compounds_with_price_move():
    """+10 % in USD with USDNOK +5 % is +15.5 % in NOK, not +15 %."""
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 100.0)])
    prices, fx = frames(alpha=(100.0, 110.0), usdnok=(10.0, 10.5))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.equity_return_pct == pytest.approx(15.5)
    assert est.fx_contribution_pct == pytest.approx(5.0)


def test_pure_currency_move_still_moves_the_fund():
    """The whole point of tracking FX: a flat stock day is not a flat NOK day."""
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 100.0)])
    prices, fx = frames(alpha=(100.0, 100.0), usdnok=(10.0, 10.07))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.equity_return_pct == pytest.approx(0.7)


def test_weights_are_normalised_over_priced_holdings():
    """Only half the fund is visible, so the visible half sets the average."""
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 50.0)])
    prices, fx = frames(beta=(50.0, 51.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.coverage_pct == pytest.approx(50.0)
    assert est.equity_return_pct == pytest.approx(2.0)
    assert any("dekker bare" in w for w in est.warnings)


def test_cash_dilutes_the_return():
    """10 % cash earns nothing, so a 2 % equity day is a 1.8 % fund day."""
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 90.0)], cash_pct=10.0)
    prices, fx = frames(beta=(50.0, 51.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.equity_return_pct == pytest.approx(2.0)
    assert est.return_pct == pytest.approx(2.0 * 0.9 - 0.005)


def test_contributions_sum_to_the_gross_return():
    """Per-holding contributions must reconcile with the headline number."""
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 60.0), ("Beta", 40.0)], cash_pct=5.0)
    prices, fx = frames(alpha=(100.0, 103.0), beta=(50.0, 49.0), usdnok=(10.0, 10.2))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    total = sum(c.contribution_pct for c in est.contributions)
    assert total == pytest.approx(est.return_pct + est.fee_drag_pct)


def test_ignored_names_are_excluded_not_priced():
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 95.0), ("Kontanter", 5.0)])
    prices, fx = frames(beta=(50.0, 51.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.coverage_pct == pytest.approx(95.0)
    assert est.unpriced == []


def test_unmapped_names_are_reported_never_silently_dropped():
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 80.0), ("Ukjent Selskap", 20.0)])
    prices, fx = frames(beta=(50.0, 51.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert len(est.unpriced) == 1
    assert "Ukjent Selskap" in est.unpriced[0]
    assert any("Ukjent Selskap" in w for w in est.warnings)


def test_fee_drag_is_subtracted_every_day():
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 100.0)])
    prices, fx = frames(beta=(50.0, 50.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.return_pct == pytest.approx(-0.005)


def test_missing_target_date_falls_back_and_warns():
    """Running before the close, or on a holiday, must be loudly visible."""
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 100.0)])
    prices, fx = frames(beta=(50.0, 51.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 31), prices, fx)

    assert any("Ingen kursdata" in w for w in est.warnings)


def test_no_priced_holdings_is_an_error_not_a_zero():
    """Silently reporting 0,00 % when nothing could be priced would be a lie."""
    fund = make_fund()
    snapshot = make_snapshot([("Ukjent", 100.0)])
    prices, fx = frames()

    with pytest.raises(ValueError, match="kunne prises"):
        estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)


def test_weights_summing_above_one_hundred_are_flagged():
    """A mistyped or double-counted weight is silent otherwise."""
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 60.0), ("Beta", 45.0)], cash_pct=2.0)
    prices, fx = frames()

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert any("over" in w and "100" in w for w in est.warnings)


def test_a_truncated_holdings_list_is_not_flagged_as_over_allocated():
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 60.0)], cash_pct=2.0)
    prices, fx = frames()

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert not any("over" in w and "100" in w for w in est.warnings)


def test_hedged_fund_ignores_currency_moves():
    """A hedged fund's NAV tracks local-currency returns, so the FX leg drops out."""
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 100.0)])
    prices, fx = frames(alpha=(100.0, 110.0), usdnok=(10.0, 10.5))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx, hedged=True)

    assert est.equity_return_pct == pytest.approx(10.0)  # not 15.5
    assert est.fx_contribution_pct == pytest.approx(0.0)


def test_hedging_defaults_to_the_fund_config():
    fund = make_fund(currency_hedged=True)
    snapshot = make_snapshot([("Alpha", 100.0)])
    prices, fx = frames(alpha=(100.0, 100.0), usdnok=(10.0, 10.5))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.equity_return_pct == pytest.approx(0.0)


def test_carried_forward_prices_are_reported_not_treated_as_a_flat_day():
    """The failure that shipped: run before the US close and half the fund is
    silently forward-filled to 0,00 %, while the headline number looks fine."""
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 60.0), ("Beta", 40.0)])
    prices, fx = frames(alpha=(100.0, 100.0), beta=(50.0, 51.0))
    # Alpha had no quote on day 1; its value was carried forward from day 0.
    observed = pd.DataFrame({"ALPHA": [True, False], "BETA": [True, True]},
                            index=[DAY0, DAY1])

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx, observed=observed)

    assert est.stale_weight_pct == pytest.approx(60.0)
    assert any("ingen kurs" in w for w in est.warnings)
    assert [c.name for c in est.contributions if c.carried_forward] == ["Alpha"]


def test_fully_observed_day_reports_no_stale_weight():
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 60.0), ("Beta", 40.0)])
    prices, fx = frames()
    observed = pd.DataFrame({"ALPHA": [True, True], "BETA": [True, True]},
                            index=[DAY0, DAY1])

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx, observed=observed)

    assert est.stale_weight_pct == 0.0
    assert not any("ingen kurs" in w for w in est.warnings)


def test_align_reports_which_cells_were_carried_forward():
    from fundtracker.sources.prices import align

    frame = pd.DataFrame({"A": [1.0, None], "B": [2.0, 3.0]}, index=[DAY0, DAY1])

    values, _, observed = align(frame)

    assert values.at[DAY1, "A"] == 1.0  # filled so the day still computes
    assert not observed.at[DAY1, "A"]   # but flagged as not real
    assert observed.at[DAY1, "B"]


def test_source_supplied_ticker_is_used_when_config_has_none():
    """A feed that reports its own tickers should not need a hand-written map."""
    fund = make_fund(tickers={})
    holding = Holding("Gamma", 100.0, ticker="GAMMA", currency="usd")

    mapping = resolve_holding(fund, holding)

    assert mapping == TickerMapping("GAMMA", "USD")


def test_config_ticker_overrides_the_one_from_the_source():
    """Hand-checked listings must win; a feed's ticker can be the wrong listing."""
    fund = make_fund(tickers={"Ericsson B": TickerMapping("ERIC-B.ST", "SEK")})
    holding = Holding("Ericsson B", 100.0, ticker="ERIC B", currency="USD")

    assert resolve_holding(fund, holding) == TickerMapping("ERIC-B.ST", "SEK")


def test_partial_source_metadata_is_not_trusted():
    """A ticker with no currency cannot be converted to NOK, so it is not usable."""
    fund = make_fund(tickers={})
    assert resolve_holding(fund, Holding("Gamma", 100.0, ticker="GAMMA")) is None


def test_priced_tickers_skips_ignored_names():
    fund = make_fund()
    snapshot = make_snapshot([("Alpha", 60.0), ("Beta", 35.0), ("Kontanter", 5.0)])

    tickers, currencies = priced_tickers(fund, snapshot)

    assert sorted(tickers) == ["ALPHA", "BETA"]
    assert currencies == {"USD", "NOK"}


def test_snapshot_age_is_measured_against_as_of_when_known():
    fund = make_fund()
    snapshot = make_snapshot([("Beta", 100.0)], as_of=date(2026, 6, 30))
    prices, fx = frames(beta=(50.0, 51.0))

    est = estimate_return(fund, snapshot, date(2026, 7, 30), prices, fx)

    assert est.snapshot_age_days == 30
