"""Actual published NAV, used to validate the estimate."""

from __future__ import annotations

import csv
import io
import logging
from datetime import date
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from ..config import REPO_ROOT, FundConfig

log = logging.getLogger(__name__)


def load_nav(fund: FundConfig, start: date, end: date) -> pd.Series:
    """Date-indexed NAV series. Empty if no source is configured or reachable."""
    spec = fund.nav_source or {}
    kind = (spec.get("type") or "manual").lower()

    series = pd.Series(dtype="float64")
    if kind == "yahoo":
        series = _from_yahoo(spec.get("ticker"), start, end)
        if series.empty:
            log.warning(
                "Yahoo ga ingen NAV for %s. Legg inn kurshistorikk manuelt i %s.",
                spec.get("ticker"),
                spec.get("manual_file"),
            )
    if series.empty:
        series = _from_manual(spec.get("manual_file"))

    if series.empty:
        return series
    mask = (series.index >= pd.Timestamp(start)) & (series.index <= pd.Timestamp(end))
    return series[mask]


def _from_yahoo(ticker: Optional[str], start: date, end: date) -> pd.Series:
    if not ticker:
        return pd.Series(dtype="float64")
    from .prices import closing_prices

    frame = closing_prices([ticker], start, end)
    if frame.empty or ticker not in frame.columns:
        return pd.Series(dtype="float64")
    return frame[ticker].dropna()


def _from_manual(rel: Optional[str]) -> pd.Series:
    if not rel:
        return pd.Series(dtype="float64")
    path = Path(rel)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.exists():
        return pd.Series(dtype="float64")

    lines = [
        ln
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    rows: dict[pd.Timestamp, float] = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines))):
        raw_date = (row.get("date") or "").strip()
        raw_nav = (row.get("nav") or "").strip().replace(" ", "").replace(",", ".")
        if not raw_date or not raw_nav:
            continue
        rows[pd.Timestamp(raw_date).normalize()] = float(raw_nav)
    return pd.Series(rows, dtype="float64").sort_index()


def nav_returns(series: pd.Series) -> pd.Series:
    """Percent change between consecutive published NAVs."""
    if series.empty:
        return series
    return series.pct_change().dropna() * 100.0


# Yahoo lists European funds under their Morningstar id with a market suffix,
# and which suffix works is not predictable from the fund alone. Rather than
# guess once and get a silent empty series, try them all and report.
YAHOO_SUFFIXES = ["", ".OL", ".F", ".SG", ".MI", ".L"]


def probe(fund: FundConfig) -> list[tuple[str, str]]:
    """Report which NAV-history sources actually return data.

    The backtest is worthless without a real NAV series, so this is the first
    thing to check when setting up a new fund.
    """
    from datetime import timedelta

    spec = fund.nav_source or {}
    end = date.today()
    start = end - timedelta(days=120)
    results: list[tuple[str, str]] = []

    base = str(spec.get("morningstar_secid") or "").strip()
    configured = str(spec.get("ticker") or "").strip()
    candidates = [configured] if configured else []
    stem = spec.get("yahoo_stem") or _stem(configured)
    if stem:
        candidates += [f"{stem}{suffix}" for suffix in YAHOO_SUFFIXES]

    for ticker in dict.fromkeys(c for c in candidates if c):
        try:
            series = _from_yahoo(ticker, start, end)
        except Exception as exc:  # noqa: BLE001 - probing must not raise
            results.append((f"Yahoo {ticker}", f"FEIL: {exc}"))
            continue
        if series.empty:
            results.append((f"Yahoo {ticker}", "ingen data"))
        else:
            results.append((
                f"Yahoo {ticker}",
                f"{len(series)} kurser, {series.index.min().date()} til "
                f"{series.index.max().date()}, siste {series.iloc[-1]:.2f}",
            ))

    if base:
        results.append((f"Morningstar secid {base}", "brukes til beholdninger, ikke NAV"))

    manual = spec.get("manual_file")
    if manual:
        series = _from_manual(manual)
        results.append((
            f"Manuell fil {manual}",
            f"{len(series)} kurser" if not series.empty else "finnes ikke / tom",
        ))
    return results


def _stem(ticker: str) -> Optional[str]:
    """Strip a market suffix so the other suffixes can be tried."""
    if not ticker:
        return None
    return ticker.split(".", 1)[0]


def append_actual(path: Path, when: date, nav: float) -> None:
    """Record a published NAV so the error log builds up over time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(["date", "nav"])
        writer.writerow([when.isoformat(), f"{nav:.4f}"])
