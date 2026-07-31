"""Turn a holdings snapshot plus price data into an estimated daily return.

The model in one line:

    fund_return = mean_return_of_priced_holdings * equity_share - daily_fee

Three assumptions are worth stating out loud, because they are where the error
comes from and the backtest exists to size them:

1. *The unpriced tail behaves like the priced part.* We normalise weights over
   the holdings we could price. For a concentrated sector fund this is mild;
   for a broad fund where we only see the top 25 it is not.
2. *Cash earns nothing today.* True enough over one day.
3. *The manager has not traded since the snapshot.* Unknowable from outside,
   and the reason estimates decay as the snapshot ages.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from .config import FundConfig, TickerMapping
from .models import Contribution, Estimate, Holding, HoldingsSnapshot

COVERAGE_WARN_PCT = 90.0
SNAPSHOT_AGE_WARN_DAYS = 45
STALE_PRICE_WARN_DAYS = 4


def estimate_return(
    fund: FundConfig,
    snapshot: HoldingsSnapshot,
    target: date,
    prices: pd.DataFrame,
    fx: pd.DataFrame,
    staleness: Optional[pd.Series] = None,
    observed: Optional[pd.DataFrame] = None,
    hedged: Optional[bool] = None,
) -> Estimate:
    """Estimate ``fund``'s return for ``target``.

    ``prices`` and ``fx`` are date-indexed frames already forward-filled by
    :func:`fundtracker.sources.prices.align`. ``observed`` is that function's
    mask of which cells were real observations rather than carried forward; the
    weight sitting behind carried-forward prices is reported on the result so
    the caller can refuse to publish a half-stale number.
    """
    warnings: list[str] = []
    staleness = staleness if staleness is not None else pd.Series(dtype="int64")
    # A hedged fund neutralises currency moves, so its NAV tracks the holdings'
    # local-currency returns. Getting this wrong biases every single day in the
    # same direction, which is exactly what it looks like.
    hedged = fund.currency_hedged if hedged is None else hedged

    ts = pd.Timestamp(target).normalize()
    curr_idx, prev_idx = _pick_rows(prices, ts, warnings)

    priced: list[Contribution] = []
    unpriced: list[str] = []

    for holding in snapshot.holdings:
        if fund.is_ignored(holding.name):
            continue
        mapping = resolve_holding(fund, holding)
        if mapping is None:
            unpriced.append(f"{holding.name} (ingen ticker konfigurert)")
            continue

        p_now = _cell(prices, curr_idx, mapping.ticker)
        p_prev = _cell(prices, prev_idx, mapping.ticker)
        fx_now = _cell(fx, curr_idx, mapping.currency)
        fx_prev = _cell(fx, prev_idx, mapping.currency)

        if None in (p_now, p_prev, fx_now, fx_prev) or p_prev == 0 or fx_prev == 0:
            unpriced.append(f"{holding.name} ({mapping.ticker}: mangler kurs)")
            continue

        local = p_now / p_prev - 1.0
        fx_ret = 0.0 if hedged else fx_now / fx_prev - 1.0
        priced.append(
            Contribution(
                name=holding.name,
                ticker=mapping.ticker,
                currency=mapping.currency,
                weight_pct=holding.weight_pct,
                local_return=local,
                fx_return=fx_ret,
                nok_return=(1.0 + local) * (1.0 + fx_ret) - 1.0,
                contribution_pct=0.0,  # filled in below, once weights are normalised
                stale_price=int(staleness.get(mapping.ticker, 0)) > STALE_PRICE_WARN_DAYS,
                carried_forward=not _was_observed(observed, curr_idx, mapping.ticker),
            )
        )

    covered = sum(c.weight_pct for c in priced)
    if covered <= 0:
        raise ValueError(
            f"Ingen av beholdningene i {fund.id} kunne prises for {target}. "
            "Sjekk ticker-oppsettet og at kursdata faktisk ble hentet."
        )

    cash_pct = snapshot.cash_pct or 0.0
    equity_share = (100.0 - cash_pct) / 100.0

    equity_return = sum(c.weight_pct * c.nok_return for c in priced) / covered
    fx_component = sum(c.weight_pct * c.fx_return for c in priced) / covered

    for c in priced:
        c.contribution_pct = (c.weight_pct / covered) * equity_share * c.nok_return * 100.0

    fee = fund.daily_fee_drag
    return_pct = equity_return * equity_share * 100.0 - fee
    stale_weight = sum(c.weight_pct for c in priced if c.carried_forward)

    _collect_warnings(warnings, snapshot, covered, unpriced, priced, target, stale_weight)

    return Estimate(
        fund_id=fund.id,
        fund_name=fund.name,
        date=target,
        return_pct=return_pct,
        equity_return_pct=equity_return * 100.0,
        fx_contribution_pct=fx_component * equity_share * 100.0,
        fee_drag_pct=fee,
        coverage_pct=covered,
        stale_weight_pct=stale_weight,
        cash_pct=snapshot.cash_pct,
        snapshot_age_days=_snapshot_age(snapshot, target),
        contributions=priced,
        unpriced=unpriced,
        warnings=warnings,
    )


def resolve_holding(fund: FundConfig, holding: Holding) -> Optional[TickerMapping]:
    """Decide which price series to use for a holding.

    The fund config wins over whatever the source supplied. A holdings feed can
    hand us a ticker that is right for the company but wrong for the listing we
    need — "ERIC B" instead of ERIC-B.ST — so a hand-checked override must never
    be overruled by an automatic one.
    """
    override = fund.resolve(holding.name)
    if override is not None:
        return override
    if holding.ticker and holding.currency:
        return TickerMapping(holding.ticker, holding.currency.upper())
    return None


def priced_tickers(fund: FundConfig, snapshot: HoldingsSnapshot) -> tuple[list[str], set[str]]:
    """Every ticker and currency the snapshot needs prices for."""
    tickers: list[str] = []
    currencies: set[str] = {fund.currency}
    for holding in snapshot.holdings:
        if fund.is_ignored(holding.name):
            continue
        mapping = resolve_holding(fund, holding)
        if mapping:
            tickers.append(mapping.ticker)
            currencies.add(mapping.currency)
    return tickers, currencies


def _no(value: float, decimals: int = 1) -> str:
    """Format a number the Norwegian way, with a comma as decimal separator."""
    return f"{value:.{decimals}f}".replace(".", ",")


def _pick_rows(
    prices: pd.DataFrame, ts: pd.Timestamp, warnings: list[str]
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Find the target row and the trading day before it."""
    idx = prices.index
    usable = idx[idx <= ts]
    if len(usable) < 2:
        raise ValueError(
            f"For lite kurshistorikk til å regne avkastning for {ts.date()} "
            f"(fant {len(usable)} rad(er))."
        )
    curr, prev = usable[-1], usable[-2]
    if curr != ts:
        warnings.append(
            f"Ingen kursdata for {ts.date()}; bruker siste handelsdag {curr.date()}. "
            "Var markedet stengt, eller kjørte jobben for tidlig?"
        )
    return curr, prev


