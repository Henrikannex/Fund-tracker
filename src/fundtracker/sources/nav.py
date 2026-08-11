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


def load_nav_remote(fund: FundConfig, start: date, end: date) -> pd.Series:
    """The remote NAV sources only, in order of preference.

    Split out from :func:`load_nav` for callers that need to tell "a source
    answered" from "we read our own file back". Anything polling for *newly
    published* days needs that distinction: falling back to the checked-in CSV
    would compare the file against itself, find nothing new, and report a dead
    source as a quiet day.
    """
    spec = fund.nav_source or {}
    kind = (spec.get("type") or "manual").lower()
    series = pd.Series(dtype="float64")

    if kind in ("yahoo", "auto"):
        series = _from_yahoo(spec.get("ticker"), start, end)
        if series.empty:
            log.warning("Yahoo ga ingen NAV for %s", spec.get("ticker"))

    if series.empty and spec.get("morningstar_secid"):
        series = _from_morningstar(spec, fund.currency, start, end)
        if series.empty:
            log.warning("Morningstar ga ingen NAV for %s", spec.get("morningstar_secid"))

    instrument_id = spec.get("instrument_id") or (fund.holdings_source or {}).get(
        "instrument_id"
    )
    if series.empty and instrument_id:
        series = _from_nordnet(str(instrument_id), start, end)
        if series.empty:
            log.warning("Nordnet ga ingen NAV for instrument %s", instrument_id)

    return _clip(series, start, end)


def load_nav(fund: FundConfig, start: date, end: date) -> pd.Series:
    """Date-indexed NAV series. Empty if no source is configured or reachable."""
    spec = fund.nav_source or {}

    # Each source is tried in turn; the manual file is the last resort. NAV
    # history is what makes the model checkable at all, so it is worth being
    # stubborn about finding it.
    series = load_nav_remote(fund, start, end)

    if series.empty:
        series = _from_manual(spec.get("manual_file"))
        if series.empty:
            log.warning(
                "Ingen NAV-kilde svarte. Legg inn historikk manuelt i %s "
                "(kolonner: date,nav).",
                spec.get("manual_file"),
            )

    return _clip(series, start, end)


def _clip(series: pd.Series, start: date, end: date) -> pd.Series:
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


def _from_morningstar(spec: dict, currency: str, start: date, end: date) -> pd.Series:
    from . import morningstar

    try:
        return morningstar.fetch_nav_history(
            str(spec["morningstar_secid"]), start, end, currency
        )
    except Exception as exc:  # noqa: BLE001 - a dead feed must not break the run
        log.warning("Morningstar NAV-henting feilet: %s", exc)
        return pd.Series(dtype="float64")


# Nordnet draws a price chart on every fund page, so the history exists behind
# some endpoint. These are the shapes their client has been seen to use.
NORDNET_PRICE_ENDPOINTS = [
    "https://www.nordnet.no/api/2/instruments/historical/prices/{id}",
    "https://www.nordnet.no/api/2/instruments/historical/returns/{id}",
]


def _from_nordnet(instrument_id: str, start: date, end: date) -> pd.Series:
    """NAV history from Nordnet's own chart feed."""
    import requests

    from .holdings import USER_AGENT

    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept": "application/json",
         "Referer": "https://www.nordnet.no/"}
    )
    for template in NORDNET_PRICE_ENDPOINTS:
        url = template.format(id=instrument_id)
        try:
            response = session.get(
                url, params={"from": start.isoformat(), "to": end.isoformat(),
                             "fields": "last"}, timeout=30
            )
            response.raise_for_status()
            series = _parse_nordnet_prices(response.json())
        except Exception as exc:  # noqa: BLE001 - candidate probing
            log.debug("Nordnet NAV-kandidat %s feilet: %s", url, exc)
            continue
        if not series.empty:
            log.info("Hentet %d NAV-punkter fra %s", len(series), url)
            return series
    return pd.Series(dtype="float64")


def _parse_nordnet_prices(payload: Any) -> pd.Series:
    """Pull [{time, last}] pairs out of whatever nesting Nordnet wraps them in."""
    rows: dict[Any, float] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            stamp = node.get("time") or node.get("timestamp") or node.get("date")
            value = node.get("last") or node.get("close") or node.get("value")
            if isinstance(stamp, (int, float)) and isinstance(value, (int, float)):
                rows[pd.Timestamp(int(stamp), unit="ms").normalize()] = float(value)
            elif isinstance(stamp, str) and isinstance(value, (int, float)):
                try:
                    rows[pd.Timestamp(stamp).normalize()] = float(value)
                except ValueError:
                    pass
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return pd.Series(rows, dtype="float64").sort_index()


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
        from . import morningstar

        try:
            results.extend(morningstar.probe_nav(base, start, end, fund.currency))
        except Exception as exc:  # noqa: BLE001 - probing must not raise
            results.append((f"Morningstar {base}", f"FEIL: {exc}"))

    instrument_id = spec.get("instrument_id") or (fund.holdings_source or {}).get(
        "instrument_id"
    )
    if instrument_id:
        series = _from_nordnet(str(instrument_id), start, end)
        results.append((
            f"Nordnet instrument {instrument_id}",
            f"{len(series)} kurser, siste {series.iloc[-1]:.2f}"
            if not series.empty else "ingen data",
        ))
    else:
        results.append(("Nordnet", "instrument_id ikke satt - trenger URL-en fra Nordnet"))

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
