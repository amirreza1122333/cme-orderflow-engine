"""Sierra Chart .scid intraday files -> the replay CSVs the collector reads.

WHY THIS EXISTS

Sierra Chart writes every tick it receives into a binary .scid file next to the
program. That file is the only place this project can get real XAUUSD tick data
without a paid feed subscription: the DTC server refuses connections during a
trial, Dukascopy blocks the datacentre, and every crypto venue is the wrong
instrument. The data is already on disk - it just needs decoding.

THE FORMAT (documented, stable)

    header   56 bytes   "SCID" | HeaderSize u32 | RecordSize u32 | Version u16 | ...
    record   40 bytes   DateTime i64 | Open f32 | High f32 | Low f32 | Close f32
                        NumTrades u32 | TotalVolume u32 | BidVolume u32 | AskVolume u32

`DateTime` is microseconds since 1899-12-30, in UTC.

THE PART THAT MATTERS

Two kinds of record share that layout, and mixing them up silently inverts the
book:

    Open == 0.0     one tick, WITH quotes.  High = ASK, Low = BID, Close = trade
    Open != 0.0     an aggregated bar.      O/H/L/C are ordinary OHLC, no quotes

Sierra Chart calls the first case SINGLE_TRADE_WITH_BID_ASK. In a file that
spans a downloaded history plus your own live recording you get both: the
downloaded part is aggregated and quoteless, the recorded part carries quotes.

For a quoteless record there is no spread to recover, so bid and ask are both
set to the close. That is a real limitation, not a rounding choice: every
spread-derived value over that range is zero, and any gate that compares
against a maximum spread will pass unconditionally. Say so in the write-up
rather than letting a reader assume the spread was measured.
"""

from __future__ import annotations

import argparse
import struct
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, NamedTuple

HEADER_SIZE = 56
RECORD_SIZE = 40
MAGIC = b"SCID"
EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)

_RECORD = struct.Struct("<qffffIIII")
# Records per read(). 200k * 40B = 8MB, big enough that syscalls stop mattering
# and small enough that a 600MB file never lands in memory at once.
_CHUNK_RECORDS = 200_000


class Tick(NamedTuple):
    """One quote, in the shape `SymbolState.add_tick` and `on_tick` take."""

    when: datetime
    bid: float
    ask: float
    volume: int
    has_quotes: bool


class ScidError(Exception):
    """The file is not a readable .scid."""


def read_header(handle) -> tuple[int, int]:
    """Validate the header and return (header_size, record_size).

    The sizes are read from the file rather than assumed: the format is
    versioned, and a future record layout would otherwise be parsed as garbage
    that still looks like plausible prices.
    """
    raw = handle.read(HEADER_SIZE)
    if len(raw) < HEADER_SIZE:
        raise ScidError("file is shorter than a .scid header")

    magic, header_size, record_size, version = struct.unpack("<4sIIH", raw[:14])
    if magic != MAGIC:
        raise ScidError(f"expected magic {MAGIC!r}, found {magic!r}")
    if record_size != RECORD_SIZE:
        raise ScidError(
            f"record size is {record_size}, not {RECORD_SIZE} - this reader "
            f"only understands version 1 records (file says version {version})"
        )
    return header_size, record_size


