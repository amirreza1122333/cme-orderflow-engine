"""Live order book through the engine's own SymbolState.

Nothing here is new code: BitstampClient fills the callbacks that
core/engine.py already wires, and SymbolState maintains the book exactly as it
does under DTC. This just prints what it is holding.

    python demo_live_book.py
"""

import logging
import time

from core.bitstamp_client import BitstampClient
from core.marketdata import SymbolState
from core.symbols import SymbolSpec

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

SYMBOL = "BTC/USD"

spec = SymbolSpec(
    symbol_id=1,
    name=SYMBOL,
    digits=2,
    tick_size=0.01,
    contract_size=1.0,
    multiplier=1.0,
    base_asset="BTC",
    quote_asset="USD",
    exchange="bitstamp",
)
state = SymbolState(spec=spec)


def on_tick(symbol, bid, ask, when):
    state.add_tick(bid, ask, timestamp=when)


def on_depth(symbol, new_quotes, deleted_ids):
    state.apply_depth(new_quotes, deleted_ids)


client = BitstampClient()
client.on_tick = on_tick
client.on_depth = on_depth

if not client.start():
    raise SystemExit("could not reach Bitstamp")

client.subscribe([SYMBOL])
client.subscribe_depth([SYMBOL])

try:
    for _ in range(15):
        time.sleep(2)
        bids, asks = state.book(5)
        if not bids or not asks:
            print("waiting for the book to fill...")
            continue

        spread = asks[0].price - bids[0].price
        print(
            f"\nticks={len(state.ticks):4}  levels={len(state.quotes):5}  "
            f"spread={spread:.2f}  imbalance={state.imbalance(5):+.3f}"
        )
        for bid, ask in zip(bids, asks):
            print(
                f"   {bid.size:>12.8f} @ {bid.price:>10.2f}   |   "
                f"{ask.price:<10.2f} @ {ask.size:<12.8f}"
            )
finally:
    client.stop()
    print("\nstopped.")
