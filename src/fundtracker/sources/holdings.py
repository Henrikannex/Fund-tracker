"""Where the portfolio composition comes from.

Two implementations today:

* ``manual``  - a CSV in the repo. Boring, always works, easy to correct by hand.
* ``nordnet`` - Nordnet's own JSON, with the CSV as a fallback.

The Nordnet reader is written defensively on purpose. Nordnet does not publish
a documented endpoint for fund holdings, so we try the shapes their web client
is known to use and report clearly which one answered. Use the ``probe``
command to find out what works from a machine that can actually reach them.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import requests

from ..config import REPO_ROOT, FundConfig
from ..models import Holding, HoldingsSnapshot

log = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = 30

# Candidate endpoints, tried in order. ``{id}`` is the Nordnet instrument id.
NORDNET_ENDPOINTS = [
    "https://www.nordnet.no/api/2/funds/{id}/holdings",
    "https://www.nordnet.no/api/2/funds/{id}",
    "https://www.nordnet.no/api/2/instruments/{id}/fund_holdings",
    "https://www.nordnet.no/api/2/instruments/{id}",
]

# Keys Nordnet/Morningstar payloads have used for the same two concepts.
NAME_KEYS = ("name", "instrument_name", "holding_name", "security_name", "companyName")
WEIGHT_KEYS = ("weight", "weight_pct", "percentage", "share", "weighting", "percent")


def load_holdings(fund: FundConfig) -> HoldingsSnapshot:
    """Fetch holdings using whatever source the fund config asks for.

    ``type: auto`` walks the remote sources in order of preference and falls
    back to the checked-in CSV. That fallback is the point: the composition only
    changes monthly, so a scraper outage costs us nothing as long as the file is
    reasonably current.
    """
    spec = fund.holdings_source or {}
    kind = (spec.get("type") or "manual").lower()

    if kind == "manual":
        return _from_manual(fund, spec)

    if kind == "auto":
        order = spec.get("order") or ["morningstar", "nordnet"]
    else:
        order = [kind]

    failures: list[str] = []
    for name in order:
        loader = _REMOTE_LOADERS.get(name)
        if loader is None:
            raise ValueError(f"Ukjent holdings_source.type: {name!r}")
        try:
            return loader(fund, spec)
        except Exception as exc:  # noqa: BLE001 - fallback must survive anything
            log.warning("Kilde %s feilet: %s", name, exc)
            failures.append(f"{name}: {exc}")

    snapshot = _from_manual(fund, spec)
    snapshot.source = f"manual (fjernkilder feilet - {'; '.join(failures)})"
    return snapshot


def _from_morningstar(fund: FundConfig, spec: dict[str, Any]) -> HoldingsSnapshot:
    from . import morningstar

    sec_id = spec.get("morningstar_secid")
    if not sec_id:
        raise ValueError("holdings_source.morningstar_secid er ikke satt")
    return morningstar.fetch_holdings(fund.id, str(sec_id))


def _from_manual(fund: FundConfig, spec: dict[str, Any]) -> HoldingsSnapshot:
    rel = spec.get("manual_file")
    if not rel:
        raise ValueError(f"{fund.id}: holdings_source.manual_file mangler")
    path = _resolve(rel)
    if not path.exists():
        raise FileNotFoundError(f"Fant ikke beholdningsfil: {path}")

    holdings = list(_parse_csv(path.read_text(encoding="utf-8")))
    if not holdings:
        raise ValueError(f"{path} inneholder ingen beholdninger")

    return HoldingsSnapshot(
        fund_id=fund.id,
        retrieved=date.today(),
        source=f"manual:{rel}",
        holdings=holdings,
        as_of=_as_of_from_spec(spec),
        cash_pct=spec.get("cash_pct"),
        equity_pct=spec.get("equity_pct"),
    )


def _parse_csv(text: str) -> Iterable[Holding]:
    lines = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    for row in csv.DictReader(io.StringIO("\n".join(lines))):
        name = (row.get("name") or "").strip()
        raw_weight = (row.get("weight_pct") or "").strip().replace("%", "").replace(",", ".")
        if not name or not raw_weight:
            continue
        yield Holding(name=name, weight_pct=float(raw_weight))


def _from_nordnet(fund: FundConfig, spec: dict[str, Any]) -> HoldingsSnapshot:
    instrument_id = resolve_instrument_id(fund)
    if not instrument_id:
        raise ValueError(
            "Fant ingen Nordnet instrument-id, verken i konfigen eller ved oppslag "
            f"på ISIN {fund.isin}."
        )

    session = _session()
    last_error: Optional[str] = None
    for template in NORDNET_ENDPOINTS:
        url = template.format(id=instrument_id)
        try:
            response = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            last_error = f"{url}: {exc}"
            continue
        if response.status_code != 200:
            last_error = f"{url}: HTTP {response.status_code}"
            continue
        try:
            payload = response.json()
        except ValueError:
            last_error = f"{url}: svarte ikke JSON"
            continue

        holdings = list(_extract_holdings(payload))
        if holdings:
            log.info("Hentet %d beholdninger fra %s", len(holdings), url)
            return HoldingsSnapshot(
                fund_id=fund.id,
                retrieved=date.today(),
                source=f"nordnet:{url}",
                holdings=holdings,
                as_of=_find_as_of(payload),
                cash_pct=_find_pct(payload, ("cash", "cash_weight", "cashPercentage")),
                equity_pct=_find_pct(payload, ("equity", "equity_weight", "stockPercentage")),
            )
        last_error = f"{url}: JSON uten gjenkjennelige beholdninger"

    raise RuntimeError(f"Ingen Nordnet-endepunkter ga beholdninger. Siste feil: {last_error}")


# Nordnet's public fund URLs carry a text slug, not the numeric instrument id
# their API wants. Looking it up by ISIN keeps the config free of magic numbers
# and works for any fund without the user having to go hunting.
NORDNET_SEARCH_ENDPOINTS = [
    "https://www.nordnet.no/api/2/instrument_search/query/fundlist?apply_filters=isin%3D{isin}",
    "https://www.nordnet.no/api/2/main_search?query={isin}&search_space=ALL&limit=10",
    "https://www.nordnet.no/api/2/instruments?query={isin}",
]

_ID_KEYS = ("instrument_id", "instrumentId", "identifier", "id")


def resolve_instrument_id(fund: FundConfig) -> Optional[str]:
    """The configured instrument id, or one looked up from the fund's ISIN."""
    configured = (fund.holdings_source or {}).get("instrument_id")
    if configured:
        return str(configured)
    found = lookup_instrument_id(fund.isin)
    if found:
        log.info("Slo opp Nordnet instrument-id %s for ISIN %s", found, fund.isin)
    return found


