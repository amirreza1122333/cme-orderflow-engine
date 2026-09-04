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


def _feature_files(target: Path) -> list[Path]:
    """One file, or every feature file in a directory.

    Merging six days by hand is six commands and six filenames, and the
    mistake that costs an afternoon is not a crash - it is one day silently
    merged against the wrong depth file, or skipped entirely, discovered
    later as a gap in the training set.
    """
    if target.is_dir():
        found = sorted(p for p in target.glob("ict_*.csv")
                       if not p.stem.endswith("_depth"))
        if not found:
            raise SystemExit(f"no ict_*.csv files in {target}/")
        return found
    return [target]


def _merge_many(targets: list[Path], by_time: dict, args) -> int:
    """Merge each day, then report them together.

    The per-file line matters less than the last two: a run where one day
    matched nothing is a run where the training set quietly lost a day, and
    that has to be visible without reading six blocks of output.
    """
    print(f"{len(targets)} feature files, {len(by_time):,} depth bars\n")
    print(f"  {'file':38}{'rows':>7}{'matched':>9}{'share':>8}")
    print("  " + "-" * 62)

    written, total_rows, total_matched, empty_days = 0, 0, 0, []
    for path in targets:
        rows = load(path, "features")
        merged, matched, basis = _merge_one(rows, by_time)
        share = matched / len(rows) if rows else 0.0
        print(f"  {path.name:38}{len(rows):>7,}{matched:>9,}{share:>7.1%}")
        total_rows += len(rows)
        total_matched += matched
        if share < args.min_match:
            empty_days.append(path.name)
            continue
        _write(merged, path, args, matched, share, basis)
        written += 1

    overall = total_matched / total_rows if total_rows else 0.0
    print(f"\n  {'total':38}{total_rows:>7,}{total_matched:>9,}"
          f"{overall:>7.1%}")
    print(f"\nwrote {written} of {len(targets)} merged files")
    if empty_days:
        print(f"SKIPPED, below --min-match {args.min_match:.0%}: "
              f"{', '.join(empty_days)}")
        print("  Those days have no depth. Training on the rest is fine; "
              "training on all of them is not.")
    return 0


def _merge_one(features: list[dict], by_time: dict):
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
            sample = _basis_of(row, found)
            if sample is not None:
                basis.append(sample)
        else:
            for name in CARRIED:
                out.setdefault(name, "")
        merged.append(out)
    return merged, matched, basis


def _write(merged: list[dict], source: Path, args, matched: int,
           share: float, basis: list[float]) -> Path:
    out_path = source.with_name(source.stem + "_depth.csv")
    with out_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(merged[0]))
        writer.writeheader()
        writer.writerows(merged)
    out_path.with_suffix(".meta.json").write_text(json.dumps({
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "features_from": str(source),
        "depth_from": [str(p) for p in args.depth],
        "rows": len(merged),
        "matched": matched,
        "match_share": round(share, 4),
        "basis_median": (round(sorted(basis)[len(basis) // 2], 2)
                         if basis else None),
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
    return out_path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--features", type=Path, required=True,
                        help="a feature CSV, or a directory of them")
    parser.add_argument("--depth", type=Path, nargs="+", required=True,
                        help="one or more depth CSVs; they are pooled, so a "
                             "multi-day file and a single-day one can be "
                             "given together")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--min-match", type=float, default=0.5,
                        help="fail below this share of feature rows matched")
    args = parser.parse_args(argv)

    # One depth pool, however many files it came in. Whether the caller
    # downloaded five days as one file or five is an accident of billing, not
    # a fact about the data.
    depth: list[dict] = []
    for path in args.depth:
        depth += load(path, "depth")
    if not depth:
        raise SystemExit("the depth file(s) hold no rows")
    by_time = {normalise(row["timestamp"]): row for row in depth}

    targets = _feature_files(args.features)
    if len(targets) > 1:
        return _merge_many(targets, by_time, args)

    features = load(targets[0], "features")
    if not features:
        raise SystemExit(f"{targets[0]} has no rows")
    args.features = targets[0]
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
    if len(pairs) < 8:
        return

    ordered = sorted(pairs)

    def at(share: float) -> float:
        return ordered[min(int(share * len(ordered)), len(ordered) - 1)]

    median = at(0.5)
    iqr = at(0.75) - at(0.25)
    tails = at(0.95) - at(0.05)
    print(f"\n{'basis (GC - spot)':18}{median:>8.2f} median over "
          f"{len(pairs):,} bars")
    print(f"{'  middle half':18}{iqr:>8.2f} points wide")
    print(f"{'  5th to 95th':18}{tails:>8.2f} points wide")
    print(f"{'  full range':18}{ordered[-1] - ordered[0]:>8.2f} points")

    # The judgement is made on the middle half, not the range. min and max
    # are a two-observation statistic: they describe the two most extreme
    # bars and say nothing about the other 274. A handful of bars where the
    # two feeds were sampled a few seconds apart in a fast market will stretch
    # the range without meaning anything, and the same number would be alarming
    # if every bar contributed to it. Deciding on the range is how a finding
    # that lives in one Thursday gets called an edge.
    if iqr > 10:
        print("  The middle half of the bars spans more than ten points. "
              "That is the bulk of the data moving,")
        print("  not a few odd bars - suspect a clock offset or a decoder "
              "before building features on this.")
    elif ordered[-1] - ordered[0] > 3 * max(iqr, 0.01):
        print("  The bulk is tight and the range is not: a few bars are far "
              "out. Most likely the two feeds")
        print("  were sampled seconds apart while gold was moving. Worth a "
              "look, not a blocker.")
    else:
        print("  Tight through the bulk and the tails - the two feeds agree "
              "about gold and the join is sound.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
