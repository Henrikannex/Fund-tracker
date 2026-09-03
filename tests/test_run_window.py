"""Which trading day a scheduled run is about.

The evening job asks for a whole window of times - 20:15 UTC and every half
hour after - and every attempt has to mean the same day. It stopped doing that
when GitHub began starting the runs hours late: the tail crossed midnight UTC,
date.today() rolled over, and the job priced a day no exchange had traded.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from fundtracker.cli import target_date


def utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_explicit_date_wins():
    """--date is the one thing that must never be second-guessed."""
    assert target_date("2026-08-14", utc("2026-09-03T22:00")) == date(2026, 8, 14)


def test_first_attempt_of_the_window_prices_its_own_day():
    assert target_date(None, utc("2026-09-02T20:15")) == date(2026, 9, 2)


def test_late_attempt_before_midnight_prices_the_same_day():
    assert target_date(None, utc("2026-09-02T23:45")) == date(2026, 9, 2)


def test_attempt_that_crossed_midnight_still_prices_the_trading_day():
    """The regression: this run died with "kunne ikke prises for 2026-09-01".

    It was the 31 August window, started 01:22 UTC on 1 September. Wednesday's
    prices were complete; the job asked about a Tuesday that had not opened.
    """
    assert target_date(None, utc("2026-09-01T01:22")) == date(2026, 8, 31)


def test_the_longest_delay_seen_still_lands_right():
    """A 27 August cron that GitHub did not start until 06:08 the next day."""
    assert target_date(None, utc("2026-08-28T06:08")) == date(2026, 8, 27)


def test_a_daytime_run_is_still_about_today():
    """Anchoring must not quietly turn a midday manual run into yesterday's."""
    assert target_date(None, utc("2026-09-03T14:00")) == date(2026, 9, 3)
