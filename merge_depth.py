#!/usr/bin/env python
"""Join order-book depth onto the collector's feature rows.

    python merge_depth.py --features data/ict_XAUUSD_20260826.csv \
                          --depth data/depth/GC_v_0_mbp-10_2026-08-26_2026-08-27.csv

Fills `l2_imbalance_5` and `l2_imbalance_3` - two of the four features that
have been constant zero since the engine was written - and leaves everything
else untouched.

THESE ARE TWO DIFFERENT INSTRUMENTS

The collector's rows are XAUUSD. The depth is GC, the COMEX futures contract.
Gold's price discovery happens in the futures book, so its imbalance is
plausibly informative about spot - but this is a cross-instrument join, not
another column from the same feed, and a model trained on it is being told
about a market it does not trade. The merged file's .meta.json sidecar
records that, because the fact has to survive the moment someone opens the
CSV six months from now with no memory of this. It is a sidecar and not a
comment line in the CSV itself: a `#` header breaks every reader that opens
the file, and a note that damages what it annotates is not documentation.

THE MATCH RATE IS THE TEST

Two CSVs and a timestamp column will always produce a file. Whether the join
meant anything depends on whether the clocks agree, and the only way to find
out is to count. A low match rate here is a timezone bug or a session-hours
mismatch, and it is much cheaper to see it as a number now than as a
mysteriously weak feature after a training run.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

FILLED = ("l2_imbalance_5", "l2_imbalance_3")
CARRIED = ("l2_levels", "l2_bid_vol_total", "l2_ask_vol_total")


def normalise(stamp: str) -> str:
    """One spelling for one instant.

    The collector writes `2026-08-26T14:00:00+00:00`; the depth converter
    writes `2026-08-26T14:00:00Z`. Same moment, two strings, and a dictionary
    lookup that silently matches nothing. Seconds are kept because a
    five-minute bar boundary has them and they are always zero.
    """
    text = stamp.strip().replace("+00:00", "Z")
    if text.endswith("Z"):
        text = text[:-1]
    return text[:19]


def load(path: Path, what: str) -> list[dict]:
    """Read a CSV, or say what is actually there instead of a traceback.

    A missing input is the most ordinary failure a script has, and
    `FileNotFoundError` five frames deep answers "does this path exist" when
    the question is "which file did you mean". Listing the neighbours costs
    one directory read and usually contains the answer - a different date, a
    different symbol, or the fact that the file lives on the other machine.
    """
    if not path.exists():
        folder = path.parent
        nearby = sorted(p.name for p in folder.glob("*.csv")) \
            if folder.exists() else []
        listing = ("\n  ".join(nearby) if nearby
                   else "(no .csv files here at all)")
        raise SystemExit(
            f"{what} file not found: {path}\n"
            f"What {folder}/ actually holds:\n  {listing}"
        )
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--depth", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--min-match", type=float, default=0.5,
                        help="fail below this share of feature rows matched")
    args = parser.parse_args(argv)

    features = load(args.features, "features")
    depth = load(args.depth, "depth")
    if not features:
        raise SystemExit(f"{args.features} has no rows")
    if not depth:
        raise SystemExit(f"{args.depth} has no rows")

    by_time = {normalise(row["timestamp"]): row for row in depth}
    matched = 0
    merged: list[dict] = []
    basis: list[float] = []
    for row in features:
        out = dict(row)
        found = by_time.get(normalise(row["timestamp"]))
        if found:
            matched += 1
            for name in FILLED + CARRIED:
                out[name] = found[name]
            # Both halves of the pair are in hand exactly here. The top of
            # book is deliberately NOT copied into the merged row - the
            # feature contract is thirty-six columns and this file feeds it -
            # so the comparison has to happen now or not at all.
            sample = _basis_of(row, found)
            if sample is not None:
                basis.append(sample)
        else:
            for name in CARRIED:
                out.setdefault(name, "")
        merged.append(out)

    share = matched / len(features)
    print(f"{'feature rows':18}{len(features):>8,}")
    print(f"{'depth rows':18}{len(depth):>8,}")
    print(f"{'matched':18}{matched:>8,}  ({share:.1%})")

    if share < 1.0:
        missing = [normalise(r["timestamp"]) for r in features
                   if normalise(r["timestamp"]) not in by_time]
        print(f"\nfirst unmatched feature timestamps: {missing[:3]}")
        print(f"first depth timestamps            : "
              f"{sorted(by_time)[:3]}")
        print("  If these look like the same times in different zones, the "
              "join is off by an offset, not by data.")

    if share < args.min_match:
        raise SystemExit(
            f"\nonly {share:.1%} of feature rows found depth, below "
            f"--min-match {args.min_match:.0%}. Nothing was written: a merged "
            f"file that is mostly empty columns is worse than no file, "
            f"because it trains."
        )

    out_path = args.out or args.features.with_name(
        args.features.stem + "_depth.csv")
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(merged[0]))
        writer.writeheader()
        writer.writerows(merged)

    # The provenance goes in a sidecar, not in the CSV. The first version
    # wrote it as a leading `#` comment - which csv.DictReader reads as the
    # header, so every column name became a fragment of that sentence and
    # every downstream reader broke. A note that damages the file it
    # annotates is not documentation. The collector already writes
    # .meta.json beside each CSV; this follows it.
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps({
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "features_from": str(args.features),
        "depth_from": str(args.depth),
        "rows": len(merged),
        "matched": matched,
        "match_share": round(share, 4),
        "warning": (
            "CROSS-INSTRUMENT: the l2_* columns are GC (COMEX gold futures); "
            "every other column is XAUUSD. Gold's price discovery happens in "
            "the futures book, so the imbalance is plausibly informative "
            "about spot - but a model trained on this is being told about a "
            "market it does not trade."
        ),
        "columns_filled": list(FILLED),
        "columns_added": list(CARRIED),
    }, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")
    print(f"      {meta_path.name}  (provenance, including the "
          f"cross-instrument warning)")

    live = [r for r in merged if r.get("l2_levels") not in ("", None, "0")]
    if live:
        vals = [float(r["l2_imbalance_5"]) for r in live]
        print(f"{'imbalance_5':18}{min(vals):>8.4f} to {max(vals):.4f}")
        if min(vals) == max(vals):
            print("  Still constant. The merge ran but the feature is not "
                  "alive - check the depth file first.")
    _report_basis(basis)
    return 0


def _basis_of(feature_row: dict, depth_row: dict) -> float | None:
    """Futures mid minus spot mid, when both are readable."""
    try:
        spot = float(feature_row["mid"])
        book = (float(depth_row["bid_px_0"]) + float(depth_row["ask_px_0"])) / 2
    except (KeyError, TypeError, ValueError):
        return None
    return book - spot if spot > 0 and book > 0 else None


def _report_basis(pairs: list[float]) -> None:
    """Do the two instruments agree about the price of gold?

    A cross-instrument join always produces columns; whether it produced a
    consistent picture is a separate question, and the basis - futures price
    minus spot - answers it for free. Gold's basis is a carry cost: it drifts
    slowly with rates and time to expiry and is close to flat over one day.

    So a basis that holds within a few points across the session says both
    feeds decoded correctly and their clocks agree. One that wanders by tens
    of points, or jumps, says a timestamp is off or a decoder is wrong - and
    that is a failure which otherwise shows up only as a feature that
    mysteriously carries no signal, weeks later, with nothing pointing at the
    cause.
    """
    if len(pairs) < 2:
        return

    low, high = min(pairs), max(pairs)
    mean = sum(pairs) / len(pairs)
    print(f"\n{'basis (GC - spot)':18}{mean:>8.2f} mean, "
          f"{low:.2f} to {high:.2f} over {len(pairs):,} bars")
    spread = high - low
    if spread > 20:
        print(f"  It moves {spread:.1f} points across one session. Gold's "
              f"basis is a carry cost and should be nearly flat over a day.")
        print("  Suspect a clock offset or a decoder before trusting any "
              "feature built on this.")
    else:
        print(f"  Steady within {spread:.1f} points - the two feeds agree "
              f"about gold, so the join is sound.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
