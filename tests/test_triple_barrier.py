"""Spec for triple_barrier.

Each fixture is a hand-built tick path with a known answer, so a failure says
which rule broke rather than that a number moved.
"""

import numpy as np
import pytest

from triple_barrier import LOSS, TIMEOUT, WIN, _resolve, label_row

TARGET, STOP = 6.50, 4.30


def path(mids, spread=0.30):
    mids = np.asarray(mids, dtype="float64")
    return mids - spread / 2, mids + spread / 2


def test_a_long_wins_when_the_bid_reaches_the_target_first():
    bid, ask = path([100.0, 103.0, 107.0])       # entry ask 100.15
    long_out, _, _, _, _, _ = label_row(0, 3, bid, ask, TARGET, STOP)
    assert long_out == WIN


def test_the_spread_is_paid_on_the_way_in_and_out():
    """A mid move of exactly the target is NOT a win: entry is the ask and
    exit is the bid, so the round trip costs a full spread."""
    bid, ask = path([100.0, 106.50])             # mid moved exactly +6.50
    long_out, _, _, _, _, _ = label_row(0, 2, bid, ask, TARGET, STOP)
    assert long_out == TIMEOUT, "pricing both sides at the mid would call this a win"

    bid, ask = path([100.0, 106.90])             # +6.90 mid clears ask->bid
    long_out, _, _, _, _, _ = label_row(0, 2, bid, ask, TARGET, STOP)
    assert long_out == WIN


def test_a_long_loses_when_the_stop_comes_first():
    bid, ask = path([100.0, 97.0, 95.0, 110.0])  # stop at index 2, target later
    long_out, ticks, _, _, _, _ = label_row(0, 4, bid, ask, TARGET, STOP)
    assert long_out == LOSS
    assert ticks == 2, "it must resolve at the stop, not run on to the target"


def test_a_short_is_scored_from_the_other_side():
    bid, ask = path([100.0, 93.0])               # entry bid 99.85, ask falls
    _, _, _, short_out, _, _ = label_row(0, 2, bid, ask, TARGET, STOP)
    assert short_out == WIN


def test_both_sides_can_lose_on_the_same_bar():
    """A whipsaw stops out a long and a short. Long and short are not
    opposites once the spread is paid, which is why they are computed apart."""
    bid, ask = path([100.0, 95.5, 105.0])
    long_out, _, _, short_out, _, _ = label_row(0, 3, bid, ask, TARGET, STOP)
    assert long_out == LOSS
    assert short_out == LOSS


def test_nothing_happening_is_a_timeout_not_a_loss():
    bid, ask = path([100.0, 100.4, 99.8, 100.1])
    long_out, _, _, short_out, _, _ = label_row(0, 4, bid, ask, TARGET, STOP)
    assert long_out == TIMEOUT
    assert short_out == TIMEOUT


def test_an_empty_window_times_out_rather_than_raising():
    bid, ask = path([100.0])
    assert label_row(0, 0, bid, ask, TARGET, STOP) == (TIMEOUT, 0, 0.0, TIMEOUT, 0, 0.0)


def test_a_tie_is_scored_as_a_loss():
    """Both barriers inside one tick: the path within that tick is unknown, so
    it is scored against the trade. Assuming the good order is how a backtest
    inflates itself."""
    assert _resolve(hit_target=5, hit_stop=5, size=10) == (LOSS, 5)


def test_first_true_does_not_confuse_a_hit_at_zero_with_no_hit():
    assert _resolve(hit_target=0, hit_stop=-1, size=10) == (WIN, 0)
    assert _resolve(hit_target=-1, hit_stop=-1, size=10) == (TIMEOUT, 10)


def test_the_window_end_is_respected():
    """The target lands one tick past the time barrier - that is a timeout."""
    bid, ask = path([100.0, 101.0, 108.0])
    long_out, _, _, _, _, _ = label_row(0, 2, bid, ask, TARGET, STOP)
    assert long_out == TIMEOUT
    long_out, _, _, _, _, _ = label_row(0, 3, bid, ask, TARGET, STOP)
    assert long_out == WIN


def test_a_win_is_priced_at_the_target_and_a_loss_at_the_stop():
    bid, ask = path([100.0, 108.0])
    _, _, long_pnl, _, _, _ = label_row(0, 2, bid, ask, TARGET, STOP)
    assert long_pnl == pytest.approx(TARGET)

    bid, ask = path([100.0, 94.0])
    _, _, long_pnl, _, _, _ = label_row(0, 2, bid, ask, TARGET, STOP)
    assert long_pnl == pytest.approx(-STOP)


def test_a_timeout_is_priced_at_the_barrier_not_at_zero():
    """A position that ran out of clock 3 points ahead made 3 points. Scoring
    it as zero is what makes an asymmetric target look worse than it is."""
    bid, ask = path([100.0, 101.0, 103.0])
    outcome, _, long_pnl, _, _, _ = label_row(0, 3, bid, ask, TARGET, STOP)
    assert outcome == TIMEOUT
    # exit bid 102.85 against entry ask 100.15
    assert long_pnl == pytest.approx(2.70, abs=1e-9)
    assert long_pnl < TARGET, "a timeout must never be priced as a full win"


def test_a_timeout_can_be_a_loss():
    bid, ask = path([100.0, 98.0])
    outcome, _, long_pnl, _, _, _ = label_row(0, 2, bid, ask, TARGET, STOP)
    assert outcome == TIMEOUT
    assert long_pnl < 0, "drifting against you and running out of clock costs money"
