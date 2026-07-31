"""Check the model against the return figures the fund itself publishes.

We have no NAV time series, but Nordnet prints cumulative returns on the fund
page — one week, one month, one year, and so on, all as of the last NAV date.
Those are real, published, and enough to tell whether the daily model is
roughly right.

The test: compound our daily estimates over exactly the same window and compare.

Read the horizons differently, though. Over a week the composition barely
changes, so a mismatch is the model's fault. Over a year the portfolio has been
traded many times and we are still holding today's weights, so a mismatch is
expected — and the *shape* of how the error grows with horizon is itself the
answer to how often the holdings need refreshing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

import pandas as pd

from .backtest import estimate_series
from .config import FundConfig
from .models import HoldingsSnapshot


@dataclass
class PeriodCheck:
    label: str
    start: date
    end: date
    published_pct: float
    estimated_pct: Optional[float]
    trading_days: int

    @property
    def error_pct(self) -> Optional[float]:
        if self.estimated_pct is None:
            return None
        return self.estimated_pct - self.published_pct


# Nordnet's own period labels, and how far back each one reaches.
PERIOD_OFFSETS = {
    "1u": ("1 uke", {"days": 7}),
    "1m": ("1 måned", {"months": 1}),
    "3m": ("3 måneder", {"months": 3}),
    "6m": ("6 måneder", {"months": 6}),
    "ytd": ("i år", {"ytd": True}),
    "1y": ("1 år", {"months": 12}),
    "3y": ("3 år", {"months": 36}),
    "5y": ("5 år", {"months": 60}),
}


def _shift(anchor: date, spec: dict) -> date:
    if spec.get("ytd"):
        return date(anchor.year - 1, 12, 31)
    if "days" in spec:
        return anchor - timedelta(days=spec["days"])
    months = spec["months"]
    year = anchor.year - (months // 12)
    month = anchor.month - (months % 12)
    if month <= 0:
        month += 12
        year -= 1
    day = min(anchor.day, [31, 29 if year % 4 == 0 else 28, 31, 30, 31, 30,
                           31, 31, 30, 31, 30, 31][month - 1])
    return date(year, month, day)


def run_validation(
    fund: FundConfig, snapshot: HoldingsSnapshot, hedged: Optional[bool] = None
) -> list[PeriodCheck]:
    """Compare compounded estimates against the fund's published period returns."""
    spec = fund.benchmarks or {}
    published = spec.get("periods") or {}
    if not published:
        raise RuntimeError(
            f"Ingen fasittall å måle mot. Legg inn benchmarks.periods for {fund.id} "
            "med tallene Nordnet viser under kursgrafen."
        )

    anchor = spec.get("as_of")
    if not isinstance(anchor, date):
        raise RuntimeError("benchmarks.as_of må være en dato (YYYY-MM-DD).")

    wanted = {k: v for k, v in PERIOD_OFFSETS.items() if k in published}
    earliest = min(_shift(anchor, offset) for _, offset in wanted.values())

    # One download covering every window, rather than one per period.
    daily = estimate_series(fund, snapshot, earliest - timedelta(days=7), anchor, hedged)

    checks = []
    for key, (label, offset) in wanted.items():
        start = _shift(anchor, offset)
        window = daily[(daily.index > pd.Timestamp(start))
                       & (daily.index <= pd.Timestamp(anchor))]
        estimated = _compound(window) if not window.empty else None
        checks.append(
            PeriodCheck(
                label=label,
                start=start,
                end=anchor,
                published_pct=float(published[key]),
                estimated_pct=estimated,
                trading_days=len(window),
            )
        )
    return sorted(checks, key=lambda c: c.start, reverse=True)


def _compound(daily_pct: pd.Series) -> float:
    """Chain daily percentage returns into a cumulative percentage.

    Returns compound, they do not add: two +1 % days are +2.01 %, not +2 %.
    Over a year that difference is large enough to matter.
    """
    return float(((1.0 + daily_pct / 100.0).prod() - 1.0) * 100.0)


def format_comparison(
    unhedged: list[PeriodCheck], hedged: list[PeriodCheck], as_of: date
) -> str:
    """Show both currency assumptions side by side and let the errors decide.

    Whether a fund hedges is a fact about the fund, but reading it off a
    prospectus is slower and more error-prone than reading it off the numbers:
    the wrong assumption biases every window in the same direction, so the right
    one is unmistakable.
    """
    lines = [
        f"Modell mot Nordnets publiserte tall, per {as_of}",
        "",
        "                          |------- avvik i %-poeng -------|",
        "  periode        dager     publisert    usikret      sikret",
    ]
    by_label = {c.label: c for c in hedged}
    for c in unhedged:
        other = by_label.get(c.label)
        lines.append(
            f"  {c.label:<12} {c.trading_days:>6}    {c.published_pct:>+8.2f} %"
            f"   {_fmt(c.error_pct):>8}   {_fmt(other.error_pct if other else None):>9}"
        )

    lines.append("")
    lines.append(_verdict(unhedged, hedged))
    return "\n".join(lines)


