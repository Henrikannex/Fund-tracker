"""Grouping the missing closes by exchange.

The whole point of the probe is the grouping: "19 tickers missing" is noise,
"every .AS, .DE, .ST and .OL missing while the US ones are fine" is a finding.
"""

from __future__ import annotations

from fundtracker.cli import _exchange_of


def test_a_suffix_is_the_exchange():
    assert _exchange_of("ASML.AS") == ".AS"
    assert _exchange_of("SAP.DE") == ".DE"


def test_a_bare_ticker_is_american():
    assert _exchange_of("MSFT") == "(US)"


def test_a_hyphenated_share_class_keeps_its_exchange():
    """ERIC-B.ST is one ticker with a class in it, not two dotted parts."""
    assert _exchange_of("ERIC-B.ST") == ".ST"


def test_the_last_dot_wins():
    """Yahoo puts the exchange last; anything earlier belongs to the name."""
    assert _exchange_of("BRK.B.US") == ".US"
