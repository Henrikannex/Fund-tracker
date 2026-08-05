"""Run the model backwards over published NAV history and measure the damage.

Two questions this answers:

1. *How wrong is the estimate on a typical day?* Mean absolute error, bias, and
   how often the sign is right. Direction accuracy is the number that matters
   when the estimate is used to satisfy curiosity rather than to trade.
2. *How fast does a holdings snapshot go stale?* We hold one composition, dated
   by the fund company, and score it against every day in the window. Grouping
   the error by the gap between that date and the day being scored says how
   quickly the portfolio drifts away from us — a flat curve means refreshing
   the holdings rarely is fine, a rising one says how often to go get them.

   Note the direction: the *oldest* days in a backtest window sit closest to a
   past snapshot date and therefore use the *freshest* holdings. Grouping by
   position in the window instead of by distance from the snapshot answers the
   question backwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from .config import FundConfig
from .estimate import estimate_return, priced_tickers, resolve_holding
from .models import HoldingsSnapshot
from .sources import nav as nav_source
from .sources import prices as price_source

# A day where this much of the fund had no quote is not a day we can score.
MAX_STALE_WEIGHT_PCT = 5.0


@dataclass
class BacktestResult:
    fund_id: str
    frame: pd.DataFrame  # date-indexed: estimated_pct, actual_pct, error_pct
    lag: int = 0
    # The date the holdings are from, so staleness can be measured against it.
    snapshot_as_of: Optional[date] = None

    @property
    def days(self) -> int:
        return len(self.frame)

    @property
    def mean_abs_error(self) -> float:
        return float(self.frame["error_pct"].abs().mean())

    @property
    def bias(self) -> float:
        """Positive means the model reads high on average."""
        return float(self.frame["error_pct"].mean())

    @property
    def rmse(self) -> float:
        return float((self.frame["error_pct"] ** 2).mean() ** 0.5)

    @property
    def direction_hit_rate(self) -> float:
        """Share of days where estimate and actual moved the same way."""
        both = self.frame[(self.frame["actual_pct"] != 0)]
        if both.empty:
            return float("nan")
        same = (both["estimated_pct"] > 0) == (both["actual_pct"] > 0)
        return float(same.mean() * 100.0)

    @property
    def correlation(self) -> float:
        return float(self.frame["estimated_pct"].corr(self.frame["actual_pct"]))

    def error_by_age(self, buckets: int = 4) -> pd.DataFrame:
        """Mean absolute error grouped by how stale the holdings were that day.

        Measured from the date the fund company says the composition is from —
        not from the newest day in the window. Those run in opposite directions:
        the oldest backtest days sit closest to the snapshot date and therefore
        use the *freshest* holdings, so grouping by position in the window
        answers the question backwards.
        """
        if self.frame.empty or self.snapshot_as_of is None:
            return pd.DataFrame()
        frame = self.frame.copy()
        frame["age_days"] = (frame.index - pd.Timestamp(self.snapshot_as_of)).days
        edges = pd.qcut(frame["age_days"], q=min(buckets, frame["age_days"].nunique()),
                        duplicates="drop")
        return (
            frame.groupby(edges, observed=True)
            .agg(days=("error_pct", "size"),
                 mean_abs_error=("error_pct", lambda s: s.abs().mean()),
                 bias=("error_pct", "mean"))
            .reset_index(names="snapshot_age_bucket")
        )


def run_backtest(
    fund: FundConfig, snapshot: HoldingsSnapshot, days: int = 250, lag: int = 0
) -> BacktestResult:
    """Score the model against published NAV.

    ``lag`` says how many trading days the fund's published NAV trails the
    closes we price against. A fund whose valuation point falls before the US
    close cannot reflect that day's American prices, so its NAV for day T lines
    up with our estimate for T-1. Which is true here is not something to guess
    at — :func:`compare_lags` measures it.
    """
    estimated, actual = gather_series(fund, snapshot, days)
    frame = align_lag(estimated, actual, lag)
    if frame.empty:
        raise RuntimeError("Ingen overlappende dager mellom estimat og faktisk NAV.")
    return BacktestResult(fund.id, frame.tail(days), lag, snapshot.as_of)


def estimate_series(
    fund: FundConfig,
    snapshot: HoldingsSnapshot,
    start: date,
    end: date,
    hedged: Optional[bool] = None,
) -> pd.Series:
    """Daily estimated returns in percent, for every day we can price properly.

    Days where a chunk of the fund had no quote are dropped rather than counted
    as flat, so a caller compounding this series is not silently accumulating
    zeros that never happened.
    """
    tickers, currencies = priced_tickers(fund, snapshot)
    raw_prices = price_source.closing_prices(tickers, start, end)
    raw_fx = price_source.fx_to_base(sorted(currencies), fund.currency, start, end)
    prices, staleness, observed = price_source.align(raw_prices)
    fx, fx_staleness, fx_observed = price_source.align(raw_fx)

    estimates = {}
    for ts in prices.index:
        if ts < pd.Timestamp(start) or ts > pd.Timestamp(end):
            continue
        try:
            est = estimate_return(
                fund, snapshot, ts.date(), prices, fx, staleness, observed,
                fx_staleness, fx_observed, hedged,
            )
        except ValueError:
            continue
        if any("Ingen kursdata" in w for w in est.warnings):
            continue
        if est.stale_weight_pct > MAX_STALE_WEIGHT_PCT:
            continue
        estimates[ts] = est.return_pct

    return pd.Series(estimates, dtype="float64").sort_index()


def gather_series(
    fund: FundConfig, snapshot: HoldingsSnapshot, days: int,
    hedged: Optional[bool] = None,
) -> tuple[pd.Series, pd.Series]:
    """Estimated and actual daily returns, both date-indexed.

    Separated from scoring so that comparing several lags costs one round of
    downloads rather than one per lag.
    """
    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 15)  # calendar days for ~`days` sessions

    actual = nav_source.nav_returns(nav_source.load_nav(fund, start, end))
    if actual.empty:
        raise RuntimeError(
            "Ingen NAV-historikk tilgjengelig. Sett nav_source.ticker til noe Yahoo "
            "kjenner, eller legg inn en CSV med kolonnene date,nav i nav_source.manual_file."
        )

    estimated = estimate_series(fund, snapshot, start, end, hedged)
    if estimated.empty:
        raise RuntimeError("Backtesten produserte ingen sammenlignbare dager.")
    return estimated, actual


def align_lag(estimated: pd.Series, actual: pd.Series, lag: int) -> pd.DataFrame:
    """Pair each estimate with the NAV move published ``lag`` trading days later.

    Shifting by position rather than by calendar days matters: weekends and
    holidays are not trading days, so "the next published NAV" is the next row,
    not tomorrow's date.
    """
    aligned = actual.sort_index()
    shifted = aligned.shift(-lag) if lag else aligned
    frame = pd.DataFrame(
        {"estimated_pct": estimated.sort_index(), "actual_pct": shifted}
    ).dropna()
    frame["error_pct"] = frame["estimated_pct"] - frame["actual_pct"]
    return frame


def compare_lags(
    fund: FundConfig, snapshot: HoldingsSnapshot, days: int = 250,
    lags: tuple[int, ...] = (0, 1, 2),
) -> list[BacktestResult]:
    """Score the model at several lags so the data can settle the timing.

    Nordnet lists a 09:00 order cut-off for this fund, which hints the valuation
    point may sit before the US close — but a hint is not a fact. Whichever lag
    fits best is the answer, and if none fits well the problem is the model, not
    the alignment.
    """
    estimated, actual = gather_series(fund, snapshot, days)
    results = []
    for lag in lags:
        frame = align_lag(estimated, actual, lag)
        if not frame.empty:
            results.append(BacktestResult(fund.id, frame.tail(days), lag, snapshot.as_of))
    return results


def holding_return_series(
    fund: FundConfig, snapshot: HoldingsSnapshot, start: date, end: date
) -> pd.DataFrame:
    """Daily return in the fund's currency for every priced holding."""
    tickers, currencies = priced_tickers(fund, snapshot)
    prices, _, _ = price_source.align(price_source.closing_prices(tickers, start, end))
    fx, _, _ = price_source.align(
        price_source.fx_to_base(sorted(currencies), fund.currency, start, end)
    )

    out = {}
    for holding in snapshot.holdings:
        mapping = resolve_holding(fund, holding)
        if mapping is None or mapping.ticker not in prices.columns:
            continue
        local = prices[mapping.ticker].pct_change()
        rate = (
            fx[mapping.currency].pct_change() if mapping.currency in fx.columns else 0.0
        )
        out[holding.name] = ((1 + local) * (1 + rate) - 1) * 100.0
    return pd.DataFrame(out)


