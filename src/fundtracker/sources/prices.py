"""Daily closing prices and FX rates, from Yahoo Finance via yfinance."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import pandas as pd

log = logging.getLogger(__name__)

# How many calendar days of price history to pull beyond the window we need,
# so that a long holiday stretch still leaves us a previous close to compare to.
LOOKBACK_PAD_DAYS = 10


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


def closing_prices(tickers: list[str], start: date, end: date) -> pd.DataFrame:
    """Closing prices in each ticker's own listing currency."""
    return _download(sorted(set(tickers)), start, end)


def fx_to_base(currencies: list[str], base: str, start: date, end: date) -> pd.DataFrame:
    """Units of ``base`` per 1 unit of each currency, one column per currency.

    The base currency itself is a constant 1.0 column, which lets the estimator
    treat domestic and foreign holdings with the same code path.
    """
    base = base.upper()
    wanted = sorted({c.upper() for c in currencies})
    foreign = [c for c in wanted if c != base]

    pairs = {c: f"{c}{base}=X" for c in foreign}
    frame = _download(list(pairs.values()), start, end) if pairs else pd.DataFrame()

    out = pd.DataFrame(index=frame.index if not frame.empty else None)
    for cur, pair in pairs.items():
        if pair in frame.columns:
            out[cur] = frame[pair]
        else:
            log.warning("No FX series for %s -> %s", cur, base)
            out[cur] = pd.NA

    if base in wanted:
        if out.empty:
            out = pd.DataFrame(index=pd.DatetimeIndex([], name="Date"))
        out[base] = 1.0
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
