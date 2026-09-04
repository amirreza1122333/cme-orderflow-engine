"""Tests for the depth converter.

The DBN reading is exercised on the real file; what is tested here is the
part that decides what a number means - which levels exist, which snapshot
represents a bar, and whether an empty book can be told from a balanced one.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

# ict.features is the engine's, and importing it is the point of the design.
# A stub stands in so these tests run anywhere, with the same formula.
_ict = types.ModuleType("ict")
_features = types.ModuleType("ict.features")


def _imbalance(bids, asks, levels):
    bid = sum(l.size for l in bids[:levels])
    ask = sum(l.size for l in asks[:levels])
    total = bid + ask
    return 0.0 if total <= 0 else (bid - ask) / total


_features.imbalance = _imbalance
_ict.features = _features
sys.modules.setdefault("ict", _ict)
sys.modules.setdefault("ict.features", _features)

from depth_features import (  # noqa: E402
    BAR_NS,
    UNDEF_PRICE,
    Level,
    bar_start,
    row_for,
    sides,
)


class _Pair:
    def __init__(self, bid_px, bid_sz, ask_px, ask_sz):
        self.bid_px, self.bid_sz = bid_px, bid_sz
        self.ask_px, self.ask_sz = ask_px, ask_sz


class _Record:
    def __init__(self, levels):
        self.levels = levels


def _px(value):
    return int(value * 1_000_000_000)


# ------------------------------------------------------------ level parsing

def test_prices_are_unscaled_from_fixed_point():
    rec = _Record([_Pair(_px(3850.10), 5, _px(3850.40), 7)])
    bids, asks = sides(rec)
    assert bids[0].price == pytest.approx(3850.10)
    assert asks[0].price == pytest.approx(3850.40)


def test_an_undefined_price_is_not_a_level():
    """A level that does not exist must not count as one of size zero.

    Whether `l2_levels` reports the depth of the book or the width of the
    array turns on exactly this.
    """
    rec = _Record([
        _Pair(_px(3850.10), 5, _px(3850.40), 7),
        _Pair(UNDEF_PRICE, 0, UNDEF_PRICE, 0),
    ])
    bids, asks = sides(rec)
    assert len(bids) == 1 and len(asks) == 1


def test_a_zero_size_level_is_not_a_level():
    rec = _Record([_Pair(_px(3850.10), 0, _px(3850.40), 3)])
    bids, asks = sides(rec)
    assert bids == []
    assert len(asks) == 1


# -------------------------------------------------------------- bucketing

def test_a_bar_starts_on_a_five_minute_boundary():
    inside = 1_756_200_000 * 1_000_000_000 + 137 * 1_000_000_000
    assert bar_start(inside) % BAR_NS == 0
    assert bar_start(inside) <= inside < bar_start(inside) + BAR_NS


def test_two_times_in_one_bar_share_a_bucket():
    base = bar_start(1_756_200_137 * 1_000_000_000)
    assert bar_start(base + 1) == bar_start(base + BAR_NS - 1) == base


# -------------------------------------- an empty book is not a balanced one

def test_a_balanced_book_and_an_empty_book_do_not_look_alike():
    balanced = row_for(0, [Level(1.0, 5.0)], [Level(2.0, 5.0)])
    empty = row_for(0, [], [])
    assert balanced["l2_imbalance_5"] == empty["l2_imbalance_5"] == 0.0
    # The imbalance cannot tell them apart. l2_levels must.
    assert balanced["l2_levels"] == 2
    assert empty["l2_levels"] == 0


def test_imbalance_is_positive_when_bids_dominate():
    row = row_for(0, [Level(1.0, 9.0)], [Level(2.0, 1.0)])
    assert row["l2_imbalance_5"] == pytest.approx(0.8)


def test_only_the_requested_number_of_levels_counts():
    bids = [Level(1.0, 10.0)] * 5
    asks = [Level(2.0, 1.0)] * 5
    row = row_for(0, bids, asks)
    # 3 levels: (30-3)/33 ; 5 levels: (50-5)/55 - both 0.8182, so use a case
    # where the deeper levels differ.
    deep = row_for(0, [Level(1.0, 1.0)] * 3 + [Level(1.0, 100.0)] * 2, asks)
    assert deep["l2_imbalance_3"] != deep["l2_imbalance_5"]
    assert row["l2_imbalance_5"] == pytest.approx(row["l2_imbalance_3"], abs=1e-4)


def test_the_raw_snapshot_travels_with_the_features():
    """So `imbalance_at` can be applied later without paying again."""
    row = row_for(0, [Level(3850.1, 5.0)], [Level(3850.4, 7.0)])
    assert row["bid_px_0"] == pytest.approx(3850.1)
    assert row["ask_sz_0"] == 7.0
    assert row["bid_px_1"] == ""      # absent, not zero


def test_the_timestamp_is_utc_and_sortable():
    row = row_for(1_756_200_000 * 1_000_000_000, [], [])
    assert row["timestamp"].endswith("Z")
    assert row["timestamp"][4] == "-" and row["timestamp"][10] == "T"
