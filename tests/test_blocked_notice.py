"""The mail that says the day could not be priced.

A withheld estimate and a broken job look identical in an inbox: nothing
arrives. That is how a week went by in September before anyone noticed, so the
last attempt of the day says so out loud.
"""

from __future__ import annotations

from argparse import Namespace
from datetime import date

import pytest

from fundtracker import cli, report
from fundtracker.models import Contribution, Estimate

BLOCKED = Estimate(
    fund_id="dnb-teknologi-a",
    fund_name="DNB Teknologi A",
    date=date(2026, 9, 2),
    currency="NOK",
    return_pct=-0.07,
    equity_return_pct=-0.07,
    fx_contribution_pct=-0.07,
    cash_drag_pct=0.0,
    fee_drag_pct=0.0045,
    coverage_pct=97.5,
    stale_weight_pct=96.4,
    cash_pct=1.6,
    snapshot_age_days=69,
    contributions=[
        Contribution("Microsoft Corp", "MSFT", "USD", 8.0, 0.0, -0.001, -0.001, -0.009)
    ],
    warnings=["96,4 % av fondet har ingen kurs for 2026-09-02 - gårsdagens er videreført."],
)


def test_the_subject_says_which_day_is_missing():
    assert report.blocked_subject(BLOCKED) == "DNB Teknologi A: ingen estimat for 02.09"


def test_the_body_names_the_share_and_the_limit():
    body = report.blocked_text(BLOCKED, max_stale=5.0)
    assert "96,4 %" in body
    assert "5,0 %" in body
    # It must not read as a fault: the gate held a wrong number back.
    assert "holdt tilbake" in body


def test_the_body_carries_the_warnings():
    assert "ingen kurs for 2026-09-02" in report.blocked_text(BLOCKED, 5.0)


@pytest.fixture
def blocked_run(monkeypatch):
    """cmd_estimate with the pricing stubbed out and the day unpriceable."""
    sent: list[str] = []

    class Fund:
        id = "dnb-teknologi-a"
        name = "DNB Teknologi A"
        peers: list[str] = []

    monkeypatch.setattr(cli, "load_fund", lambda fund_id: Fund())
    monkeypatch.setattr(cli.holdings_mod, "load_holdings", lambda f: object())
    monkeypatch.setattr(cli, "_price_context", lambda *a: (None, None, None, None))
    monkeypatch.setattr(cli, "estimate_return", lambda *a: BLOCKED)
    monkeypatch.setattr(cli, "_peer_estimates", lambda *a: [])
    monkeypatch.setattr(cli.notify, "send_email", lambda subject, *a: sent.append(subject))
    return sent


def args_with(**overrides) -> Namespace:
    base = dict(
        fund="dnb-teknologi-a",
        date="2026-09-02",
        email=True,
        save=False,
        latest=False,
        max_stale=5.0,
        allow_stale=False,
        skip_if_logged=False,
        notify_if_blocked=False,
        verbose=False,
    )
    return Namespace(**{**base, **overrides})


def test_a_blocked_day_says_so_when_asked_to(blocked_run):
    assert cli.cmd_estimate(args_with(notify_if_blocked=True)) == 3
    assert blocked_run == ["DNB Teknologi A: ingen estimat for 02.09"]


def test_the_earlier_attempts_stay_quiet(blocked_run):
    """Every attempt in the window hits the same gate; only the last one tells."""
    assert cli.cmd_estimate(args_with()) == 3
    assert blocked_run == []


def test_a_run_without_email_never_mails_anything(blocked_run):
    """--notify-if-blocked is about which mail, not about mailing at all."""
    assert cli.cmd_estimate(args_with(email=False, notify_if_blocked=True)) == 3
    assert blocked_run == []
