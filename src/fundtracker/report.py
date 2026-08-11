"""Human-readable rendering of an estimate, for the terminal and for email."""

from __future__ import annotations

import textwrap
from html import escape

from .models import Estimate

_MONTHS = [
    "januar", "februar", "mars", "april", "mai", "juni",
    "juli", "august", "september", "oktober", "november", "desember",
]


def _no_date(value) -> str:
    return f"{value.day}. {_MONTHS[value.month - 1]} {value.year}"


def _pct(value: float, decimals: int = 2) -> str:
    """Signed percentage, Norwegian decimal comma."""
    return f"{value:+.{decimals}f} %".replace(".", ",")


def _num(value: float, decimals: int = 1) -> str:
    """Unsigned number, Norwegian decimal comma."""
    return f"{value:.{decimals}f}".replace(".", ",")


def short_name(name: str, width: int | None = None) -> str:
    """Strip registry boilerplate from a holding name, for display.

    Morningstar spells names out in full — "AppLovin Corp Ordinary Shares -
    Class A" — which is right for a filing and wrong for a daily email. The
    underlying name is never touched; this only affects what gets printed.
    """
    cleaned = name.replace(" Ordinary Shares", "").replace(" - ", " ")
    cleaned = cleaned.removesuffix(" New").strip()
    # "Amazon.com Inc" looks like a domain, and mobile mail clients turn it into
    # a link. Dropping the suffix reads better anyway, and the ticker beside it
    # keeps the row unambiguous.
    cleaned = cleaned.replace(".com", "")
    if width and len(cleaned) > width:
        cleaned = cleaned[: width - 1].rstrip() + "…"
    return cleaned


def subject(est: Estimate) -> str:
    return f"{est.fund_name}: {_pct(est.return_pct)} ({est.date.strftime('%d.%m')})"


def _peer_note(est: Estimate, peers: list[Estimate]) -> str:
    """Spell out the currencies, but only when they actually differ.

    Each fund's return is measured in its own base currency, because that is
    what the fund company publishes. Comparing a NOK number with a EUR one is
    only fair if you know that is what you are doing, so when the currencies
    differ the mail says so — and when they happen to match, the line would be
    noise and is left out.
    """
    currencies = {est.currency, *(p.currency for p in peers)}
    if len(currencies) < 2:
        return ""
    return (
        "Hvert fond måles i sin egen basisvaluta, slik forvalteren rapporterer "
        "det. Tallene er derfor ikke direkte sammenlignbare."
    )


def to_text(est: Estimate, peers: list[Estimate] | None = None) -> str:
    peers = peers or []
    lines = [
        f"{est.fund_name}",
        f"Estimert avkastning {_no_date(est.date)}: {_pct(est.return_pct)}",
    ]

    if peers:
        lines += ["", "Til sammenligning:"]
        for fund in [est, *peers]:
            lines.append(_peer_line(fund))
        for note in (_peer_note(est, peers), _accuracy_note([est, *peers])):
            if note:
                lines.append("")
                lines += textwrap.wrap(note, width=74, initial_indent="  ",
                                       subsequent_indent="  ")

    lines += [
        "",
        "Sammensetning:",
        _summary_line(f"Aksjeavkastning i {est.currency}", _pct(est.equity_return_pct)),
        _summary_line("Herav valutaeffekt", _pct(est.fx_contribution_pct)),
        _summary_line("Forvaltningshonorar", _pct(-est.fee_drag_pct, 4)),
        "",
        f"Dekning: {_num(est.coverage_pct)} % av fondet"
        + (f", {_num(est.stale_weight_pct)} % uten dagskurs"
           if est.stale_weight_pct > 0 else "")
        + (f", kontanter {_num(est.cash_pct)} %" if est.cash_pct is not None else "")
        + (f", snapshot {est.snapshot_age_days} dager gammelt"
           if est.snapshot_age_days is not None else ""),
        "",
        "Sterkeste bidrag:",
    ]
    for c in est.top_contributors():
        lines.append(_contribution_line(c))
    lines.append("")
    lines.append("Svakeste bidrag:")
    for c in reversed(est.bottom_contributors()):
        lines.append(_contribution_line(c))

    if est.warnings:
        lines += ["", "Forbehold:"]
        lines += [f"  - {w}" for w in est.warnings]

    lines += [
        "",
        "Dette er et estimat regnet ut fra fondets kjente beholdninger og dagens",
        "sluttkurser. Det er ikke fondets offisielle NAV.",
    ]
    return "\n".join(lines)


