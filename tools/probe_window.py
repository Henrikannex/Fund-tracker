"""Why does Yahoo serve a European close one evening and not the next morning?

On 4 August at 22:15 the estimate ran with full coverage. On 5 August at 06:17
the same code, asking for the same day, got nothing for all twenty European
tickers - and got nothing again when it asked for them one at a time. So this
is not throttling, and the tickers are not delisted. Something about *when* we
ask, or about the window we ask for, decides whether the 4 August bar exists.

This script asks the same question several ways and prints what came back. It
is a throwaway: delete it once the answer is in the code.

Run it on a runner, not locally - Yahoo is not reachable from the dev sandbox.
"""

from __future__ import annotations

import sys
from datetime import date, timedelta

import pandas as pd
import yfinance as yf

# One from each venue that failed, plus a US name that worked as a control.
TICKERS = ["SAP.DE", "ASML.AS", "TELIA.ST", "ATEA.OL", "SMSN.IL", "MSFT"]
TARGET = date(2026, 8, 4)


def show(label: str, frame: pd.DataFrame) -> None:
    print(f"\n=== {label} ===")
    if frame is None or frame.empty:
        print("  (tomt)")
        return
    idx = pd.to_datetime(frame.index).tz_localize(None).normalize()
    frame = frame.copy()
    frame.index = idx
    print(f"  siste indeks: {idx.max().date()}   rader: {len(frame)}")
    tail = frame.tail(3)
    for when, row in tail.iterrows():
        cells = ", ".join(
            f"{c}={'-' if pd.isna(v) else format(v, '.2f')}" for c, v in row.items()
        )
        print(f"  {when.date()}  {cells}")
    if pd.Timestamp(TARGET) in idx:
        row = frame.loc[pd.Timestamp(TARGET)]
        missing = [c for c in frame.columns if pd.isna(row[c])]
        print(f"  {TARGET} finnes. Mangler: {missing or 'ingen'}")
    else:
        print(f"  {TARGET} finnes IKKE i indeksen.")


def closes(raw: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    if isinstance(raw.columns, pd.MultiIndex):
        return raw["Close"]
    return raw[["Close"]].rename(columns={"Close": tickers[0]})


def batch(label: str, tickers: list[str], start: date, end: date) -> None:
    raw = yf.download(
        tickers=tickers, start=start, end=end, interval="1d",
        auto_adjust=True, progress=False, threads=True, group_by="column",
    )
    show(label, closes(raw, tickers))


def main() -> int:
    print(f"Kjører {pd.Timestamp.utcnow()} UTC. Mål: {TARGET}")

    # A: exactly what the failing run asked for (end is exclusive, so +1 day).
    batch("A  end=TARGET+1 (som i dag)", TICKERS, TARGET - timedelta(days=10),
          TARGET + timedelta(days=1))

    # B: ask past the target. If the 4 August bar appears here but not in A,
    # the window edge is the whole bug and the fix is one line.
    batch("B  end=TARGET+3", TICKERS, TARGET - timedelta(days=10),
          TARGET + timedelta(days=3))

    # C: no explicit window at all.
    raw = yf.download(
        tickers=TICKERS, period="1mo", interval="1d", auto_adjust=True,
        progress=False, threads=True, group_by="column",
    )
    show("C  period=1mo (ingen datoer)", closes(raw, TICKERS))

    # D: the per-ticker history endpoint, which is a different code path in
    # yfinance than download() and may not apply the same window filtering.
    print("\n=== D  Ticker.history(period='1mo') per ticker ===")
    for t in TICKERS:
        try:
            hist = yf.Ticker(t).history(period="1mo", auto_adjust=True)
        except Exception as exc:  # noqa: BLE001 - diagnostics, print anything
            print(f"  {t:12s} FEIL: {exc}")
            continue
        if hist.empty:
            print(f"  {t:12s} tomt")
            continue
        idx = pd.to_datetime(hist.index).tz_localize(None).normalize()
        has = pd.Timestamp(TARGET) in idx
        last = idx.max().date()
        val = hist["Close"].iloc[list(idx).index(pd.Timestamp(TARGET))] if has else None
        shown = f"{val:.2f}" if val is not None else "-"
        print(f"  {t:12s} siste={last}  {TARGET}={'JA ' + shown if has else 'NEI'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
