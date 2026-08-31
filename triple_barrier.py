"""Triple-barrier labels: would this trade have won?

    python triple_barrier.py --ticks data --features features --symbol XAUUSD

WHY THIS EXISTS

The existing label asks "will mid be higher 5 minutes from now". The engine
trades a 4.30 stop and a 6.50 target. Those are different questions, and a
model that answers the first perfectly still tells the engine nothing about
the second: the median 5-minute move on this data is about 1.92, so reaching
6.50 takes several moves in a row, and whether the stop is touched on the way
is exactly what the 5-minute label cannot see.

This walks the raw ticks forward from each feature row and answers the
question the engine actually asks:

    entering here, does the target get hit before the stop, within max_hold?

THREE DECISIONS, ALL OF WHICH CHANGE THE LABEL

1. A long enters at the ASK and exits at the BID; a short does the reverse.
   Pricing both sides at the mid would hand the trade the spread for free and
   label moves as winners that could not be entered and exited. Costing the
   crossing here is the point: it puts the cost inside the label instead of
   leaving it as a caveat.

2. Long and short are computed separately. They are not opposites once the
   spread is paid - a bar can be a losing long AND a losing short, and on this
   instrument that is common. Collapsing them early hides it.

3. The time barrier is real, not a formality. A position that never resolves
   is not a win. `timeout_pct` in the report is the number to watch: if most
   rows time out, the target is too far for the horizon and the label is
   mostly measuring "nothing happened".

CONSEQUENCE WORTH KNOWING

These labels are tied to a specific stop and target. Change them in config.py
and the labels change with them - that is the whole point, but it means a
training set is only valid for the risk configuration that produced it. The
manifest records both.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

WIN, TIMEOUT, LOSS = 1, 0, -1
BUY, WAIT, SELL = 1, 0, -1


def load_ticks(directory: Path, symbol: str) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """All ticks for one symbol as (epoch_ns, bid, ask), oldest first."""
    files = sorted(directory.glob(f"ict_{symbol}_*.csv"))
    files = [f for f in files if not f.name.endswith(".meta.json")]
    if not files:
        raise SystemExit(f"no ict_{symbol}_*.csv tick files in {directory}")

    frames = []
    for path in files:
        frame = pd.read_csv(path, usecols=["timestamp", "bid", "ask"])
        frames.append(frame)
    ticks = pd.concat(frames, ignore_index=True)
    ticks["timestamp"] = pd.to_datetime(ticks["timestamp"], utc=True, format="mixed")
    ticks = ticks.sort_values("timestamp", kind="mergesort").reset_index(drop=True)

    return (
        ticks["timestamp"].to_numpy(dtype="datetime64[ns]").astype("int64"),
        ticks["bid"].to_numpy(dtype="float64"),
        ticks["ask"].to_numpy(dtype="float64"),
    )


def _first_true(mask: np.ndarray) -> int:
    """Index of the first True, or -1. argmax alone cannot tell an all-False
    array from a hit at position 0."""
    if not mask.any():
        return -1
    return int(np.argmax(mask))


def label_row(
    start: int,
    stop_index: int,
    bid: np.ndarray,
    ask: np.ndarray,
    target: float,
    stop: float,
) -> tuple[int, int, float, int, int, float]:
    """(long_outcome, long_ticks, long_pnl, short_outcome, short_ticks, short_pnl).

    `start` is the first tick at or after the bar close; `stop_index` is one
    past the last tick inside the time barrier.

    P&L is returned per side because the outcome alone is not enough to judge
    the label. A timeout is not zero: the position is closed at the market
    price when the barrier falls, and those exits carry real money either way.
    Counting only decided trades makes the win rate look like something it is
    not - the nearer barrier resolves more often inside a finite window, so an
    asymmetric target/stop biases the decided set toward whichever is closer.
    """
    window_bid = bid[start:stop_index]
    window_ask = ask[start:stop_index]
    if window_bid.size == 0:
        return TIMEOUT, 0, 0.0, TIMEOUT, 0, 0.0

    # LONG: pay the ask to enter, receive the bid to exit.
    entry = ask[start]
    hit_target = _first_true(window_bid >= entry + target)
    hit_stop = _first_true(window_bid <= entry - stop)
    long_outcome, long_ticks = _resolve(hit_target, hit_stop, window_bid.size)
    long_pnl = _pnl(long_outcome, target, stop, window_bid[-1] - entry)

    # SHORT: receive the bid to enter, pay the ask to exit.
    entry = bid[start]
    hit_target = _first_true(window_ask <= entry - target)
    hit_stop = _first_true(window_ask >= entry + stop)
    short_outcome, short_ticks = _resolve(hit_target, hit_stop, window_ask.size)
    short_pnl = _pnl(short_outcome, target, stop, entry - window_ask[-1])

    return long_outcome, long_ticks, long_pnl, short_outcome, short_ticks, short_pnl


def _pnl(outcome: int, target: float, stop: float, at_barrier: float) -> float:
    """Money the trade made, in price units. A timeout exits at the market."""
    if outcome == WIN:
        return target
    if outcome == LOSS:
        return -stop
    return float(at_barrier)


def _resolve(hit_target: int, hit_stop: int, size: int) -> tuple[int, int]:
    """Whichever barrier came first wins. A tie is a loss.

    Both barriers can fall inside the same tick window, and which one the
    trade actually met is decided by order, not by preference. When they land
    on the SAME tick the tick's own path is unknown, so it is scored as a loss
    - the pessimistic reading, because assuming the favourable order is how a
    backtest quietly inflates itself.
    """
    if hit_target < 0 and hit_stop < 0:
        return TIMEOUT, size
    if hit_stop < 0:
        return WIN, hit_target
    if hit_target < 0:
        return LOSS, hit_stop
    if hit_target < hit_stop:
        return WIN, hit_target
    return LOSS, hit_stop


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ticks", type=Path, default=Path("data"))
    parser.add_argument("--features", type=Path, default=Path("features"))
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--target", type=float, default=6.50)
    parser.add_argument("--stop", type=float, default=4.30)
    parser.add_argument(
        "--max-hold", type=int, default=240,
        help="time barrier in minutes (default 240 = 4 hours)",
    )
    parser.add_argument("--out", type=Path, default=Path("data/triple_barrier.csv"))
    args = parser.parse_args(argv)

    stamps, bid, ask = load_ticks(args.ticks, args.symbol)
    print(f"ticks     : {len(stamps):,}")

    feature_files = sorted(args.features.glob(f"ict_{args.symbol}_*.csv"))
    if not feature_files:
        raise SystemExit(f"no feature CSVs in {args.features}")
    rows = pd.concat((pd.read_csv(f) for f in feature_files), ignore_index=True)
    rows["timestamp"] = pd.to_datetime(rows["timestamp"], utc=True, format="mixed")
    rows = rows.sort_values("timestamp").drop_duplicates("timestamp")
    rows = rows.reset_index(drop=True)
    print(f"feature rows: {len(rows):,}")

    entry_ns = rows["timestamp"].to_numpy(dtype="datetime64[ns]").astype("int64")
    hold_ns = args.max_hold * 60 * 1_000_000_000
    starts = np.searchsorted(stamps, entry_ns, side="left")
    ends = np.searchsorted(stamps, entry_ns + hold_ns, side="right")

    long_out = np.empty(len(rows), dtype="int8")
    short_out = np.empty(len(rows), dtype="int8")
    long_ticks = np.empty(len(rows), dtype="int64")
    short_ticks = np.empty(len(rows), dtype="int64")
    long_pnl = np.empty(len(rows), dtype="float64")
    short_pnl = np.empty(len(rows), dtype="float64")
    truncated = np.zeros(len(rows), dtype=bool)

    last_ns = stamps[-1]
    for i in range(len(rows)):
        start, end = int(starts[i]), int(ends[i])
        # A row whose time barrier runs past the end of the data has not been
        # observed for its full window. Scoring it would count "the data ran
        # out" as a timeout, which is not the same event.
        truncated[i] = entry_ns[i] + hold_ns > last_ns
        (
            long_out[i], long_ticks[i], long_pnl[i],
            short_out[i], short_ticks[i], short_pnl[i],
        ) = label_row(start, end, bid, ask, args.target, args.stop)

    rows["tb_long"] = long_out
    rows["tb_short"] = short_out
    rows["tb_long_ticks"] = long_ticks
    rows["tb_short_ticks"] = short_ticks
    rows["tb_long_pnl"] = long_pnl
    rows["tb_short_pnl"] = short_pnl
    rows["tb_truncated"] = truncated

    # The trade the engine would take: long when only the long wins, short
    # when only the short wins. Both winning is impossible with a target above
    # the spread; both losing is the common case and is genuinely WAIT.
    label = np.full(len(rows), WAIT, dtype="int8")
    label[(long_out == WIN) & (short_out != WIN)] = BUY
    label[(short_out == WIN) & (long_out != WIN)] = SELL
    rows["tb_label"] = label

    # A second label that ignores the barriers and asks only which side made
    # money. It is the honest one to train on if timeouts dominate: a trade
    # that drifted 3.00 in your favour and ran out of clock was not a failure.
    profitable = np.full(len(rows), WAIT, dtype="int8")
    profitable[(long_pnl > 0) & (long_pnl >= short_pnl)] = BUY
    profitable[(short_pnl > 0) & (short_pnl > long_pnl)] = SELL
    rows["tb_label_pnl"] = profitable

    usable = rows.loc[~rows["tb_truncated"]]
    report(usable, args)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    rows.to_csv(args.out, index=False)
    print(f"\nwrote {args.out}  ({len(rows):,} rows, {int(truncated.sum()):,} truncated)")

    meta = {
        "symbol": args.symbol,
        "target": args.target,
        "stop": args.stop,
        "max_hold_minutes": args.max_hold,
        "rows": int(len(rows)),
        "truncated": int(truncated.sum()),
    }
    meta_path = args.out.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"wrote {meta_path}")
    return 0


def report(rows: pd.DataFrame, args) -> None:
    total = max(len(rows), 1)
    breakeven = args.stop / (args.target + args.stop)

    print(f"\ntarget {args.target}  stop {args.stop}  max hold {args.max_hold}m")
    print(f"R:R {args.target / args.stop:.2f}  ->  breakeven win rate "
          f"{breakeven:.1%}")
    print(f"\n{len(rows):,} rows observed for their full window\n")

    print(f"{'':10}{'win':>10}{'loss':>10}{'timeout':>10}{'win rate*':>12}")
    print("-" * 52)
    for name, column in (("long", "tb_long"), ("short", "tb_short")):
        outcomes = rows[column]
        win = (outcomes == WIN).sum()
        loss = (outcomes == LOSS).sum()
        out = (outcomes == TIMEOUT).sum()
        decided = win + loss
        rate = win / decided if decided else float("nan")
        flag = "" if pd.isna(rate) else ("  ABOVE" if rate > breakeven else "  below")
        print(f"{name:10}{win:>10,}{loss:>10,}{out:>10,}{rate:>11.1%}{flag}")
    print("\n* of DECIDED trades only, and read it with care: inside a finite")
    print("  window the nearer barrier resolves more often, so an asymmetric")
    print("  target and stop bias this number toward whichever is closer. It")
    print("  is not comparable to the breakeven rate above. The expectancy")
    print("  below is, because it prices every row including the timeouts.")

    print("\nEXPECTANCY - every row priced, timeouts exited at the barrier")
    print(f"{'':10}{'per trade':>12}{'gross win':>12}{'gross loss':>12}{'PF':>8}")
    print("-" * 54)
    for name, column in (("long", "tb_long_pnl"), ("short", "tb_short_pnl")):
        pnl = rows[column]
        won = pnl[pnl > 0].sum()
        lost = -pnl[pnl < 0].sum()
        factor = won / lost if lost > 0 else float("inf")
        flag = "  >1" if factor > 1 else ""
        print(f"{name:10}{pnl.mean():>12.4f}{won:>12.1f}{lost:>12.1f}"
              f"{factor:>8.2f}{flag}")
    print("\n  Per trade is in price units, before commission. Negative means")
    print("  taking every row on that side loses money - which is the correct")
    print("  baseline: a model has to beat it, not merely differ from it.")

    print("\nderived 3-class label:")
    counts = rows["tb_label"].value_counts()
    for value, name in ((BUY, "BUY "), (WAIT, "WAIT"), (SELL, "SELL")):
        n = int(counts.get(value, 0))
        print(f"  {name}  {n:>7,}  {n / total:6.1%}")

    timeout_pct = ((rows["tb_long"] == TIMEOUT) & (rows["tb_short"] == TIMEOUT)).mean()
    print(f"\ntimeout on both sides: {timeout_pct:.1%}")
    if timeout_pct > 0.5:
        print("  ^ over half of rows resolved neither way inside the time")
        print("    barrier. The target is far for this horizon: the label is")
        print("    mostly recording that nothing happened. Widen --max-hold or")
        print("    narrow the target before reading anything into the rest.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
