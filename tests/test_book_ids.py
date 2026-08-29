"""Spec for core/book_ids.LevelIds.

The fixtures are real diff_order_book_btcusd messages captured from Bitstamp
on 2026-08-29 - the appear / update / delete / reappear sequence that the
translation has to survive.
"""

from __future__ import annotations

import pytest

from core.book_ids import LevelIds

# Captured live. Trimmed to the levels that matter for each case.
MSG_1 = {
    "bids": [["77693.19", "0.00000000"], ["77691.10", "0.00000000"]],
    "asks": [["77684.15", "0.06445210"], ["77687.40", "0.06445210"]],
}
MSG_2 = {
    "bids": [["77683.99", "0.25000000"], ["77683.23", "1.02000000"]],
    "asks": [["77684.15", "0.00000000"], ["77684.51", "0.64371819"]],
}
MSG_3 = {
    "bids": [["77683.99", "1.27000000"], ["77683.23", "0.00000000"]],
    "asks": [["77684.51", "0.00000000"], ["77684.50", "0.08450080"]],
}


@pytest.fixture
def ids() -> LevelIds:
    return LevelIds()


def test_a_new_level_becomes_a_quote(ids):
    new_quotes, deleted = ids.translate([], [["77684.15", "0.06445210"]])

    assert deleted == []
    assert len(new_quotes) == 1
    quote_id, side, price, size = new_quotes[0]
    assert side == "ask"
    assert price == pytest.approx(77684.15)
    assert size == pytest.approx(0.06445210)
    assert isinstance(quote_id, int)


def test_prices_and_sizes_are_floats_not_strings(ids):
    new_quotes, _ = ids.translate([["77683.99", "0.25"]], [])
    _, _, price, size = new_quotes[0]

    assert isinstance(price, float)
    assert isinstance(size, float)


def test_zero_size_is_a_delete_not_a_quote(ids):
    ids.translate([], [["77684.15", "0.06445210"]])        # level appears
    new_quotes, deleted = ids.translate([], [["77684.15", "0.00000000"]])

    assert new_quotes == []
    assert len(deleted) == 1


def test_the_same_price_keeps_its_id_while_it_lives(ids):
    first, _ = ids.translate([["77683.99", "0.25000000"]], [])
    second, _ = ids.translate([["77683.99", "1.27000000"]], [])

    assert first[0][0] == second[0][0], "a resized level is the same level"
    assert second[0][3] == pytest.approx(1.27)


def test_bids_and_asks_at_the_same_price_are_different_levels(ids):
    new_quotes, _ = ids.translate([["77684.00", "1.0"]], [["77684.00", "2.0"]])

    bid_id = next(q[0] for q in new_quotes if q[1] == "bid")
    ask_id = next(q[0] for q in new_quotes if q[1] == "ask")
    assert bid_id != ask_id


def test_deleting_a_level_we_never_saw_is_ignored(ids):
    new_quotes, deleted = ids.translate([["77000.00", "0.00000000"]], [])

    assert new_quotes == []
    assert deleted == [], "do not invent an id just to delete it"


def test_a_reappearing_price_reuses_its_id(ids):
    """Decided deliberately - see the note in the README/PR.

    `SymbolState.apply_depth` stores `quotes[quote_id] = (side, price, size)`.
    Reusing the id means a stale entry for that price can only ever be
    overwritten; minting a new one risks two live ids for one price, which
    `book()` would render as a duplicated level and `imbalance()` would
    double-count.
    """
    first, _ = ids.translate([["77683.99", "0.25000000"]], [])
    ids.translate([["77683.99", "0.00000000"]], [])          # leaves the book
    again, _ = ids.translate([["77683.99", "0.90000000"]], [])

    assert again[0][0] == first[0][0]


def test_a_real_three_message_sequence(ids):
    q1, d1 = ids.translate(MSG_1["bids"], MSG_1["asks"])
    q2, d2 = ids.translate(MSG_2["bids"], MSG_2["asks"])
    q3, d3 = ids.translate(MSG_3["bids"], MSG_3["asks"])

    # msg 1: both bids are deletes of levels we never saw; both asks are new.
    assert d1 == []
    assert len(q1) == 2

    # msg 2: 77684.15 dies, three levels are new or updated.
    assert len(d2) == 1
    assert len(q2) == 3

    # msg 3: 77683.23 and 77684.51 die; 77683.99 resizes, 77684.50 is new.
    assert len(d3) == 2
    assert len(q3) == 2
