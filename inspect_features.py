"""Audit a collected feature set before it goes anywhere near a model.

    python inspect_features.py features

Three questions, none of which a training script will ask for you:

1. WHICH COLUMNS ARE DEAD?  A column with one distinct value carries no
   information. On this dataset the l2_* family is the obvious candidate: the
   .scid file holds trades and top-of-book quotes, not depth, so there is no
   order book to compute an imbalance from. A model quietly ignores those
   columns; a README that counts them as features does not.

2. WHICH COLUMNS DRIFT WITH TIME?  Raw price levels (bid, ask, mid) rose
   monotonically across this window. In a walk-forward split, train is the
   early period and test the late one, so a single split on `mid` separates
   them perfectly. That is not signal, it is the calendar leaking in through
   a feature. Reported here as the correlation between each column and row
   order - anything above about 0.9 is a calendar proxy.

3. WHAT DO THE LABELS LOOK LIKE?  future_mid_5m and future_direction live in
   the same CSV as the features. They must never reach the model as inputs -
   future_mid_5m > mid IS the label, one subtraction away. This prints them
   so their presence is a decision you made rather than one you inherited.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

LABELS = ["future_mid_5m", "future_direction"]
PRICEY = ["bid", "ask", "mid"]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python inspect_features.py <features_dir>")
        return 2

    directory = Path(argv[0])
    files = sorted(directory.glob("ict_*.csv"))
    if not files:
        raise SystemExit(f"no ict_*.csv in {directory}")

    frame = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)
    print(f"\n{len(files)} file(s), {len(frame):,} rows, {len(frame.columns)} columns\n")

    numeric = frame.select_dtypes("number")
    order = pd.Series(range(len(frame)), index=frame.index, dtype=float)

    dead: list[str] = []
    drifting: list[tuple[str, float]] = []

    print(f"{'column':32}{'unique':>8}{'min':>14}{'max':>14}{'corr(t)':>10}")
    print("-" * 78)
    for name in frame.columns:
        if name not in numeric:
            uniq = frame[name].nunique()
            print(f"{name:32}{uniq:>8}{'(text)':>14}{'':>14}{'':>10}")
            continue
        column = numeric[name]
        uniq = column.nunique()
        # A constant column has zero variance, so correlating it divides by
        # zero. Skip it rather than let numpy warn about arithmetic we already
        # know is undefined.
        corr = column.corr(order) if uniq > 1 else float("nan")
        corr_text = "-" if pd.isna(corr) else f"{corr:+.2f}"
        flag = ""
        if uniq <= 1:
            dead.append(name)
            flag = "  DEAD"
        elif not pd.isna(corr) and abs(corr) > 0.9 and name not in LABELS:
            drifting.append((name, corr))
            flag = "  DRIFT"
        print(
            f"{name:32}{uniq:>8}{column.min():>14.5f}{column.max():>14.5f}"
            f"{corr_text:>10}{flag}"
        )

    print()
    if dead:
        print(f"DEAD ({len(dead)}): one distinct value, no information at all")
        for name in dead:
            print(f"  {name}  = {frame[name].iloc[0]}")
        print(f"  -> {len(frame.columns) - len(LABELS) - len(dead)} live feature "
              f"columns, not {len(frame.columns) - len(LABELS)}. Say the real "
              f"number wherever you quote one.")
        print()

    if drifting:
        print("DRIFT: correlates with row order above 0.9 - in a walk-forward")
        print("split this separates train from test on its own, which is the")
        print("calendar leaking in through a feature, not signal.")
        for name, corr in drifting:
            note = "  (raw price level)" if name in PRICEY else ""
            print(f"  {name:30} corr {corr:+.3f}{note}")
        print()

    for name in LABELS:
        if name not in frame:
            print(f"LABEL {name}: NOT PRESENT")
            continue
        column = frame[name]
        print(f"LABEL {name}: {column.nunique()} distinct")
        if column.nunique() <= 5:
            counts = column.value_counts().sort_index()
            for value, count in counts.items():
                print(f"  {value:>10}   {count:>6,}  {count / len(frame):6.1%}")
        else:
            print(f"  min {column.min():.5f}  max {column.max():.5f}")
    print()

    if "daily_bias" in frame:
        print("daily_bias (the strict three-candle rule):")
        counts = frame["daily_bias"].value_counts().sort_index()
        for value, count in counts.items():
            print(f"  {value:>10}   {count:>6,}  {count / len(frame):6.1%}")
        print()

    print("Next: open ict/prepare.py and confirm both label columns are dropped")
    print("from X. future_mid_5m > mid is the label - if it survives into the")
    print("feature matrix the model scores near-perfectly and means nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