def lookup_instrument_id(isin: str) -> Optional[str]:
    for _, found, _ in _lookup_attempts(isin):
        if found:
            return found
    return None


def probe_instrument_lookup(isin: str) -> list[tuple[str, str]]:
    """Report why the ISIN lookup did or did not find an id.

    Worth surfacing rather than swallowing: a 403 means Nordnet is blocking the
    runner and no amount of retrying will help, while a 200 with no match means
    the response shape changed and the parser needs updating. Those call for
    completely different fixes.
    """
    return [(url, outcome) for url, _, outcome in _lookup_attempts(isin)]


def _lookup_attempts(isin: str):
    """Yield ``(url, found_id, human_readable_outcome)`` for each candidate."""
    session = _session()
    for template in NORDNET_SEARCH_ENDPOINTS:
        url = template.format(isin=isin)
        try:
            response = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as exc:
            yield url, None, f"FEIL: {exc}"
            continue
        if response.status_code != 200:
            yield url, None, f"HTTP {response.status_code}"
            continue
        try:
            payload = response.json()
        except ValueError:
            yield url, None, f"HTTP 200 men ikke JSON ({len(response.content)} bytes)"
            continue
        found = _find_instrument_id(payload, isin)
        outcome = (
            f"HTTP 200, instrument-id {found}"
            if found
            else f"HTTP 200, {len(response.content)} bytes, ingen post med ISIN {isin}"
        )
        yield url, found, outcome


