#!/usr/bin/env python
"""Turn a Databento MBP-10 file into the L2 features the engine already has.

    python depth_features.py data/depth/GC_v_0_mbp-10_2026-08-26_2026-08-27.dbn.zst

Writes one row per five-minute bar: the top-ten book as it stood at the bar
boundary, plus `l2_imbalance_5` and `l2_imbalance_3`. Those two names are not
new - they are four of the thirty-six features `ict/features.py` already
declares, and they have been constant zero on every dataset so far because
the .scid file holds trades and top-of-book only. This is what fills them in.

THE FORMULA IS IMPORTED, NOT COPIED

`imbalance` comes from `ict.features`. Writing it out again here would give
two definitions of one quantity, and the version that drifts is always the
one nobody is looking at. The same reason diagnose.py imports its boundary
rule from edgar.py rather than keeping a copy.

THE SNAPSHOT IS THE LAST ONE BEFORE THE BOUNDARY, NOT AN AVERAGE

The book updates thousands of times a second, so a five-minute bar contains
millions of states and any single number is a choice. A time-weighted mean is
the smoother, better-behaved statistic - and it is the wrong one. Live, the
engine reads the book at the moment it decides, which is one instantaneous
state. Train a model on a mean it will never be shown and the feature it
learned does not exist at decision time. Matching how the value will be read
matters more than making it well-behaved.

ZERO MEANS BALANCED, AND SOMETIMES IT MEANS NOTHING WAS THERE

`imbalance` returns 0.0 for a balanced book and 0.0 for an empty one. That
collapse is exactly how four dead features sat in the dataset for weeks
looking like measurements: their constant zero read as "perfectly balanced"
when it meant "this feed has no depth". So every row also carries
`l2_levels`, the number of populated levels behind the number. A zero with
ten levels behind it is a fact; a zero with none is a gap, and the two must
never print the same again.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ict.features import imbalance  # noqa: E402  the one definition

UNDEF_PRICE = 9_223_372_036_854_775_807
PRICE_SCALE = 1_000_000_000
BAR_NS = 5 * 60 * 1_000_000_000


@dataclass(frozen=True)
class Level:
    """What `ict.features.imbalance` expects: a price and a size."""
    price: float
    size: float


def sides(record) -> tuple[list[Level], list[Level]]:
    """The populated levels of one book snapshot, best first.

    An undefined price is a level that does not exist. It is skipped rather
    than counted as a level of size zero - the difference decides whether
    `l2_levels` reports the depth of the book or the width of the array.
    """
    bids: list[Level] = []
    asks: list[Level] = []
    for pair in record.levels:
        if pair.bid_px != UNDEF_PRICE and pair.bid_sz > 0:
            bids.append(Level(pair.bid_px / PRICE_SCALE, float(pair.bid_sz)))
        if pair.ask_px != UNDEF_PRICE and pair.ask_sz > 0:
            asks.append(Level(pair.ask_px / PRICE_SCALE, float(pair.ask_sz)))
    return bids, asks


def bar_start(ts_ns: int) -> int:
    return ts_ns - (ts_ns % BAR_NS)


def row_for(ts_ns: int, bids: list[Level], asks: list[Level]) -> dict:
    stamp = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
    row = {
        "timestamp": stamp.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "l2_levels": len(bids) + len(asks),
        "l2_bid_vol_total": sum(level.size for level in bids),
        "l2_ask_vol_total": sum(level.size for level in asks),
        "l2_imbalance_5": round(imbalance(bids, asks, 5), 4),
        "l2_imbalance_3": round(imbalance(bids, asks, 3), 4),
    }
    # The raw snapshot travels with the features so `imbalance_at` can be
    # applied later, at whatever price level the session logic picks, without
    # paying for the download again.
    for i in range(10):
        row[f"bid_px_{i}"] = bids[i].price if i < len(bids) else ""
        row[f"bid_sz_{i}"] = bids[i].size if i < len(bids) else ""
        row[f"ask_px_{i}"] = asks[i].price if i < len(asks) else ""
        row[f"ask_sz_{i}"] = asks[i].size if i < len(asks) else ""
    return row


def convert(path: Path, timestamp: str = "event") -> list[dict]:
    """Last book state in each five-minute bucket."""
    import databento as db

    store = db.DBNStore.from_file(path)
    last: dict[int, tuple[int, object]] = {}
    seen = 0

    for record in store:
        if not hasattr(record, "levels"):
            continue                       # symbol mappings, system messages
        ts = getattr(record, f"ts_{'event' if timestamp == 'event' else 'recv'}")
        if not ts:
            ts = record.ts_recv
        seen += 1
        bucket = bar_start(ts)
        if bucket not in last or ts >= last[bucket][0]:
            last[bucket] = (ts, record)

    print(f"{seen:,} book updates -> {len(last):,} five-minute bars")
    rows = []
    for bucket in sorted(last):
        _, record = last[bucket]
        bids, asks = sides(record)
        rows.append(row_for(bucket, bids, asks))
    return rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="a .dbn or .dbn.zst file")
    parser.add_argument("--out", type=Path, default=None,
                        help="default: same name with a .csv extension")
    parser.add_argument("--timestamp", choices=["event", "recv"],
                        default="event",
                        help="event is the exchange's own clock and is what "
                             "aligns with the .scid data; recv is Databento's "
                             "capture time")
    args = parser.parse_args(argv)

    if not args.path.exists():
        raise SystemExit(f"{args.path} does not exist")

    rows = convert(args.path, args.timestamp)
    if not rows:
        raise SystemExit("no book snapshots found in that file")

    out = args.out or args.path.with_suffix("").with_suffix(".csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    empty = sum(1 for r in rows if r["l2_levels"] == 0)
    print(f"wrote {out}")
    print(f"{'bars':16}{len(rows):>8,}")
    print(f"{'with no depth':16}{empty:>8,}"
          + ("  <- these are gaps, not balanced books" if empty else ""))
    live = [r for r in rows if r["l2_levels"]]
    if live:
        vals = [r["l2_imbalance_5"] for r in live]
        print(f"{'imbalance_5':16}{min(vals):>8.4f} to {max(vals):.4f}, "
              f"mean {sum(vals) / len(vals):+.4f}")
        if min(vals) == max(vals):
            print("  Constant across every bar. That is the signature of a "
                  "dead feature, not a calm market.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