def test_weights(
    fund: FundConfig, snapshot: HoldingsSnapshot, days: int = 250, top: int = 10
) -> pd.DataFrame:
    """Test one holding's weight at a time, using our own estimate as a control.

    For each candidate we fit ``actual = a + b*estimate + c*return_of_holding``.
    Our estimate already contains the holding at the weight we believe; if that
    weight is right, the holding's return carries no information beyond the
    estimate and ``c`` lands on zero. Carry too much and ``c`` goes negative by
    the size of the excess.

    Three parameters against fifty-odd observations is a well-posed fit. The
    earlier attempt regressed the error on all sixty-three holdings separately,
    which is neither — tech stocks move together, so each slope absorbed the
    market instead of the position.
    """
    import numpy as np

    estimated, actual = gather_series(fund, snapshot, days)
    frame = align_lag(estimated, actual, 0)
    if len(frame) < 30:
        raise RuntimeError("For få dager til å teste vekter.")

    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 15)
    returns = holding_return_series(fund, snapshot, start, end)

    weights = {h.name: h.weight_pct for h in snapshot.holdings}
    candidates = sorted(
        (n for n in returns.columns if n in weights), key=lambda n: -weights[n]
    )[:top]

    rows = []
    for name in candidates:
        joined = pd.concat(
            [frame[["actual_pct", "estimated_pct"]], returns[name].rename("r")], axis=1
        ).dropna()
        if len(joined) < 30:
            continue
        X = np.column_stack(
            [np.ones(len(joined)), joined["estimated_pct"].to_numpy(), joined["r"].to_numpy()]
        )
        y = joined["actual_pct"].to_numpy()
        beta, *_ = np.linalg.lstsq(X, y, rcond=None)
        resid = y - X @ beta
        dof = len(joined) - X.shape[1]
        cov = (resid @ resid / dof) * np.linalg.pinv(X.T @ X)
        se = float(np.sqrt(cov[2, 2]))
        rows.append({
            "name": name,
            "weight_pct": weights[name],
            # c is a weight fraction; express it in the same unit as the weights.
            "weight_error_pct": float(beta[2]) * 100.0,
            "t": float(beta[2]) / se if se > 0 else float("nan"),
            "implied_weight_pct": weights[name] + float(beta[2]) * 100.0,
        })
    return pd.DataFrame(rows)