def _find_instrument_id(node: Any, isin: str) -> Optional[str]:
    """Find the id on whichever object actually carries our ISIN.

    Search responses contain several instruments; matching on the ISIN rather
    than grabbing the first id avoids silently tracking the wrong share class.
    """
    if isinstance(node, dict):
        if str(node.get("isin") or node.get("isin_code") or "").upper() == isin.upper():
            for key in _ID_KEYS:
                value = node.get(key)
                if isinstance(value, (int, str)) and str(value).strip():
                    return str(value)
        for child in node.values():
            found = _find_instrument_id(child, isin)
            if found:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _find_instrument_id(child, isin)
            if found:
                return found
    return None


# Only these are worth printing out of the hundreds of /api/ paths a page
# like this embeds - the rest is Nordnet's whole-app typed API client
# (account opening, pensions, messages...), not anything about this stock.
PRICE_PATH_KEYWORDS = (
    "chart", "quote", "price", "instrument", "history",
    "historical", "graph", "trade", "market", "tick",
)


def probe_nordnet_price_page(url: str) -> str:
    """Fetch a Nordnet stock page and report what it reveals about a price API.

    Purely investigative, same spirit as probe_instrument_lookup: Nordnet's
    charts are drawn by front-end JS calling an undocumented API, and the
    only way to find that API's shape is to look at what a real page actually
    contains - embedded JSON, an instrument id, any /api/ path literal in the
    HTML - rather than guess at URLs the way the Stooq fallback had to.

    The page bundles its whole-app typed API client (account opening,
    pensions, messages...) alongside anything instrument-specific, so the raw
    path list is filtered down to what could plausibly serve a price chart;
    an unfiltered dump both buries the signal and risks tripping CI log
    truncation on an output that large.
    """
    session = _session()
    session.headers["Accept"] = "text/html,application/xhtml+xml"
    try:
        response = session.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        return f"{url}: FEIL - {exc}"

    body = response.text
    lines = [f"{url}: HTTP {response.status_code}, {len(response.content)} bytes"]

    if response.status_code != 200:
        lines.append(f"Utdrag: {body[:300]!r}")
        return "\n".join(lines)

    # Trailing backslash is a JSON-in-JSON escaping artefact (an embedded,
    # double-encoded blob), not part of the real path - stripped for clarity.
    all_paths = {m.rstrip("\\") for m in re.findall(r'"(/api/[^"\s]+)"', body)}
    relevant = sorted(p for p in all_paths if any(k in p.lower() for k in PRICE_PATH_KEYWORDS))
    lines.append(f"Fant {len(all_paths)} /api/-stier totalt, {len(relevant)} ser "
                 f"pris/instrument-relevante ut:")
    lines.extend(f"  {p}" for p in relevant[:25])

    ids = sorted(set(re.findall(r'"identifier"\s*:\s*"([^"]+)"', body)))[:10]
    if ids:
        lines.append(f"Fant identifier-felt: {', '.join(ids)}")

    next_data = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', body, re.DOTALL
    )
    if next_data:
        blob = next_data.group(1)
        lines.append(f"__NEXT_DATA__ funnet, {len(blob)} tegn.")
        for keyword in ("instrument", "identifier", "symbol", "chartData"):
            hit = re.search(rf'"[^"]*{keyword}[^"]*"\s*:\s*("[^"]{{1,80}}"|\{{|\[)', blob, re.IGNORECASE)
            if hit:
                start = hit.start()
                lines.append(f"  rundt {keyword!r}: {blob[start:start + 150]!r}")
    elif len(body) < 5000:
        lines.append(f"Siden er liten ({len(body)} tegn) - trolig et JS-skall uten "
                      f"server-rendert data. Utdrag: {body[:300]!r}")

    return "\n".join(lines)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Language": "nb-NO,nb;q=0.9,en;q=0.8",
            "Referer": "https://www.nordnet.no/",
        }
    )
    return session


def _extract_holdings(payload: Any) -> Iterable[Holding]:
    """Walk an unknown JSON shape and pull out anything name+weight shaped."""
    for node in _walk_lists(payload):
        found = []
        for item in node:
            if not isinstance(item, dict):
                break
            name = _first(item, NAME_KEYS)
            weight = _first(item, WEIGHT_KEYS)
            if not isinstance(name, str) or not isinstance(weight, (int, float)):
                break
            found.append(Holding(name=name.strip(), weight_pct=float(weight)))
        # A holdings list has several entries and weights that look like percent.
        if len(found) >= 5 and 20.0 <= sum(h.weight_pct for h in found) <= 105.0:
            return found
    return []


