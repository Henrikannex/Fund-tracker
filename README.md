# Fund tracker

Estimates a fund's daily return the evening before the fund publishes it. Nordnet shows
yesterday's return late the following day, but it does show the holdings. Pricing those
holdings in their own listing currencies gives the number about 24 hours earlier.

```
return = mean NOK return of priced holdings × (1 − cash weight) − daily management fee

per holding: (price_t / price_t−1) × (fx_t / fx_t−1) − 1
```

The FX term matters as much as the price term. The fund reports in NOK but owns Microsoft
in USD, Adyen in EUR, Ericsson in SEK and Sony in JPY, and USDNOK can move 0.7% in a day.
Holdings are priced on a specific listing, not a ticker: STMicroelectronics in Paris and
in New York track each other closely but carry different currency exposure.

## Accuracy

Measured against 55 days of published NAV, May to July 2026:

| Measure | Result |
|---|---|
| Mean absolute error | 0.63 pp |
| Systematic bias | −0.15 pp |
| Correct direction | 87.3% of days |
| Correlation with NAV | 0.842 |

Close enough to know how the day went, not close enough to trade on.

## Stale prices

Exchanges close at different times, so the price table has gaps. Gaps are carried forward,
because that is what the fund does in its own NAV. Run before the US close, though, and
every US position contributes 0.00% while the headline number still looks normal. The
system tracks which prices were real observations, reports the share of the portfolio that
was not, and withholds the estimate when that share exceeds 5%. Holdings without a mapped
ticker are reported rather than dropped.

## What the backtest settled

The backtest groups error by the age of the holdings snapshot, which separates model error
from portfolio drift. Four assumptions were resolved with data rather than argument:

- **Pricing day.** Same day. A one-day lag gives 1.69 pp error and negative correlation.
- **FX hedging.** Inconclusive: 0.632 pp with the FX term, 0.612 pp without. The term stays,
  which is correct for an unhedged NOK fund.
- **Coverage.** 25 holdings at 79.8% of the fund gave 0.632 pp; all 63 at 98.5% gave 0.639 pp.
  The missing tail was never the explanation for the error.
- **Refresh rate.** Bias grows about 0.05 pp per month of snapshot age, so refreshing
  holdings twice a year is enough.

## The code

5,245 lines of Python: 3,883 across 12 modules, and 1,362 in 11 test suites holding
105 tests. Four source integrations: manual CSV holdings, Morningstar, Nordnet, and
Yahoo for prices and FX. Four GitHub Actions workflows cover the scheduled estimate,
the morning catch-up, diagnostics and tests.

The daily job runs twice on weekdays so it lands at 22:15 Oslo time under both US
daylight-saving regimes; the second run exits if the day is already logged. One YAML file
per fund, with the listing currency required for every ticker.

The day a run is about is anchored eight hours back from the start time rather than
read off the clock, because GitHub has started these runs up to six hours late and the
tail of the retry window then crossed midnight UTC into a day nothing had traded. When
a whole evening passes without complete prices — Yahoo has been missing every European
close for a full week at a time — a morning job prices yesterday and sends it then, and
says so in a mail of its own if even that fails. A withheld estimate and a broken job
look the same in an inbox, which is how a week of silence went unnoticed.

```bash
pip install -r requirements.txt && export PYTHONPATH=src

python -m fundtracker.cli snapshot dnb-teknologi-a
python -m fundtracker.cli estimate dnb-teknologi-a
python -m fundtracker.cli backtest dnb-teknologi-a --days 250
```

## Limits

Three assumptions carry the error: the unpriced tail behaves like the priced holdings,
cash returns nothing over a day, and the manager has not traded since the snapshot. Every
estimate reports `coverage_pct` so the first one is visible. A correlation of 0.842 leaves
roughly 30% of daily variation unexplained.

A per-holding regression intended to identify mis-stated weights was built and then
removed: technology names move together, and 57 observations cannot identify 63 weights,
so it returned implied weights below zero.

Method notes, source evaluation and the development log are in
[docs/METHOD.no.md](docs/METHOD.no.md), in Norwegian.
