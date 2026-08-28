"""Trade and signal journal.

The `contracts` column replaced `lots` in the 2026-08-21 futures pivot. Old
trades-*.csv files from the cTrader era keep their own header and are still
readable; nothing merges the two.

Every decision the engine makes is appended to a JSONL file, and every closed
trade also lands in a CSV you can open in a spreadsheet. This is the only way
to find out whether the strategy actually works, so it is written on the spot
and flushed immediately rather than buffered.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("journal")

TRADE_FIELDS = [
    "closed_at",
    "opened_at",
    "symbol",
    "side",
    "volume",
    "contracts",
    "entry",
    "exit",
    "stop_loss",
    "take_profit",
    "exit_reason",
    "gross_pnl",
    "commission",
    "pnl",
    "balance",
    "mode",
    "confidence",
    "score",
]


class Journal:
    def __init__(self, directory: Path, mode: str) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.mode = mode
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        self.events_path = self.directory / f"events-{stamp}.jsonl"
        self.trades_path = self.directory / f"trades-{stamp}.csv"
        if not self.trades_path.exists():
            with self.trades_path.open("w", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=TRADE_FIELDS).writeheader()

    def event(self, kind: str, **payload) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "mode": self.mode,
            **payload,
        }
        try:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, default=str) + "\n")
        except OSError:
            log.exception("Could not write journal event")

    def trade(self, record: dict) -> None:
        row = {field: record.get(field, "") for field in TRADE_FIELDS}
        row["mode"] = record.get("mode", self.mode)
        try:
            with self.trades_path.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=TRADE_FIELDS).writerow(row)
        except OSError:
            log.exception("Could not write trade row")
        self.event("trade_closed", **record)
