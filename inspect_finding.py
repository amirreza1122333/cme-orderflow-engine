"""Break one apparent finding down by day, before believing it.

    python inspect_finding.py data/triple_barrier.csv \
        --feature asian_range_width --max 44.26 --side short

A permutation test over many features answers one question: could the BEST OF
MANY features look this good by chance? It cannot answer the other one: are
these rows really independent evidence?

They usually are not. Bars five minutes apart on the same day share a session,
a daily bias, an Asian range and a trend. Two hundred rows drawn from two days
is closer to two observations than to two hundred, and no amount of statistics
applied to the row count will notice.

So this prints the finding one day at a time. What you want to see is most
days pointing the same way with similar size. What kills it is one day
carrying the whole result - and that is the common outcome, which is why this
check exists.

It also separates rows where the feature is exactly zero. On asian_range_width
zero does not mean "narrow", it means no range was built at all: warm-up, a
missing session, a gap. Those rows are a different population and averaging
them in describes something nobody can trade.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def summarise(frame: pd.DataFrame, side: str) -> dict:
    pnl = frame[f"tb_{side}_pnl"]
    outcome = frame[f"tb_{side}"]
    wins = int((outcome == 1).sum())
    losses = int((outcome == -1).sum())
    decided = wins + losses
    return {
        "n": len(frame),
        "win": wins,
        "loss": losses,
        "timeout": int((outcome == 0).sum()),
        "win_rate": wins / decided if decided else float("nan"),
        "mean_pnl": float(pnl.mean()) if len(frame) else float("nan"),
    }


def line(label: str, stats: dict) -> str:
    rate = "-" if np.isnan(stats["win_rate"]) else f"{stats['win_rate']:.1%}"
    return (f"{label:14}{stats['n']:>7}{stats['win']:>7}{stats['loss']:>7}"
            f"{rate:>10}{stats['mean_pnl']:>+11.4f}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path,
                        default=Path("data/triple_barrier.csv"))
    parser.add_argument("--feature", required=True)
    parser.add_argument("--min", type=float, default=None)
    parser.add_argument("--max", type=float, default=None)
    parser.add_argument("--side", choices=["long", "short"], required=True)
    parser.add_argument(
        "--drop-zero", action="store_true",
        help="exclude rows where the feature is exactly 0 (often 'not "
             "computed' rather than a small value)",
    )
    args = parser.parse_args(argv)

    frame = pd.read_csv(args.path)
    if "tb_truncated" in frame:
        frame = frame.loc[~frame["tb_truncated"]].reset_index(drop=True)
    frame["day"] = pd.to_datetime(frame["timestamp"], utc=True,
                                  format="mixed").dt.date

    if args.feature not in frame:
        raise SystemExit(f"no column {args.feature!r}")

    values = frame[args.feature]
    mask = pd.Series(True, index=frame.index)
    if args.min is not None:
        mask &= values >= args.min
    if args.max is not None:
        mask &= values <= args.max
    zero_mask = mask & (values == 0)
    if args.drop_zero:
        mask &= values != 0

    inside = frame.loc[mask]
    outside = frame.loc[~mask]
    if inside.empty:
        raise SystemExit("the bucket is empty")

    bounds = f"{args.min if args.min is not None else '-inf'}" \
             f" .. {args.max if args.max is not None else '+inf'}"
    print(f"\n{args.feature}  in  [{bounds}]   side: {args.side}")
    if not args.drop_zero and zero_mask.any():
        print(f"\n  {int(zero_mask.sum())} row(s) in this bucket have the "
              f"feature exactly 0.")
        print("  Re-run with --drop-zero to see the finding without them: on "
              "a range\n  width, 0 means no range was built, not a narrow one.")

    print(f"\n{'':14}{'n':>7}{'win':>7}{'loss':>7}{'win rate':>10}{'mean P&L':>11}")
    print("-" * 56)
    print(line("in bucket", summarise(inside, args.side)))
    print(line("everything", summarise(frame, args.side)))
    print(line("rest", summarise(outside, args.side)))

    print("\nDAY BY DAY, inside the bucket")
    print(f"{'':14}{'n':>7}{'win':>7}{'loss':>7}{'win rate':>10}{'mean P&L':>11}")
    print("-" * 56)
    positive = 0
    days = 0
    contributions = []
    for day, group in inside.groupby("day"):
        stats = summarise(group, args.side)
        print(line(str(day), stats))
        days += 1
        if stats["mean_pnl"] > 0:
            positive += 1
        contributions.append((str(day), stats["mean_pnl"] * stats["n"]))

    total = sum(value for _, value in contributions)
    print()
    print(f"{positive} of {days} day(s) positive")
    if total > 0:
        biggest_day, biggest = max(contributions, key=lambda item: item[1])
        share = biggest / total
        print(f"largest single day: {biggest_day} contributes "
              f"{share:.0%} of the total P&L")
        if share > 0.6:
            print("\n  ONE DAY CARRIES THIS. Remove it and the finding is gone.")
            print("  That is not an edge, it is a day. Wait for more data before")
            print("  spending any more time on this column.")
        elif positive <= days / 2:
            print("\n  Half the days or fewer point the same way. The average is")
            print("  being held up by a minority, which is what noise looks like")
            print("  when you average it.")
        else:
            print("\n  Spread across days, most of them agreeing. That is the")
            print("  shape a real effect has. Still six days - check it again")
            print("  when the collector has twenty.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
