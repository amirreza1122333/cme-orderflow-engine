"""Instrument metadata and the unit conversions the rest of the engine relies on.

FUTURES, not FX. Rewritten 2026-08-21 for CME contracts over DTC. The cTrader
model (fractional lots, volume in hundredths of a base unit, prices as scaled
integers on the wire) is gone. A futures position is a whole number of
contracts and nothing else - there is no 0.01 of a Micro Gold contract.

Three numbers define an instrument here, and mixing them up is the classic way
to size a position ten times wrong:

* ``contract_size`` - how much underlying is in one contract. MGC: 10 troy oz.
  Descriptive only; the engine never computes P&L from it.
* ``multiplier``    - account-currency P&L for a 1.00 move in price, per
  contract. MGC: $10.00. **This is the number every risk calculation uses.**
* ``tick_size``     - the smallest price increment the exchange accepts.
  MGC: 0.10, so one tick is 0.10 x $10 = $1.00 per contract.

Position size is therefore always an integer:

    contracts = floor(risk_budget / (stop_distance * multiplier))

and a size of 0 means "this account cannot afford one contract at this stop",
which is a refusal, never a rounding-down to something smaller.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Legacy cTrader wire scaling. Kept only so the archived client and any old
# recorded data still decode; DTC delivers real doubles and needs neither.
PRICE_SCALE = 100_000.0


@dataclass(frozen=True)
class SymbolSpec:
    """Static contract specification for one instrument.

    Under DTC there is no broker-side symbol download that hands us lot sizes
    and volume steps the way cTrader's ProtoOASymbol did, so these come from
    `config.py` (the exchange publishes them and they change about never).
    """

    symbol_id: int
    name: str
    digits: int                    # price decimals. MGC: 1 (e.g. 2650.4)
    tick_size: float               # minimum price increment. MGC: 0.10
    contract_size: float           # underlying units per contract. MGC: 10 oz
    multiplier: float              # account currency per 1.00 move, per contract
    min_contracts: int = 1         # a futures order is whole contracts, floor 1
    max_contracts: int = 0         # 0 = no cap enforced locally
    step_contracts: int = 1
    base_asset: str = ""
    quote_asset: str = "USD"
    exchange: str = "CME"

    # ---- price conversions -------------------------------------------------

    def round_price(self, price: float) -> float:
        return round(price, self.digits)

    def snap_to_tick(self, distance: float) -> float:
        """Round a price *distance* up to a whole number of ticks.

        Rounding up, not to nearest: a stop quietly shrunk below the tick grid
        is a stop the exchange will reject or reprice, and either way the risk
        we sized against was not the risk we took.
        """
        if self.tick_size <= 0:
            return abs(distance)
        ticks = math.ceil(round(abs(distance) / self.tick_size, 9))
        return round(max(1, ticks) * self.tick_size, self.digits)

    def ticks(self, distance: float) -> float:
        """A price distance expressed in ticks."""
        if self.tick_size <= 0:
            return 0.0
        return distance / self.tick_size

    @property
    def tick_value(self) -> float:
        """Account-currency value of one tick, per contract. MGC: $1.00."""
        return self.tick_size * self.multiplier

    def price_from_relative(self, relative: int) -> float:
        """DEPRECATED - cTrader wire format only. DTC prices are already floats.

        Retained because `ict/` and the archived client still reference it, and
        because it is the identity operation on anything already scaled.
        """
        return round(relative / PRICE_SCALE, self.digits)

    def price_to_relative(self, price: float) -> int:
        """DEPRECATED - cTrader wire format only."""
        return int(round(price * PRICE_SCALE))

    # ---- size conversions --------------------------------------------------
    #
    # `volume` throughout the engine is now simply the number of contracts, as
    # an int. The old hundredths-of-a-unit encoding is gone.

    def contracts(self, volume: int) -> int:
        """Contracts represented by an order volume (now the same number)."""
        return int(volume)

    def units(self, volume: int) -> float:
        """Underlying units in a position. MGC 2 contracts -> 20 oz."""
        return int(volume) * self.contract_size

    def lots(self, volume: int) -> float:
        """DEPRECATED alias for `contracts()`. Use `contracts()` in new code.

        Kept because `ict/signal.py` calls it to fill its `lot_size` display
        field, and ict/ is platform-agnostic by contract and is not edited
        during the DTC migration. The value it returns is correct - a contract
        count - only the name is left over from the cTrader era.
        """
        return float(self.contracts(volume))

    def snap_volume(self, volume: int) -> int:
        """Round *down* to a tradable contract count; 0 means 'below minimum'."""
        step = self.step_contracts or 1
        snapped = (int(volume) // step) * step
        if snapped < self.min_contracts:
            return 0
        if self.max_contracts and snapped > self.max_contracts:
            snapped = (self.max_contracts // step) * step
        return snapped

    # ---- money -------------------------------------------------------------

    def money_per_price_unit(self, volume: int) -> float:
        """P&L for a 1.00 move in price at this size.

        For MGC: 1 contract x $10 multiplier = $10.00 per $1.00 of gold.
        Exact while the contract settles in the account's deposit currency,
        which holds for every CME USD-denominated future on a USD account.
        `SymbolRegistry.check_currency` warns when that does not hold.
        """
        return int(volume) * self.multiplier

    def pnl(self, volume: int, entry: float, exit_price: float, is_buy: bool) -> float:
        direction = 1.0 if is_buy else -1.0
        return (exit_price - entry) * direction * self.money_per_price_unit(volume)

    def volume_for_risk(self, risk_amount: float, stop_distance: float) -> int:
        """Largest contract count whose loss at `stop_distance` stays under
        `risk_amount`. Returns 0 when even one contract risks too much.

        On MGC a $4.00 stop is $40 of risk for the single smallest position
        there is. An account whose per-trade cap is below that gets 0 here and
        the trade is refused - the size cannot be shaved to fit.
        """
        if stop_distance <= 0 or risk_amount <= 0:
            return 0
        per_contract = abs(stop_distance) * self.multiplier
        if per_contract <= 0:
            return 0
        return self.snap_volume(int(math.floor(risk_amount / per_contract)))

    def risk_for_volume(self, volume: int, stop_distance: float) -> float:
        return abs(stop_distance) * self.money_per_price_unit(volume)

    def describe(self) -> str:
        return (
            f"{self.name} @ {self.exchange}: 1 contract = {self.contract_size:g} "
            f"{self.base_asset or 'units'}, {self.multiplier:g} {self.quote_asset} "
            f"per 1.00 move, tick {self.tick_size:g} "
            f"({self.tick_value:g} {self.quote_asset}/tick)"
        )


class SymbolRegistry:
    """Name -> SymbolSpec.

    Under cTrader this was populated at startup from the broker's symbol list.
    Under DTC the contract specifications come from `config.py`; the DTC client
    only has to map each name to whatever id the Sierra Chart session uses.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, SymbolSpec] = {}
        self._by_id: dict[int, SymbolSpec] = {}
        self.deposit_asset: str = "USD"

    def add(self, spec: SymbolSpec) -> None:
        self._by_name[spec.name.upper()] = spec
        self._by_id[spec.symbol_id] = spec

    def by_name(self, name: str) -> SymbolSpec | None:
        return self._by_name.get(name.upper())

    def by_id(self, symbol_id: int) -> SymbolSpec | None:
        return self._by_id.get(symbol_id)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def ids(self) -> list[int]:
        return sorted(self._by_id)

    def check_currency(self) -> list[str]:
        """Contracts not settled in the account's deposit currency.

        For those, realised P&L needs an FX conversion the engine does not do,
        so the paper broker's numbers would drift from the real ones.
        """
        if not self.deposit_asset:
            return []
        return [
            spec.name
            for spec in self._by_name.values()
            if spec.quote_asset and spec.quote_asset != self.deposit_asset
        ]
