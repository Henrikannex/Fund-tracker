"""Tests for recipient parsing, which fails silently when it goes wrong."""

from __future__ import annotations

from fundtracker.notify import parse_recipients


def test_single_address():
    assert parse_recipients("a@example.com") == ["a@example.com"]


def test_comma_separated():
    assert parse_recipients("a@example.com,b@example.com") == [
        "a@example.com",
        "b@example.com",
    ]


def test_spaces_around_addresses_are_trimmed():
    """A secret pasted by hand usually has spaces after the commas."""
    assert parse_recipients(" a@example.com , b@example.com ") == [
        "a@example.com",
        "b@example.com",
    ]


def test_semicolons_are_accepted():
    """Outlook copies addresses semicolon-separated; that must not silently fail."""
    assert parse_recipients("a@example.com; b@example.com") == [
        "a@example.com",
        "b@example.com",
    ]


def test_trailing_separator_does_not_produce_an_empty_recipient():
    assert parse_recipients("a@example.com,") == ["a@example.com"]


def test_missing_or_blank_gives_no_recipients():
    assert parse_recipients(None) == []
    assert parse_recipients("   ") == []


def test_registry_boilerplate_is_stripped_for_display():
    from fundtracker.report import short_name

    assert short_name("AppLovin Corp Ordinary Shares - Class A") == "AppLovin Corp Class A"
    assert short_name("Match Group Inc Ordinary Shares - New") == "Match Group Inc"
    assert short_name("Microsoft Corp") == "Microsoft Corp"


def test_long_names_are_truncated_so_columns_line_up():
    from fundtracker.report import short_name

    out = short_name("Telefonaktiebolaget L M Ericsson Class B", 28)
    assert len(out) == 28
    assert out.endswith("…")


def test_blank_environment_value_falls_back_to_the_default(monkeypatch):
    """GitHub passes an unset secret as "", so get(name, default) never defaults.

    This crashed the first real send: SMTP_PORT was "" and int("") raised.
    """
    from fundtracker.notify import env

    monkeypatch.setenv("SMTP_PORT", "")
    assert env("SMTP_PORT", "587") == "587"

    monkeypatch.setenv("SMTP_PORT", "   ")
    assert env("SMTP_PORT", "587") == "587"


def test_set_environment_value_wins_and_is_trimmed(monkeypatch):
    from fundtracker.notify import env

    monkeypatch.setenv("SMTP_PORT", " 465 ")
    assert env("SMTP_PORT", "587") == "465"


def test_absent_variable_falls_back(monkeypatch):
    from fundtracker.notify import env

    monkeypatch.delenv("SMTP_PORT", raising=False)
    assert env("SMTP_PORT", "587") == "587"


def test_a_non_numeric_port_is_a_configuration_error_not_a_crash(monkeypatch):
    import pytest

    from fundtracker.notify import NotConfigured, send_email

    monkeypatch.setenv("SMTP_USER", "a@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")
    monkeypatch.setenv("SMTP_PORT", "ikke-et-tall")

    with pytest.raises(NotConfigured, match="SMTP_PORT"):
        send_email("emne", "tekst")


def test_domain_like_names_are_defused_for_mobile_clients():
    """iOS Mail turns "Amazon.com" into a tappable link; the row is not a link."""
    from fundtracker.report import short_name

    assert short_name("Amazon.com Inc") == "Amazon Inc"


def test_html_disables_mobile_data_detectors():
    from datetime import date

    from fundtracker.models import Contribution, Estimate
    from fundtracker.report import to_html

    est = Estimate(
        fund_id="t", fund_name="Test", date=date(2026, 7, 30), currency="NOK",
        return_pct=1.0,
        equity_return_pct=1.0, fx_contribution_pct=0.0, fee_drag_pct=0.0,
        coverage_pct=99.0, stale_weight_pct=0.0, cash_pct=1.0, snapshot_age_days=1,
        contributions=[Contribution("Amazon.com Inc", "AMZN", "USD", 5.0,
                                    0.01, 0.0, 0.01, 0.05)],
    )
    html = to_html(est)

    assert "format-detection" in html
    assert "x-apple-data-detectors" in html
    assert "Amazon Inc" in html and "Amazon.com" not in html
