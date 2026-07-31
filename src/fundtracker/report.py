"""Human-readable rendering of an estimate, for the terminal and for email."""

from __future__ import annotations

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
    if width and len(cleaned) > width:
        cleaned = cleaned[: width - 1].rstrip() + "…"
    return cleaned


def subject(est: Estimate) -> str:
    return f"{est.fund_name}: {_pct(est.return_pct)} ({est.date.strftime('%d.%m')})"


def to_text(est: Estimate) -> str:
    lines = [
        f"{est.fund_name}",
        f"Estimert avkastning {_no_date(est.date)}: {_pct(est.return_pct)}",
        "",
        "Sammensetning:",
        _summary_line("Aksjeavkastning i NOK", _pct(est.equity_return_pct)),
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


def _contribution_line(c) -> str:
    """One fixed-width row, so the columns line up in a monospaced client."""
    return (
        f"  {short_name(c.name, 28):<28} {_pct(c.contribution_pct, 3):>10}"
        f"   (kurs {_pct(c.local_return * 100)}, valuta {_pct(c.fx_return * 100)})"
    )


def to_html(est: Estimate) -> str:
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

    return f"""<div style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;max-width:640px;color:#1a1a1a">
  <div style="font-size:14px;font-weight:600">{escape(est.fund_name)}</div>
  <div style="font-size:40px;font-weight:600;color:{colour};line-height:1.2;margin:4px 0">
    {_pct(est.return_pct)}
  </div>
  <div style="font-size:14px;color:#666">estimert for {_no_date(est.date)}</div>

  <table style="margin-top:24px;font-size:14px;border-collapse:collapse">
    <tr><td style="padding:3px 20px 3px 0;color:#666">Aksjeavkastning i NOK</td>
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
</div>"""
