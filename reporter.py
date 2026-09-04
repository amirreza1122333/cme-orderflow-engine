#!/usr/bin/env python
"""A market journalist. Reports what is there, every few hours.

    python reporter.py --symbol XAUUSD --data data --out reports

Reads the 5-minute rows the collector writes, describes the current state of
the market in prose, and archives the report together with the state it was
written from.

WHAT THIS IS NOT

It is not the trading engine and it does not decide anything. It takes no
position, has no stop, and its output is never an instruction. The engine
answers "should I trade"; this answers "what is going on", which is a
different question and a much easier one.

TWO RULES, BOTH ENFORCED IN CODE

**Every number in the report comes from the data.** Not "should" - cannot. The
renderer can only emit a number through `Ledger.num()`, which records it, and
`verify()` then walks the finished text and fails on any numeral the ledger
never issued. Write `f"gold is at 3850"` into a template and the report is
rejected before it is saved. This matters more than it looks: the whole point
of a later language-model layer is fluent phrasing, and a language model that
invents a price level is worse than no report at all. The guard is here from
report number one so the model can be dropped in behind it without trusting it.

**A comparison against history states its `n`.** "The narrowest range in a
while" is not a fact. "The 3rd narrowest of 42 sessions" is. Below
`MIN_HISTORY` observations the report says the history is too short instead of
computing a percentile from six points and sounding certain about it.

THE ARCHIVE

Each run writes `<timestamp>.md` and `<timestamp>.state.json` side by side.
The report is what you read; the state is what it was written from. Without
the second one, going back later to ask why the report said what it said is
guesswork - and improving the reports over time is exactly that exercise,
repeated. Retrofitting the archive is not possible: the early reports, the
ones with the most to teach, would already be gone.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# Below this many past observations, no percentile is reported. Six sessions
# of data cannot say what is unusual, and a number computed from six points
# reads exactly as confident as one computed from six hundred.
MIN_HISTORY = 30

SESSION_NAMES = {0: "no session", 1: "Asian", 2: "London", 3: "New York"}


# ----------------------------------------------------------------- the ledger

_NUMERAL = re.compile(r"\d+(?:[.,]\d+)*")


class Ledger:
    """Records every number the renderer is allowed to have written.

    The renderer never formats a number itself; it calls `num`, which returns
    the string AND remembers it. `verify` then checks the finished text
    contains no numeral this ledger did not issue.
    """

    def __init__(self) -> None:
        self.emitted: set[str] = set()

    def num(self, value: float | int, fmt: str = "{:.2f}") -> str:
        text = fmt.format(value)
        for token in _NUMERAL.findall(text):
            self.emitted.add(token)
        return text

    def literal(self, text: str) -> str:
        """For a timestamp or symbol that legitimately carries digits."""
        for token in _NUMERAL.findall(text):
            self.emitted.add(token)
        return text


class UnsourcedNumber(ValueError):
    """A number appeared in the report that did not come from the data."""


def verify(report: str, ledger: Ledger) -> None:
    unsourced = sorted(set(_NUMERAL.findall(report)) - ledger.emitted)
    if unsourced:
        raise UnsourcedNumber(
            f"{len(unsourced)} number(s) in the report came from nowhere: "
            f"{', '.join(unsourced)}. Every figure must be read from the "
            f"state, never written into a template."
        )


# ------------------------------------------------------------- observations

@dataclass(frozen=True)
class Observation:
    """One quantity now, and where it sits among the values seen before."""

    name: str
    value: float
    unit: str
    below: int          # how many past values were smaller
    n: int              # how many past values there were

    @property
    def comparable(self) -> bool:
        return self.n >= MIN_HISTORY

    @property
    def percentile(self) -> float | None:
        if not self.comparable:
            return None
        return 100.0 * self.below / self.n

    @property
    def rank_from_bottom(self) -> int:
        return self.below + 1


def observe(name: str, value: float, history: list[float],
            unit: str = "") -> Observation:
    past = [v for v in history if v == v]        # drop NaN
    return Observation(name=name, value=value, unit=unit,
                       below=sum(1 for v in past if v < value), n=len(past))


# -------------------------------------------------------------- reading data

def load_rows(folder: Path, symbol: str) -> list[dict]:
    """Every 5-minute row the collector has written for this symbol, in order.

    Files are one UTC day each. They are read whole rather than tailed: the
    history is the point, and a few days of five-minute rows is a few thousand
    lines.
    """
    rows: list[dict] = []
    for path in sorted(folder.glob(f"ict_{symbol}_*.csv")):
        with path.open(newline="", encoding="utf-8") as handle:
            rows.extend(csv.DictReader(handle))
    rows.sort(key=lambda r: r.get("timestamp", ""))
    return rows


def _f(row: dict, key: str, default: float = float("nan")) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return default


def session_rows(rows: list[dict]) -> dict[str, list[dict]]:
    """Rows grouped by UTC day, so 'per session' comparisons are per day."""
    by_day: dict[str, list[dict]] = {}
    for row in rows:
        day = str(row.get("timestamp", ""))[:10]
        by_day.setdefault(day, []).append(row)
    return by_day


# ------------------------------------------------------------------- the report

@dataclass
class Report:
    text: str
    state: dict
    observations: list[Observation] = field(default_factory=list)


def build(rows: list[dict], symbol: str, now: datetime | None = None) -> Report:
    if not rows:
        raise SystemExit("no rows to report on")

    latest = rows[-1]
    history = rows[:-1]
    ledger = Ledger()
    lines: list[str] = []

    stamp = ledger.literal(str(latest.get("timestamp", "")))
    lines.append(f"## {ledger.literal(symbol)} — {stamp} UTC\n")

    mid = _f(latest, "mid")
    bid, ask = _f(latest, "bid"), _f(latest, "ask")
    session = SESSION_NAMES.get(int(_f(latest, "session", 0)), "unknown")
    progress = _f(latest, "session_progress")

    lines.append(
        f"Mid {ledger.num(mid)}, spread {ledger.num(ask - bid)}. "
        f"{session} session, {ledger.num(progress * 100, '{:.0f}')}% elapsed."
    )

    observations: list[Observation] = []

    # Asian range width, against every past day's final value for it.
    by_day = session_rows(history)
    past_widths = [
        _f(day_rows[-1], "asian_range_width")
        for day_rows in by_day.values() if day_rows
    ]
    width = _f(latest, "asian_range_width")
    if width == width:
        obs = observe("Asian range width", width, past_widths, "points")
        observations.append(obs)
        lines.append(_phrase_rank(obs, ledger, "sessions"))

    # Unfilled fair-value gaps, against every past 5-minute row.
    gaps = _f(latest, "fvg_count_unfilled")
    if gaps == gaps:
        obs = observe("unfilled FVGs", gaps,
                      [_f(r, "fvg_count_unfilled") for r in history], "")
        observations.append(obs)
        lines.append(
            f"{ledger.num(gaps, '{:.0f}')} unfilled fair-value gap(s). "
            + _phrase_rank(obs, ledger, "bars", lead=False)
        )

    # Structure that is either true or not - no percentile needed.
    flags = [
        ("swept the Asian high", _f(latest, "asian_swept_high")),
        ("swept the Asian low", _f(latest, "asian_swept_low")),
        ("returned inside the Asian range from above",
         _f(latest, "asian_returned_high")),
        ("returned inside the Asian range from below",
         _f(latest, "asian_returned_low")),
        ("swept yesterday's high", _f(latest, "swept_prev_high")),
        ("swept yesterday's low", _f(latest, "swept_prev_low")),
    ]
    happened = [name for name, value in flags if value >= 0.5]
    if happened:
        lines.append("Today: " + "; ".join(happened) + ".")
    else:
        lines.append("No sweep or return has been recorded today.")

    # The features that are structurally dead on this feed. Saying so is the
    # difference between a quiet zero and a missing measurement.
    dead = [name for name in ("l2_imbalance_5", "l2_imbalance_3",
                              "l2_imbalance_at_asian_high",
                              "l2_imbalance_at_asian_low")
            if _f(latest, name, 0.0) == 0.0]
    if len(dead) == 4:
        lines.append(
            "Order-book imbalance is unavailable on this feed, so nothing "
            "here describes resting size."
        )

    lines.append(
        f"\n_Built from {ledger.num(len(rows), '{:,}')} five-minute rows "
        f"across {ledger.num(len(by_day) + 1, '{:,}')} day(s)._"
    )

    text = "\n\n".join(lines)
    verify(text, ledger)
    return Report(text=text, state=dict(latest), observations=observations)


def _phrase_rank(obs: Observation, ledger: Ledger, unit: str,
                 lead: bool = True) -> str:
    """Say where a value sits, or say the history is too short to know."""
    head = f"{obs.name.capitalize()} {ledger.num(obs.value)}" if lead else ""
    if not obs.comparable:
        tail = (f"no comparison yet — {ledger.num(obs.n, '{:,}')} past "
                f"{unit} on record, {ledger.num(MIN_HISTORY, '{:,}')} needed")
    else:
        tail = (f"{ledger.num(obs.percentile, '{:.0f}')}th percentile of "
                f"{ledger.num(obs.n, '{:,}')} {unit}")
    return f"{head} — {tail}." if lead else f"{tail.capitalize()}."


def archive(report: Report, out: Path, stamp: str) -> tuple[Path, Path]:
    """Write the report and the state it came from, together.

    Together is the point. A report whose inputs are gone cannot be argued
    with later, and arguing with it later is how the next one gets better.
    """
    out.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^0-9A-Za-z]+", "-", stamp).strip("-")
    md, js = out / f"{safe}.md", out / f"{safe}.state.json"
    md.write_text(report.text, encoding="utf-8")
    js.write_text(json.dumps({
        "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "state": report.state,
        "observations": [
            {"name": o.name, "value": o.value, "below": o.below, "n": o.n,
             "percentile": o.percentile}
            for o in report.observations
        ],
    }, indent=2), encoding="utf-8")
    return md, js


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--out", type=Path, default=Path("reports"))
    parser.add_argument("--print-only", action="store_true",
                        help="render without archiving, for a quick look")
    args = parser.parse_args(argv)

    rows = load_rows(args.data, args.symbol)
    if not rows:
        raise SystemExit(
            f"no ict_{args.symbol}_*.csv in {args.data}/ — is the collector "
            f"running, and is this the machine it writes on?"
        )
    report = build(rows, args.symbol)
    print(report.text)
    if not args.print_only:
        md, js = archive(report, args.out, str(rows[-1].get("timestamp", "")))
        print(f"\narchived {md.name} and {js.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
