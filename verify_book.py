"""Reconcile the locally maintained book against a fresh REST snapshot.

Standard practice for any incrementally maintained order book: the deltas can
drift, so periodically compare against an independent full snapshot. A book
that silently diverges produces features that look plausible and are wrong.

    python verify_book.py
"""

import logging
import time

import requests

from core.bitstamp_client import BitstampClient, to_pair
from core.marketdata import SymbolState
from core.symbols import SymbolSpec

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

SYMBOL = "BTC/USD"
DEPTH = 10
SETTLE_SECONDS = 15

spec = SymbolSpec(
    symbol_id=1, name=SYMBOL, digits=2, tick_size=0.01,
    contract_size=1.0, multiplier=1.0,
    base_asset="BTC", quote_asset="USD", exchange="bitstamp",
)
state = SymbolState(spec=spec)

# Short period so a run of this length exercises the repair path more than
# once. The client's own default is minutes.
client = BitstampClient(reseed_seconds=8)
client.on_tick = lambda s, bid, ask, when: state.add_tick(bid, ask, timestamp=when)
client.on_depth = lambda s, new_q, dead: state.apply_depth(new_q, dead)

if not client.start():
    raise SystemExit("could not reach Bitstamp")
client.subscribe_depth([SYMBOL])

print(f"maintaining the book for {SETTLE_SECONDS}s, then reconciling...")
time.sleep(SETTLE_SECONDS)

# Freeze the local book BEFORE fetching the comparison snapshot.
#
# The other order makes the instrument lie: the reader thread keeps applying
# deltas while the REST request is in flight, so the local book ends up AHEAD
# of the snapshot and every order that arrived in that window reads as a stale
# level we failed to delete. Stopping first means local can only ever be
# behind, so a level local holds and the snapshot does not is genuinely stale.
client.stop()
snap = requests.get(
    f"https://www.bitstamp.net/api/v2/order_book/{to_pair(SYMBOL)}/", timeout=10
).json()

mine_bids, mine_asks = state.book(DEPTH)
their_bids = [(float(p), float(q)) for p, q in snap["bids"][:DEPTH]]
their_asks = [(float(p), float(q)) for p, q in snap["asks"][:DEPTH]]

def show(label, mine, theirs):
    print(f"\n{label}")
    print(f"{'local':>28}   {'rest snapshot':>28}   match")
    for i in range(DEPTH):
        m = f"{mine[i].size:.8f} @ {mine[i].price:.2f}" if i < len(mine) else "-"
        t = f"{theirs[i][1]:.8f} @ {theirs[i][0]:.2f}" if i < len(theirs) else "-"
        same = (
            i < len(mine) and i < len(theirs)
            and abs(mine[i].price - theirs[i][0]) < 0.005
        )
        print(f"{m:>28}   {t:>28}   {'OK' if same else 'DIFF'}")

show("BIDS", mine_bids, their_bids)
show("ASKS", mine_asks, their_asks)

print(f"\nlocal levels tracked: {len(state.quotes)}")
print()
print("The snapshot is taken after the local book is frozen, so it is strictly")
print("newer. Levels the snapshot has and the local book lacks are expected.")
print("Levels the LOCAL book has and the snapshot lacks are the real signal:")
print("those are deletes that went missing.")
