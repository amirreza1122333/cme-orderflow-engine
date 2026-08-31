"""Is the label economically meaningful, or only statistically balanced?

    python inspect_labels.py data/training_data.csv

The class-balance note in ict/prepare.py asks a statistical question: is WAIT
between 40% and 85%. That is worth asking, but it is not the one that decides
whether a trained model can make money.

The threshold decides which moves count as directional. If it sits below the
spread, the label calls a move "BUY" that could not be entered and exited at a
profit - the move is smaller than the cost of taking it. A model can then
learn that label perfectly and still lose on every trade. Balanced classes
will not warn you: they look healthy either way.

So this prints the threshold next to the things it has to beat - the spread
actually observed in the data, and the stop and target the engine is
configured to use - and then sweeps THRESHOLD_MULT so choosing a new one is a
decision with numbers under it rather than a guess.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

# From ict/prepare.py. Kept here so this script stays runnable on its own.
CLOSE_TO_RANGE = 1.0 / 0.7
SWEEP = (0.30, 0.40, 0.50, 0.60, 0.70, 0.85, 1.00, 1.25)


def reference_distances() -> dict[str, float]:
    """stop/target/max_spread from config.py, if it can be imported."""
    try:
        sys.path.insert(0, str(Path.cwd()))
        from config import load_settings

        settings = load_settings()
        pool = {**settings.symbols, **getattr(settings, "research_symbols", {})}
        config = pool["XAUUSD"]
        return {
            "max_spread": config.max_spread,
            "stop_distance": config.stop_distance,
            "target_distance": config.target_distance,
        }
    except Exception as error:  # config is optional context, never a blocker
        print(f"(could not read config.py for reference distances: {error})\n")
        return {}


def main(argv: list[str]) -> int:
    path = Path(argv[0] if argv else "data/training_data.csv")
    if not path.exists():
        raise SystemExit(f"not found: {path}")

    frame = pd.read_csv(path)
    for column in ("price_change", "threshold", "spread"):
        if column not in frame:
            raise SystemExit(
                f"{path} has no '{column}' column. Columns are:\n  "
                + ", ".join(frame.columns)
            )

    move = frame["price_change"].abs()
    threshold = frame["threshold"].abs()
    spread = frame["spread"].abs()

    print(f"\n{len(frame):,} labelled rows\n")
    print(f"{'':22}{'median':>12}{'mean':>12}{'p90':>12}")
    print("-" * 58)
    for name, series in (
        ("|price change| 5m", move),
        ("threshold", threshold),
        ("spread", spread),
    ):
        print(
            f"{name:22}{series.median():>12.4f}{series.mean():>12.4f}"
            f"{series.quantile(0.90):>12.4f}"
        )

    print()
    median_threshold = threshold.median()
    median_spread = spread.median()
    print("THE COMPARISON THAT MATTERS")
    if median_spread > 0:
        ratio = median_threshold / median_spread
        verdict = (
            "BELOW the spread - a 'directional' label can be a move too small "
            "to trade"
            if ratio < 1
            else "above the spread"
        )
        print(f"  threshold / spread          {ratio:6.2f}x   {verdict}")
    else:
        print("  spread is zero in this data (aggregated .scid records carry")
        print("  no quotes), so this check cannot run. Compare against the")
        print("  configured max_spread below instead.")

    for name, value in reference_distances().items():
        if value:
            print(f"  threshold / {name:16}{median_threshold / value:6.2f}x")

    print()
    print("SWEEP: class balance at other THRESHOLD_MULT values")
    print("  (threshold scales linearly, so this is exact, not a simulation)")
    print()
    print(f"{'MULT':>8}{'threshold':>12}{'BUY':>9}{'WAIT':>9}{'SELL':>9}")
    print("-" * 47)
    # threshold = MULT * CLOSE_TO_RANGE * atr_est. MULT is the only term that
    # changes here, so a new threshold is the current one scaled by the ratio
    # of multipliers - exact arithmetic, not a re-simulation.
    current_mult = _current_mult(threshold, frame)
    signed = frame["price_change"]
    for mult in SWEEP:
        scaled = threshold * (mult / current_mult)
        buy = (signed > scaled).mean()
        sell = (signed < -scaled).mean()
        wait = 1.0 - buy - sell
        flag = "" if 0.40 <= wait <= 0.85 else "   outside 40-85%"
        print(
            f"{mult:>8.2f}{scaled.median():>12.4f}{buy:>8.1%}{wait:>9.1%}"
            f"{sell:>9.1%}{flag}"
        )

    print()
    print("Pick a MULT for what a tradeable move actually costs, then check the")
    print("balance is inside the band - not the other way round.")
    return 0


def _current_mult(threshold: pd.Series, frame: pd.DataFrame) -> float:
    """Recover THRESHOLD_MULT from the data rather than trusting a constant.

    ict/prepare.py computes it in two steps:

        atr_est   = mean|close-to-close| * CLOSE_TO_RANGE
        threshold = atr_est * THRESHOLD_MULT

    so CLOSE_TO_RANGE is already inside atr_est and must NOT be applied again
    here. An earlier version of this function divided by it a second time and
    every label in the sweep column came out wrong by a factor of 1/0.7 - the
    thresholds were right, the multipliers naming them were not.
    """
    if "atr_est" in frame:
        ratio = (threshold / frame["atr_est"].abs()).median()
        if pd.notna(ratio) and ratio > 0:
            return float(ratio)
    return 0.30   # the value in ict/prepare.py at the time of writing


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
