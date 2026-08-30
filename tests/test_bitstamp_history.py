"""Spec for BitstampClient.history().

No network: the HTTP call is isolated in `_get_ohlc`, which these tests
replace. A test that needs the internet is a test that fails on a train.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.bitstamp_client import BitstampClient, DTCError

STEP = 3600


def bar_row(ts: int, close: float = 100.0) -> dict:
    """One row shaped exactly as the endpoint returns it - strings throughout."""
    return {
        "timestamp": str(ts),
        "open": f"{close - 1:.2f}",
        "high": f"{close + 2:.2f}",
        "low": f"{close - 3:.2f}",
        "close": f"{close:.2f}",
        "volume": "1.50000000",
    }


@pytest.fixture
def client() -> BitstampClient:
    return BitstampClient()


def install_pages(client: BitstampClient, pages: list[list[dict]]) -> list[dict]:
    """Serve `pages` in order, recording the query each call was made with."""
    calls: list[dict] = []
    remaining = list(pages)

    def fake(pair, step, start, limit):
        calls.append({"pair": pair, "step": step, "start": start, "limit": limit})
        return remaining.pop(0) if remaining else []

    client._get_ohlc = fake
    return calls


def test_rows_become_bars_with_real_types(client):
    ts = 1_788_000_000
    install_pages(client, [[bar_row(ts, 78_100.0)]])

    bars = client.history(
        "BTC/USD", STEP,
        datetime.fromtimestamp(ts - STEP, tz=timezone.utc),
        datetime.fromtimestamp(ts + STEP, tz=timezone.utc),
    )

    assert len(bars) == 1
    bar = bars[0]
    assert bar.start == datetime.fromtimestamp(1_788_000_000, tz=timezone.utc)
    assert bar.start.tzinfo is not None, "engine bar times are timezone-aware UTC"
    assert isinstance(bar.close, float)
    assert bar.close == pytest.approx(78_100.0)
    assert bar.high == pytest.approx(78_102.0)
    assert bar.volume == pytest.approx(1.5)


def test_bars_come_back_oldest_first(client):
    """The endpoint happens to send ascending rows; the contract promises it,
    so history() sorts rather than trusting the venue to keep doing so."""
    base = 1_788_000_000
    install_pages(client, [[
        bar_row(base + 2 * STEP), bar_row(base), bar_row(base + STEP),
    ]])

    bars = client.history(
        "BTC/USD", STEP,
        datetime.fromtimestamp(base, tz=timezone.utc),
        datetime.fromtimestamp(base + 5 * STEP, tz=timezone.utc),
    )

    assert [int(b.start.timestamp()) for b in bars] == [
        base, base + STEP, base + 2 * STEP,
    ]


def test_an_unsupported_step_fails_loudly(client):
    with pytest.raises(DTCError) as excinfo:
        client.history("BTC/USD", 137, datetime(2026, 8, 1, tzinfo=timezone.utc),
                       datetime(2026, 8, 2, tzinfo=timezone.utc))

    assert "137" in str(excinfo.value), "the message must name the bad step"


def test_it_pages_until_the_window_is_covered(client):
    base = 1_788_000_000
    page_1 = [bar_row(base + i * STEP) for i in range(1000)]
    page_2 = [bar_row(base + (1000 + i) * STEP) for i in range(200)]
    calls = install_pages(client, [page_1, page_2])

    bars = client.history(
        "BTC/USD", STEP,
        datetime.fromtimestamp(base, tz=timezone.utc),
        datetime.fromtimestamp(base + 1300 * STEP, tz=timezone.utc),
    )

    assert len(bars) == 1200
    assert len(calls) >= 2, "1000 is the endpoint's hard limit; one call cannot do it"
    assert calls[1]["start"] > calls[0]["start"], "the cursor must advance"


def test_a_short_page_ends_the_walk(client):
    """A page shorter than the limit means the server has no more."""
    calls = install_pages(client, [[bar_row(1_788_000_000)]])

    client.history("BTC/USD", STEP,
                   datetime(2026, 1, 1, tzinfo=timezone.utc),
                   datetime(2026, 12, 31, tzinfo=timezone.utc))

    assert len(calls) == 1, "do not keep asking once the server runs dry"


def test_no_data_returns_an_empty_list_not_an_error(client):
    """A symbol with no history must not stop the engine from starting."""
    install_pages(client, [[]])

    bars = client.history("BTC/USD", STEP,
                          datetime(2026, 8, 1, tzinfo=timezone.utc),
                          datetime(2026, 8, 2, tzinfo=timezone.utc))

    assert bars == []


def test_max_bars_trims_to_the_most_recent(client):
    base = 1_788_000_000
    install_pages(client, [[bar_row(base + i * STEP) for i in range(10)]])

    bars = client.history(
        "BTC/USD", STEP,
        datetime.fromtimestamp(base, tz=timezone.utc),
        datetime.fromtimestamp(base + 20 * STEP, tz=timezone.utc),
        max_bars=3,
    )

    assert len(bars) == 3
    assert int(bars[-1].start.timestamp()) == base + 9 * STEP, "keep the newest"


def test_bars_outside_the_window_are_dropped(client):
    base = 1_788_000_000
    install_pages(client, [[bar_row(base - STEP), bar_row(base), bar_row(base + 99 * STEP)]])

    bars = client.history(
        "BTC/USD", STEP,
        datetime.fromtimestamp(base, tz=timezone.utc),
        datetime.fromtimestamp(base + STEP, tz=timezone.utc),
    )

    assert [int(b.start.timestamp()) for b in bars] == [base]
