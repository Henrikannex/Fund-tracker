"""Published NAV from Investing.com.

Investing.com carries this fund's daily price under its Morningstar id, and
publishes a day earlier than Nordnet shows it. That makes it two useful things
at once: the source of truth for measuring our own estimates, and the reason
the NAV history no longer has to be pasted in by hand.

Their historical table is served by an internal JSON API. It is undocumented,
sits behind bot protection, and rejects plenty of datacentre traffic — so every
call here is written to fail softly and report why, and the checked-in CSV stays
the fallback. Use ``probe`` from a machine with real network access to find out
what actually answers.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Iterable, Optional

import pandas as pd
import requests

log = logging.getLogger(__name__)

TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# The numeric pair id their API wants is not in the public URL; it is embedded
# in the page. These patterns cover the shapes their markup has used.
PAIR_ID_PATTERNS = [
    r'"pairId"\s*:\s*"?(\d{4,})"?',
    r'"pair_ID"\s*:\s*"?(\d{4,})"?',
    r'data-pair-id="(\d{4,})"',
    r'instrumentId["\s:=]+(\d{4,})',
]

HISTORICAL_ENDPOINTS = [
    "https://api.investing.com/api/financialdata/historical/{pair_id}",
    "https://tvc4.investing.com/api/financialdata/historical/{pair_id}",
]


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.investing.com/",
        # Their API rejects requests without this; it identifies the site
        # variant rather than authenticating anything.
        "domain-id": "www",
    })
    return session


def find_pair_id(page_url: str, session: Optional[requests.Session] = None) -> Optional[str]:
    """Dig the numeric instrument id out of the fund's page."""
    session = session or _session()
    response = session.get(page_url, timeout=TIMEOUT)
    response.raise_for_status()
    for pattern in PAIR_ID_PATTERNS:
        match = re.search(pattern, response.text)
        if match:
            return match.group(1)
    return None


def fetch_nav(
    page_url: str, start: date, end: date, pair_id: Optional[str] = None
) -> pd.Series:
    """Daily closing NAV, or an empty series if nothing answered."""
    session = _session()
    try:
        pair_id = pair_id or find_pair_id(page_url, session)
    except requests.RequestException as exc:
        log.warning("Fant ikke instrument-id på %s: %s", page_url, exc)
        return pd.Series(dtype="float64")
    if not pair_id:
        log.warning("Ingen instrument-id i sidekilden til %s", page_url)
        return pd.Series(dtype="float64")

    for template in HISTORICAL_ENDPOINTS:
        url = template.format(pair_id=pair_id)
        try:
            response = session.get(url, params=_params(start, end), timeout=TIMEOUT)
            response.raise_for_status()
            series = _parse(response.json())
        except Exception as exc:  # noqa: BLE001 - candidate probing
            log.debug("NAV-kandidat %s feilet: %s", url, exc)
            continue
        if not series.empty:
            log.info("Hentet %d NAV-punkter fra %s", len(series), url)
            return series
    return pd.Series(dtype="float64")


def _params(start: date, end: date) -> dict[str, str]:
    return {
        "start-date": start.isoformat(),
        "end-date": end.isoformat(),
        "time-frame": "Daily",
        "add-missing-rows": "false",
    }


def _parse(payload: Any) -> pd.Series:
    """Read {date, last_close} rows out of whatever wrapper the API uses."""
    rows: dict[pd.Timestamp, float] = {}
    for item in _iter_records(payload):
        when = _read_date(item)
        value = _read_price(item)
        if when is not None and value is not None:
            rows[when] = value
    return pd.Series(rows, dtype="float64").sort_index()


def _iter_records(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_records(value)
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                yield item
            else:
                yield from _iter_records(item)


_DATE_KEYS = ("rowDate", "date", "rowDateRaw", "direction_color", "time")
_PRICE_KEYS = ("last_close", "last_closeRaw", "close", "price", "last")


def _read_date(item: dict) -> Optional[pd.Timestamp]:
    for key in ("rowDateTimestamp", "rowDateRaw", "time", "timestamp"):
        value = item.get(key)
        if isinstance(value, (int, float)) and value > 10**8:
            unit = "ms" if value > 10**11 else "s"
            return pd.Timestamp(int(value), unit=unit).normalize()
    for key in _DATE_KEYS:
        value = item.get(key)
        if isinstance(value, str):
            for fmt in ("%b %d, %Y", "%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y"):
                try:
                    return pd.Timestamp(datetime.strptime(value.strip(), fmt)).normalize()
                except ValueError:
                    continue
    return None


def _read_price(item: dict) -> Optional[float]:
    for key in _PRICE_KEYS:
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            cleaned = value.replace(",", "").replace(" ", "").strip()
            try:
                return float(cleaned)
            except ValueError:
                continue
    return None


def probe(page_url: str, start: date, end: date) -> list[tuple[str, str]]:
    """Report what each step returns, so a block is distinguishable from a bug."""
    session = _session()
    results: list[tuple[str, str]] = []

    try:
        response = session.get(page_url, timeout=TIMEOUT)
        note = f"HTTP {response.status_code}, {len(response.content)} bytes"
        pair_id = None
        if response.status_code == 200:
            for pattern in PAIR_ID_PATTERNS:
                match = re.search(pattern, response.text)
                if match:
                    pair_id = match.group(1)
                    break
            note += f", instrument-id {pair_id}" if pair_id else ", fant ingen instrument-id"
        results.append((page_url, note))
    except requests.RequestException as exc:
        results.append((page_url, f"FEIL: {exc}"))
        return results

    if not pair_id:
        return results

    for template in HISTORICAL_ENDPOINTS:
        url = template.format(pair_id=pair_id)
        try:
            response = session.get(url, params=_params(start, end), timeout=TIMEOUT)
        except requests.RequestException as exc:
            results.append((url, f"FEIL: {exc}"))
            continue
        note = f"HTTP {response.status_code}, {len(response.content)} bytes"
        if response.status_code == 200:
            try:
                series = _parse(response.json())
            except (ValueError, json.JSONDecodeError):
                note += ", ikke JSON"
            else:
                note += (
                    f", {len(series)} kurser, siste {series.index.max().date()} "
                    f"= {series.iloc[-1]:.3f}"
                    if not series.empty else ", JSON uten gjenkjennelige kurser"
                )
        results.append((url, note))
    return results
