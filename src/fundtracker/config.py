"""Loading and validation of per-fund configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
FUNDS_DIR = REPO_ROOT / "config" / "funds"
DATA_DIR = REPO_ROOT / "data"


@dataclass(frozen=True)
class TickerMapping:
    ticker: str
    currency: str


@dataclass
class FundConfig:
    id: str
    name: str
    isin: str
    currency: str
    total_cost_pct: float
    trading_days_per_year: int
    holdings_source: dict[str, Any]
    nav_source: dict[str, Any]
    tickers: dict[str, TickerMapping] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
    # Published period returns to check the model against, when no NAV series
    # is available. See fundtracker.validate.
    benchmarks: dict[str, Any] = field(default_factory=dict)

    @property
    def daily_fee_drag(self) -> float:
        """TER expressed as a per-trading-day drag, in percent.

        1.14 % a year over 252 trading days is roughly -0.0045 % a day. Small,
        but it is a one-directional bias, so leaving it out means the estimate
        reads high every single day.
        """
        return self.total_cost_pct / self.trading_days_per_year

    def snapshot_dir(self) -> Path:
        return DATA_DIR / "holdings" / self.id

    def estimates_file(self) -> Path:
        return DATA_DIR / "estimates" / f"{self.id}.csv"

    def resolve(self, name: str) -> Optional[TickerMapping]:
        """Map a holdings-source name to a price ticker.

        Matching is deliberately forgiving about case and whitespace, because
        the source occasionally reformats names, but it never guesses: an
        unknown name is reported rather than silently dropped.
        """
        if name in self.tickers:
            return self.tickers[name]
        key = _normalise(name)
        for raw, mapping in self.tickers.items():
            if _normalise(raw) == key:
                return mapping
        return None

    def is_ignored(self, name: str) -> bool:
        key = _normalise(name)
        return any(_normalise(i) == key for i in self.ignore)


def _normalise(name: str) -> str:
    return " ".join(name.lower().replace(".", " ").replace(",", " ").split())


def load_fund(fund_id: str) -> FundConfig:
    path = FUNDS_DIR / f"{fund_id}.yaml"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in FUNDS_DIR.glob("*.yaml"))) or "none"
        raise FileNotFoundError(f"No config for fund {fund_id!r}. Available: {available}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    tickers: dict[str, TickerMapping] = {}
    for name, spec in (raw.get("tickers") or {}).items():
        if not isinstance(spec, dict) or "ticker" not in spec or "currency" not in spec:
            raise ValueError(
                f"{path}: ticker entry {name!r} needs both 'ticker' and 'currency'"
            )
        tickers[name] = TickerMapping(spec["ticker"], spec["currency"].upper())

    return FundConfig(
        id=raw["id"],
        name=raw["name"],
        isin=raw["isin"],
        currency=raw.get("currency", "NOK").upper(),
        total_cost_pct=float(raw.get("total_cost_pct", 0.0)),
        trading_days_per_year=int(raw.get("trading_days_per_year", 252)),
        holdings_source=raw.get("holdings_source") or {},
        nav_source=raw.get("nav_source") or {},
        tickers=tickers,
        ignore=list(raw.get("ignore") or []),
        benchmarks=raw.get("benchmarks") or {},
    )


def list_funds() -> list[str]:
    return sorted(p.stem for p in FUNDS_DIR.glob("*.yaml"))
