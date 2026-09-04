"""Daily closing prices and FX rates, from Yahoo Finance via yfinance."""

from __future__ import annotations

import io
import logging
import re
import time
from datetime import date, timedelta

import pandas as pd
import requests

log = logging.getLogger(__name__)

# How many calendar days of price history to pull beyond the window we need,
# so that a long holiday stretch still leaves us a previous close to compare to.
LOOKBACK_PAD_DAYS = 10

# Pause between the individual retries in closing_prices(). Tickers vanish from
# a batch because Yahoo throttled the lookup, so hammering it again the same
# second reproduces the failure instead of fixing it.
RETRY_PAUSE_SECONDS = 2.0

# Pause between per-ticker Stooq lookups, same reasoning as RETRY_PAUSE_SECONDS.
STOOQ_PAUSE_SECONDS = 1.0

# Yahoo's exchange suffix -> candidate Stooq suffixes to try, in that order.
# Stooq's suffix scheme is undocumented outside their own site and only partly
# overlaps Yahoo's, so this is a short list of educated guesses per exchange
# rather than a verified mapping. A wrong guess costs nothing: it only "wins"
# by returning a real row for the exact date we asked about, so it cannot
# silently substitute the wrong instrument's price the way a bad ticker
# override could - it just stays missing, same as with no fallback at all.
STOOQ_SUFFIX_CANDIDATES: dict[str, list[str]] = {
    "": [".us"],       # bare Yahoo tickers are US-listed
    ".DE": [".de"],
    ".PA": [".fr"],
    ".AS": [".nl"],
    ".ST": [".st"],
    ".OL": [".ol"],
    ".HE": [".fi"],
    ".SW": [".sw"],
    ".L": [".uk"],
    ".IL": [".uk"],    # London-quoted GDRs
    ".LS": [".pt"],
}

# Stooq's CSV export has been seen to block the default python-requests
# user agent. A plain browser-shaped header is enough to avoid that.
STOOQ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}