def format_weight_test(frame: pd.DataFrame) -> str:
    """Report only what the data can carry: a verdict per holding, with the t."""
    if frame.empty:
        return "For lite data til å teste vektene."
    lines = [
        "Test av enkeltvekter (actual = a + b*estimat + c*postens avkastning)",
        "",
        "  selskap                       vekt   avvik      t   antydet   konklusjon",
    ]
    for _, r in frame.iterrows():
        # |t| under 2 is noise. Saying so beats printing a number that invites
        # a story, which is how the previous version of this went wrong.
        verdict = (
            "for mye" if r["t"] <= -2 else "for lite" if r["t"] >= 2 else "ingen utslag"
        )
        lines.append(
            f"  {r['name'][:27]:<27} {r['weight_pct']:>5.2f} %"
            f" {r['weight_error_pct']:>+7.2f} {r['t']:>+6.1f}"
            f" {r['implied_weight_pct']:>8.2f} %   {verdict}"
        )
    lines += [
        "",
        "  Bare utslag med |t| over 2 betyr noe. Resten er støy, uansett hvor",
        "  interessant tallet ser ut.",
    ]
    return "\n".join(lines)


def compare_currency(
    fund: FundConfig, snapshot: HoldingsSnapshot, days: int = 250
) -> tuple[BacktestResult, BacktestResult]:
    """Score the model with and without the currency leg, against real NAV.

    A NOK fund holding dollar assets sees its NAV move when the krone moves —
    unless it hedges. Which of the two this fund does is settled by measuring
    both against its published NAV, not by reasoning about it.
    """
    out = []
    for hedged in (False, True):
        estimated, actual = gather_series(fund, snapshot, days, hedged=hedged)
        frame = align_lag(estimated, actual, 0)
        if frame.empty:
            raise RuntimeError("Ingen overlappende dager mellom estimat og faktisk NAV.")
        out.append(BacktestResult(fund.id, frame.tail(days), 0, snapshot.as_of))
    return out[0], out[1]