def _cell(frame: pd.DataFrame, row: pd.Timestamp, col: str) -> Optional[float]:
    if col not in frame.columns or row not in frame.index:
        return None
    value = frame.at[row, col]
    if pd.isna(value):
        return None
    return float(value)


def _was_observed(observed: Optional[pd.DataFrame], row: pd.Timestamp, col: str) -> bool:
    """Whether this price was a real quote on that date, not carried forward.

    Absent a mask we assume everything was observed, which keeps the pure
    function usable with hand-built frames in tests.
    """
    if observed is None or col not in observed.columns or row not in observed.index:
        return True
    return bool(observed.at[row, col])


def _snapshot_age(snapshot: HoldingsSnapshot, target: date) -> Optional[int]:
    """How stale the composition was on ``target``, in days.

    Clamped at zero: when we scraped today and are estimating today (or
    yesterday), the data is as fresh as it can be, and a negative age would
    just be noise.
    """
    reference = snapshot.as_of or snapshot.retrieved
    if reference is None:
        return None
    return max(0, (target - reference).days)


def _collect_warnings(
    warnings: list[str],
    snapshot: HoldingsSnapshot,
    covered: float,
    unpriced: list[str],
    priced: list[Contribution],
    target: date,
    stale_weight: float,
) -> None:
    # A portfolio cannot add up to more than itself. Under 100 % is normal —
    # a truncated holdings list — but over means a weight was mistyped or a
    # position counted twice, and that is worth catching at the source.
    total = snapshot.total_weight_pct + (snapshot.cash_pct or 0.0)
    if total > 100.5:
        warnings.append(
            f"Beholdninger og kontanter summerer til {_no(total, 2)} %, altså over "
            "100. En vekt er trolig feil eller telt to ganger."
        )

    if stale_weight > 0:
        names = [c.ticker for c in priced if c.carried_forward]
        warnings.append(
            f"{_no(stale_weight)} % av fondet har ingen kurs for {target} - "
            f"gårsdagens er videreført, så de bidrar 0,0 % uten at det er sant. "
            f"Kjørte jobben før børsen stengte? Gjelder: {', '.join(sorted(names))}"
        )
    if covered < COVERAGE_WARN_PCT:
        warnings.append(
            f"Estimatet dekker bare {_no(covered)} % av fondet. De resterende "
            f"{_no(100 - covered)} % antas å bevege seg som gjennomsnittet."
        )
    age = _snapshot_age(snapshot, target)
    if age is not None and age > SNAPSHOT_AGE_WARN_DAYS:
        warnings.append(
            f"Beholdningene er {age} dager gamle. Forvalter kan ha handlet siden da."
        )
    if unpriced:
        warnings.append(f"{len(unpriced)} beholdning(er) uten kurs: " + "; ".join(unpriced))
    stale = [c.ticker for c in priced if c.stale_price]
    if stale:
        warnings.append("Gammel kurs (mistenkelig ticker?): " + ", ".join(sorted(stale)))
