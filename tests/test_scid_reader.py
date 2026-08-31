"""Spec for core/scid_reader.

The fixtures build a .scid in a temp file rather than shipping one: a real
Sierra Chart file is 600MB, and a synthetic one lets a test state the exact
record mix it is about it.
"""

from __future__ import annotations

import struct
from datetime import datetime, timedelta, timezone

import pytest

from core.scid_reader import (
    EPOCH,
    HEADER_SIZE,
    RECORD_SIZE,
    ScidError,
    read_ticks,
    write_daily_csvs,
)

RECORD = struct.Struct("<qffffIIII")


def header(record_size: int = RECORD_SIZE, version: int = 1) -> bytes:
    raw = struct.pack("<4sIIHHI", b"SCID", HEADER_SIZE, record_size, version, 0, 0)
    return raw + b"\x00" * (HEADER_SIZE - len(raw))


def quoted(when: datetime, bid: float, ask: float, volume: int = 1) -> bytes:
    """A SINGLE_TRADE_WITH_BID_ASK record: Open 0, High ask, Low bid."""
    micros = int((when - EPOCH).total_seconds() * 1_000_000)
    trade = (bid + ask) / 2
    return RECORD.pack(micros, 0.0, ask, bid, trade, 1, volume, 0, 0)


def aggregated(when: datetime, o: float, h: float, l: float, c: float) -> bytes:
    """An ordinary OHLC record - no quotes to recover."""
    micros = int((when - EPOCH).total_seconds() * 1_000_000)
    return RECORD.pack(micros, o, h, l, c, 5, 5, 0, 0)


@pytest.fixture
def base() -> datetime:
    return datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def build(tmp_path, *records: bytes, head: bytes | None = None):
    path = tmp_path / "XAUUSD.scid"
    path.write_bytes((head or header()) + b"".join(records))
    return path


def test_a_quoted_record_maps_high_to_ask_and_low_to_bid(tmp_path, base):
    """Getting this backwards inverts the book and every l2 feature with it."""
    path = build(tmp_path, quoted(base, bid=4456.80, ask=4457.70))

    tick = next(iter(read_ticks(path)))

    assert tick.bid == pytest.approx(4456.80, abs=1e-3)
    assert tick.ask == pytest.approx(4457.70, abs=1e-3)
    assert tick.ask > tick.bid, "an inverted book is the bug this test exists for"
    assert tick.has_quotes is True


def test_an_aggregated_record_has_no_spread(tmp_path, base):
    path = build(tmp_path, aggregated(base, 5036.0, 5037.0, 5035.0, 5036.5))

    tick = next(iter(read_ticks(path)))

    assert tick.bid == tick.ask == pytest.approx(5036.5, abs=1e-3)
    assert tick.has_quotes is False, "callers must be able to tell these apart"


def test_timestamps_are_utc_aware(tmp_path, base):
    tick = next(iter(read_ticks(build(tmp_path, quoted(base, 1.0, 2.0)))))

    assert tick.when.tzinfo is not None
    assert tick.when == base


def test_both_record_kinds_survive_one_file(tmp_path, base):
    """Real files hold downloaded history and live recording end to end."""
    path = build(
        tmp_path,
        aggregated(base, 5036.0, 5037.0, 5035.0, 5036.5),
        aggregated(base + timedelta(seconds=1), 5036.0, 5037.0, 5035.0, 5036.0),
        quoted(base + timedelta(seconds=2), 4456.80, 4457.70),
    )

    ticks = list(read_ticks(path))

    assert [t.has_quotes for t in ticks] == [False, False, True]


def test_the_window_is_inclusive_at_both_ends(tmp_path, base):
    path = build(
        tmp_path,
        quoted(base, 1.0, 2.0),
        quoted(base + timedelta(minutes=1), 3.0, 4.0),
        quoted(base + timedelta(minutes=2), 5.0, 6.0),
    )

    ticks = list(read_ticks(path, start=base, end=base + timedelta(minutes=1)))

    assert len(ticks) == 2


def test_every_thins_without_reordering(tmp_path, base):
    records = [quoted(base + timedelta(seconds=i), i, i + 1) for i in range(10)]
    path = build(tmp_path, *records)

    ticks = list(read_ticks(path, every=3))

    assert len(ticks) == 3
    assert [t.when for t in ticks] == sorted(t.when for t in ticks)


def test_a_wrong_magic_is_refused(tmp_path):
    path = tmp_path / "not.scid"
    path.write_bytes(b"XXXX" + b"\x00" * 100)

    with pytest.raises(ScidError, match="magic"):
        list(read_ticks(path))


def test_an_unknown_record_size_is_refused(tmp_path, base):
    """A future record layout parsed as version 1 yields plausible nonsense."""
    path = build(tmp_path, quoted(base, 1.0, 2.0), head=header(record_size=48))

    with pytest.raises(ScidError, match="record size"):
        list(read_ticks(path))


def test_csvs_are_split_by_utc_day_and_named_for_the_replay_glob(tmp_path, base):
    path = build(
        tmp_path,
        quoted(base, 4456.80, 4457.70),
        quoted(base + timedelta(days=1), 4460.10, 4461.00),
    )
    out = tmp_path / "out"

    summary = write_daily_csvs(path, out, symbol="XAUUSD")

    assert summary["files"] == 2
    assert (out / "ict_XAUUSD_20260820.csv").exists()
    assert (out / "ict_XAUUSD_20260821.csv").exists()


def test_the_csv_header_is_what_the_replay_expects(tmp_path, base):
    path = build(tmp_path, quoted(base, 4456.80, 4457.70))
    out = tmp_path / "out"

    write_daily_csvs(path, out)
    lines = (out / "ict_XAUUSD_20260820.csv").read_text(encoding="utf-8").splitlines()

    assert lines[0] == "timestamp,bid,ask"
    stamp, bid, ask = lines[1].split(",")
    assert datetime.fromisoformat(stamp) == base
    assert float(bid) < float(ask)


def test_the_summary_counts_quoted_and_quoteless_separately(tmp_path, base):
    path = build(
        tmp_path,
        aggregated(base, 5036.0, 5037.0, 5035.0, 5036.5),
        quoted(base + timedelta(seconds=1), 4456.80, 4457.70),
    )

    summary = write_daily_csvs(path, tmp_path / "out")

    assert summary["with_quotes"] == 1
    assert summary["without_quotes"] == 1, "the zero-spread range must be reportable"