def _walk_lists(node: Any) -> Iterable[list]:
    if isinstance(node, list):
        yield node
        for child in node:
            yield from _walk_lists(child)
    elif isinstance(node, dict):
        for child in node.values():
            yield from _walk_lists(child)


def _first(item: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in item:
            return item[key]
    return None


def _find_as_of(payload: Any) -> Optional[date]:
    for key in ("as_of_date", "portfolio_date", "holdings_date", "date", "asOfDate"):
        value = _deep_get(payload, key)
        if isinstance(value, str):
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S"):
                try:
                    return datetime.strptime(value[: len(fmt) + 2].strip(), fmt).date()
                except ValueError:
                    continue
    return None


def _find_pct(payload: Any, keys: tuple[str, ...]) -> Optional[float]:
    for key in keys:
        value = _deep_get(payload, key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _deep_get(node: Any, key: str) -> Any:
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for child in node.values():
            found = _deep_get(child, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for child in node:
            found = _deep_get(child, key)
            if found is not None:
                return found
    return None


def probe_all(fund: FundConfig) -> list[tuple[str, list[tuple[str, str]]]]:
    """Try every configured remote source and report exactly what answered.

    This exists because none of these endpoints are documented or promised. The
    only honest way to know which one works is to call it from a machine with
    real network access and read the result.
    """
    from . import morningstar

    spec = fund.holdings_source or {}
    out: list[tuple[str, list[tuple[str, str]]]] = []

    sec_id = spec.get("morningstar_secid")
    out.append((
        "Morningstar",
        morningstar.probe(str(sec_id)) if sec_id
        else [("-", "morningstar_secid ikke satt i konfigen")],
    ))

    configured = (fund.holdings_source or {}).get("instrument_id")
    if configured:
        out.append(("Nordnet", probe_nordnet(str(configured))))
        return out

    attempts = probe_instrument_lookup(fund.isin)
    found = next(
        (
            outcome.split()[-1]
            for _, outcome in attempts
            if "instrument-id" in outcome
        ),
        None,
    )
    results = [(f"ISIN-oppslag: {url}", outcome) for url, outcome in attempts]
    if found:
        results.extend(probe_nordnet(found))
    out.append(("Nordnet", results))
    return out


def probe_nordnet(instrument_id: str) -> list[tuple[str, str]]:
    """Try every candidate Nordnet endpoint and report what came back."""
    session = _session()
    results: list[tuple[str, str]] = []
    for template in NORDNET_ENDPOINTS:
        url = template.format(id=instrument_id)
        try:
            response = session.get(url, timeout=TIMEOUT)
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
                found = list(_extract_holdings(payload))
                summary += f", JSON, {len(found)} beholdninger gjenkjent"
                if found:
                    preview = ", ".join(f"{h.name} {h.weight_pct}" for h in found[:3])
                    summary += f" ({preview} ...)"
        results.append((url, summary))
    return results


def _as_of_from_spec(spec: dict[str, Any]) -> Optional[date]:
    value = spec.get("as_of")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


_REMOTE_LOADERS = {
    "morningstar": _from_morningstar,
    "nordnet": _from_nordnet,
}


def _resolve(rel: str) -> Path:
    path = Path(rel)
    return path if path.is_absolute() else REPO_ROOT / path


def save_snapshot(snapshot: HoldingsSnapshot, directory: Path) -> Path:
    """Persist a snapshot so we can see when the composition actually changed."""
    import json

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{snapshot.retrieved.isoformat()}.json"
    path.write_text(
        json.dumps(
            {
                "fund_id": snapshot.fund_id,
                "retrieved": snapshot.retrieved.isoformat(),
                "as_of": snapshot.as_of.isoformat() if snapshot.as_of else None,
                "source": snapshot.source,
                "cash_pct": snapshot.cash_pct,
                "equity_pct": snapshot.equity_pct,
                "holdings": [
                    {"name": h.name, "weight_pct": h.weight_pct} for h in snapshot.holdings
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path
