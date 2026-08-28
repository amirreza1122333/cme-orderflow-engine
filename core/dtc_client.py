"""DTC protocol client for Sierra Chart. INTERFACE BLUEPRINT - not implemented.

This file is the contract. Every signature below is what `core/engine.py` and
`ict/service.py` already call; filling in the bodies is the whole remaining
job. Nothing else has to change when they are filled in - both callers are
written against this surface today and refuse to run without it.

Verified environment (2026-08-21)
---------------------------------
    host       localhost
    port       11099
    transport  plain TCP socket, binary encoding
    status     connection, LOGON and HEARTBEAT round-trip confirmed;
               market data observed flowing
    instrument MGC  (CME Micro Gold, 10 oz, $10 per $1.00, 0.10 tick)
    broker     CQG (credentials pending)

Wire format
-----------
Binary DTC frames are little-endian and self-describing:

    struct s_Header { uint16 Size; uint16 Type; }   then Size-4 bytes of payload

so `socket` plus `struct` covers the whole protocol - no third-party package.
Handshake order matters:

    1. ENCODING_REQUEST  (type 6)  -> ENCODING_RESPONSE  (7)   pick BINARY
    2. LOGON_REQUEST     (1)       -> LOGON_RESPONSE     (2)
    3. HEARTBEAT         (3)       every HeartbeatIntervalInSeconds, both ways

Sierra Chart drops a session that misses its heartbeat, so the heartbeat must
run on its own thread and must not be starved by callback work. Symbols are
addressed by a client-assigned `SymbolID` (uint32) chosen at subscribe time and
echoed on every update for that instrument - the mapping is ours to keep.

Threading contract
------------------
`start()` spawns a reader thread and a heartbeat thread and returns
immediately. Every `on_*` callback fires **on the reader thread**. Callers must
therefore treat callbacks as they already do: fast, non-blocking, no network
calls (`core/engine.py` pushes its news and analyst HTTP work onto separate
threads via `_in_thread` for exactly this reason).

Prices arrive as real doubles. There is no `price_from_relative` unscaling step
of the kind cTrader's 1e5-scaled integers needed - see
core/ctrader_DEPRECATED.py for the shapes this replaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# DTC message type numbers used by this client. Listed here so the
# implementation has no magic integers and a reader can check them against the
# published DTC specification in one place.
ENCODING_REQUEST = 6
ENCODING_RESPONSE = 7
LOGON_REQUEST = 1
LOGON_RESPONSE = 2
HEARTBEAT = 3
LOGOFF = 5
MARKET_DATA_REQUEST = 101
MARKET_DATA_SNAPSHOT = 104
MARKET_DATA_UPDATE_TRADE = 107
MARKET_DATA_UPDATE_BID_ASK = 108
MARKET_DEPTH_REQUEST = 102
MARKET_DEPTH_SNAPSHOT_LEVEL = 122
MARKET_DEPTH_UPDATE_LEVEL = 123
SUBMIT_NEW_SINGLE_ORDER = 208
CANCEL_ORDER = 203
ORDER_UPDATE = 301
POSITION_UPDATE = 306
ACCOUNT_BALANCE_UPDATE = 600
HISTORICAL_PRICE_DATA_REQUEST = 800
HISTORICAL_PRICE_DATA_RESPONSE_HEADER = 801
HISTORICAL_PRICE_DATA_RECORD_RESPONSE = 803

# DTC HistoricalDataIntervalInSeconds values the engine asks for. These are the
# same second-counts `core.marketdata.ALL_TFS` and `engine.SEED_PLAN` use, so
# no mapping table is needed - the field takes seconds directly.
INTERVAL_1_MIN = 60
INTERVAL_5_MIN = 300
INTERVAL_15_MIN = 900
INTERVAL_1_HOUR = 3600
INTERVAL_1_DAY = 86400


# --------------------------------------------------------------- data shapes


@dataclass(frozen=True)
class Bar:
    """One historical OHLC bar, as `history()` returns it.

    Field names match `SymbolState.seed_bar`'s keyword arguments deliberately,
    so seeding is `state.seed_bar(period=..., start=bar.start, o=bar.open, ...)`
    with no translation layer.
    """

    start: datetime            # bar OPEN time, UTC, timezone-aware
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class DepthLevel:
    """One side of one price level in the order book."""

    quote_id: int              # our stable id for this level (see on_depth)
    side: str                  # "bid" | "ask"
    price: float
    size: float


@dataclass(frozen=True)
class ExecutionReport:
    """A fill, a rejection, or a position change, normalised.

    Built by the client from ORDER_UPDATE / POSITION_UPDATE so that
    `core.broker.LiveBroker.handle_execution` never has to know DTC field
    numbering. `quantity` is a CONTRACT COUNT - the whole codebase dropped
    fractional lots in the futures pivot.
    """

    symbol: str
    order_id: str
    label: str                 # our client order id, echoed back; re-attaches metadata
    status: str                # "filled" | "partial" | "cancelled" | "rejected"
    side: str                  # "BUY" | "SELL"
    quantity: int              # contracts
    price: float               # average fill price
    when: datetime
    is_close: bool = False     # True when this closed an existing position
    realised_pnl: float = 0.0  # account currency, populated on a close
    commission: float = 0.0
    text: str = ""             # broker's own message, verbatim


@dataclass
class AccountSummary:
    """What ACCOUNT_BALANCE_UPDATE reports. Used to replace the simulated
    starting balance with the real one in live mode."""

    account: str = ""
    balance: float = 0.0
    cash_balance: float = 0.0
    securities_value: float = 0.0
    margin_requirement: float = 0.0
    currency: str = "USD"


class DTCError(Exception):
    """Any protocol-level failure: bad handshake, logon refused, socket dead."""


# ------------------------------------------------------------------- client


class DTCClient:
    """Sierra Chart DTC client. NOT IMPLEMENTED - every method raises.

    Usage, exactly as `core/engine.py` and `ict/service.py` already write it:

        client = DTCClient(host="localhost", port=11099)
        client.on_tick  = handle_tick
        client.on_depth = handle_depth
        client.start()                       # returns once logged on
        client.subscribe(["MGC"])
        bars = client.history("MGC", 300, start, end)
        ...
        client.stop()
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 11099,
        username: str = "",
        password: str = "",
        trade_account: str = "",
        heartbeat_seconds: int = 15,
        client_name: str = "ict-bot",
    ) -> None:
        """Configure the client. Opens no socket - `start()` does that.

        `trade_account` is the Sierra Chart trade account orders route to;
        blank means whatever the logon response reports as default.
        `heartbeat_seconds` is a proposal - the server's LOGON_RESPONSE may
        shorten it, and the agreed value is what must actually be sent.
        """
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.trade_account = trade_account
        self.heartbeat_seconds = heartbeat_seconds
        self.client_name = client_name

        # Set by the caller before start(). See the callback section below.
        self.on_tick = None
        self.on_depth = None
        self.on_execution = None
        self.on_order_error = None
        self.on_connected = None
        self.on_disconnected = None
        self.on_account = None

        self.connected: bool = False
        self.account = AccountSummary()
        # name -> client-assigned DTC SymbolID, and back again.
        self._symbol_ids: dict[str, int] = {}
        self._symbol_names: dict[int, str] = {}

        raise NotImplementedError(
            "core/dtc_client.py is a blueprint - the socket code is not written "
            "yet. Run the engine and the collector with feed='dry'."
        )

    # ------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Connect, negotiate binary encoding, log on, start the threads.

        Blocks until LOGON_RESPONSE arrives or the attempt fails, so the caller
        knows whether it is live before it subscribes. Spawns:

          * a reader thread     - frames in, callbacks out
          * a heartbeat thread  - HEARTBEAT every agreed interval

        Returns True on success. Raises `DTCError` when the server refuses the
        logon (bad credentials, trading not enabled, encoding rejected);
        returns False on a plain connection failure so a supervisor can retry
        without a traceback.

        Sets `self.connected` and fires `on_connected()` - including on every
        later reconnect, which is the signal callers use to re-subscribe.
        """
        raise NotImplementedError

    def stop(self) -> None:
        """Send LOGOFF, stop both threads, close the socket. Idempotent.

        Must not raise: it runs from shutdown paths that are already handling
        an error.
        """
        raise NotImplementedError

    # ---------------------------------------------------------- market data

    def subscribe(self, symbols: list[str], exchange: str = "CME") -> None:
        """Start bid/ask streaming for each contract (MARKET_DATA_REQUEST).

        Assigns each name a client-side SymbolID, remembers the mapping, and
        resolves it back to the name before every callback - callers work in
        names only and never see a wire id.

        Subscriptions do NOT survive a reconnect: a fresh LOGON starts with an
        empty subscription set, so `on_connected` must call this again. Safe to
        call twice for the same symbol (re-request, same id).
        """
        raise NotImplementedError

    def subscribe_depth(self, symbols: list[str], levels: int = 10) -> None:
        """Start level-2 streaming (MARKET_DEPTH_REQUEST).

        Optional in the strict sense: the four `l2_*` features degrade to 0.0
        without it and every other feature and gate still works, so a failure
        here should be logged and swallowed, not fatal. CQG does publish MGC
        depth; some feeds do not.
        """
        raise NotImplementedError

    def history(
        self,
        symbol: str,
        period_seconds: int,
        start: datetime,
        end: datetime,
        max_bars: int = 0,
    ) -> list[Bar]:
        """Fetch historical OHLC bars. BLOCKING - never call from a callback.

        Sends HISTORICAL_PRICE_DATA_REQUEST with
        HistoricalDataIntervalInSeconds = `period_seconds` (use the INTERVAL_*
        constants; they are plain seconds and match `engine.SEED_PLAN` keys),
        then collects HISTORICAL_PRICE_DATA_RECORD_RESPONSE records until the
        final flag.

        Called at startup by `engine._seed_history` (5m/15m/1h) and
        `engine._seed_ict_daily` (86400), and by the collector for the same
        reasons. Returns bars oldest-first. Returns an empty list when the
        server has no data for the window rather than raising - a symbol with
        no history must not stop the process from starting.

        NOTE on daily bars: a CME futures "day" is the 17:00-16:00 ET session,
        not the UTC calendar day the old spot feed gave us. Decide explicitly
        which convention the daily bias uses before wiring this up; it moves
        every previous-day level the ICT rules key off.
        """
        raise NotImplementedError

    # ---------------------------------------------------------------- orders

    def market_order(
        self,
        symbol: str,
        side: str,
        contracts: int,
        stop_price: float = 0.0,
        target_price: float = 0.0,
        label: str = "",
        text: str = "",
    ) -> str:
        """Send a market order with an optional bracket. Returns the order id.

        `contracts` is a whole number - there is no fractional futures order.

        `stop_price` and `target_price` are ABSOLUTE PRICES, not the distances
        cTrader took. Callers hold distances, so `core.broker.LiveBroker` does
        the conversion against the entry side before calling. Both must sit on
        the contract's tick grid or Sierra Chart will reject or reprice the
        order - use `SymbolSpec.snap_to_tick` first.

        `label` is echoed back on the resulting ORDER_UPDATE as the client
        order id; that is how signal metadata is re-attached to a fill.

        Raises `DTCError` if not connected. Rejections arrive asynchronously on
        `on_order_error`, not as an exception here.
        """
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> None:
        """Cancel a working order (CANCEL_ORDER)."""
        raise NotImplementedError

    def flatten(self, symbol: str) -> None:
        """Close any open position in `symbol` at market.

        The panic path. Must work even when local position bookkeeping and the
        broker's disagree, so it should read the size from the last
        POSITION_UPDATE rather than from `LiveBroker.positions`.
        """
        raise NotImplementedError

    # ------------------------------------------------------------- callbacks
    #
    # Assigned by the caller before start(); all fire on the reader thread.
    # These are documented as methods purely to pin their signatures - the
    # implementation calls the assigned attributes, it does not override these.

    def _doc_on_tick(
        self, symbol: str, bid: float, ask: float, when: datetime
    ) -> None:
        """MARKET_DATA_UPDATE_BID_ASK arrived.

        Wired to `TradingEngine._on_tick` and `CollectorService._on_tick`.
        `when` is timezone-aware UTC, from the message's DateTime field where
        present and `datetime.now(timezone.utc)` otherwise. Prices are real
        doubles. Either side may be 0.0 when only one side changed; both
        callers carry the other side forward, so the client need not.
        """

    def _doc_on_depth(
        self,
        symbol: str,
        new_quotes: list[tuple],
        deleted_ids: list[int],
    ) -> None:
        """MARKET_DEPTH_UPDATE_LEVEL arrived, already normalised.

        `new_quotes` is a list of `(quote_id, side, price, size)` where side is
        "bid" or "ask" - the exact shape `SymbolState.apply_depth` takes.
        `deleted_ids` are levels that left the book.

        DTC identifies a level by price, not by id, while `SymbolState` keys
        its book by id. The client owns that translation: assign a stable id
        per (side, price), emit it in `new_quotes`, and put it in
        `deleted_ids` when the level's size goes to zero.
        """

    def _doc_on_execution(self, report: ExecutionReport) -> None:
        """A fill or position change. Wired to `TradingEngine._on_execution`,
        which hands it to `LiveBroker.handle_execution`."""

    def _doc_on_order_error(
        self, code: str, description: str, order_id: str = ""
    ) -> None:
        """An order was rejected. Wired to `TradingEngine._on_order_error`."""

    def _doc_on_connected(self) -> None:
        """Fired after every successful logon, first and subsequent.

        Wired to `TradingEngine._on_reconnected`, whose job is to re-issue the
        subscriptions a fresh session does not carry over.
        """

    def _doc_on_disconnected(self, reason: str) -> None:
        """The session dropped. The client reconnects on its own; this is for
        logging and for marking the feed stale so nothing trades on prices
        that stopped updating."""

    def _doc_on_account(self, summary: AccountSummary) -> None:
        """ACCOUNT_BALANCE_UPDATE arrived.

        In live mode the engine uses this to replace the simulated
        `PAPER_START_BALANCE` with the real balance, so the risk manager's
        daily limits are measured against actual capital.
        """
