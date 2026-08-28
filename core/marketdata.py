"""Live market state: ticks, multi-timeframe bars, and the level-2 order book.

Bars are kept for several timeframes at once from the same tick stream. The
engine trades the *entry* timeframe and uses the higher ones only to decide
which direction is allowed - see `core/strategy.py`.

The depth feed is incremental (a `ProtoOADepthEvent` carries new quotes and the
ids of removed ones), so the book is a dict keyed by quote id, re-sorted on read.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from core.symbols import SymbolSpec

# Timeframes in seconds. The first is where entries are decided; the rest are
# confirmation only. Changing these changes what every indicator period means.
ENTRY_TF = 300                      # 5 minutes
CONFIRM_TFS: tuple[int, ...] = (900, 3600)   # 15 minutes, 1 hour
ALL_TFS: tuple[int, ...] = (ENTRY_TF, *CONFIRM_TFS)

TF_NAMES = {60: "1m", 300: "5m", 900: "15m", 1800: "30m", 3600: "1h", 14400: "4h"}


def tf_name(seconds: int) -> str:
    return TF_NAMES.get(seconds, f"{seconds}s")


@dataclass
class Tick:
    timestamp: datetime
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class Bar:
    start: datetime
    open: float
    high: float
    low: float
    close: float
    ticks: int = 0

    def update(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.ticks += 1


@dataclass
class DepthLevel:
    price: float
    size: float  # base-currency units


@dataclass
class SymbolState:
    spec: SymbolSpec
    timeframes: tuple[int, ...] = ALL_TFS
    max_ticks: int = 2000
    max_bars: int = 400

    ticks: deque[Tick] = field(init=False)
    series: dict[int, deque[Bar]] = field(init=False)
    quotes: dict[int, tuple[str, float, float]] = field(default_factory=dict)
    last_depth_update: datetime | None = None

    def __post_init__(self) -> None:
        self.ticks = deque(maxlen=self.max_ticks)
        self.series = {tf: deque(maxlen=self.max_bars) for tf in self.timeframes}

    # ------------------------------------------------------------------ ticks

    def add_tick(self, bid: float, ask: float, timestamp: datetime | None = None) -> Tick:
        tick = Tick(timestamp or datetime.now(timezone.utc), bid, ask)
        self.ticks.append(tick)
        epoch = int(tick.timestamp.timestamp())
        for period in self.timeframes:
            self._roll(period, epoch, tick.mid)
        return tick

    def _roll(self, period: int, epoch: int, price: float) -> None:
        bucket = epoch - (epoch % period)
        start = datetime.fromtimestamp(bucket, tz=timezone.utc)
        bars = self.series[period]
        if bars and bars[-1].start == start:
            bars[-1].update(price)
        else:
            bars.append(
                Bar(start=start, open=price, high=price, low=price,
                    close=price, ticks=1)
            )

    def seed_bar(self, period: int, start: datetime,
                 o: float, h: float, l: float, c: float) -> None:
        """Insert a historical bar (used to warm indicators at startup)."""
        if period in self.series:
            self.series[period].append(
                Bar(start=start, open=o, high=h, low=l, close=c, ticks=0)
            )

    @property
    def last_tick(self) -> Tick | None:
        return self.ticks[-1] if self.ticks else None

    # ------------------------------------------------------------------- bars

    def bars(self, period: int) -> deque[Bar]:
        return self.series.get(period, deque())

    def bar_count(self, period: int) -> int:
        return len(self.series.get(period, ()))

    def closes(self, period: int, count: int) -> list[float]:
        return [bar.close for bar in list(self.bars(period))[-count:]]

    def highs(self, period: int, count: int) -> list[float]:
        return [bar.high for bar in list(self.bars(period))[-count:]]

    def lows(self, period: int, count: int) -> list[float]:
        return [bar.low for bar in list(self.bars(period))[-count:]]

    def warmed(self, minimum: int) -> bool:
        """True when every timeframe has at least `minimum` bars."""
        return all(self.bar_count(tf) >= minimum for tf in self.timeframes)

    # ---------------------------------------------------------------- spreads

    def median_spread(self, count: int = 200) -> float:
        recent = [tick.spread for tick in list(self.ticks)[-count:]]
        return statistics.median(recent) if recent else 0.0

    def tick_momentum(self, count: int = 60) -> float:
        """Signed mid-price change over the last `count` ticks, in price units."""
        recent = list(self.ticks)[-count:]
        if len(recent) < 2:
            return 0.0
        return recent[-1].mid - recent[0].mid

    # ------------------------------------------------------------------ depth

    def apply_depth(
        self,
        new_quotes: list[tuple[int, str, float, float]],
        deleted_ids: list[int],
        timestamp: datetime | None = None,
    ) -> None:
        for quote_id in deleted_ids:
            self.quotes.pop(quote_id, None)
        for quote_id, side, price, size in new_quotes:
            self.quotes[quote_id] = (side, price, size)
        self.last_depth_update = timestamp or datetime.now(timezone.utc)

    def book(self, levels: int = 5) -> tuple[list[DepthLevel], list[DepthLevel]]:
        bids = [
            DepthLevel(price, size)
            for side, price, size in self.quotes.values()
            if side == "bid"
        ]
        asks = [
            DepthLevel(price, size)
            for side, price, size in self.quotes.values()
            if side == "ask"
        ]
        bids.sort(key=lambda level: -level.price)
        asks.sort(key=lambda level: level.price)
        return bids[:levels], asks[:levels]

    def imbalance(self, levels: int = 5) -> float:
        """-1 (all offers) .. +1 (all bids). 0 when the book is empty.

        Resting size is not intent, and a retail feed shows a fraction of real
        liquidity - one weak input, not a prediction. It matters less on a
        5-minute chart than it did on a 1-minute one, and is weighted
        accordingly in the strategy.
        """
        bids, asks = self.book(levels)
        bid_size = sum(level.size for level in bids)
        ask_size = sum(level.size for level in asks)
        total = bid_size + ask_size
        if total <= 0:
            return 0.0
        return (bid_size - ask_size) / total

    def has_depth(self) -> bool:
        bids, asks = self.book(1)
        return bool(bids and asks)


class MarketData:
    """All per-symbol state, keyed by symbol name."""

    def __init__(self, timeframes: tuple[int, ...] = ALL_TFS) -> None:
        self.timeframes = timeframes
        self._states: dict[str, SymbolState] = {}
        self._by_id: dict[int, SymbolState] = {}

    def register(self, spec: SymbolSpec) -> SymbolState:
        state = SymbolState(spec=spec, timeframes=self.timeframes)
        self._states[spec.name.upper()] = state
        self._by_id[spec.symbol_id] = state
        return state

    def get(self, name: str) -> SymbolState | None:
        return self._states.get(name.upper())

    def by_id(self, symbol_id: int) -> SymbolState | None:
        return self._by_id.get(symbol_id)

    def names(self) -> list[str]:
        return sorted(self._states)

    def __iter__(self):
        return iter(self._states.values())