def _summary_line(label: str, value: str) -> str:
    """One row of the composition block, with the values in a fixed column."""
    return f"  {label:<24}{value}"


def _peer_line(est: Estimate) -> str:
    """One fund in the comparison block: name, return, currency, and error bar."""
    error = f"± {_num(est.mean_abs_error_pp, 2)}" if est.mean_abs_error_pp else ""
    return (f"  {short_name(est.fund_name, 30):<30} {_pct(est.return_pct):>9}"
            f"  {est.currency:<4}{error:>7}")


def _accuracy_note(funds: list[Estimate]) -> str:
    """Explain the error bars, naming the window each was measured over.

    A tolerance with no window behind it invites more trust than it has earned,
    and a fund with no measurement at all must not be allowed to look like one
    with a small error - so the funds that have never been checked are named.
    """
    measured = [f for f in funds if f.mean_abs_error_pp]
    if not measured:
        return ""

    windows = ", ".join(
        f"{short_name(f.fund_name)} {f.accuracy_days} dager" for f in measured
    )
    note = (
        "± er modellens gjennomsnittlige bom mot fondets faktiske NAV, målt over "
        f"{windows}. Det er et snitt, ikke en grense: enkeltdager bommer mer."
    )

    unmeasured = [f for f in funds if not f.mean_abs_error_pp]
    if unmeasured:
        names = ", ".join(short_name(f.fund_name) for f in unmeasured)
        note += (
            f" {names} står uten tall fordi modellen aldri er målt mot NAV der - "
            "ikke fordi den treffer bedre."
        )
    return note


def _contribution_line(c) -> str:
    """One fixed-width row, so the columns line up in a monospaced client."""
    return (
        f"  {short_name(c.name, 28):<28} {_pct(c.contribution_pct, 3):>10}"
        f"   (kurs {_pct(c.local_return * 100)}, valuta {_pct(c.fx_return * 100)})"
    )


def _peers_html(est: Estimate, peers: list[Estimate]) -> str:
    """The comparison block: every fund's headline number, nothing else.

    Deliberately just the returns. Each peer has its own strongest and weakest
    contributors, and printing three sets of those turns a mail you read in ten
    seconds into one you scroll past.
    """
    if not peers:
        return ""

    rows = []
    for fund, own in [(est, True)] + [(p, False) for p in peers]:
        sign = "#137333" if fund.return_pct >= 0 else "#c5221f"
        weight = "600" if own else "400"
        rows.append(
            f"<tr>"
            f"<td style='padding:5px 16px 5px 0;font-weight:{weight}'>"
            f"{escape(short_name(fund.fund_name))}</td>"
            f"<td style='padding:5px 8px 5px 0;text-align:right;color:{sign};"
            f"font-weight:600;white-space:nowrap'>{_pct(fund.return_pct)}</td>"
            f"<td style='padding:5px 12px 5px 0;color:#888;font-size:12px'>"
            f"{escape(fund.currency)}</td>"
            f"<td style='padding:5px 0;color:#888;font-size:12px;white-space:nowrap'>"
            f"{('± ' + _num(fund.mean_abs_error_pp, 2)) if fund.mean_abs_error_pp else ''}</td>"
            f"</tr>"
        )

    notes = [n for n in (_peer_note(est, peers), _accuracy_note([est, *peers])) if n]
    note_html = "".join(
        f"<div style='margin-top:8px;font-size:12px;color:#888;line-height:1.5'>"
        f"{escape(n)}</div>" for n in notes
    )

    return (
        "<h3 style='margin:28px 0 8px;font-size:14px'>Til sammenligning</h3>"
        "<table style='border-collapse:collapse;font-size:14px'>"
        f"{''.join(rows)}</table>{note_html}"
    )


