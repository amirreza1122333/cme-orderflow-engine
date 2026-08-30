"""Bitstamp market-data client.

The third venue this engine speaks, and the reason the strategy layer's
independence is a fact rather than a claim: `ict/` imports nothing from here,
exactly as it imports nothing from `core/dtc_client.py`.

It implements the same surface as `DTCClient` - same callback signatures, same
`Bar` shape, same threading contract - so `core/engine.py` and
`ict/service.py` can hold either one behind the same variable.

WHY BITSTAMP

CME depth needs a Sierra Chart licence and a paid Denali subscription. Bitstamp
publishes full book depth over a public WebSocket with no key and no cost, so
the collector, the feature extractor and the panel can all be exercised
end-to-end for free. The microstructure is NOT the same as CME's - 24/7, no
auction, different participants - so a model trained here does not transfer to
MGC. This validates the engineering, not the edge.

CHANNEL CHOICE

    order_book_{pair}        full top-100 snapshot, ~10/s  -> on_tick
    diff_order_book_{pair}   incremental deltas            -> on_depth

Top-of-book comes from the snapshot channel because it is already exactly a
bid/ask pair. Depth comes from the delta channel because `on_depth` is defined
in terms of arrivals and departures, not whole books. Using the snapshot for
depth would mean diffing it ourselves to find what changed - work the exchange
has already done.

NOT IMPLEMENTED HERE

`history()` (REST OHLC) and the order methods. The engine runs `mode: paper`,
so no order path is needed; the read-only methods raise rather than pretending.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any
from datetime import datetime, timezone

from core.book_ids import LevelIds
from core.dtc_client import Bar, DTCError

log = logging.getLogger("bitstamp")

WS_URL = "wss://ws.bitstamp.net"
REST_URL = "https://www.bitstamp.net/api/v2"

# The endpoint's own list, verified against it rather than copied from a doc.
# The engine's SEED_PLAN needs 300 / 900 / 3600 / 86400; all four are here.
OHLC_STEPS = (60, 180, 300, 900, 1800, 3600, 7200, 14400, 21600, 43200, 86400, 259200)
# Hard cap: 1001 is refused with a validation error, so a long window pages.
OHLC_MAX_LIMIT = 1000


def to_pair(symbol: str) -> str:
    """'BTC/USD' -> 'btcusd'. Callers work in names; the wire wants pairs."""
    return symbol.replace("/", "").replace("-", "").lower()


class ReadOnlyFeed(DTCError):
    """Raised by the order methods. This adapter observes; it cannot trade."""


class BitstampClient:
    """Public Bitstamp market data, shaped like `DTCClient`.

        client = BitstampClient()
        client.on_tick  = handle_tick
        client.on_depth = handle_depth
        client.start()
        client.subscribe(["BTC/USD"])
        client.subscribe_depth(["BTC/USD"])
        ...
        client.stop()
    """

    def __init__(
        self,
        ws_url: str = WS_URL,
        rest_url: str = REST_URL,
        reconnect_seconds: float = 5.0,
        reseed_seconds: float = 300.0,
        client_name: str = "ict-bot",
    ) -> None:
        self.ws_url = ws_url
        self.rest_url = rest_url
        self.reconnect_seconds = reconnect_seconds
        # How often to diff the local book against a fresh snapshot. Drift is
        # slow and one-directional, so minutes is the right order, not seconds.
        self.reseed_seconds = reseed_seconds
        self.client_name = client_name

        # Set by the caller before start(), same as DTCClient.
        self.on_tick = None
        self.on_depth = None
        self.on_execution = None
        self.on_order_error = None
        self.on_connected = None
        self.on_disconnected = None
        self.on_account = None

        self.connected: bool = False

        self._ws = None
        self._reader: threading.Thread | None = None
        self._running = threading.Event()
        self._lock = threading.Lock()

        # channel -> symbol name, so a message resolves back to what the
        # caller asked for rather than to a wire pair.
        self._channels: dict[str, str] = {}
        # One id map per symbol. Bid 100 on BTC and bid 100 on ETH are not the
        # same level, and a shared map would collide them.
        self._level_ids: dict[str, LevelIds] = {}
        # Symbols whose book has been seeded from REST and is now live.
        self._seeded: set[str] = set()
        # Deltas that arrived while the snapshot was in flight, per symbol.
        self._pending: dict[str, list[dict]] = {}

    # --------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Connect and start the reader thread. Returns False if unreachable.

        Mirrors `DTCClient.start`: returns False on a plain connection failure
        so a supervisor can retry without a traceback, and fires
        `on_connected` on this and every later reconnect - the signal callers
        use to re-subscribe, since a fresh session carries no subscriptions.
        """
        if not self._connect():
            return False

        self._running.set()
        self._reader = threading.Thread(
            target=self._read_loop, name="bitstamp-reader", daemon=True
        )
        self._reader.start()
        return True

    def stop(self) -> None:
        """Close the socket and stop the reader. Idempotent, never raises."""
        self._running.clear()
        with self._lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - shutdown path, already unwinding
                pass
        self.connected = False

    def _connect(self) -> bool:
        import websocket  # imported lazily so the module imports without it

        try:
            ws = websocket.create_connection(self.ws_url, timeout=10)
        except Exception as exc:  # noqa: BLE001 - any transport failure
            log.warning("bitstamp connect failed: %s", exc)
            return False

        with self._lock:
            self._ws = ws
        self.connected = True
        self._fire(self.on_connected)
        return True

    # ------------------------------------------------------------ market data

    def subscribe(self, symbols: list[str], exchange: str = "bitstamp") -> None:
        """Top-of-book streaming -> `on_tick`. `exchange` is ignored."""
        for symbol in symbols:
            self._subscribe_channel(f"order_book_{to_pair(symbol)}", symbol)

    def subscribe_depth(self, symbols: list[str], levels: int = 10) -> None:
        """Level-2 streaming -> `on_depth`.

        `levels` is accepted for interface compatibility and ignored: Bitstamp
        publishes the whole book and the consumer decides how deep to look.

        Non-fatal by contract - a depth failure degrades the l2_* features to
        0.0 and leaves every other feature and gate working, so it is logged
        and swallowed rather than raised.
        """
        for symbol in symbols:
            try:
                self._level_ids.setdefault(symbol, LevelIds())
                # Subscribe BEFORE fetching the snapshot. The other order loses
                # every update that happens while the REST call is in flight,
                # and the book stays quietly wrong for as long as the process
                # runs. Deltas that arrive now are buffered, not dropped.
                self._subscribe_channel(f"diff_order_book_{to_pair(symbol)}", symbol)
                threading.Thread(
                    target=self._seed_loop,
                    args=(symbol,),
                    name=f"bitstamp-seed-{to_pair(symbol)}",
                    daemon=True,
                ).start()
            except Exception as exc:  # noqa: BLE001 - depth is optional
                log.warning("depth subscribe failed for %s: %s", symbol, exc)

    def _seed_loop(self, symbol: str) -> None:
        """Seed the book, then re-diff it against a fresh snapshot forever.

        The first pass is the initial seed: `diff_order_book` sends no starting
        book, so without it the local book is whatever subset of levels
        happened to change since we connected.

        Every later pass is repair. A delete is lost now and then - the REST
        snapshot is served slightly stale, so whatever died between its
        as-of moment and our subscription is in neither stream - and nothing
        in the delta feed ever reports the loss. Measured drift was one or two
        stale levels a few rows below the touch; the top of book stayed exact,
        which is precisely why it is worth repairing on a schedule rather than
        trusting it to stay small.
        """
        while self._running.is_set():
            self._seed_book(symbol)
            # Sleep in one-second slices so stop() does not wait out the period.
            for _ in range(max(1, int(self.reseed_seconds))):
                if not self._running.is_set():
                    return
                time.sleep(1)

    def _seed_book(self, symbol: str) -> None:
        """Diff a fresh REST snapshot into the local book, deltas and all."""
        import requests

        # Buffer incoming deltas for the duration of the request.
        with self._lock:
            self._seeded.discard(symbol)
            self._pending[symbol] = []

        url = f"{self.rest_url}/order_book/{to_pair(symbol)}/"
        try:
            snapshot = requests.get(url, timeout=10).json()
        except Exception as exc:  # noqa: BLE001 - depth is optional
            log.warning("book snapshot failed for %s: %s", symbol, exc)
            return

        ids = self._level_ids.setdefault(symbol, LevelIds())

        # reconcile(), not translate(): a snapshot only says what IS resting.
        # Absence is not a delete, and `apply_depth` drops a level only when
        # its id arrives in deleted_ids - so a stale level would survive every
        # re-seed unless the snapshot is diffed against what we believe.
        new_quotes, deleted_ids = ids.reconcile(
            snapshot.get("bids") or [], snapshot.get("asks") or []
        )
        self._fire(self.on_depth, symbol, new_quotes, deleted_ids)
        log.info(
            "%s book reconciled: %d levels, %d stale removed",
            symbol, len(new_quotes), len(deleted_ids),
        )

        # Drain whatever queued up while that request was in flight.
        #
        # Every buffered delta is replayed, including ones older than the
        # snapshot. Bitstamp sends ABSOLUTE sizes - ["77559.25", "0.25"] means
        # "this level is now 0.25", not "add 0.25" - so applying a delta twice
        # is idempotent, and replaying in arrival order leaves each price at
        # its newest value either way.
        #
        # An earlier version filtered on the snapshot's microtimestamp to
        # avoid double-application. That filter bought nothing (the operation
        # is idempotent) and cost real deletes whenever the REST clock and the
        # stream clock disagreed, leaving stale levels wedged in the book a few
        # rows below the touch. Reconciliation against a fresh snapshot is what
        # caught it - see verify_book.py.
        with self._lock:
            buffered = self._pending.pop(symbol, [])
            self._seeded.add(symbol)

        for data in buffered:
            self._apply_depth(symbol, ids, data)
        if buffered:
            log.info("%s replayed %d buffered deltas", symbol, len(buffered))

    def _subscribe_channel(self, channel: str, symbol: str) -> None:
        self._channels[channel] = symbol
        self._send({"event": "bts:subscribe", "data": {"channel": channel}})

    def _send(self, payload: dict) -> None:
        with self._lock:
            ws = self._ws
        if ws is None:
            raise DTCError("not connected")
        ws.send(json.dumps(payload))

    def _get_ohlc(self, pair: str, step: int, start: int, limit: int) -> list[dict]:
        """One page of raw OHLC rows. The only part of history() that does I/O,
        kept separate so the paging logic can be tested without a network."""
        import requests

        response = requests.get(
            f"{self.rest_url}/ohlc/{pair}/",
            params={"step": step, "limit": limit, "start": start},
            timeout=20,
        )
        response.raise_for_status()
        return (response.json().get("data") or {}).get("ohlc") or []

    def history(
        self,
        symbol: str,
        period_seconds: int,
        start: datetime,
        end: datetime,
        max_bars: int = 0,
    ) -> list[Bar]:
        """Historical OHLC bars, oldest first. BLOCKING - never call from a callback.

        Same contract as `DTCClient.history`: seeds the indicator warm-up at
        startup, returns an empty list when the venue has no data for the
        window rather than raising, because one bare symbol must not stop the
        process from starting.

        An unsupported `period_seconds` DOES raise. That is a wiring mistake,
        not a data condition, and a silent empty list would surface much later
        as indicators that never warmed up.
        """
        if period_seconds not in OHLC_STEPS:
            raise DTCError(
                f"step {period_seconds}s is not offered by Bitstamp; "
                f"pick one of {OHLC_STEPS}"
            )

        pair = to_pair(symbol)
        window_start = int(start.timestamp())
        window_end = int(end.timestamp())

        rows: dict[int, dict] = {}   # timestamp -> row, so overlap cannot double-count
        cursor = window_start

        while cursor <= window_end:
            page = self._get_ohlc(pair, period_seconds, cursor, OHLC_MAX_LIMIT)
            if not page:
                break

            newest = cursor
            for row in page:
                ts = int(row["timestamp"])
                newest = max(newest, ts)
                if window_start <= ts <= window_end:
                    rows[ts] = row

            if len(page) < OHLC_MAX_LIMIT:
                break                      # the server has nothing further
            if newest <= cursor:
                break                      # cursor stuck: stop rather than spin
            cursor = newest + period_seconds

        bars = [self._to_bar(rows[ts]) for ts in sorted(rows)]
        if max_bars and len(bars) > max_bars:
            bars = bars[-max_bars:]        # keep the most recent
        return bars

    @staticmethod
    def _to_bar(row: dict) -> Bar:
        """One raw row -> a Bar. Everything on the wire is a string."""
        return Bar(
            start=datetime.fromtimestamp(int(row["timestamp"]), tz=timezone.utc),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume") or 0.0),
        )

    # ---------------------------------------------------------------- orders

    def market_order(self, *args, **kwargs) -> str:
        raise ReadOnlyFeed("BitstampClient is a market-data adapter; it cannot trade")

    def cancel_order(self, order_id: str) -> None:
        raise ReadOnlyFeed("BitstampClient is a market-data adapter; it cannot trade")

    def flatten(self, symbol: str) -> None:
        raise ReadOnlyFeed("BitstampClient is a market-data adapter; it cannot trade")

    # ----------------------------------------------------------- reader thread

    def _read_loop(self) -> None:
        while self._running.is_set():
            try:
                with self._lock:
                    ws = self._ws
                if ws is None:
                    raise DTCError("socket closed")
                self._handle(json.loads(ws.recv()))
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                if not self._running.is_set():
                    return
                self.connected = False
                self._fire(self.on_disconnected, str(exc))
                self._reconnect()

    def _reconnect(self) -> None:
        while self._running.is_set():
            time.sleep(self.reconnect_seconds)
            if not self._connect():
                continue
            # A fresh session starts with no subscriptions. Re-issue them all;
            # the id maps survive, so quote ids stay stable across the gap.
            for channel in list(self._channels):
                try:
                    self._send({"event": "bts:subscribe", "data": {"channel": channel}})
                except Exception:  # noqa: BLE001 - dropped again mid-resubscribe
                    break
            else:
                return

    # ---------------------------------------------------------------- routing

    def _handle(self, msg: dict) -> None:
        event = msg.get("event")
        channel = msg.get("channel", "")

        if event in ("bts:subscription_succeeded", "bts:unsubscription_succeeded"):
            return
        if event == "bts:error":
            log.error("bitstamp error on %s: %s", channel, msg.get("data"))
            return
        if event != "data":
            return

        symbol = self._channels.get(channel)
        if symbol is None:
            return

        data = msg.get("data") or {}
        when = self._when(data)

        if channel.startswith("diff_order_book_"):
            self._emit_depth(symbol, data, when)
        elif channel.startswith("order_book_"):
            self._emit_tick(symbol, data, when)

    @staticmethod
    def _when(data: dict) -> datetime:
        """`microtimestamp` is microseconds since the epoch, as a string."""
        micro = data.get("microtimestamp")
        if micro:
            return datetime.fromtimestamp(int(micro) / 1_000_000, tz=timezone.utc)
        return datetime.now(timezone.utc)

    def _emit_tick(self, symbol: str, data: dict, when: datetime) -> None:
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        if not bids or not asks:
            return
        self._fire(self.on_tick, symbol, float(bids[0][0]), float(asks[0][0]), when)

    def _emit_depth(self, symbol: str, data: dict, when: datetime) -> None:
        with self._lock:
            if symbol not in self._seeded:
                # Snapshot still in flight - hold this delta, do not drop it.
                self._pending.setdefault(symbol, []).append(data)
                return
        self._apply_depth(symbol, self._level_ids.setdefault(symbol, LevelIds()), data)

    def _apply_depth(self, symbol: str, ids: LevelIds, data: dict) -> None:
        new_quotes, deleted_ids = ids.translate(
            data.get("bids") or [], data.get("asks") or []
        )
        if new_quotes or deleted_ids:
            self._fire(self.on_depth, symbol, new_quotes, deleted_ids)

    @staticmethod
    def _fire(callback, *args) -> None:
        """Call a callback if set. A raising consumer must not kill the reader."""
        if callback is None:
            return
        try:
            callback(*args)
        except Exception:  # noqa: BLE001 - consumer's bug, not the feed's
            log.exception("callback failed")
