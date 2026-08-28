#!/usr/bin/env python
"""Daily review of the trade journal.

    python daily_report.py            all days
    python daily_report.py --days 7   last 7 days only

Reads every logs/trades-*.csv (not just the newest - each file holds one UTC
day) and every logs/events-*.jsonl, and prints:

  * per-day and overall P&L, win rate, profit factor and expectancy
  * exit-reason breakdown, so you can see whether targets or stops dominate
  * why trades did NOT happen - the veto and block tallies

In week one the bottom section matters more than the top one. If there are no
trades, that section tells you whether the strategy never fired, or fired and
was blocked by spread, news, risk or the analyst.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"


def load_trades(since: datetime | None) -> list[dict]:
    trades: list[dict] = []
    for path in sorted(LOG_DIR.glob("trades-*.csv")):
        with open(path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                closed_at = row.get("closed_at") or ""
                if not closed_at:
                    continue
                try:
                    when = datetime.fromisoformat(closed_at)
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if since and when < since:
                    continue
                try:
                    row["_when"] = when
                    row["_pnl"] = float(row.get("pnl") or 0.0)
                except ValueError:
                    continue
                trades.append(row)
    return trades


def load_events(since: datetime | None) -> list[dict]:
    events: list[dict] = []
    for path in sorted(LOG_DIR.glob("events-*.jsonl")):
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                try:
                    when = datetime.fromisoformat(record.get("time", ""))
                except ValueError:
                    continue
                if since and when < since:
                    continue
                record["_when"] = when
                events.append(record)
    return events


def stats(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "count": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "net": sum(pnls),
        "win_rate": (len(wins) / len(pnls) * 100) if pnls else 0.0,
        # Undefined rather than 0 when nothing lost yet - a profit factor of
        # "inf" on three trades is not an edge, it is a small sample.
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (gross_loss / len(losses)) if losses else 0.0,
        "expectancy": (sum(pnls) / len(pnls)) if pnls else 0.0,
    }


def _pf(value: float) -> str:
    return "  n/a" if value == float("inf") else f"{value:5.2f}"


def report(days: int | None = None) -> int:
    since = (
        datetime.now(timezone.utc) - timedelta(days=days) if days else None
    )
    trades = load_trades(since)
    events = load_events(since)

    print()
    print("=" * 78)
    print("TRADE PERFORMANCE" + (f"  (last {days} days)" if days else "  (all data)"))
    print("=" * 78)

    if not trades:
        print("No closed trades yet.")
    else:
        by_day: dict[str, list[float]] = defaultdict(list)
        for trade in trades:
            by_day[trade["_when"].strftime("%Y-%m-%d")].append(trade["_pnl"])

        print(
            f"{'Date':<12}{'N':>4}{'W':>4}{'L':>4}{'Net':>9}{'Cumul':>9}"
            f"{'Win%':>7}{'PF':>7}{'AvgW':>8}{'AvgL':>8}"
        )
        print("-" * 78)
        cumulative = 0.0
        for day in sorted(by_day):
            row = stats(by_day[day])
            cumulative += row["net"]
            print(
                f"{day:<12}{row['count']:>4}{row['wins']:>4}{row['losses']:>4}"
                f"{row['net']:>9.2f}{cumulative:>9.2f}{row['win_rate']:>6.1f}%"
                f"{_pf(row['profit_factor']):>7}"
                f"{row['avg_win']:>8.2f}{row['avg_loss']:>8.2f}"
            )

        overall = stats([t["_pnl"] for t in trades])
        print("-" * 78)
        print(
            f"{'OVERALL':<12}{overall['count']:>4}{overall['wins']:>4}"
            f"{overall['losses']:>4}{overall['net']:>9.2f}{'':>9}"
            f"{overall['win_rate']:>6.1f}%{_pf(overall['profit_factor']):>7}"
            f"{overall['avg_win']:>8.2f}{overall['avg_loss']:>8.2f}"
        )
        print()
        print(f"  Expectancy per trade : {overall['expectancy']:+.3f}")
        print(f"  Largest win / loss   : "
              f"{max(t['_pnl'] for t in trades):+.2f} / "
              f"{min(t['_pnl'] for t in trades):+.2f}")

        by_symbol: dict[str, list[float]] = defaultdict(list)
        by_reason: Counter = Counter()
        for trade in trades:
            by_symbol[trade.get("symbol", "?")].append(trade["_pnl"])
            by_reason[trade.get("exit_reason", "?")] += 1
        print()
        print("  By symbol:")
        for symbol, pnls in sorted(by_symbol.items()):
            row = stats(pnls)
            print(
                f"    {symbol:<9} {row['count']:>3} trades  net {row['net']:>8.2f}"
                f"  win {row['win_rate']:>5.1f}%  PF {_pf(row['profit_factor'])}"
            )
        print("  Exit reasons: " + ", ".join(
            f"{reason} x{count}" for reason, count in by_reason.most_common()
        ))

        if overall["count"] < 30:
            print()
            print(
                f"  NOTE: {overall['count']} trades is too small a sample to judge."
                "\n  Profit factor needs roughly 30-50 trades before it means"
                "\n  anything, and 100+ before it means much."
            )

    # ---------------------------------------------------------------- blocks
    print()
    print("=" * 78)
    print("WHY TRADES DID NOT HAPPEN")
    print("=" * 78)

    if not events:
        print("No journal events found. Has the engine run yet?")
        return 0

    kinds = Counter(event.get("kind", "?") for event in events)
    latest_vetoes: dict[str, int] = {}
    for event in events:
        if event.get("kind") == "veto_summary":
            latest_vetoes = event.get("counts", {}) or latest_vetoes

    if latest_vetoes:
        print("Strategy vetoes (cumulative, most recent snapshot):")
        for key, count in sorted(
            latest_vetoes.items(), key=lambda item: -item[1]
        )[:12]:
            print(f"  {count:>7}  {key}")
    else:
        print("No strategy-veto snapshots yet (they are written every 5 minutes).")

    print()
    print("Gate blocks (signal was tradable, something downstream said no):")
    for kind in ("blocked_news", "blocked_analyst", "blocked_risk", "signal_disarmed"):
        count = kinds.get(kind, 0)
        if not count:
            continue
        reasons = Counter(
            event.get("reason", "")
            for event in events
            if event.get("kind") == kind and event.get("reason")
        )
        detail = "; ".join(f"{r} (x{c})" for r, c in reasons.most_common(3))
        print(f"  {kind:<18} x{count}" + (f"   {detail}" if detail else ""))
    if not any(kinds.get(k) for k in
               ("blocked_news", "blocked_analyst", "blocked_risk", "signal_disarmed")):
        print("  none")

    print()
    print(f"Trades opened: {kinds.get('trade_opened', 0)} | "
          f"closed: {kinds.get('trade_closed', 0)} | "
          f"order errors: {kinds.get('order_error', 0)}")
    print()
    return 0


def main() -> int:
    global LOG_DIR
    parser = argparse.ArgumentParser(description="Daily trade journal report")
    parser.add_argument("--days", type=int, default=None,
                        help="only consider the last N days")
    parser.add_argument("--logs", type=Path, default=None,
                        help="read journals from a different directory")
    args = parser.parse_args()
    if args.logs:
        LOG_DIR = args.logs
    if not LOG_DIR.exists():
        print(f"No logs directory at {LOG_DIR}")
        return 1
    return report(args.days)


if __name__ == "__main__":
    raise SystemExit(main())
