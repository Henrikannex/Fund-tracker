"""Notice when the fund publishes a new day, and score our estimate against it.

This closes the loop the project was missing. Until now the actual NAV had to be
read off a web page by hand, so the model could only be checked when someone
remembered to. Polling for new published days turns that into something the
system does for itself: every new day both extends the history the backtest
runs on, and settles whether that evening's estimate was any good.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from .config import REPO_ROOT, FundConfig
from .sources import investing
from .sources import nav as nav_source

log = logging.getLogger(__name__)


@dataclass
class NewDay:
    """A published day we had not recorded before."""

    date: date
    nav: float
    actual_pct: float
    estimated_pct: Optional[float]

    @property
    def error_pct(self) -> Optional[float]:
        if self.estimated_pct is None:
            return None
        return self.estimated_pct - self.actual_pct


def poll(fund: FundConfig, lookback_days: int = 30) -> list[NewDay]:
    """Fetch published NAV and return the days that are new to us.

    Only days strictly newer than what we already hold count as new. Re-reading
    the same day must stay silent, or a job that runs every few hours turns into
    a job that mails every few hours.
    """
    spec = fund.nav_source or {}
    page = spec.get("investing_url")
    if not page:
        raise RuntimeError(
            f"nav_source.investing_url er ikke satt for {fund.id}, "
            "så det finnes ingen side å følge med på."
        )

    end = date.today()
    fetched = investing.fetch_nav(
        page, end - timedelta(days=lookback_days), end, spec.get("investing_pair_id")
    )
    if fetched.empty:
        log.warning("Ingen kurser hentet fra %s", page)
        return []

    path = _nav_path(fund)
    known = nav_source._from_manual(str(path)) if path.exists() else pd.Series(dtype="float64")
    latest_known = known.index.max() if not known.empty else None

    merged = known.combine_first(fetched).sort_index()
    fresh = [ts for ts in fetched.index if latest_known is None or ts > latest_known]
    if not fresh:
        log.info("Ingen nye dager. Siste kjente er %s.", latest_known.date()
                 if latest_known is not None else "ingen")
        return []

    _write_nav(path, merged)
    returns = nav_source.nav_returns(merged)
    estimates = _logged_estimates(fund)

    out = []
    for ts in fresh:
        if ts not in returns.index:
            continue  # first row of the whole series has no previous close
        out.append(NewDay(
            date=ts.date(),
            nav=float(merged.loc[ts]),
            actual_pct=float(returns.loc[ts]),
            estimated_pct=estimates.get(ts.date().isoformat()),
        ))
    return out


def _nav_path(fund: FundConfig) -> Path:
    rel = (fund.nav_source or {}).get("manual_file") or f"data/nav/{fund.id}.csv"
    path = Path(rel)
    return path if path.is_absolute() else REPO_ROOT / path


def _write_nav(path: Path, series: pd.Series) -> None:
    """Rewrite the NAV file, keeping the comment header it carries."""
    header = []
    if path.exists():
        header = [
            line for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("#")
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    body = [f"{ts.date().isoformat()},{value:.3f}" for ts, value in series.items()]
    path.write_text("\n".join(header + ["date,nav"] + body) + "\n", encoding="utf-8")


def _logged_estimates(fund: FundConfig) -> dict[str, float]:
    path = fund.estimates_file()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {
            row["date"]: float(row["estimated_pct"])
            for row in csv.DictReader(fh)
            if row.get("date") and row.get("estimated_pct")
        }


def _pct(value: float) -> str:
    return f"{value:+.2f} %".replace(".", ",")


def subject(fund: FundConfig, days: list[NewDay]) -> str:
    newest = days[-1]
    return f"{fund.name}: fasit {_pct(newest.actual_pct)} ({newest.date.strftime('%d.%m')})"


def to_text(fund: FundConfig, days: list[NewDay]) -> str:
    lines = [fund.name, "", "Nye publiserte dager:", ""]
    for day in days:
        lines.append(f"  {day.date}   NAV {day.nav:,.3f}   {_pct(day.actual_pct)}"
                     .replace(",", " "))
        if day.estimated_pct is None:
            lines.append("            (vi hadde ikke noe estimat for denne dagen)")
        else:
            lines.append(
                f"            estimert {_pct(day.estimated_pct)}, "
                f"bom {day.error_pct:+.2f} %-poeng".replace(".", ",")
            )
    scored = [d for d in days if d.error_pct is not None]
    if scored:
        mae = sum(abs(d.error_pct) for d in scored) / len(scored)
        lines += ["", f"Snittbom på disse: {mae:.2f} %-poeng".replace(".", ",")]
    lines += ["", "Fasit fra fondets publiserte kurs. Historikken er oppdatert."]
    return "\n".join(lines)
