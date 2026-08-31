"""Compare two collector runs that differ only in the Asian accumulation window.

    python compare_presets.py features_spec features_tokyo

The AMD premise is that the Asian session ACCUMULATES - builds a tight range -
and London sweeps its edges. So the two presets are not a matter of taste:
whichever one produces tighter ranges that get swept more often is the one the
premise actually describes. This prints the numbers to decide with.

Reads every ict_*.csv in each directory and reports, per preset:

    asian_range_width   mean and median - narrower is more consistent with
                        "accumulation" than with "the overnight range"
    swept high / low    share of rows, and how many distinct days saw a sweep
                        at all. The day count matters more: a sweep is a
                        once-a-day event, and the row share only measures how
                        long the flag stayed up afterwards.
    returned high / low  confirmed sweep-and-returns - the actual signal
    manipulation_bias   share of rows carrying a non-zero directional bias

A preset that sweeps on more days is finding more of the thing the strategy
trades. One that sweeps on fewer is describing a range London does not care
about.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

FLAGS = [
    "asian_swept_high",
    "asian_swept_low",
    "asian_returned_high",
    "asian_returned_low",
]


def load(directory: Path) -> pd.DataFrame:
    files = sorted(directory.glob("ict_*.csv"))
    if not files:
        raise SystemExit(f"no ict_*.csv files in {directory}")
    frame = pd.concat((pd.read_csv(f) for f in files), ignore_index=True)

    stamp = next(
        (c for c in ("timestamp", "time", "datetime") if c in frame.columns), None
    )
    if stamp is None:
        raise SystemExit(
            f"{directory}: no timestamp column found. Columns are:\n  "
            + ", ".join(frame.columns)
        )
    frame["_day"] = pd.to_datetime(frame[stamp], utc=True, format="mixed").dt.date
    return frame


def describe(frame: pd.DataFrame) -> dict[str, str]:
    out: dict[str, str] = {}
    days = frame["_day"].nunique()
    out["rows"] = f"{len(frame):,}"
    out["days"] = str(days)

    if "asian_range_width" in frame:
        # Zero means no valid range was built that bar; averaging those in
        # would report a narrower range than ever actually existed.
        width = frame.loc[frame["asian_range_width"] > 0, "asian_range_width"]
        out["range width mean"] = f"{width.mean():.2f}" if len(width) else "-"
        out["range width median"] = f"{width.median():.2f}" if len(width) else "-"
        out["rows with a range"] = f"{len(width) / len(frame):6.1%}"
    else:
        out["range width mean"] = "column missing"

    for flag in FLAGS:
        if flag not in frame:
            out[flag] = "column missing"
            continue
        hit = frame[flag] > 0
        day_hits = frame.loc[hit, "_day"].nunique()
        out[flag] = f"{hit.mean():6.1%} of rows   {day_hits}/{days} days"

    if "manipulation_bias" in frame:
        bias = frame["manipulation_bias"]
        out["manipulation_bias"] = (
            f"{(bias != 0).mean():6.1%} non-zero   "
            f"(+1 {(bias > 0).mean():.1%} / -1 {(bias < 0).mean():.1%})"
        )
    return out


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        print("usage: python compare_presets.py <dir_a> <dir_b>")
        return 2

    names = [Path(a).name for a in argv]
    results = [describe(load(Path(a))) for a in argv]

    keys: list[str] = []
    for result in results:
        for key in result:
            if key not in keys:
                keys.append(key)

    label_w = max(len(k) for k in keys) + 2
    col_w = max(
        max((len(v) for r in results for v in r.values()), default=0), len(names[0])
    ) + 2

    print()
    print(" " * label_w + "".join(n.ljust(col_w) for n in names))
    print("-" * (label_w + col_w * 2))
    for key in keys:
        row = "".join(r.get(key, "-").ljust(col_w) for r in results)
        print(key.ljust(label_w) + row)
    print()
    print("The day counts are the ones to read. Row shares mostly measure how")
    print("early in the day a flag went up, not how often the event happened.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