def format_currency_comparison(unhedged: BacktestResult, hedged: BacktestResult) -> str:
    lines = [
        f"Med og uten valuta, mot faktisk NAV ({unhedged.days} dager)",
        "",
        "                     snittfeil    skjevhet    treffer retning   korrelasjon",
    ]
    for label, r in (("med valuta", unhedged), ("uten valuta", hedged)):
        lines.append(
            f"  {label:<16} {r.mean_abs_error:>9.3f}   {r.bias:>+9.3f}   "
            f"{r.direction_hit_rate:>13.1f} %   {r.correlation:>11.3f}"
        )

    # A winner is only a winner by a margin worth acting on. A couple of percent
    # difference in MAE across a few dozen days is noise, and calling it a
    # finding sends the reader off to change config for no reason.
    low, high = sorted((unhedged.mean_abs_error, hedged.mean_abs_error))
    decisive = (high - low) > 0.1 and low < high * 0.85

    lines.append("")
    if not decisive:
        lines += [
            f"  Forskjellen er {high - low:.3f} %-poeng - for liten til å avgjøre noe.",
            "  Dataene skiller ikke sikret fra usikret. Valutaleddet beholdes, som er",
            "  det teoretisk riktige for et usikret fond i norske kroner.",
        ]
        return "\n".join(lines)

    better = unhedged if unhedged.mean_abs_error < hedged.mean_abs_error else hedged
    name = "Med valuta" if better is unhedged else "Uten valuta"
    lines.append(f"  {name} treffer klart best: {low:.3f} mot {high:.3f} %-poeng.")
    lines.append(
        "  Fondet er altså ikke valutasikret, og valutaleddet skal være med."
        if better is unhedged
        else "  Fondet oppfører seg valutasikret; sett currency_hedged: true i konfigen."
    )
    return "\n".join(lines)


def format_lag_comparison(results: list[BacktestResult]) -> str:
    """Which alignment between our estimate and the published NAV actually fits."""
    if not results:
        return "Ingen backtest kunne kjøres."

    lines = [
        "Hvilken dag treffer estimatet?",
        "",
        "  forsinkelse   dager    snittfeil   treffer retning   korrelasjon",
    ]
    for r in results:
        label = {0: "samme dag", 1: "+1 dag", 2: "+2 dager"}.get(r.lag, f"+{r.lag} dager")
        lines.append(
            f"  {label:<12} {r.days:>6}   {r.mean_abs_error:>8.3f}   "
            f"{r.direction_hit_rate:>13.1f} %   {r.correlation:>11.3f}"
        )

    best = max(results, key=lambda r: (r.correlation if pd.notna(r.correlation) else -9))
    label = {0: "samme dag", 1: "én dag etter", 2: "to dager etter"}.get(
        best.lag, f"{best.lag} dager etter"
    )
    lines += [
        "",
        f"  Best treff: NAV-en publiseres {label} kursene vi regner på.",
    ]
    if best.lag > 0:
        lines.append(
            "  Kveldsestimatet forutsier altså NAV-en som merkes neste handelsdag, "
            "ikke dagens."
        )
    return "\n".join(lines)


def format_report(result: BacktestResult) -> str:
    lines = [
        f"Backtest for {result.fund_id} - {result.days} handelsdager, "
        f"forsinkelse {result.lag}",
        "",
        f"  Gjennomsnittlig absolutt feil : {result.mean_abs_error:6.3f} %-poeng",
        f"  Systematisk skjevhet          : {result.bias:+6.3f} %-poeng"
        f"  {'(modellen leser for hoyt)' if result.bias > 0 else '(modellen leser for lavt)'}",
        f"  RMSE                          : {result.rmse:6.3f} %-poeng",
        f"  Traff riktig retning          : {result.direction_hit_rate:6.1f} %",
        f"  Korrelasjon med faktisk NAV   : {result.correlation:6.3f}",
        "",
        "Feil gruppert etter hvor gammelt beholdnings-snapshotet er:",
    ]
    by_age = result.error_by_age()
    if by_age.empty:
        lines.append("  (ikke nok data)")
    else:
        for _, row in by_age.iterrows():
            lines.append(
                f"  {str(row['snapshot_age_bucket']):>18}  "
                f"n={int(row['days']):>4}  "
                f"MAE={row['mean_abs_error']:.3f}  "
                f"bias={row['bias']:+.3f}"
            )
    return "\n".join(lines)
