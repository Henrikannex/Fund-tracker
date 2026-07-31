"""Command line entry point."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import date, datetime, timedelta

import pandas as pd

from . import backtest as backtest_mod
from . import notify, report
from . import validate as validate_mod
from .config import list_funds, load_fund
from .estimate import estimate_return, priced_tickers, resolve_holding
from .sources import holdings as holdings_mod
from .sources import nav as nav_source
from .sources import prices as price_source

log = logging.getLogger("fundtracker")

# Enough history for a previous close even across a long holiday.
ESTIMATE_LOOKBACK_DAYS = 21


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fundtracker", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p_est = sub.add_parser("estimate", help="Estimer dagens avkastning")
    p_est.add_argument("fund")
    p_est.add_argument("--date", help="YYYY-MM-DD, standard er i dag")
    p_est.add_argument("--email", action="store_true", help="Send resultatet på e-post")
    p_est.add_argument("--save", action="store_true", help="Logg estimatet til data/estimates")
    p_est.add_argument(
        "--latest",
        action="store_true",
        help="Bruk siste dag det finnes fullstendige kurser for, i stedet for å "
        "avbryte fordi dagens børsdag ikke er ferdig.",
    )
    p_est.add_argument(
        "--max-stale",
        type=float,
        default=5.0,
        help="Avbryt hvis mer enn denne andelen av fondet mangler kurs for dagen. "
        "Uten dette ville en kjøring før børsslutt gitt et selvsikkert, galt tall.",
    )
    p_est.add_argument(
        "--allow-stale",
        action="store_true",
        help="Send estimatet selv om kurser mangler. Kun for feilsøking.",
    )
    p_est.add_argument(
        "--skip-if-logged",
        action="store_true",
        help="Avslutt uten å gjøre noe hvis datoen allerede er logget. "
        "Lar workflowen kjøre på flere klokkeslett for å dekke sommer- og vintertid.",
    )

    p_back = sub.add_parser("backtest", help="Mål modellen mot faktisk NAV")
    p_back.add_argument("fund")
    p_back.add_argument("--days", type=int, default=250)

    p_snap = sub.add_parser("snapshot", help="Hent og lagre beholdninger")
    p_snap.add_argument("fund")

    p_res = sub.add_parser("resolve", help="Vis hvilke beholdninger som mangler ticker")
    p_res.add_argument("fund")

    p_probe = sub.add_parser("probe", help="Test hvilke Nordnet-endepunkter som svarer")
    p_probe.add_argument("fund")

    p_val = sub.add_parser(
        "validate", help="Mål modellen mot fondets publiserte periodeavkastning"
    )
    p_val.add_argument("fund")

    sub.add_parser("funds", help="List konfigurerte fond")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if args.verbose:
        # Only our own loggers. yfinance at DEBUG emits several hundred lines per
        # ticker, which buries the diagnostics this flag exists to surface.
        logging.getLogger("fundtracker").setLevel(logging.DEBUG)

    handlers = {
        "estimate": cmd_estimate,
        "backtest": cmd_backtest,
        "validate": cmd_validate,
        "snapshot": cmd_snapshot,
        "resolve": cmd_resolve,
        "probe": cmd_probe,
        "funds": cmd_funds,
    }
    return handlers[args.command](args)


def cmd_funds(_args) -> int:
    for fund_id in list_funds():
        print(fund_id)
    return 0


def cmd_snapshot(args) -> int:
    fund = load_fund(args.fund)
    snapshot = holdings_mod.load_holdings(fund)
    path = holdings_mod.save_snapshot(snapshot, fund.snapshot_dir())
    print(
        f"{len(snapshot.holdings)} beholdninger, sum {snapshot.total_weight_pct:.2f} %, "
        f"kilde {snapshot.source}"
    )
    print(f"Lagret: {path}")
    return 0


def cmd_resolve(args) -> int:
    fund = load_fund(args.fund)
    snapshot = holdings_mod.load_holdings(fund)
    mapped, unmapped = 0.0, []
    for holding in snapshot.holdings:
        if fund.is_ignored(holding.name):
            continue
        mapping = resolve_holding(fund, holding)
        if mapping:
            mapped += holding.weight_pct
            origin = "konfig" if fund.resolve(holding.name) else "fra kilden"
            print(f"  OK      {holding.name:<30} {holding.weight_pct:>6.2f} %  "
                  f"-> {mapping.ticker} ({mapping.currency}) [{origin}]")
        else:
            unmapped.append(holding)
            print(f"  MANGLER {holding.name:<30} {holding.weight_pct:>6.2f} %")
    print()
    print(f"Kartlagt {mapped:.2f} % av fondet, {len(unmapped)} navn uten ticker.")
    return 1 if unmapped else 0


def cmd_probe(args) -> int:
    fund = load_fund(args.fund)
    answered = False
    for source_name, results in holdings_mod.probe_all(fund):
        print(f"\n=== {source_name} ===")
        for url, summary in results:
            print(f"{url}\n    {summary}")
            answered = answered or "beholdninger" in summary

    print("\n=== NAV-historikk ===")
    for label, summary in nav_source.probe(fund):
        print(f"{label}\n    {summary}")

    if not answered:
        print(
            "\nIngen kilde ga beholdninger. Den manuelle CSV-fila brukes videre.",
            file=sys.stderr,
        )
    return 0


def _price_context(fund, snapshot, target: date):
    """Fetch and align everything needed to estimate a single day."""
    tickers, currencies = priced_tickers(fund, snapshot)
    start = target - timedelta(days=ESTIMATE_LOOKBACK_DAYS)
    raw_prices = price_source.closing_prices(tickers, start, target)
    raw_fx = price_source.fx_to_base(sorted(currencies), fund.currency, start, target)
    prices, staleness, observed = price_source.align(raw_prices)
    fx, _, _ = price_source.align(raw_fx)
    return prices, fx, staleness, observed


def cmd_estimate(args) -> int:
    fund = load_fund(args.fund)
    target = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else date.today()

    if getattr(args, "skip_if_logged", False) and _already_logged(fund, target):
        log.info("Estimat for %s er allerede logget; hopper over.", target)
        return 0

    snapshot = holdings_mod.load_holdings(fund)
    prices, fx, staleness, observed = _price_context(fund, snapshot, target)

    if args.verbose:
        with pd.option_context("display.max_columns", None, "display.width", 200):
            print("\nSiste kursrader:\n", prices.tail(3), "\n")
            print("Siste valutarader:\n", fx.tail(3), "\n")

    est = estimate_return(fund, snapshot, target, prices, fx, staleness, observed)

    if args.latest and est.stale_weight_pct > args.max_stale:
        est = _latest_complete(
            fund, snapshot, target, prices, fx, staleness, observed, args.max_stale
        ) or est

    print(report.to_text(est))

    if est.stale_weight_pct > args.max_stale and not args.allow_stale:
        print(
            f"\nAVBRUTT: {est.stale_weight_pct:.1f} % av fondet mangler kurs for "
            f"{target}, over grensen på {args.max_stale:.1f} %. Estimatet ville vært "
            "misvisende, så det logges ikke og sendes ikke. Kjør etter at børsen "
            "har stengt, eller bruk --allow-stale.",
            file=sys.stderr,
        )
        return 3

    if args.save:
        _append_estimate(fund, est)
    if args.email:
        notify.send_email(report.subject(est), report.to_text(est), report.to_html(est))
    return 0


def cmd_validate(args) -> int:
    fund = load_fund(args.fund)
    snapshot = holdings_mod.load_holdings(fund)
    as_of = fund.benchmarks["as_of"]

    unhedged = validate_mod.run_validation(fund, snapshot, hedged=False)
    hedged = validate_mod.run_validation(fund, snapshot, hedged=True)
    print(validate_mod.format_comparison(unhedged, hedged, as_of))

    print()
    print(validate_mod.format_validation(
        hedged if fund.currency_hedged else unhedged, as_of
    ))

    week = (fund.benchmarks.get("periods") or {}).get("1u")
    if week is not None:
        print()
        print(validate_mod.format_window_sweep(fund, snapshot, as_of, float(week)))

    _explain_worst_day(fund, snapshot, as_of)
    return 0


def _explain_worst_day(fund, snapshot, as_of: date, span: int = 12) -> None:
    """Break down the largest single-day move near the anchor.

    One bad price in one holding can wreck a whole period, and it shows up as a
    day whose size no market move explains. Printing that day's contributions
    names the culprit instead of leaving us to guess which ticker it was.
    """
    daily = backtest_mod.estimate_series(
        fund, snapshot, as_of - timedelta(days=span * 3), as_of
    ).tail(span)
    if daily.empty:
        return

    worst = daily.abs().idxmax()
    print()
    print(f"Største enkeltdag i vinduet er {worst.date()} "
          f"({daily[worst]:+.2f} %). Fordelt på beholdninger:")
    print()

    prices, fx, staleness, observed = _price_context(fund, snapshot, worst.date())
    est = estimate_return(
        fund, snapshot, worst.date(), prices, fx, staleness, observed
    )
    for c in sorted(est.contributions, key=lambda c: c.contribution_pct):
        print(f"  {c.name:<28} {c.ticker:<10} vekt {c.weight_pct:>5.2f} %"
              f"   kurs {c.local_return * 100:>+8.2f} %"
              f"   bidrag {c.contribution_pct:>+7.3f} %")


def cmd_backtest(args) -> int:
    fund = load_fund(args.fund)
    snapshot = holdings_mod.load_holdings(fund)

    results = backtest_mod.compare_lags(fund, snapshot, days=args.days)
    if not results:
        raise RuntimeError("Backtesten kunne ikke kjøres for noen forsinkelse.")
    print(backtest_mod.format_lag_comparison(results))

    best = max(results, key=lambda r: r.correlation if r.correlation == r.correlation else -9)
    print()
    print(backtest_mod.format_report(best))
    return 0


def _latest_complete(
    fund, snapshot, target, prices, fx, staleness, observed, max_stale, back=7
):
    """Walk back to the most recent day every market had actually closed.

    Useful during the day, when today's estimate is mostly carried-forward
    prices but yesterday's is complete.
    """
    candidates = [ts for ts in prices.index if ts <= pd.Timestamp(target)]
    for ts in reversed(candidates[-back - 1:-1]):
        try:
            candidate = estimate_return(
                fund, snapshot, ts.date(), prices, fx, staleness, observed
            )
        except ValueError:
            continue
        if candidate.stale_weight_pct <= max_stale:
            log.info("Bruker %s, siste dag med fullstendige kurser.", ts.date())
            return candidate
    return None


def _already_logged(fund, target: date) -> bool:
    path = fund.estimates_file()
    if not path.exists():
        return False
    with path.open(encoding="utf-8", newline="") as fh:
        return any(row.get("date") == target.isoformat() for row in csv.DictReader(fh))


def _append_estimate(fund, est) -> None:
    path = fund.estimates_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        if not exists:
            writer.writerow(
                ["date", "estimated_pct", "coverage_pct", "fx_contribution_pct",
                 "snapshot_age_days", "warnings"]
            )
        writer.writerow([
            est.date.isoformat(),
            f"{est.return_pct:.4f}",
            f"{est.coverage_pct:.2f}",
            f"{est.fx_contribution_pct:.4f}",
            est.snapshot_age_days if est.snapshot_age_days is not None else "",
            " | ".join(est.warnings),
        ])
    log.info("Logget estimat til %s", path)


if __name__ == "__main__":
    raise SystemExit(main())