def _download(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Return a date-indexed frame of closes, one column per ticker.

    Prices are split *and* dividend adjusted. That is deliberate: when a holding
    goes ex-dividend its price drops, but the fund receives the cash, so the
    fund's NAV barely moves. Using total-return prices keeps our estimate from
    showing a phantom loss on every ex-dividend date.
    """
    import yfinance as yf

    if not tickers:
        return pd.DataFrame()

    raw = yf.download(
        tickers=tickers,
        start=start - timedelta(days=LOOKBACK_PAD_DAYS),
        end=end + timedelta(days=1),  # yfinance treats end as exclusive
        interval="1d",
        auto_adjust=True,
        progress=False,
        threads=True,
        group_by="column",
    )
    if raw is None or raw.empty:
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        closes = raw["Close"]
    else:
        # A single ticker comes back with flat columns.
        closes = raw[["Close"]].rename(columns={"Close": tickers[0]})

    closes = closes.copy()
    closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
    # Keep only columns we asked for, and add empty ones for anything Yahoo
    # silently dropped so callers can detect the gap rather than KeyError.
    for t in tickers:
        if t not in closes.columns:
            log.warning("Yahoo returned no data for ticker %s", t)
            closes[t] = pd.NA
    return closes[tickers].astype("float64")


def raw_closes(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Exactly what Yahoo answered, before any retry or fallback touches it.

    closing_prices() is deliberately forgiving: it re-asks per ticker and then
    tries Stooq, so by the time a caller sees the frame it can no longer tell
    what the source actually had. A diagnosis of "which listings is Yahoo
    missing, and when" needs the unforgiving version.
    """
    return _download(sorted(set(tickers)), start, end)


def closing_prices(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Closing prices in each ticker's own listing currency.

    Yahoo drops tickers from batch downloads now and then — Cisco vanished from
    one run and came back on the next. Nothing errors: yfinance fails to look up
    the ticker's timezone, gives up on it, and the column comes back empty for
    the most recent day. That reads exactly like a market that was closed, so
    the freshness gate stops the run and no mail goes out over a hiccup.

    Anything missing its last row is therefore fetched again one at a time, with
    a pause in between so the retry does not walk into the same throttling.

    This does not rescue every gap, and it is worth knowing which. On the
    morning of 5 August all twenty European tickers were missing their 4 August
    close, and the retries changed nothing: Yahoo had built the 4 August bar and
    left the close null. Asking again, asking with a wider window, and asking
    the per-ticker history endpoint all returned the same null. When the source
    has no number, the gate is the thing that saves us, not this.
    """
    wanted = sorted(set(tickers))
    frame = _download(wanted, start, end)
    if frame.empty:
        return frame

    latest = frame.index.max()
    missing = [t for t in wanted if pd.isna(frame.at[latest, t])]
    if not missing:
        return frame

    log.warning(
        "Yahoo ga ingen kurs for %s på %s; henter dem enkeltvis.",
        ", ".join(missing), latest.date(),
    )
    for ticker in missing:
        time.sleep(RETRY_PAUSE_SECONDS)
        retry = _download([ticker], start, end)
        if retry.empty or ticker not in retry.columns:
            continue
        # Only fill the holes, so a good batch value is never overwritten.
        frame[ticker] = frame[ticker].combine_first(retry[ticker])

    still = [t for t in missing if pd.isna(frame.at[latest, t])]
    if still:
        rescued = _stooq_fill(still, start, end, latest)
        for ticker, value in rescued.items():
            frame.at[latest, ticker] = value
        still = [t for t in still if t not in rescued]
        if rescued:
            log.info("Stooq reddet kurs for %s på %s", ", ".join(rescued), latest.date())

    if still:
        log.warning("Fortsatt uten kurs for %s etter nytt forsøk: %s",
                    latest.date(), ", ".join(still))
    return frame


def _stooq_candidates(ticker: str) -> list[str]:
    """Yahoo ticker -> plausible Stooq symbols, in the order to try them."""
    if "." not in ticker:
        return [f"{ticker}.us".lower()]
    root, _, suffix = ticker.rpartition(".")
    return [f"{root}{stooq_suffix}".lower()
            for stooq_suffix in STOOQ_SUFFIX_CANDIDATES.get(f".{suffix}".upper(), [])]


def _download_stooq_symbol(symbol: str, start: date, end: date) -> pd.DataFrame:
    """One symbol's daily closes from Stooq's CSV export, or an empty frame.

    Unlike Yahoo's auto_adjust=True prices, these are plain closes - not
    dividend-adjusted. That only matters on a holding's actual ex-dividend
    day, and only for a ticker Yahoo already failed to price; a slightly
    imperfect number there beats none at all.
    """
    url = f"https://stooq.com/q/d/l/?s={symbol}&d1={start:%Y%m%d}&d2={end:%Y%m%d}&i=d"
    try:
        response = requests.get(url, timeout=15, headers=STOOQ_HEADERS)
    except requests.RequestException as exc:
        log.info("Stooq svarte ikke for %s: %s", symbol, exc)
        return pd.DataFrame()
    if response.status_code != 200 or not response.text.startswith("Date,"):
        # Logged at INFO, not DEBUG: this is a best-effort fallback with an
        # unverified suffix table (see STOOQ_SUFFIX_CANDIDATES), so seeing
        # *why* a symbol did not resolve is how the table gets corrected
        # rather than staying a permanent silent no-op.
        log.info("Stooq ga ikke data for %s: HTTP %s, %r",
                  symbol, response.status_code, response.text[:120])
        return pd.DataFrame()
    try:
        parsed = pd.read_csv(io.StringIO(response.text), parse_dates=["Date"])
    except Exception as exc:  # noqa: BLE001 - a malformed export must not crash the run
        log.info("Kunne ikke lese Stooq-CSV for %s: %s", symbol, exc)
        return pd.DataFrame()
    if "Close" not in parsed.columns or parsed.empty:
        return pd.DataFrame()
    parsed = parsed.set_index("Date")[["Close"]]
    parsed.index = parsed.index.normalize()
    return parsed


def _stooq_fill(
    missing: list[str], start: date, end: date, target_date: pd.Timestamp
) -> dict[str, float]:
    """Best-effort Stooq closes, for tickers Yahoo could not provide at all."""
    found: dict[str, float] = {}
    for ticker in missing:
        for symbol in _stooq_candidates(ticker):
            time.sleep(STOOQ_PAUSE_SECONDS)
            frame = _download_stooq_symbol(symbol, start, end)
            if frame.empty or target_date not in frame.index:
                continue
            value = frame.at[target_date, "Close"]
            if pd.isna(value):
                continue
            found[ticker] = float(value)
            break
    return found


def fx_to_base(currencies: list[str], base: str, start: date, end: date) -> pd.DataFrame:
    """Units of ``base`` per 1 unit of each currency, one column per currency.

    The base currency itself is a constant 1.0 column, which lets the estimator
    treat domestic and foreign holdings with the same code path.
    """
    base = base.upper()
    wanted = sorted({c.upper() for c in currencies})
    foreign = [c for c in wanted if c != base]

    pairs = {c: f"{c}{base}=X" for c in foreign}
    # Same retry as for equities. A dropped FX pair is worse than a dropped
    # stock: it silently zeroes the currency effect for every holding in that
    # currency, and USD alone is most of the fund.
    frame = closing_prices(list(pairs.values()), start, end) if pairs else pd.DataFrame()

    series: dict[str, pd.Series] = {}
    missing: list[str] = []
    latest = frame.index.max() if not frame.empty else None
    for cur, pair in pairs.items():
        column = frame[pair] if pair in frame.columns else None
        if column is None or not column.notna().any():
            missing.append(cur)
            continue
        series[cur] = column
        # A pair that answers for history but not for the day being priced is
        # the more common failure, and the more dangerous one: the column looks
        # usable, so the holding silently keeps yesterday's rate and its
        # currency leg reads 0,00 % without anything saying so.
        if latest is not None and pd.isna(column.get(latest, pd.NA)):
            missing.append(cur)

    if missing:
        for cur, crossed in _cross_via_usd(missing, base, start, end).items():
            # combine_first, not assignment: where the direct pair did answer it
            # is the better number, and the cross only fills its holes.
            series[cur] = series[cur].combine_first(crossed) if cur in series else crossed

    index = frame.index
    for column in series.values():
        index = index.union(column.index)

    out = pd.DataFrame(index=index)
    for cur in foreign:
        if cur in series:
            out[cur] = series[cur].reindex(index)
        else:
            log.warning("No FX series for %s -> %s", cur, base)
            out[cur] = pd.NA

    if base in wanted:
        if out.empty:
            out = pd.DataFrame(index=pd.DatetimeIndex([], name="Date"))
        out[base] = 1.0
    return out


def _cross_via_usd(
    currencies: list[str], base: str, start: date, end: date
) -> dict[str, pd.Series]:
    """Build the missing ``CUR -> base`` rates out of their two USD legs.

    Yahoo quotes USDKRW=X and USDSEK=X but not KRWSEK=X — the exotic crosses
    are simply not published, and asking for one returns nothing rather than an
    error. Without this the Korean holdings in a Swedish fund get dropped for
    want of a quote, which is a sixth of that fund silently normalised away.

    Both legs are "units of X per one USD", so the cross is their ratio. Only
    reached when the direct pair came back empty, so a fund whose currencies
    Yahoo does quote directly is unaffected.
    """
    crossable = [c for c in currencies if c != "USD"]
    if not crossable:
        # USD -> base failing is not something a USD detour can rescue.
        return {}

    legs = {c: f"USD{c}=X" for c in crossable}
    base_pair = None if base == "USD" else f"USD{base}=X"
    frame = closing_prices(
        sorted({*legs.values(), *([base_pair] if base_pair else [])}), start, end
    )

    base_leg = None
    if base_pair is not None:
        if base_pair not in frame.columns or not frame[base_pair].notna().any():
            log.warning(
                "Ingen USD-kurs for basisvalutaen %s, så ingen kryss er mulige.", base
            )
            return {}
        base_leg = frame[base_pair]

    out: dict[str, pd.Series] = {}
    for cur, pair in legs.items():
        if pair not in frame.columns or not frame[pair].notna().any():
            continue
        # A zero would divide into infinity and poison the whole column, so it
        # is treated as the missing observation it really is.
        per_usd = frame[pair].replace(0, pd.NA)
        out[cur] = (base_leg / per_usd) if base_leg is not None else 1.0 / per_usd
        log.info("Krysset %s -> %s via USD.", cur, base)
    return out


def align(
    frame: pd.DataFrame, max_stale_days: int = 5
) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Forward-fill across non-trading days, and record what was filled.

    Markets close on different days. A Tokyo holiday does not mean Sony's
    contribution is unknown — it means Sony's price did not move, which is
    exactly what the fund's NAV will reflect too. Forward-filling reproduces
    that.

    But forward-filling is also how a nowcast quietly lies. Run this before the
    US close and every American holding gets yesterday's price carried forward,
    producing a confident-looking 0.00 % for half the portfolio. So the mask of
    which cells were *actually observed* is returned alongside the values, and
    callers are expected to check it rather than trust the numbers blindly.

    Returns ``(values, staleness_days, observed)``.
    """
    if frame.empty:
        return frame, pd.Series(dtype="int64"), frame

    ordered = frame.sort_index()
    observed = ordered.notna()
    filled = ordered.ffill(limit=max_stale_days)
    last_real = ordered.apply(lambda col: col.last_valid_index())
    latest = ordered.index.max()
    staleness = last_real.apply(
        lambda d: (latest - d).days if pd.notna(d) else 10**6
    ).astype("int64")
    return filled, staleness, observed


# Same fields yfinance itself looks for in Yahoo's own JSON, reused here so a
# hit means "this is the same kind of number our pipeline already trusts."
YAHOO_QUOTE_PRICE_FIELDS = (
    "regularMarketPrice", "regularMarketPreviousClose",
    "previousClose", "regularMarketTime",
)


def probe_yahoo_quote_page(ticker: str) -> str:
    """Fetch a Yahoo Finance quote page and report what price data it embeds.

    yfinance's own download endpoint sometimes has a null close for the most
    recent day on European/Nordic tickers even long after that market shut
    (see closing_prices' docstring - it happened on both 4 and 5 August for
    the same ~20 names). The public quote page might be fed by a faster or
    more current pipeline for at least a live/previous-close number, so this
    checks what is actually embedded there rather than assuming either way -
    same reasoning as probe_nordnet_price_page for a different source.
    """
    url = f"https://finance.yahoo.com/quote/{ticker}/"
    try:
        response = requests.get(url, timeout=15, headers=STOOQ_HEADERS)
    except requests.RequestException as exc:
        return f"{url}: FEIL - {exc}"

    body = response.text
    lines = [f"{url}: HTTP {response.status_code}, {len(response.content)} bytes"]
    if response.status_code != 200:
        lines.append(f"Utdrag: {body[:300]!r}")
        return "\n".join(lines)

    found_any = False
    for key in YAHOO_QUOTE_PRICE_FIELDS:
        hit = re.search(rf'"{key}"\s*:\s*(\{{[^}}]*\}}|"[^"]*"|[0-9.eE+-]+)', body)
        if hit:
            found_any = True
            lines.append(f"  {key}: {hit.group(1)[:150]}")

    if not found_any:
        lines.append("Ingen kjente pris-felt funnet i rå HTML - trolig lastes "
                      "prisen inn separat (XHR) etter at siden vises, ikke "
                      "server-rendret inn i denne responsen.")

    return "\n".join(lines)
