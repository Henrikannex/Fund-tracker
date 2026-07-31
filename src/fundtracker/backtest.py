"""Run the model backwards over published NAV history and measure the damage.

Two questions this answers:

1. *How wrong is the estimate on a typical day?* Mean absolute error, bias, and
   how often the sign is right. Direction accuracy is the number that matters
   when the estimate is used to satisfy curiosity rather than to trade.
2. *How fast does a holdings snapshot go stale?* We only have today's
   composition, so running it against older NAVs deliberately conflates model
   error with drift in the portfolio. Bucketing the error by how far back we
   went separates the two: a flat curve means freshness barely matters and
   scraping monthly is plenty; a rising curve tells us how often to re-scrape.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd

from .config import FundConfig
from .estimate import estimate_return, priced_tickers
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
        """Mean absolute error grouped by how far back in time the day was."""
        if self.frame.empty:
            return pd.DataFrame()
        frame = self.frame.copy()
        newest = frame.index.max()
        frame["age_days"] = (newest - frame.index).days
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
    end = date.today()
    start = end - timedelta(days=int(days * 1.6) + 15)  # calendar days for ~`days` sessions

    actual = nav_source.nav_returns(nav_source.load_nav(fund, start, end))
    if actual.empty:
        raise RuntimeError(
            "Ingen NAV-historikk tilgjengelig. Sett nav_source.ticker til noe Yahoo "
            "kjenner, eller legg inn en CSV med kolonnene date,nav i nav_source.manual_file."
        )

    tickers, currencies = priced_tickers(fund, snapshot)

    raw_prices = price_source.closing_prices(tickers, start, end)
    raw_fx = price_source.fx_to_base(sorted(currencies), fund.currency, start, end)
    prices, staleness, observed = price_source.align(raw_prices)
    fx, _, _ = price_source.align(raw_fx)

    estimates = {}
    for ts in actual.index:
        day = ts.date()
        try:
            est = estimate_return(fund, snapshot, day, prices, fx, staleness, observed)
        except ValueError:
            continue
        # Skip days our data cannot actually speak to: no bar for the date, or a
        # big chunk of the fund carried forward. Those measure the plumbing, not
        # the model, and averaging them in would flatter the error statistics.
        if any("Ingen kursdata" in w for w in est.warnings):
            continue
        if est.stale_weight_pct > MAX_STALE_WEIGHT_PCT:
            continue
        estimates[ts] = est.return_pct

    if not estimates:
        raise RuntimeError("Backtesten produserte ingen sammenlignbare dager.")

    estimated = pd.Series(estimates, dtype="float64").sort_index()
    frame = align_lag(estimated, actual, lag)
    if frame.empty:
        raise RuntimeError("Ingen overlappende dager mellom estimat og faktisk NAV.")
    return BacktestResult(fund_id=fund.id, frame=frame.tail(days), lag=lag)


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
    results = []
    for lag in lags:
        try:
            results.append(run_backtest(fund, snapshot, days=days, lag=lag))
        except RuntimeError:
            continue
    return results


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