def to_html(est: Estimate, peers: list[Estimate] | None = None) -> str:
    peers = peers or []
    colour = "#137333" if est.return_pct >= 0 else "#c5221f"

    def rows(contribs) -> str:
        out = []
        for c in contribs:
            sign = "#137333" if c.contribution_pct >= 0 else "#c5221f"
            out.append(
                f"<tr>"
                f"<td style='padding:4px 12px 4px 0'>{escape(short_name(c.name))}"
                f"<span style='color:#888;font-size:12px'> · {escape(c.ticker)}</span></td>"
                f"<td style='padding:4px 12px 4px 0;text-align:right'>"
                f"{_num(c.weight_pct, 2)} %</td>"
                f"<td style='padding:4px 12px 4px 0;text-align:right'>"
                f"{_pct(c.local_return * 100)}</td>"
                f"<td style='padding:4px 12px 4px 0;text-align:right'>"
                f"{_pct(c.fx_return * 100)}</td>"
                f"<td style='padding:4px 0;text-align:right;color:{sign};font-weight:600'>"
                f"{_pct(c.contribution_pct, 3)}</td>"
                f"</tr>"
            )
        return "".join(out)

    header = (
        "<tr style='font-size:12px;color:#666;text-align:left'>"
        "<th style='padding-bottom:6px'>Selskap</th>"
        "<th style='padding-bottom:6px;text-align:right'>Vekt</th>"
        "<th style='padding-bottom:6px;text-align:right'>Kurs</th>"
        "<th style='padding-bottom:6px;text-align:right'>Valuta</th>"
        "<th style='padding-bottom:6px;text-align:right'>Bidrag</th></tr>"
    )

    meta = f"Dekning {_num(est.coverage_pct)} % av fondet"
    if est.stale_weight_pct > 0:
        meta += f" · {_num(est.stale_weight_pct)} % uten dagskurs"
    if est.cash_pct is not None:
        meta += f" · kontanter {_num(est.cash_pct)} %"
    if est.snapshot_age_days is not None:
        meta += f" · beholdninger {est.snapshot_age_days} dager gamle"

    warnings_html = ""
    if est.warnings:
        items = "".join(
            f"<li style='margin-bottom:4px'>{escape(w)}</li>" for w in est.warnings
        )
        warnings_html = (
            "<div style='margin-top:28px'>"
            "<h3 style='margin:0 0 8px;font-size:14px'>Forbehold</h3>"
            "<ul style='margin:0;padding-left:18px;font-size:13px;color:#666'>"
            f"{items}</ul></div>"
        )

    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- iOS Mail linkifies anything resembling a phone number, date or address.
     Nothing here is meant to be tappable, so the detectors are turned off, and
     the rule below neutralises the styling on any the client links anyway. -->
<meta name="format-detection" content="telephone=no,date=no,address=no,email=no,url=no">
<style>
  a[x-apple-data-detectors] {{
    color: inherit !important;
    text-decoration: none !important;
    font-size: inherit !important;
    font-weight: inherit !important;
  }}
</style>
</head><body style="margin:0">
<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;max-width:640px;color:#1a1a1a">
  <div style="font-size:14px;font-weight:600">{escape(est.fund_name)}</div>
  <div style="font-size:40px;font-weight:600;color:{colour};line-height:1.2;margin:4px 0">
    {_pct(est.return_pct)}
  </div>
  <div style="font-size:14px;color:#666">estimert for {_no_date(est.date)}</div>

  {_peers_html(est, peers)}

  <table style="margin-top:24px;font-size:14px;border-collapse:collapse">
    <tr><td style="padding:3px 20px 3px 0;color:#666">Aksjeavkastning i {escape(est.currency)}</td>
        <td style="text-align:right">{_pct(est.equity_return_pct)}</td></tr>
    <tr><td style="padding:3px 20px 3px 0;color:#666">Herav valutaeffekt</td>
        <td style="text-align:right;color:#666">{_pct(est.fx_contribution_pct)}</td></tr>
    <tr><td style="padding:3px 20px 3px 0;color:#666">Forvaltningshonorar</td>
        <td style="text-align:right;color:#666">{_pct(-est.fee_drag_pct, 4)}</td></tr>
  </table>

  <div style="margin-top:20px;font-size:12px;color:#888">{escape(meta)}</div>

  <h3 style="margin:28px 0 8px;font-size:14px">Sterkeste bidrag</h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px">{header}{rows(est.top_contributors())}</table>

  <h3 style="margin:24px 0 8px;font-size:14px">Svakeste bidrag</h3>
  <table style="width:100%;border-collapse:collapse;font-size:13px">{header}{rows(list(reversed(est.bottom_contributors())))}</table>

  {warnings_html}

  <p style="margin-top:28px;font-size:12px;color:#888;line-height:1.5">
    Estimat basert på fondets kjente beholdninger og dagens sluttkurser.
    Ikke fondets offisielle NAV.
  </p>
</div>
</body></html>"""
