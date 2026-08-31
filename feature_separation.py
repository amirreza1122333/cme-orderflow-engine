"""Does any single feature separate winning entries from losing ones?

    python feature_separation.py data/triple_barrier.csv

WHY BEFORE TRAINING

A model on 1,319 rows and 30 features will find separation whether or not any
exists - that is what overfitting is. Asking one feature at a time is a
smaller question with a much clearer answer: bucket the rows by that feature
and see whether the trade outcome differs across buckets.

If nothing separates here, a model has nothing real to learn from, and any
score it reports is memorised. If something does separate, you know which
feature and can go look at why - which is the part that tells you whether it
is a market mechanism or an accident of six days.

THE PART MOST PEOPLE SKIP

Testing 30 features and reporting the best one is not one test, it is thirty.
The best of thirty noise features looks impressive by construction. So the
bar here is not "is this bucket profitable" but "is it more profitable than
the best bucket of the best feature when the outcomes are SHUFFLED".

The permutation null does exactly that: it keeps every feature and every
bucket boundary, and only breaks the link between a row and its outcome. Any
separation surviving that is separation the shuffling could not manufacture.
It is still not proof - six days is six days - but it is the difference
between a finding and a coincidence.

Buckets below --min-rows are ignored. A bucket of twenty rows can show any
mean you like, and the best of many small buckets is always noise.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

EXCLUDE_PREFIXES = ("tb_",)
EXCLUDE = {"timestamp", "label", "future_mid", "future_mid_5m", "future_direction",
           "price_change", "atr_est", "threshold", "spread", "mid", "bid", "ask"}


def bucket_codes(values: np.ndarray, bins: int) -> tuple[np.ndarray, int, list[str]]:
    """Bucket a column, falling back to distinct values when there are few.

    Quantile edges on a column with many ties collapse, so a boolean feature
    would silently become one bucket and report no separation for the wrong
    reason.
    """
    distinct = np.unique(values[~np.isnan(values)])
    if len(distinct) <= bins:
        lookup = {value: index for index, value in enumerate(distinct)}
        codes = np.array([lookup.get(v, -1) for v in values], dtype="int64")
        return codes, len(distinct), [f"{v:g}" for v in distinct]

    edges = np.unique(np.nanquantile(values, np.linspace(0, 1, bins + 1)))
    codes = np.clip(np.searchsorted(edges, values, side="right") - 1,
                    0, len(edges) - 2)
    labels = [f"{edges[i]:.4g}..{edges[i+1]:.4g}" for i in range(len(edges) - 1)]
    return codes.astype("int64"), len(edges) - 1, labels


def best_bucket_mean(codes: np.ndarray, k: int, pnl: np.ndarray,
                     min_rows: int) -> tuple[float, int]:
    """Highest mean P&L among buckets holding at least `min_rows` rows."""
    counts = np.bincount(codes, minlength=k)
    totals = np.bincount(codes, weights=pnl, minlength=k)
    with np.errstate(invalid="ignore", divide="ignore"):
        means = np.where(counts > 0, totals / np.maximum(counts, 1), -np.inf)
    means = np.where(counts >= min_rows, means, -np.inf)
    if not np.isfinite(means).any():
        return -np.inf, -1
    index = int(np.argmax(means))
    return float(means[index]), index


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", nargs="?", type=Path,
                        default=Path("data/triple_barrier.csv"))
    parser.add_argument("--bins", type=int, default=5)
    parser.add_argument("--min-rows", type=int, default=80)
    parser.add_argument("--permutations", type=int, default=500)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args(argv)

    frame = pd.read_csv(args.path)
    if "tb_truncated" in frame:
        frame = frame.loc[~frame["tb_truncated"]].reset_index(drop=True)
    long_pnl = frame["tb_long_pnl"].to_numpy(dtype="float64")
    short_pnl = frame["tb_short_pnl"].to_numpy(dtype="float64")

    print(f"\n{len(frame):,} rows, buckets of at least {args.min_rows}")
    print(f"baseline: every long {long_pnl.mean():+.4f}   "
          f"every short {short_pnl.mean():+.4f}   per trade\n")

    candidates = []
    for name in frame.columns:
        if name in EXCLUDE or name.startswith(EXCLUDE_PREFIXES):
            continue
        column = frame[name]
        if not pd.api.types.is_numeric_dtype(column):
            continue
        if column.nunique() <= 1:
            continue
        candidates.append(name)

    if not candidates:
        raise SystemExit("no usable feature columns found")

    prepared = {}
    for name in candidates:
        values = frame[name].to_numpy(dtype="float64")
        codes, k, labels = bucket_codes(values, args.bins)
        prepared[name] = (codes, k, labels)

    rows = []
    for name, (codes, k, labels) in prepared.items():
        for side, pnl in (("long", long_pnl), ("short", short_pnl)):
            mean, index = best_bucket_mean(codes, k, pnl, args.min_rows)
            if index < 0:
                continue
            mask = codes == index
            wins = (frame.loc[mask, f"tb_{side}"] == 1).sum()
            losses = (frame.loc[mask, f"tb_{side}"] == -1).sum()
            decided = wins + losses
            rows.append({
                "feature": name,
                "side": side,
                "bucket": labels[index],
                "n": int(mask.sum()),
                "mean_pnl": mean,
                "win_rate": wins / decided if decided else float("nan"),
            })

    results = pd.DataFrame(rows).sort_values("mean_pnl", ascending=False)

    # The null: keep every feature and every bucket edge, break only the link
    # between a row and its outcome, and record the best bucket found anywhere.
    rng = np.random.default_rng(args.seed)
    null = np.empty(args.permutations)
    order = np.arange(len(frame))
    for i in range(args.permutations):
        rng.shuffle(order)
        shuffled_long, shuffled_short = long_pnl[order], short_pnl[order]
        best = -np.inf
        for codes, k, _ in prepared.values():
            for pnl in (shuffled_long, shuffled_short):
                value, _ = best_bucket_mean(codes, k, pnl, args.min_rows)
                best = max(best, value)
        null[i] = best

    bar = float(np.quantile(null, 0.95))

    print(f"{'feature':30}{'side':7}{'bucket':22}{'n':>6}"
          f"{'mean P&L':>11}{'win rate':>10}")
    print("-" * 86)
    for _, row in results.head(15).iterrows():
        flag = "   PASSES" if row["mean_pnl"] > bar else ""
        rate = "-" if pd.isna(row["win_rate"]) else f"{row['win_rate']:.1%}"
        print(f"{row['feature']:30}{row['side']:7}{row['bucket']:22}"
              f"{row['n']:>6}{row['mean_pnl']:>+11.4f}{rate:>10}{flag}")

    passed = int((results["mean_pnl"] > bar).sum())
    print()
    print(f"permutation null ({args.permutations} shuffles):")
    print(f"  best bucket found in SHUFFLED data, 95th percentile: {bar:+.4f}")
    print(f"  median: {np.median(null):+.4f}    max: {null.max():+.4f}")
    print()
    if passed:
        print(f"{passed} feature/side combination(s) beat the shuffled bar.")
        print("That is worth looking at - go read why that bucket is different,")
        print("and check it holds on data the split has not seen. Six days can")
        print("still produce this; the null rules out luck-of-many-features, not")
        print("luck-of-one-week.")
    else:
        print("NOTHING beats the shuffled bar.")
        print("Every apparent separation here is inside what shuffling produces")
        print("on its own. A model trained on these features has no single-column")
        print("signal to build from - which does not prove no interaction exists,")
        print("but does mean a good score from one would deserve heavy suspicion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
