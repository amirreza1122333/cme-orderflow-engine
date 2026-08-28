"""Risk manager - the part that keeps a small account alive.

Two jobs:

1. **Sizing.** Position size is derived from the stop distance and a fixed
   cash risk per trade, never chosen up front. If ONE CONTRACT would risk more
   than the cap, the trade is refused rather than shrunk.
2. **Circuit breakers.** Daily loss, daily profit, trade count, consecutive
   losses and a cooldown. Every one of them can only *block* a trade.

FUTURES SIZING (changed 2026-08-21 with the move to CME contracts over DTC).
Size is now an integer contract count, and the floor is 1. Under cTrader a
stop that was too wide for the risk cap could be absorbed by dropping to a
smaller fraction of a lot; on MGC there is nothing below one contract, so the
same situation is a hard refusal:

    risk per contract = stop_distance x multiplier      (MGC: x 10)
    contracts         = floor(budget / risk_per_contract)

A 4.30 stop on MGC is $43 of risk for the smallest position that exists. If
`risk_per_trade` is under that, `can_trade` refuses every single evaluation -
which is a correct answer for an underfunded account, not a bug. The refusal
message says so explicitly so an empty trade log is self-explaining.

All limits are evaluated in UTC days so the reset point does not move with
local time or DST.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from config import Settings
from core.symbols import SymbolSpec

log = logging.getLogger("risk")


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    contracts: int = 0          # whole contracts; 0 when the trade is refused
    risk_amount: float = 0.0

    @property
    def volume(self) -> int:
        """Alias - under futures, order volume IS the contract count."""
        return self.contracts


@dataclass
class DayStats:
    day: date
    realised: float = 0.0
    trades: int = 0
    wins: int = 0
    losses: int = 0
    consecutive_losses: int = 0
    blocked_until: datetime | None = None
    gross_profit: float = 0.0
    gross_loss: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def profit_factor(self) -> float:
        return self.gross_profit / self.gross_loss if self.gross_loss > 0 else 0.0


class RiskManager:
    def __init__(self, settings: Settings, starting_balance: float) -> None:
        self.settings = settings
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.equity = starting_balance
        self.day = DayStats(day=datetime.now(timezone.utc).date())
        self.history: list[dict] = []
        self._last_trade_at: dict[str, datetime] = {}

    # ------------------------------------------------------------- day rollup

    def _roll_day(self, now: datetime) -> None:
        today = now.date()
        if today != self.day.day:
            log.info(
                "New UTC day. Previous: %d trades, realised %.2f, win rate %.0f%%",
                self.day.trades,
                self.day.realised,
                self.day.win_rate * 100,
            )
            self.day = DayStats(day=today)

    # ------------------------------------------------------------ gate checks

    def can_trade(
        self,
        spec: SymbolSpec,
        stop_distance: float,
        open_positions: int,
        now: datetime | None = None,
    ) -> RiskDecision:
        now = now or datetime.now(timezone.utc)
        self._roll_day(now)
        settings = self.settings

        if self.day.blocked_until and now < self.day.blocked_until:
            remaining = (self.day.blocked_until - now).total_seconds() / 60
            return RiskDecision(False, f"cooldown active for {remaining:.0f} more min")

        if self.day.realised <= -abs(settings.max_daily_loss):
            return RiskDecision(
                False, f"daily loss limit hit ({self.day.realised:+.2f})"
            )

        if self.day.realised >= abs(settings.max_daily_profit):
            return RiskDecision(
                False, f"daily profit target hit ({self.day.realised:+.2f})"
            )

        if self.day.trades >= settings.max_daily_trades:
            return RiskDecision(
                False, f"daily trade cap reached ({self.day.trades})"
            )

        if open_positions >= settings.max_open_positions:
            return RiskDecision(
                False, f"already holding {open_positions} position(s)"
            )

        last = self._last_trade_at.get(spec.name)
        if last is not None:
            wait = (now - last).total_seconds()
            symbol_config = settings.symbols.get(spec.name)
            minimum = symbol_config.min_seconds_between_trades if symbol_config else 0
            if wait < minimum:
                return RiskDecision(
                    False, f"only {wait:.0f}s since last {spec.name} trade"
                )

        if stop_distance <= 0:
            return RiskDecision(False, "stop distance must be positive")

        # Never risk more than what is left of today's loss budget.
        budget = min(
            settings.risk_per_trade,
            abs(settings.max_daily_loss) + self.day.realised,
        )
        if budget <= 0:
            return RiskDecision(False, "no risk budget left today")

        contracts = spec.volume_for_risk(budget, stop_distance)
        if contracts <= 0:
            # One contract is the floor. There is no smaller position to fall
            # back to, so name the shortfall rather than reporting a generic
            # sizing failure - this is the account being too small for the
            # instrument, and the fix is a wider budget or a tighter stop.
            smallest = spec.risk_for_volume(spec.min_contracts, stop_distance)
            return RiskDecision(
                False,
                f"1 {spec.name} contract at a {stop_distance:g} stop risks "
                f"{smallest:.2f} ({spec.multiplier:g} per 1.00 move), budget "
                f"is {budget:.2f} - underfunded for this contract",
            )

        risk_amount = spec.risk_for_volume(contracts, stop_distance)
        if risk_amount > settings.risk_per_trade * 1.05:
            return RiskDecision(
                False, f"sized risk {risk_amount:.2f} exceeds cap"
            )

        return RiskDecision(
            True, "ok", contracts=contracts, risk_amount=risk_amount
        )

    # -------------------------------------------------------------- recording

    def record_open(self, symbol: str, now: datetime | None = None) -> None:
        self._last_trade_at[symbol] = now or datetime.now(timezone.utc)

    def record_close(
        self, symbol: str, pnl: float, now: datetime | None = None, **extra
    ) -> None:
        now = now or datetime.now(timezone.utc)
        self._roll_day(now)

        self.balance += pnl
        self.equity = self.balance
        self.day.realised += pnl
        self.day.trades += 1

        if pnl >= 0:
            self.day.wins += 1
            self.day.gross_profit += pnl
            self.day.consecutive_losses = 0
        else:
            self.day.losses += 1
            self.day.gross_loss += abs(pnl)
            self.day.consecutive_losses += 1
            if self.day.consecutive_losses >= self.settings.max_consecutive_losses:
                self.day.blocked_until = now + timedelta(
                    minutes=self.settings.cooldown_after_losses_minutes
                )
                log.warning(
                    "%d losses in a row - pausing until %s UTC",
                    self.day.consecutive_losses,
                    self.day.blocked_until.strftime("%H:%M"),
                )

        record = {
            "time": now.isoformat(),
            "symbol": symbol,
            "pnl": round(pnl, 2),
            "balance": round(self.balance, 2),
            "daily_pnl": round(self.day.realised, 2),
            **extra,
        }
        self.history.append(record)
        log.info(
            "Closed %s pnl=%+.2f | day %+.2f in %d trades | balance %.2f",
            symbol,
            pnl,
            self.day.realised,
            self.day.trades,
            self.balance,
        )

    # ---------------------------------------------------------------- reports

    def status(self) -> dict:
        return {
            "balance": round(self.balance, 2),
            "starting_balance": round(self.starting_balance, 2),
            "total_pnl": round(self.balance - self.starting_balance, 2),
            "day": self.day.day.isoformat(),
            "daily_pnl": round(self.day.realised, 2),
            "trades_today": self.day.trades,
            "wins": self.day.wins,
            "losses": self.day.losses,
            "win_rate": round(self.day.win_rate * 100, 1),
            "profit_factor": round(self.day.profit_factor, 2),
            "consecutive_losses": self.day.consecutive_losses,
            "cooling_down": bool(
                self.day.blocked_until
                and datetime.now(timezone.utc) < self.day.blocked_until
            ),
        }