def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:+.2f}"


def _verdict(unhedged: list[PeriodCheck], hedged: list[PeriodCheck]) -> str:
    """State which currency assumption fits, using only the short windows.

    Long windows are dominated by portfolio drift, which has nothing to do with
    hedging, so including them would muddy exactly the signal we want.
    """
    def score(checks):
        short = [c for c in checks if c.trading_days and c.trading_days <= 30
                 and c.error_pct is not None]
        return sum(abs(c.error_pct) for c in short) / len(short) if short else None

    a, b = score(unhedged), score(hedged)
    if a is None or b is None:
        return "  For lite data til å avgjøre valutaspørsmålet."

    best = min(a, b)
    # Declaring a winner only makes sense if the winner is actually right. When
    # both are far off, the better one is merely less wrong, and calling it the
    # answer would send us looking in the wrong place.
    if best > 1.0:
        return (
            f"  Begge tar feil (usikret {a:.2f}, sikret {b:.2f} %-poeng snittavvik "
            "på korte vinduer).\n"
            "  Valuta er altså ikke hovedforklaringen - feilen ligger et annet sted."
        )
    if abs(a - b) < 0.3:
        return (
            f"  Ingen tydelig forskjell (usikret {a:.2f}, sikret {b:.2f} %-poeng "
            "snittavvik). Valuta forklarer ikke bommen."
        )
    winner, loser = ("sikret", "usikret") if b < a else ("usikret", "sikret")
    return (
        f"  {winner.capitalize()} passer klart best: {best:.2f} mot "
        f"{max(a, b):.2f} %-poeng i snittavvik på korte vinduer.\n"
        f"  Fondet oppfører seg altså som et {winner} fond, ikke et {loser}."
    )


def format_window_sweep(
    fund: FundConfig, snapshot: HoldingsSnapshot, as_of: date,
    published_week_pct: float, window: int = 5, span: int = 12,
) -> str:
    """Show daily estimates, and every 5-day window's total, around the anchor.

    The published one-week figure is a single number; the question is which five
    trading days actually produced it. Printing the daily series and each
    candidate window's total answers that directly, instead of assuming the
    window ends on the date the fund page happens to display.
    """
    daily = estimate_series(
        fund, snapshot, as_of - timedelta(days=span * 3), as_of + timedelta(days=3)
    )
    if daily.empty:
        return "Ingen daglige estimater å vise."

    daily = daily.tail(span)
    lines = ["Daglige estimater rundt måledatoen:", ""]
    for ts, value in daily.items():
        marker = "  <- måledato" if ts.date() == as_of else ""
        lines.append(f"  {ts.date()}   {value:>+7.2f} %{marker}")

    lines += [
        "",
        f"Sammenkjedet over {window} dager, per sluttdato "
        f"(fasit {published_week_pct:+.2f} %):",
        "",
    ]
    values = list(daily.items())
    for i in range(window - 1, len(values)):
        end_ts = values[i][0]
        chunk = pd.Series([v for _, v in values[i - window + 1:i + 1]])
        total = _compound(chunk)
        lines.append(
            f"  til {end_ts.date()}   {total:>+7.2f} %   "
            f"avvik {total - published_week_pct:>+6.2f}"
        )
    return "\n".join(lines)


def format_validation(checks: list[PeriodCheck], as_of: date) -> str:
    lines = [
        f"Modell mot Nordnets publiserte tall, per {as_of}",
        "",
        "  periode        dager    publisert    estimert       avvik",
    ]
    for c in checks:
        if c.estimated_pct is None:
            lines.append(f"  {c.label:<12} {'-':>6}   {c.published_pct:>+9.2f} %"
                         f"   {'ingen data':>10}")
            continue
        lines.append(
            f"  {c.label:<12} {c.trading_days:>6}   {c.published_pct:>+9.2f} %"
            f"   {c.estimated_pct:>+9.2f} %   {c.error_pct:>+9.2f}"
        )

    short = [c for c in checks if c.trading_days and c.trading_days <= 30
             and c.error_pct is not None]
    lines.append("")
    if short:
        worst = max(abs(c.error_pct) for c in short)
        lines.append(
            f"  På korte vinduer, der porteføljen knapt har endret seg, "
            f"bommer modellen med opptil {worst:.2f} %-poeng."
        )
        lines.append(
            "  Det er den tallstørrelsen som sier noe om dagsestimatets kvalitet."
        )
    lines.append(
        "  Lange vinduer måler noe annet: der har forvalter handlet, mens vi "
        "fortsatt holder dagens vekter."
    )
    return "\n".join(lines)
