"""Morningstar's undocumented SAL service.

Everything on Nordnet's fund page — holdings, asset allocation, ratings — is
Morningstar data under licence. Going to the source removes one hop of staleness
and, more usefully, returns a *ticker and listing currency per holding*, which
is the part we otherwise have to maintain by hand.

These endpoints are not documented or supported. They power Morningstar's own
public web widgets, and the API key below is the public one those widgets ship
with; it is not a credential belonging to anyone. Expect this to break without
notice, which is why the manual CSV stays as the fallback.

The free tier caps the holdings list, historically around 25 rows — the same
place Nordnet's list appears to stop. Whether that is enough is exactly what
the ``probe`` command is for.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Iterable, Optional

import requests

from ..models import Holding, HoldingsSnapshot

log = logging.getLogger(__name__)

BASE = "https://api-global.morningstar.com/sal-service/v1"
PUBLIC_WIDGET_KEY = "lstzFDEOhfFNMLikKa0am9mgEKLBl49T"
TIMEOUT = 30

COMMON_PARAMS = {
    "languageId": "en",
    "locale": "en",
    "clientId": "MDC",
    "version": "4.4.0",
}


# Morningstar's chart widgets read NAV history from a separate host with its own
# public key. The id carries a suffix encoding which series to return; which
# variant a given fund answers on is not predictable, so both are tried.
TIMESERIES_HOSTS = [
    "https://tools.morningstar.co.uk/api/rest.svc/timeseries_price/t92wz0sj7c",
    "https://tools.morningstar.no/api/rest.svc/timeseries_price/t92wz0sj7c",
]
TIMESERIES_ID_SUFFIXES = ["]2]1]", "]22]1]"]


def fetch_nav_history(sec_id: str, start: date, end: date, currency: str = "NOK"):
    """Daily NAV series for a Morningstar security id, as a pandas Series.

    Returns an empty Series rather than raising: a missing NAV feed should
    degrade the backtest, not take down the daily estimate.
    """
    import pandas as pd

    session = _session()
    for url, series in _timeseries_candidates(session, sec_id, start, end, currency):
        if not series.empty:
            log.info("Hentet %d NAV-punkter fra %s", len(series), url)
            return series
    return pd.Series(dtype="float64")


def _timeseries_candidates(session, sec_id: str, start: date, end: date, currency: str):
    import pandas as pd

    for host in TIMESERIES_HOSTS:
        for suffix in TIMESERIES_ID_SUFFIXES:
            params = {
                "id": f"{sec_id}{suffix}",
                "currencyId": currency,
                "idtype": "Morningstar",
                "frequency": "daily",
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "outputType": "COMPACTJSON",
            }
            try:
                response = session.get(host, params=params, timeout=TIMEOUT)
                response.raise_for_status()
                payload = response.json()
            except Exception as exc:  # noqa: BLE001 - candidate probing
                log.debug("NAV-kandidat %s%s feilet: %s", host, suffix, exc)
                yield f"{host} ({suffix})", pd.Series(dtype="float64")
                continue
            yield f"{host} ({suffix})", _parse_timeseries(payload)


def _parse_timeseries(payload: Any):
    """COMPACTJSON is a bare list of [epoch_millis, value] pairs."""
    import pandas as pd

    if not isinstance(payload, list):
        return pd.Series(dtype="float64")
    rows: dict[Any, float] = {}
    for item in payload:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        stamp, value = item[0], item[1]
        if not isinstance(stamp, (int, float)) or not isinstance(value, (int, float)):
            continue
        rows[pd.Timestamp(int(stamp), unit="ms").normalize()] = float(value)
    return pd.Series(rows, dtype="float64").sort_index()


def probe_nav(sec_id: str, start: date, end: date, currency: str = "NOK"):
    """Report which NAV timeseries variant answers, for the diagnostics run."""
    session = _session()
    results = []
    for label, series in _timeseries_candidates(session, sec_id, start, end, currency):
        if series.empty:
            results.append((label, "ingen data"))
        else:
            results.append((
                label,
                f"{len(series)} kurser, {series.index.min().date()} til "
                f"{series.index.max().date()}, siste {series.iloc[-1]:.2f}",
            ))
    return results


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "ApiKey": PUBLIC_WIDGET_KEY,
            "Accept": "application/json",
            "Origin": "https://www.morningstar.com",
            "Referer": "https://www.morningstar.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
        }
    )
    return session


def holdings_url(sec_id: str) -> str:
    return f"{BASE}/fund/portfolio/holding/v2/{sec_id}/data"


def allocation_url(sec_id: str) -> str:
    return f"{BASE}/fund/portfolio/assetAllocation/v2/{sec_id}/data"


def fetch_holdings(fund_id: str, sec_id: str) -> HoldingsSnapshot:
    """Fetch the portfolio for a Morningstar security id (e.g. F0GBR04NGU)."""
    session = _session()

    payload = _get_json(
        session,
        holdings_url(sec_id),
        {**COMMON_PARAMS, "premiumNum": 1000, "freeNum": 1000,
         "component": "sal-components-mip-holdings"},
    )
    holdings = list(_parse_holdings(payload))
    if not holdings:
        raise RuntimeError(f"Morningstar ga ingen beholdninger for {sec_id}")

    cash_pct, equity_pct = None, None
    try:
        allocation = _get_json(
            session,
            allocation_url(sec_id),
            {**COMMON_PARAMS, "component": "sal-components-mip-asset-allocation"},
        )
        cash_pct, equity_pct = _parse_allocation(allocation)
    except Exception as exc:  # noqa: BLE001 - allocation is a nice-to-have
        log.warning("Kunne ikke hente aktivafordeling fra Morningstar: %s", exc)

    return HoldingsSnapshot(
        fund_id=fund_id,
        retrieved=date.today(),
        source=f"morningstar:{sec_id}",
        holdings=holdings,
        as_of=_parse_as_of(payload),
        cash_pct=cash_pct,
        equity_pct=equity_pct,
    )


def _get_json(session: requests.Session, url: str, params: dict[str, Any]) -> Any:
    response = session.get(url, params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _parse_holdings(payload: Any) -> Iterable[Holding]:
    """Read the holding lists out of a SAL portfolio response.

    Morningstar splits the portfolio across ``equityHoldingPage``,
    ``boldHoldingPage`` and ``otherHoldingPage``; a technology equity fund lives
    almost entirely in the first, but reading all of them keeps the weights
    honest for any fund with a bond or derivative sleeve.
    """
    if not isinstance(payload, dict):
        return []

    seen: dict[str, Holding] = {}
    for key, value in payload.items():
        if not key.endswith("HoldingPage") or not isinstance(value, dict):
            continue
        for item in value.get("holdingList") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("securityName") or item.get("secName")
            weight = item.get("weighting")
            if not isinstance(name, str) or not isinstance(weight, (int, float)):
                continue
            name = name.strip()
            ticker = item.get("ticker") or None
            currency = item.get("currency") or item.get("localCurrencyCode") or None
            seen[name] = Holding(
                name=name,
                weight_pct=float(weight),
                ticker=ticker.strip() if isinstance(ticker, str) else None,
                currency=currency.strip().upper() if isinstance(currency, str) else None,
            )
    return sorted(seen.values(), key=lambda h: -h.weight_pct)


def _parse_allocation(payload: Any) -> tuple[Optional[float], Optional[float]]:
    """Pull net cash and equity percentages out of an asset-allocation response."""
    if not isinstance(payload, dict):
        return None, None
    table = payload.get("allocationMap") or payload.get("assetAllocation") or {}
    if not isinstance(table, dict):
        return None, None

    def pick(*names: str) -> Optional[float]:
        for name in names:
            entry = table.get(name)
            if isinstance(entry, dict):
                for field in ("netAllocation", "net", "value"):
                    value = entry.get(field)
                    if isinstance(value, (int, float)):
                        return float(value)
                    if isinstance(value, str):
                        try:
                            return float(value)
                        except ValueError:
                            continue
        return None

    return pick("AssetAllocCash", "cash"), pick("AssetAllocEquity", "equity")


def _parse_as_of(payload: Any) -> Optional[date]:
    if not isinstance(payload, dict):
        return None
    for key in ("portfolioDate", "holdingActiveShareDate", "lastTurnoverDate"):
        value = payload.get(key)
        if isinstance(value, str):
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(value[:19] if "T" in value else value, fmt).date()
                except ValueError:
                    continue
    return None


def probe(sec_id: str) -> list[tuple[str, str]]:
    """Report what each Morningstar endpoint returns, without trusting any of it."""
    session = _session()
    results: list[tuple[str, str]] = []

    checks = [
        (
            holdings_url(sec_id),
            {**COMMON_PARAMS, "premiumNum": 1000, "freeNum": 1000,
             "component": "sal-components-mip-holdings"},
            "holdings",
        ),
        (
            allocation_url(sec_id),
            {**COMMON_PARAMS, "component": "sal-components-mip-asset-allocation"},
            "allocation",
        ),
    ]

    for url, params, kind in checks:
        try:
            response = session.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException as exc:
            results.append((url, f"FEIL: {exc}"))
            continue
        summary = f"HTTP {response.status_code}, {len(response.content)} bytes"
        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                summary += ", ikke JSON"
            else:
                if kind == "holdings":
                    found = list(_parse_holdings(payload))
                    total = sum(h.weight_pct for h in found)
                    with_ticker = sum(1 for h in found if h.ticker)
                    summary += (
                        f", {len(found)} beholdninger, sum {total:.2f} %, "
                        f"{with_ticker} med ticker, dato {_parse_as_of(payload)}"
                    )
                    if found:
                        summary += " | " + ", ".join(
                            f"{h.name} {h.weight_pct:.2f}% [{h.ticker or '-'} "
                            f"{h.currency or '-'}]"
                            for h in found[:3]
                        )
                else:
                    cash, equity = _parse_allocation(payload)
                    summary += f", kontanter {cash}, aksjer {equity}"
        results.append((url, summary))
    return results
