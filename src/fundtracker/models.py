"""Core data structures shared across sources, estimation and reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional


@dataclass(frozen=True)
class Holding:
    """A single position as reported by a holdings source.

    ``weight_pct`` is the position's share of the *whole fund* (so cash and any
    positions the source did not report are part of the missing remainder).
    """

    name: str
    weight_pct: float
    ticker: Optional[str] = None
    currency: Optional[str] = None


@dataclass
class HoldingsSnapshot:
    """Everything a holdings source could tell us at a point in time.

    ``as_of`` is the date the *fund company* says the composition is from, when
    that is published. ``retrieved`` is when we scraped it. The gap between the
    two is the staleness that the backtest measures the cost of.
    """

    fund_id: str
    retrieved: date
    source: str
    holdings: list[Holding]
    as_of: Optional[date] = None
    cash_pct: Optional[float] = None
    equity_pct: Optional[float] = None

    @property
    def total_weight_pct(self) -> float:
        return sum(h.weight_pct for h in self.holdings)


@dataclass
class Contribution:
    """One holding's decomposed contribution to the fund's daily return."""

    name: str
    ticker: str
    currency: str
    weight_pct: float
    local_return: float
    fx_return: float
    nok_return: float
    contribution_pct: float
    stale_price: bool = False
    # True when this price was carried forward rather than quoted on the day,
    # which makes the holding look flat when we simply do not know yet.
    carried_forward: bool = False


@dataclass
class Estimate:
    """The result of one nowcast."""

    fund_id: str
    fund_name: str
    date: date
    # The fund's own base currency. The return is measured in it, so a mail
    # putting several funds side by side has to say which is which.
    currency: str
    return_pct: float
    equity_return_pct: float
    fx_contribution_pct: float
    fee_drag_pct: float
    coverage_pct: float
    # Share of the fund whose price on the day was carried forward, not quoted.
    stale_weight_pct: float
    cash_pct: Optional[float]
    snapshot_age_days: Optional[int]
    # Mean absolute error the last backtest measured, in percentage points,
    # and how many days it ran over. None when the fund has never been measured
    # - which is itself worth seeing, so it is never filled in with a guess.
    mean_abs_error_pp: Optional[float] = None
    accuracy_days: Optional[int] = None
    contributions: list[Contribution] = field(default_factory=list)
    unpriced: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def top_contributors(self, n: int = 5) -> list[Contribution]:
        return sorted(self.contributions, key=lambda c: -c.contribution_pct)[:n]

    def bottom_contributors(self, n: int = 5) -> list[Contribution]:
        return sorted(self.contributions, key=lambda c: c.contribution_pct)[:n]
