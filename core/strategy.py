"""Signal generation - multi-timeframe.

Direction is decided by the higher timeframes; timing is decided by the entry
timeframe. Concretely:

    1h   trend  ->  which direction is permitted at all
    15m  trend  ->  must agree, or the setup is a counter-trend bounce
    5m          ->  the actual entry: pullback into the trend, not an extension

A trade needs all three aligned. That is deliberately restrictive - it means
far fewer signals than the 1-minute version produced, and most of what it
rejects is exactly the chop that a fast chart mistakes for a trend.

The weights below are a starting point, not a measured edge. Change one thing
at a time and judge it against the journal, not against a hunch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from config import SymbolConfig
from core import indicators
from core.marketdata import CONFIRM_TFS, ENTRY_TF, SymbolState, tf_name

FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
ATR_PERIOD = 14
MIN_BARS = SLOW_EMA + 5          # per timeframe

# A higher-timeframe trend only counts when the EMAs are actually separated;
# below this (in ATRs of that timeframe) it is treated as "no opinion".
FLAT_TREND = 0.15


@dataclass
class TrendRead:
    """One timeframe's directional read."""
    period: int
    direction: int = 0        # +1 up, -1 down, 0 flat/unknown
    separation: float = 0.0   # EMA gap in ATRs of that timeframe
    ready: bool = False

    @property
    def label(self) -> str:
        word = {1: "up", -1: "down", 0: "flat"}[self.direction]
        return f"{tf_name(self.period)} {word}"


@dataclass
class Signal:
    symbol: str
    direction: str | None            # "BUY" | "SELL" | None
    confidence: float                # 0..1
    score: float                     # -100..100
    reasons: list[str] = field(default_factory=list)
    vetoes: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    @property
    def tradable(self) -> bool:
        return self.direction is not None and not self.vetoes

    def describe(self) -> str:
        head = f"{self.symbol} {self.direction or 'FLAT'} conf={self.confidence:.2f}"
        if self.vetoes:
            return f"{head} | veto: {'; '.join(self.vetoes)}"
        return f"{head} | {'; '.join(self.reasons)}"


def read_trend(state: SymbolState, period: int) -> TrendRead:
    """EMA trend on one timeframe, scaled by that timeframe's own ATR."""
    read = TrendRead(period=period)
    closes = state.closes(period, 200)
    if len(closes) < MIN_BARS:
        return read

    fast = indicators.ema(closes, FAST_EMA)
    slow = indicators.ema(closes, SLOW_EMA)
    atr = indicators.atr(
        state.highs(period, 200), state.lows(period, 200), closes, ATR_PERIOD
    )
    if fast is None or slow is None:
        return read

    read.ready = True
    gap = fast - slow
    read.separation = abs(gap) / atr if atr else 0.0
    if read.separation >= FLAT_TREND:
        read.direction = 1 if gap > 0 else -1
    return read