def read_ticks(
    path: Path,
    start: datetime | None = None,
    end: datetime | None = None,
    every: int = 1,
) -> Iterator[Tick]:
    """Stream the file as `Tick`s, oldest first.

    `start` and `end` are inclusive and compared against the record's own
    timestamp. `every` keeps one record in N - use it to thin a file that is
    denser than the study needs, never to make a broken file smaller.
    """
    if every < 1:
        raise ValueError("every must be >= 1")

    path = Path(path)
    with path.open("rb") as handle:
        header_size, record_size = read_header(handle)
        handle.seek(header_size)

        kept = 0
        chunk_bytes = record_size * _CHUNK_RECORDS

        while True:
            buffer = handle.read(chunk_bytes)
            if not buffer:
                return

            for offset in range(0, len(buffer) - record_size + 1, record_size):
                micros, o, high, low, close, _trades, volume, _bv, _av = (
                    _RECORD.unpack_from(buffer, offset)
                )
                when = EPOCH + timedelta(microseconds=micros)

                if start is not None and when < start:
                    continue
                if end is not None and when > end:
                    return

                kept += 1
                if kept % every:
                    continue

                if o == 0.0:
                    # SINGLE_TRADE_WITH_BID_ASK: High is the ask, Low is the bid.
                    yield Tick(when, low, high, volume, True)
                else:
                    # Aggregated bar. No quotes exist to recover, so the close
                    # stands in for both sides and the spread is zero.
                    yield Tick(when, close, close, volume, False)


def write_daily_csvs(
    scid_path: Path,
    out_dir: Path,
    symbol: str = "XAUUSD",
    start: datetime | None = None,
    end: datetime | None = None,
    every: int = 1,
) -> dict:
    """Write `ict_{symbol}_{YYYYMMDD}.csv` files the dry replay can read.

    One file per UTC day, matching the collector's own output naming so
    `run.py collect --feed dry --source <out_dir>` picks them up unchanged.
    Returns a summary dict.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    handle = None
    current: date | None = None
    rows = 0
    quoted = 0
    files: list[str] = []
    first: datetime | None = None
    last: datetime | None = None

    try:
        for tick in read_ticks(scid_path, start=start, end=end, every=every):
            day = tick.when.date()
            if day != current:
                if handle is not None:
                    handle.close()
                name = f"ict_{symbol}_{day:%Y%m%d}.csv"
                handle = (out_dir / name).open("w", newline="", encoding="utf-8")
                handle.write("timestamp,bid,ask\n")
                files.append(name)
                current = day

            handle.write(f"{tick.when.isoformat()},{tick.bid:.5f},{tick.ask:.5f}\n")
            rows += 1
            quoted += tick.has_quotes
            if first is None:
                first = tick.when
            last = tick.when
    finally:
        if handle is not None:
            handle.close()

    return {
        "rows": rows,
        "with_quotes": quoted,
        "without_quotes": rows - quoted,
        "files": len(files),
        "first": first,
        "last": last,
        "out_dir": str(out_dir),
    }


def _parse_day(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a Sierra Chart .scid file into dry-replay tick CSVs"
    )
    parser.add_argument("--file", type=Path, required=True, help="path to the .scid")
    parser.add_argument("--out", type=Path, default=Path("data"))
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--start", help="YYYY-MM-DD, inclusive")
    parser.add_argument("--end", help="YYYY-MM-DD, inclusive")
    parser.add_argument(
        "--every", type=int, default=1, help="keep one tick in N (default: all)"
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="report what is in the file and write nothing",
    )
    args = parser.parse_args(argv)

    if args.inspect:
        total = quoted = 0
        first = last = None
        switch = None
        for tick in read_ticks(args.file, _parse_day(args.start), _parse_day(args.end)):
            total += 1
            if tick.has_quotes:
                quoted += 1
                if switch is None:
                    switch = tick.when
            if first is None:
                first = tick.when
            last = tick.when
        print(f"ticks           : {total:,}")
        print(f"  with quotes   : {quoted:,}")
        print(f"  without       : {total - quoted:,}")
        print(f"range           : {first}  ->  {last}")
        print(f"quotes start at : {switch}")
        return 0

    summary = write_daily_csvs(
        args.file, args.out, args.symbol,
        _parse_day(args.start), _parse_day(args.end), args.every,
    )
    print(f"wrote {summary['rows']:,} ticks into {summary['files']} file(s)")
    print(f"  with quotes   : {summary['with_quotes']:,}")
    print(f"  without       : {summary['without_quotes']:,}  (spread is 0 for these)")
    print(f"  range         : {summary['first']}  ->  {summary['last']}")
    print(f"  directory     : {summary['out_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
