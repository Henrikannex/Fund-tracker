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