class Strategy:
    def __init__(self, config: SymbolConfig) -> None:
        self.config = config

    def evaluate(self, state: SymbolState) -> Signal:
        symbol = state.spec.name
        signal = Signal(symbol=symbol, direction=None, confidence=0.0, score=0.0)

        tick = state.last_tick
        if tick is None:
            signal.vetoes.append("no ticks yet")
            return signal

        # ---- warm-up, per timeframe ---------------------------------------
        for period in (ENTRY_TF, *CONFIRM_TFS):
            have = state.bar_count(period)
            if have < MIN_BARS:
                signal.vetoes.append(
                    f"warming up {tf_name(period)} ({have}/{MIN_BARS} bars)"
                )
        if signal.vetoes:
            return signal

        # ---- higher-timeframe direction gate ------------------------------
        confirms = [read_trend(state, period) for period in CONFIRM_TFS]
        higher = [c.direction for c in confirms]
        signal.context["confirm"] = {
            tf_name(c.period): {
                "direction": c.direction, "separation": round(c.separation, 3)
            }
            for c in confirms
        }

        if any(direction == 0 for direction in higher):
            flat = ", ".join(c.label for c in confirms if c.direction == 0)
            signal.vetoes.append(f"higher timeframe has no trend ({flat})")
            return signal
        if len(set(higher)) > 1:
            signal.vetoes.append(
                "higher timeframes disagree ("
                + ", ".join(c.label for c in confirms) + ")"
            )
            return signal

        allowed = higher[0]                       # +1 or -1, both agree
        allowed_side = "BUY" if allowed > 0 else "SELL"
        signal.reasons.append(
            "; ".join(f"{c.label} (sep {c.separation:.2f})" for c in confirms)
        )

        # ---- entry timeframe ----------------------------------------------
        closes = state.closes(ENTRY_TF, 200)
        fast = indicators.ema(closes, FAST_EMA)
        slow = indicators.ema(closes, SLOW_EMA)
        rsi = indicators.rsi(closes, RSI_PERIOD)
        atr = indicators.atr(
            state.highs(ENTRY_TF, 200), state.lows(ENTRY_TF, 200), closes, ATR_PERIOD
        )
        if fast is None or slow is None or rsi is None:
            signal.vetoes.append("entry indicators not ready")
            return signal

        price = tick.mid
        spread = tick.spread
        median_spread = state.median_spread()
        imbalance = state.imbalance(self.config.depth_levels)
        entry_up = fast > slow

        signal.context.update({
            "price": price,
            "spread": spread,
            "median_spread": median_spread,
            "ema_fast": fast,
            "ema_slow": slow,
            "rsi": rsi,
            "atr": atr,
            "imbalance": imbalance,
            "has_depth": state.has_depth(),
            "allowed_side": allowed_side,
        })

        # ---- hard vetoes ---------------------------------------------------
        if self.config.max_spread and spread > self.config.max_spread:
            signal.vetoes.append(
                f"spread {spread:.5f} > max {self.config.max_spread:.5f}"
            )
        elif median_spread > 0 and spread > median_spread * 2.5:
            signal.vetoes.append(
                f"spread {spread:.5f} is {spread / median_spread:.1f}x its median"
            )

        # The target has to be reachable in a handful of entry-timeframe bars.
        if atr and atr > 0:
            bars_to_target = self.config.target_distance / atr
            signal.context["bars_to_target"] = bars_to_target
            if bars_to_target > 6:
                signal.vetoes.append(
                    f"too quiet: target is {bars_to_target:.1f} bars of "
                    f"{tf_name(ENTRY_TF)} ATR away"
                )

        if self.config.target_distance and spread > self.config.target_distance * 0.35:
            signal.vetoes.append("spread eats too much of the target")

        # ---- entry-timeframe trend must match the higher ones --------------
        if (allowed > 0) != entry_up:
            signal.vetoes.append(
                f"{tf_name(ENTRY_TF)} trend opposes the higher timeframes"
            )
            return signal

        # ---- scoring -------------------------------------------------------
        score = 0.0

        # Higher-timeframe conviction (30). Strong, separated agreement scores
        # more than two barely-trending charts that happen to point the same way.
        conviction = min(sum(c.separation for c in confirms) / 2.0, 1.0)
        score += 30.0 * conviction * allowed

        # Pullback (35) - the core of the entry. With the trend, we want price
        # back near the fast EMA, not extended away from it.
        stretch = (price - fast) / atr if atr else 0.0
        signal.context["stretch"] = stretch
        if allowed > 0:
            if -1.2 <= stretch <= 0.35:
                score += 35.0
                signal.reasons.append(f"pullback into trend (stretch {stretch:+.2f})")
            elif stretch > 1.5:
                score -= 25.0
                signal.reasons.append(f"extended above EMA (stretch {stretch:+.2f})")
        else:
            if -0.35 <= stretch <= 1.2:
                score -= 35.0
                signal.reasons.append(f"pullback into trend (stretch {stretch:+.2f})")
            elif stretch < -1.5:
                score += 25.0
                signal.reasons.append(f"extended below EMA (stretch {stretch:+.2f})")

        # RSI (20) as an exhaustion brake, not an entry trigger.
        if rsi >= 75:
            score -= 20.0
            signal.reasons.append(f"RSI {rsi:.0f} overbought")
        elif rsi <= 25:
            score += 20.0
            signal.reasons.append(f"RSI {rsi:.0f} oversold")

        # Order-book imbalance (10). Worth less on a 5-minute chart than it was
        # on a 1-minute one - it is a microstructure signal, not a trend one.
        if state.has_depth():
            score += 10.0 * max(-1.0, min(1.0, imbalance))
            if abs(imbalance) > 0.25:
                side = "bid" if imbalance > 0 else "ask"
                signal.reasons.append(f"L2 {side} pressure {imbalance:+.2f}")

        # Tick momentum (5), normalised by entry-timeframe ATR.
        momentum = state.tick_momentum()
        if atr and atr > 0:
            score += 5.0 * max(-1.0, min(1.0, momentum / atr))

        score = max(-100.0, min(100.0, score))
        signal.score = score
        signal.confidence = min(abs(score) / 100.0, 1.0)

        threshold = 30.0
        if score >= threshold:
            signal.direction = "BUY"
        elif score <= -threshold:
            signal.direction = "SELL"
        else:
            signal.vetoes.append(f"signal too weak (score {score:+.0f})")
            return signal

        # Belt and braces: the score can only ever confirm the side the higher
        # timeframes already permitted.
        if signal.direction != allowed_side:
            signal.vetoes.append(
                f"score says {signal.direction}, higher timeframes allow "
                f"{allowed_side}"
            )

        if signal.confidence < self.config.min_confidence:
            signal.vetoes.append(
                f"confidence {signal.confidence:.2f} < {self.config.min_confidence:.2f}"
            )

        return signal
