"""The guard against mailing the same day twice.

--skip-if-logged used to be checked only against the date the run asked for.
The morning catch-up run asks for one day and can land on another, so the check
has to happen again once the estimate knows which day it is really about.
"""

from __future__ import annotations

import csv
from argparse import Namespace
from datetime import date

import pytest

from fundtracker import cli
from fundtracker.models import Contribution, Estimate

LOGGED = date(2026, 9, 2)


def estimate_for(day: date) -> Estimate:
    return Estimate(
        fund_id="dnb-teknologi-a",
        fund_name="DNB Teknologi A",
        date=day,
        currency="NOK",
        return_pct=0.29,
        equity_return_pct=0.30,
        fx_contribution_pct=-0.32,
        cash_drag_pct=0.0,
        fee_drag_pct=0.0045,
        coverage_pct=97.5,
        stale_weight_pct=0.0,
        cash_pct=1.6,
        snapshot_age_days=68,
        contributions=[
            Contribution("Microsoft Corp", "MSFT", "USD", 8.0, -0.0084, -0.0027, -0.011, -0.098)
        ],
    )


@pytest.fixture
def fund(tmp_path):
    """A fund whose estimate log already holds 2 September."""
    path = tmp_path / "dnb-teknologi-a.csv"
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cli.ESTIMATE_COLUMNS)
        writer.writeheader()
        writer.writerow({"date": LOGGED.isoformat(), "estimated_pct": "0.2900"})

    class Fund:
        id = "dnb-teknologi-a"
        name = "DNB Teknologi A"
        peers: list[str] = []

        def estimates_file(self):
            return path

    return Fund()


def run_for(monkeypatch, fund, asked_for: date, lands_on: date) -> list[str]:
    """Run cmd_estimate with the pricing stubbed out; return the mails it sent."""
    sent: list[str] = []
    monkeypatch.setattr(cli, "load_fund", lambda fund_id: fund)
    monkeypatch.setattr(cli.holdings_mod, "load_holdings", lambda f: object())
    monkeypatch.setattr(cli, "_price_context", lambda *a: (None, None, None, None))
    monkeypatch.setattr(cli, "estimate_return", lambda *a: estimate_for(lands_on))
    monkeypatch.setattr(cli, "_peer_estimates", lambda *a: [])
    monkeypatch.setattr(cli.notify, "send_email", lambda subject, *a: sent.append(subject))

    args = Namespace(
        fund="dnb-teknologi-a",
        date=asked_for.isoformat(),
        email=True,
        save=False,
        latest=False,
        max_stale=5.0,
        allow_stale=False,
        skip_if_logged=True,
        verbose=False,
    )
    assert cli.cmd_estimate(args) == 0
    return sent


def test_a_day_that_is_already_logged_is_not_mailed_again(monkeypatch, fund):
    """The catch-up run asks for 3 September, walks back to 2 - already sent."""
    assert run_for(monkeypatch, fund, date(2026, 9, 3), LOGGED) == []


def test_a_day_that_is_not_logged_is_still_mailed(monkeypatch, fund):
    """The guard must not swallow the mail it exists to protect."""
    subjects = run_for(monkeypatch, fund, date(2026, 9, 3), date(2026, 9, 3))
    assert subjects == ["DNB Teknologi A: +0,29 % (03.09)"]
