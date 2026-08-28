"""Execution: a simulated broker and a real one behind the same interface.

`PaperBroker` is the default and is deliberately pessimistic - it fills at the
far side of the spread, charges commission, and lets price trade *through* a
level before filling the stop. Paper results that look good on an optimistic
simulator are worthless, so this one is built to under-promise. It is fully
functional today and needs no broker connection.

`LiveBroker` sends real market orders with server-side SL/TP. It refuses to do
anything unless every gate in `config` is open and `armed` has been set
explicitly by the engine.

    LIVE EXECUTION IS OFFLINE during the DTC migration (2026-08-21). The
    cTrader order path was removed with `core/ctrader.py`; the DTC replacement
    is not written yet. `LiveBroker` keeps its gating and its bookkeeping so
    the engine wiring stays intact, but every order attempt is refused and
    logged. Fill in `core/dtc_client.py`, then the two marked methods here.

`volume` throughout this module is a whole number of CONTRACTS - see
core/symbols.py. Commission is charged per contract, per round turn.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

# TODO: Replace with DTC Client - `from core.dtc_client import DTCClient`.
# The cTrader client was archived to core/ctrader_DEPRECATED.py.
from core.marketdata import SymbolState
from core.symbols import SymbolSpec

log = logging.getLogger("broker")


@dataclass
class Position:
    position_id: str
    symbol: str
    side: str                 # "BUY" | "SELL"
    volume: int
    entry: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    commission: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def is_buy(self) -> bool:
        return self.side == "BUY"


class BrokerBase:
    """Common bookkeeping. Subclasses implement open/close."""

    def __init__(self) -> None:
        self.positions: dict[str, Position] = {}
        self.on_closed = None  # callable(trade: dict)

    @property
    def open_count(self) -> int:
        return len(self.positions)

    def positions_for(self, symbol: str) -> list[Position]:
        return [p for p in self.positions.values() if p.symbol == symbol]

    def _emit_closed(self, trade: dict) -> None:
        if self.on_closed:
            self.on_closed(trade)


class PaperBroker(BrokerBase):
    """Local fill simulation against the live bid/ask stream."""

    def __init__(
        self,
        commission_per_contract: float = 0.0,
        slippage_price: float = 0.0,
        stop_slippage_price: float = 0.0,
    ) -> None:
        super().__init__()
        self.commission_per_contract = commission_per_contract
        self.slippage_price = slippage_price
        self.stop_slippage_price = stop_slippage_price
        self._sequence = 0

    def open(
        self,
        spec: SymbolSpec,
        side: str,
        volume: int,
        stop_distance: float,
        target_distance: float,
        state: SymbolState,
        meta: dict | None = None,
    ) -> Position | None:
        tick = state.last_tick
        if tick is None:
            return None

        # Buy pays the ask, sell receives the bid - the spread is a real cost
        # and has to be paid on entry, not averaged away at the mid.
        if side == "BUY":
            entry = tick.ask + self.slippage_price
            stop_loss = entry - stop_distance
            take_profit = entry + target_distance
        else:
            entry = tick.bid - self.slippage_price
            stop_loss = entry + stop_distance
            take_profit = entry - target_distance

        self._sequence += 1
        commission = self.commission_per_contract * spec.contracts(volume)
        position = Position(
            position_id=f"paper-{self._sequence}",
            symbol=spec.name,
            side=side,
            volume=volume,
            entry=spec.round_price(entry),
            stop_loss=spec.round_price(stop_loss),
            take_profit=spec.round_price(take_profit),
            opened_at=tick.timestamp,
            commission=commission,
            meta=meta or {},
        )
        self.positions[position.position_id] = position
        log.info(
            "[paper] %s %d %s contract(s) @ %.*f  SL %.*f  TP %.*f",
            side,
            spec.contracts(volume),
            spec.name,
            spec.digits, position.entry,
            spec.digits, position.stop_loss,
            spec.digits, position.take_profit,
        )
        return position

    def on_tick(self, spec: SymbolSpec, state: SymbolState) -> list[dict]:
        """Check open positions against the new tick; return closed trades."""
        tick = state.last_tick
        if tick is None:
            return []

        closed: list[dict] = []
        for position in list(self.positions.values()):
            if position.symbol != spec.name:
                continue
            # A long is closed at the bid, a short at the ask.
            exit_price = tick.bid if position.is_buy else tick.ask
            reason = None
            fill = exit_price

            if position.is_buy:
                if exit_price <= position.stop_loss:
                    reason = "stop_loss"
                    fill = min(exit_price, position.stop_loss) - self.stop_slippage_price
                elif exit_price >= position.take_profit:
                    reason = "take_profit"
                    fill = position.take_profit
            else:
                if exit_price >= position.stop_loss:
                    reason = "stop_loss"
                    fill = max(exit_price, position.stop_loss) + self.stop_slippage_price
                elif exit_price <= position.take_profit:
                    reason = "take_profit"
                    fill = position.take_profit

            if reason:
                closed.append(self._close(spec, position, fill, reason, tick.timestamp))
        return closed

    def close_at_market(
        self, spec: SymbolSpec, position: Position, state: SymbolState, reason: str
    ) -> dict | None:
        tick = state.last_tick
        if tick is None:
            return None
        fill = tick.bid if position.is_buy else tick.ask
        return self._close(spec, position, fill, reason, tick.timestamp)

    def _close(
        self,
        spec: SymbolSpec,
        position: Position,
        fill: float,
        reason: str,
        when: datetime,
    ) -> dict:
        gross = spec.pnl(position.volume, position.entry, fill, position.is_buy)
        net = gross - position.commission
        self.positions.pop(position.position_id, None)
        trade = {
            "closed_at": when.isoformat(),
            "opened_at": position.opened_at.isoformat(),
            "symbol": position.symbol,
            "side": position.side,
            "volume": position.volume,
            "contracts": spec.contracts(position.volume),
            "entry": position.entry,
            "exit": spec.round_price(fill),
            "stop_loss": position.stop_loss,
            "take_profit": position.take_profit,
            "exit_reason": reason,
            "gross_pnl": round(gross, 2),
            "commission": round(position.commission, 2),
            "pnl": round(net, 2),
            "mode": "paper",
            **{k: v for k, v in position.meta.items() if k in ("confidence", "score")},
        }
        self._emit_closed(trade)
        return trade


class LiveBroker(BrokerBase):
    """Real orders. Every gate must be open before a single byte is sent.

    OFFLINE pending core/dtc_client.py. The gating, the arming flag and the
    position bookkeeping below are the parts worth keeping - they are what
    stops an accident - so they stay wired into the engine exactly as before.
    The two methods that actually talk to a broker are stubbed and refuse.
    """

    def __init__(self, client=None, enabled: bool = False) -> None:
        super().__init__()
        # TODO: Replace with DTC Client - this will be a DTCClient instance
        # from core.dtc_client, connected to the Sierra Chart DTC server.
        self.client = client
        self.enabled = enabled
        self.armed = False
        self._pending: dict[str, dict] = {}

    def open(
        self,
        spec: SymbolSpec,
        side: str,
        volume: int,
        stop_distance: float,
        target_distance: float,
        state: SymbolState,
        meta: dict | None = None,
    ):
        if not self.enabled:
            log.error("Live order blocked: EXECUTION_MODE is not 'live'")
            return None
        if not self.armed:
            log.error("Live order blocked: broker not armed")
            return None
        if self.client is None:
            # The only outcome available right now. Refusing here rather than
            # raising keeps a mis-set EXECUTION_MODE from killing a session
            # that is otherwise collecting perfectly good data.
            log.error(
                "Live order blocked: no DTC client. The cTrader order path was "
                "removed in the 2026-08-21 pivot and core/dtc_client.py is not "
                "implemented yet. Run EXECUTION_MODE=paper."
            )
            return None

        # TODO: Replace with DTC Client - SUBMIT_NEW_SINGLE_ORDER carrying
        # OrderQuantity = contract count, plus the bracket (SL/TP) as an OCO
        # pair. Sierra Chart wants absolute stop/target PRICES, not the
        # relative distances cTrader took, so convert against the fill.
        # The label is how signal metadata is re-attached to the fill that
        # comes back; keep the mechanism, it survives the protocol change.
        label = f"bot-{datetime.now(timezone.utc).strftime('%H%M%S')}"
        self._pending[label] = {"meta": meta or {}, "spec": spec}
        raise NotImplementedError(
            f"DTC order submission is not implemented: would have sent {side} "
            f"{spec.contracts(volume)} {spec.name} contract(s), SL "
            f"{stop_distance:g} / TP {target_distance:g} - see core/dtc_client.py"
        )

    # -- execution events ----------------------------------------------------

    def handle_execution(self, event, registry) -> dict | None:
        """Track fills and closes from the broker's own execution reports.

        Returns a closed-trade record when a fill closed a position, else None.

        TODO: Replace with DTC Client - the cTrader implementation decoded
        ProtoOAExecutionEvent (opening deal vs. closePositionDetail, money
        scaled by moneyDigits). The DTC equivalent is ORDER_UPDATE for fills
        and POSITION_UPDATE for the resulting position, with prices and P&L
        already as real doubles - no money-digit scaling step. The archived
        version is in core/ctrader_DEPRECATED.py's caller if you need the
        field-by-field mapping.
        """
        log.warning("Execution report ignored: DTC client not implemented")
        return None
