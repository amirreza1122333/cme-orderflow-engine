"""Price-keyed depth deltas -> id-keyed quotes.

Both feeds this project speaks identify an order-book level by its PRICE:

    Bitstamp  diff_order_book   ["77683.99", "1.27000000"]
    DTC       MARKET_DEPTH_UPDATE_LEVEL   price + size, no id

`SymbolState.quotes` is keyed by an integer id instead, so something has to
own the translation. That is this module, and it is shared rather than
duplicated in each client: the mapping problem is identical, only the wire
format differs.

A size of zero means the level left the book.
"""

from __future__ import annotations


class LevelIds:
    """Assigns a stable integer id to every (side, price) level.

    Usage:

        ids = LevelIds()
        new_quotes, deleted_ids = ids.translate(msg["bids"], msg["asks"])
        state.apply_depth(new_quotes, deleted_ids, timestamp=when)
    """

    def __init__(self) -> None:
        # (side, price) -> quote_id
        self._ids: dict[tuple[str, float], int] = {}
        self._next_id: int = 1

    def _id_for(self, side: str, price: float) -> int:
        """Existing id for this level, or a freshly minted one."""
        key = (side, price)
        if key not in self._ids:
            self._ids[key] = self._next_id
            self._next_id += 1
        return self._ids[key]

    def translate(
        self, bids: list, asks: list
    ) -> tuple[list[tuple[int, str, float, float]], list[int]]:
        """One depth message -> the two lists `apply_depth` takes.

        `bids` and `asks` are lists of [price, size] pairs, as strings on the
        wire. Returns:

            new_quotes  [(quote_id, side, price, size), ...]   size > 0
            deleted_ids [quote_id, ...]                        size == 0

        A price whose level is not currently mapped and whose size is zero is
        a delete for something we never saw - skipped, not invented.

        A level that dies keeps its id: the map is append-only. `apply_depth`
        stores `quotes[quote_id] = (side, price, size)`, so reusing the id
        means a stale entry for that price can only ever be overwritten.
        Minting a fresh id would risk two live ids for one price, which
        `book()` renders as a duplicated level and `imbalance()` double-counts.
        """
        new_quotes: list[tuple[int, str, float, float]] = []
        deleted_ids: list[int] = []

        for side, levels in (("bid", bids), ("ask", asks)):
            for raw_price, raw_size in levels:
                price = float(raw_price)
                size = float(raw_size)

                if size > 0:
                    new_quotes.append((self._id_for(side, price), side, price, size))
                elif (side, price) in self._ids:
                    # Size zero: the level left the book. Only levels we have
                    # actually seen can be deleted.
                    deleted_ids.append(self._ids[(side, price)])

        return new_quotes, deleted_ids

    def __len__(self) -> int:
        """How many distinct (side, price) levels have ever been seen.

        Append-only by design, so this only grows. See the note in the README
        about bounding it for a long-running collector.
        """
        return len(self._ids)
