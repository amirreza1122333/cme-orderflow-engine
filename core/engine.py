"""The engine: wires every component together and runs the decision loop.

Flow for one tick:

    tick -> market data -> paper broker exit check
                        -> (throttled) strategy evaluation
                        -> news blackout gate
                        -> analyst gate
                        -> risk gate (also sizes the position)
                        -> broker.open

Any gate can only say no. There is no path where a component makes the engine
trade more.

DATA FEED (rewritten 2026-08-21). The cTrader/Twisted transport is gone -
`core/ctrader.py` is archived as `core/ctrader_DEPRECATED.py`, and with it the
protobuf message types, the reactor and the whole Deferred style. Nothing in
this module imports either package any more, and nothing here opens a socket.

Two feeds are defined:

    "dry" - the default, and the only one that works today. Replays the ICT
            collector's historical CSVs through exactly the decision path a
            live tick takes, so the ML pipeline and every gate can be
            exercised offline with no broker at all.
    "dtc" - Sierra Chart over DTC. Refused until core/dtc_client.py exists;
            every attachment point is marked `TODO: Replace with DTC Client`.

The decision logic below the feed - strategy, ICT conditions, news, analyst,
risk, paper fills - is unchanged by the pivot and platform-agnostic.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

from config import DATA_DIR, LOG_DIR, Settings
from core import news as news_module
from core.analyst import Analyst, Verdict
from core.broker import LiveBroker, PaperBroker
# TODO: Replace with DTC Client - `from core.dtc_client import DTCClient`.
# core/ctrader.py was archived to core/ctrader_DEPRECATED.py in the pivot and
# must not be imported from here again.
from core.journal import Journal
from core.marketdata import ALL_TFS, ENTRY_TF, MarketData, tf_name
from core.news import NewsFilter
from core.risk import RiskManager
from core.symbols import SymbolRegistry
from core import indicators
from core.strategy import Strategy
from ict.daily_sweep import DailyCandle
from ict.features import ICTFeatureExtractor
from ict.predictor import BUY as ICT_BUY
from ict.predictor import ICTPredictor
from ict.signal import ICTSignalGenerator

log = logging.getLogger("engine")

EVALUATE_EVERY_SECONDS = 1.0
# Per timeframe: (bars to keep, lookback days). The lookback is wide enough to
# span a weekend, a holiday or the CME daily maintenance break in every case.
# The cTrader period enums that used to live in the third slot are gone; the
# DTC client will map these second-counts onto its own bar-period field.
SEED_PLAN = {
    300:  (240, 7),
    900:  (160, 10),
    3600: (120, 21),
    60:   (240, 7),   # kept so a 1m entry timeframe still works
}
STATE_REFRESH_SECONDS = 5   # how often the dashboard snapshot is republished
BLOCK_REPEAT_SECONDS = 300  # don't re-log an unchanged block reason more often

# Where the ICT collector writes its per-day feature CSVs. Dry mode replays
# these; columns 0-2 are timestamp, bid, ask (see ict/collect.py CSV_HEADER).
# This is DATA_DIR, not LOG_DIR: it has to be the directory the collector
# writes to and ict/prepare.py reads from, or the replay silently sees nothing.
DRY_CSV_GLOB = "ict_{symbol}_*.csv"


class _Periodic:
    """A repeating background task - the replacement for task.LoopingCall.

    Twisted went out with ctrader-open-api, and the engine only ever used
    LoopingCall for housekeeping (news refresh, status log, state file), never
    for anything on the trade path. A daemon thread with a timed wait covers
    that exactly, and `stop()` is prompt because the wait is on an Event
    rather than a sleep.
    """

    def __init__(self, interval: float, function, name: str) -> None:
        self.interval = interval
        self.function = function
        self.name = name
        self.running = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, now: bool = False) -> None:
        self.running = True
        if now:
            self._call()
        self._thread = threading.Thread(
            target=self._loop, name=f"periodic-{self.name}", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            self._call()

    def _call(self) -> None:
        try:
            self.function()
        except Exception:
            # One broken housekeeping task must never stop the others or the
            # trade loop; the traceback is enough to fix it after the session.
            log.exception("Periodic task %s failed", self.name)

    def stop(self) -> None:
        self.running = False
        self._stop.set()


def _in_thread(function, on_success=None, on_error=None, name: str = "work"):
    """Run a blocking call off the trade path - replaces threads.deferToThread.

    Same contract as the Deferred version it replaces: the caller returns
    immediately, and exactly one of the two callbacks fires when the work
    finishes. Used only for the news fetch and the Claude analyst call, both of
    which are HTTP requests that must never stall tick handling.
    """

    def run() -> None:
        try:
            result = function()
        except Exception as error:            # noqa: BLE001 - handed to caller
            if on_error is not None:
                on_error(error)
            return
        if on_success is not None:
            on_success(result)

    thread = threading.Thread(target=run, name=name, daemon=True)
    thread.start()
    return thread


class TradingEngine:
    def __init__(self, settings: Settings, feed: str = "dry") -> None:
        self.settings = settings
        self.feed = feed
        # TODO: Replace with DTC Client - this becomes
        #     DTCClient(host=settings.dtc_host, port=settings.dtc_port)
        # and `start()` below attaches the callbacks it fires. It stays None
        # while the engine runs dry, and every use of it is guarded.
        self.client = None
        # Contract specs used to arrive from the broker's symbol list. Under
        # DTC they come from config.py, so the registry is built locally and
        # is complete before anything connects.
        self.registry = SymbolRegistry()
        self.market = MarketData(timeframes=ALL_TFS)
        self.strategies: dict[str, Strategy] = {}
        self.journal = Journal(LOG_DIR, mode=settings.execution_mode)
        self.news = NewsFilter(
            before_minutes=settings.news_blackout_before_min,
            after_minutes=settings.news_blackout_after_min,
        )
        self.analyst = Analyst(
            api_key=settings.anthropic_api_key,
            model=settings.anthropic_model,
            interval_minutes=settings.analyst_interval_minutes,
        )
        self.risk = RiskManager(settings, starting_balance=settings.paper_start_balance)

        if settings.live_execution:
            # TODO: Replace with DTC Client - pass the connected DTCClient
            # here. Until then LiveBroker has no transport and refuses every
            # order; see core/broker.py.
            self.broker = LiveBroker(self.client, enabled=True)
        else:
            self.broker = PaperBroker(
                commission_per_contract=settings.commission_per_contract,
                slippage_price=0.0,
                stop_slippage_price=0.0,
            )
        self.broker.on_closed = self._on_trade_closed

        # ICT mode keeps its own feature extractors, fed from the same tick
        # stream. They are separate objects from the collector's - different
        # process, no shared state - so neither can corrupt the other.
        self.ict_mode = settings.strategy_mode == "ict"
        self.predictor: ICTPredictor | None = None
        self.ict_extractors: dict[str, ICTFeatureExtractor] = {}
        self.ict_signals: dict[str, ICTSignalGenerator] = {}
        self._ict_bucket: dict[str, int] = {}
        if self.ict_mode:
            self.predictor = ICTPredictor()

        self.autotrade = settings.autotrade_default
        # Set when startup fails, so the process can exit non-zero and a
        # supervisor with Restart=on-failure actually restarts it.
        self.failed = False
        self._veto_counts: Counter = Counter()
        self._block_state: dict[tuple[str, str], tuple[str | None, datetime | None]] = {}
        self._last_evaluation: dict[str, datetime] = {}
        self._loops: list[task.LoopingCall] = []
        self._active_symbols: list[str] = []
        self._started_at: datetime | None = None

    # -------------------------------------------------------------- lifecycle

    def start(self) -> bool:
        """Bring the engine up. Returns True when it is running.

        No network call happens on any path through here today. The dry feed
        needs none, and the DTC feed is refused because its client does not
        exist yet - which is the point of the pivot: the engine must never
        silently fall back to trying cTrader.
        """
        if self.feed == "dtc":
            # TODO: Replace with DTC Client. This is the attachment point:
            #     self.client = DTCClient(settings.dtc_host, settings.dtc_port)
            #     self.client.on_tick      = self._on_tick
            #     self.client.on_depth     = self._on_depth
            #     self.client.on_execution = self._on_execution
            #     self.client.on_order_error = self._on_order_error
            #     self.client.on_connected = self._on_reconnected
            #     self.client.start()
            # then call self._bootstrap().
            self._fatal(
                "DTC feed requested but core/dtc_client.py is not implemented. "
                "Run the engine with feed='dry' until it is."
            )
            return False
        if self.feed != "dry":
            self._fatal(f"Unknown feed {self.feed!r} - expected 'dry' or 'dtc'")
            return False

        try:
            self._bootstrap()
        except Exception as error:                      # noqa: BLE001
            self._fatal(error)
            return False
        return True

    def _bootstrap(self) -> None:
        settings = self.settings
        wanted = [name for name, cfg in settings.symbols.items() if cfg.enabled]
        if not wanted:
            raise SystemExit("No contracts enabled in config.py")

        # Contract specifications come from config.py now, not from a broker
        # symbol download. The ids are local and only have to be stable within
        # a session; the DTC client will map names to whatever Sierra Chart
        # uses on the wire.
        registry = self.registry
        for index, name in enumerate(wanted, start=1):
            registry.add(settings.symbols[name].to_spec(symbol_id=index))
        registry.deposit_asset = "USD"

        log.info(
            "Feed: %s | account balance %.2f (simulated)",
            self.feed.upper(), self.risk.balance,
        )
        # TODO: Replace with DTC Client - in live mode, read the real account
        # balance from the DTC ACCOUNT_BALANCE_UPDATE message and set
        # self.risk.starting_balance / balance / equity from it, the way the
        # cTrader trader() call used to.

        for problem in settings.underfunded_warnings():
            log.warning("UNDERFUNDED  %s", problem)

        self._active_symbols = []
        for name in wanted:
            spec = registry.by_name(name)
            if spec is None:
                continue
            self.market.register(spec)
            self.strategies[spec.name] = Strategy(settings.symbols[name])
            if self.ict_mode:
                self.ict_extractors[spec.name] = ICTFeatureExtractor(
                    spec.name, tick_size=10.0 ** (-spec.digits),
                    entry_period=ENTRY_TF,
                )
                self.ict_signals[spec.name] = ICTSignalGenerator(
                    spec.name, predictor=self.predictor,
                    require_model=settings.ict_require_model,
                )
            self._active_symbols.append(spec.name)
            log.info(
                "%s | %s | stop %g -> %.2f risk per contract, min %d contract(s)",
                spec.describe(),
                f"tick {spec.tick_size:g} = {spec.tick_value:g} {spec.quote_asset}",
                settings.symbols[name].stop_distance,
                spec.risk_for_volume(
                    spec.min_contracts, settings.symbols[name].stop_distance
                ),
                spec.min_contracts,
            )

        mismatched = registry.check_currency()
        if mismatched:
            log.warning(
                "P&L for %s is computed in its quote currency, not %s - "
                "paper numbers for those contracts will drift from the real ones.",
                ", ".join(mismatched),
                registry.deposit_asset,
            )

        symbol_ids = [registry.by_name(n).symbol_id for n in self._active_symbols]
        self._seed_history(symbol_ids)
        if self.ict_mode:
            self._seed_ict_daily(symbol_ids)

        # TODO: Replace with DTC Client data feed. This is where the engine
        # used to subscribe to cTrader spot and depth streams:
        #     self.client.subscribe(self._active_symbols)   # MARKET_DATA_REQUEST
        #     self.client.subscribe_depth(self._active_symbols)  # MARKET_DEPTH_REQUEST
        # Ticks then arrive on self._on_tick(symbol, bid, ask, when) and depth
        # on self._on_depth(symbol, bids, asks). Dry mode drives the exact same
        # two callbacks from CSV instead - see `replay()`.
        log.info(
            "No live subscription: feed is %s. Ticks come from %s.",
            self.feed.upper(),
            "replay() over the collector CSVs" if self.feed == "dry"
            else "the DTC client",
        )

        self._started_at = datetime.now(timezone.utc)
        self._start_loops()

        mode = "LIVE" if settings.live_execution else "PAPER"
        log.info("=" * 62)
        log.info("Engine running in %s mode | autotrade=%s", mode, self.autotrade)
        log.info("Strategy: %s", settings.strategy_mode.upper())
        if self.ict_mode:
            ready = self.predictor is not None and self.predictor.ready
            log.info("  entry logic : ict/signal.py, 8 conditions, on 5m bar close")
            log.info("  model       : %s",
                     self.predictor.status() if self.predictor else "none")
            if settings.ict_require_model and not ready:
                bang = "!" * 56
                log.warning("  %s", bang)
                log.warning("  ICT_REQUIRE_MODEL=true and no trained model exists.")
                log.warning("  THIS ENGINE WILL NOT OPEN ANY TRADE until one does.")
                log.warning("  Set ICT_REQUIRE_MODEL=false in .env to run the eight")
                log.warning("  ICT conditions on their own while data collects.")
                log.warning("  %s", bang)
        if settings.live_execution:
            log.warning(
                "EXECUTION_MODE=live, but the live order path is OFFLINE until "
                "core/dtc_client.py exists. No order can be sent."
            )
        log.info("=" * 62)
        self.journal.event(
            "engine_started", mode=mode, feed=self.feed,
            symbols=self._active_symbols,
        )

    # ---------------------------------------------------------------- seeding

    def _csv_rows(self, symbol: str):
        """Yield (timestamp, bid, ask) from the collector's CSVs for a symbol.

        The collector's format is fixed (ict/collect.py CSV_HEADER): the first
        four columns are timestamp, bid, ask, mid and the rest are features we
        do not need here - the engine recomputes its own. Files are read in
        date order so the replay is chronological across day boundaries.
        """
        paths = sorted(DATA_DIR.glob(DRY_CSV_GLOB.format(symbol=symbol)))
        for path in paths:
            try:
                with path.open("r", encoding="utf-8", newline="") as handle:
                    for row in csv.DictReader(handle):
                        try:
                            when = datetime.fromisoformat(row["timestamp"])
                            bid = float(row["bid"])
                            ask = float(row["ask"])
                        except (KeyError, ValueError):
                            continue
                        if when.tzinfo is None:
                            when = when.replace(tzinfo=timezone.utc)
                        if bid > 0 and ask > 0:
                            yield when, bid, ask
            except OSError as error:
                log.warning("Could not read %s: %s", path.name, error)

    def _seed_history(self, symbol_ids: list[int]) -> None:
        """Warm every timeframe from history so we can trade soon after start.

        Without this the 1-hour series would need 26 hours of live ticks before
        it produced a trend read, and nothing would trade until then.

        TODO: Replace with DTC Client data feed. This used to be one historical
        bars request per timeframe against cTrader
        (`self.client.trendbars(symbol_id, from_ms, to_ms, period=...)`),
        decoding the deltaOpen/deltaHigh/deltaClose encoding off the wire. The
        DTC equivalent is HISTORICAL_PRICE_DATA_REQUEST, which returns plain
        OHLC doubles per bar - map SEED_PLAN's second-counts onto its
        bar-period field and feed each bar to `state.seed_bar`. The lookback
        per timeframe is deliberately wide: a short window started on a weekend
        or over the CME maintenance break falls entirely inside the gap and
        returns nothing.

        Dry mode instead folds the collector's recorded ticks into bars, which
        warms the same structures from the same data the ML pipeline uses.
        """
        for symbol_id in symbol_ids:
            state = self.market.by_id(symbol_id)
            if state is None:
                continue
            name = state.spec.name
            ticks = 0
            for when, bid, ask in self._csv_rows(name):
                state.add_tick(bid, ask, when)
                ticks += 1
            if not ticks:
                log.warning(
                    "%s: no history seeded - no %s files in %s. Every timeframe "
                    "starts cold, so nothing will evaluate until enough live "
                    "bars have formed.",
                    name, DRY_CSV_GLOB.format(symbol=name), DATA_DIR,
                )
                continue
            seeded = [
                f"{tf_name(tf)}={state.bar_count(tf)}" for tf in state.timeframes
            ]
            log.info(
                "%s: seeded %s bars from %d recorded ticks",
                name, " ".join(seeded), ticks,
            )

    def _seed_ict_daily(self, symbol_ids: list[int]) -> None:
        """Give the ICT extractors their daily candles.

        Without this `daily_bias` is NEUTRAL forever, `prev_day_high/low` stay
        unset, the daily box never forms and the 1h-fakeout path cannot fire -
        so condition 2 vetoes every single evaluation and the veto counts say
        nothing about the market.

        TODO: Replace with DTC Client data feed. The cTrader version pulled ten
        days of D1 trendbars; ask DTC for daily HISTORICAL_PRICE_DATA_REQUEST
        bars instead. Note that a CME futures "day" is the 17:00-16:00 ET
        session, NOT the UTC calendar day the spot feed gave us - decide
        explicitly which one the daily bias should use before wiring this up,
        because it moves every previous-day level the ICT rules key off.

        Dry mode aggregates whatever bars the CSV replay produced into UTC
        days, which is the convention the cTrader path used.
        """
        for symbol_id in symbol_ids:
            state = self.market.by_id(symbol_id)
            if state is None:
                continue
            extractor = self.ict_extractors.get(state.spec.name)
            if extractor is None:
                continue

            by_day: dict = {}
            for bar in state.bars(ENTRY_TF):
                day = bar.start.date()
                candle = by_day.get(day)
                if candle is None:
                    by_day[day] = DailyCandle(
                        day=day, high=bar.high, low=bar.low,
                        close=bar.close, open=bar.open,
                    )
                else:
                    candle.high = max(candle.high, bar.high)
                    candle.low = min(candle.low, bar.low)
                    candle.close = bar.close

            today = datetime.now(timezone.utc).date()
            # Today's candle is still forming; bias needs completed ones.
            completed = [by_day[day] for day in sorted(by_day) if day < today][-5:]
            if completed:
                extractor.seed_daily(completed)
                log.info(
                    "%s daily seeded: %d candles, prev day %.*f-%.*f, bias %+d",
                    state.spec.name, len(completed),
                    state.spec.digits, completed[-1].low,
                    state.spec.digits, completed[-1].high,
                    extractor.daily.bias,
                )
            else:
                log.warning(
                    "%s got no completed daily candles - condition 2 will veto "
                    "everything until this is fixed", state.spec.name,
                )

    def _refresh_ict_daily(self) -> None:
        """Re-seed after a UTC day rolls over, so a long-running process does
        not keep trading yesterday's structure."""
        if not self.ict_mode:
            return
        symbol_ids = [
            self.registry.by_name(name).symbol_id
            for name in self._active_symbols
            if self.registry.by_name(name)
        ]
        try:
            self._seed_ict_daily(symbol_ids)
        except Exception as error:                      # noqa: BLE001
            log.warning("Daily re-seed failed: %s", error)

    # ------------------------------------------------------------- dry replay

    def replay(self, limit: int | None = None) -> int:
        """Push recorded ticks through the live decision path. Dry feed only.

        This is the offline driver: it calls `_on_tick` exactly as the DTC
        client will, so the strategy, the ICT extractors, the gates, the risk
        sizing and the paper fills all run against historical data with no
        broker anywhere. That makes it the harness the ML pipeline is checked
        against, and it is why the engine still starts with cTrader gone.

        Returns the number of ticks replayed.
        """
        if self.feed != "dry":
            raise RuntimeError("replay() is only valid on the dry feed")
        total = 0
        for name in self._active_symbols:
            for when, bid, ask in self._csv_rows(name):
                self._on_tick(name, bid, ask, when)
                total += 1
                if limit is not None and total >= limit:
                    log.info("Replay stopped at the %d tick limit", limit)
                    return total
        log.info("Replayed %d recorded ticks", total)
        return total

    def _start_loops(self) -> None:
        news_loop = _Periodic(
            self.settings.news_refresh_minutes * 60, self._refresh_news, "news"
        )
        news_loop.start(now=True)
        self._loops.append(news_loop)

        status_loop = _Periodic(300, self._log_status, "status")
        status_loop.start(now=False)
        self._loops.append(status_loop)

        state_loop = _Periodic(STATE_REFRESH_SECONDS, self._write_state, "state")
        state_loop.start(now=True)
        self._loops.append(state_loop)

        if self.ict_mode:
            # Daily structure goes stale at the UTC rollover.
            daily_loop = _Periodic(3600, self._refresh_ict_daily, "daily")
            daily_loop.start(now=False)
            self._loops.append(daily_loop)

        # No analyst polling loop on purpose - see `_request_analyst`.

    def stop(self) -> None:
        for loop in self._loops:
            if loop.running:
                loop.stop()
        self._loops.clear()
        if self.broker.positions:
            log.warning(
                "Stopping with %d open position(s): %s",
                len(self.broker.positions),
                ", ".join(
                    f"{p.symbol} {p.side}" for p in self.broker.positions.values()
                ),
            )
        self._log_status()
        self.journal.event("engine_stopped", **self.risk.status())
        # TODO: Replace with DTC Client - close the socket and stop the
        # heartbeat thread here (`self.client.stop()`).
        if self.client is not None:
            self.client.stop()

    def _fatal(self, error) -> None:
        """Record an unrecoverable startup problem.

        Under Twisted this took a Failure and stopped the reactor. There is no
        reactor now: the caller (run.py) checks `engine.failed` and exits
        non-zero so a supervisor with Restart=on-failure restarts the process.
        """
        self.failed = True
        message = str(error)
        log.error("Startup failed: %s", message)
        self.journal.event("startup_failed", error=message)

    def _on_reconnected(self) -> None:
        """Re-subscribe after the feed drops and comes back.

        TODO: Replace with DTC Client data feed. Sierra Chart drops a session
        that misses its heartbeat, and a reconnect starts from a clean LOGON
        with no subscriptions, so this must re-issue MARKET_DATA_REQUEST and
        MARKET_DEPTH_REQUEST for every active contract - the equivalent of the
        cTrader resubscribe this replaces.
        """
        if not self._active_symbols:
            return  # first connect; _bootstrap handles subscriptions
        log.info("Reconnected - resubscribing to %s",
                 ", ".join(self._active_symbols))
        if self.client is None:
            return

    # ----------------------------------------------------------- market events

    def _on_tick(
        self,
        symbol: str,
        bid: float,
        ask: float,
        when: datetime | None = None,
    ) -> None:
        """One quote update. The single entry point for all market data.

        TODO: Replace with DTC Client data feed - this is the callback the DTC
        client fires. Its signature is deliberately protocol-free: a contract
        name and two real prices. The old cTrader version took a protobuf
        ProtoOASpotEvent and had to un-scale integer prices through
        `price_from_relative` and carry a partial quote forward when only one
        side moved; DTC MARKET_DATA_UPDATE_BID_ASK carries both sides as
        doubles, so that decoding step is gone. Dry replay calls this too, so
        anything that works offline works live.
        """
        state = self.market.get(symbol)
        if state is None:
            return
        spec = state.spec

        # A feed may send one side only; carry the other forward rather than
        # dropping the update.
        last = state.last_tick
        if bid <= 0 and last is not None:
            bid = last.bid
        if ask <= 0 and last is not None:
            ask = last.ask
        if bid <= 0 or ask <= 0:
            return

        timestamp = when or datetime.now(timezone.utc)
        state.add_tick(bid, ask, timestamp)

        if isinstance(self.broker, PaperBroker):
            self.broker.on_tick(spec, state)

        if self.ict_mode:
            self._on_tick_ict(spec.name, timestamp, bid, ask, state)
        else:
            self._maybe_trade(state)

    def _on_depth(
        self,
        symbol: str,
        new_quotes: list[tuple],
        deleted_ids: list | None = None,
    ) -> None:
        """Incremental level-2 update.

        TODO: Replace with DTC Client data feed. `new_quotes` is a list of
        (quote_id, "bid"|"ask", price, size) and `deleted_ids` the ids that
        left the book - the shape `SymbolState.apply_depth` already wants. The
        cTrader version decoded a ProtoOADepthEvent to get here; the DTC
        MARKET_DEPTH_UPDATE_LEVEL messages carry price, quantity and a
        side/operation flag, so the translation belongs in the client, not
        here. Depth is optional: the imbalance feature degrades to 0 without
        it and every other gate still runs.
        """
        state = self.market.get(symbol)
        if state is None:
            return
        state.apply_depth(new_quotes, list(deleted_ids or []))

    def _on_execution(self, report) -> None:
        """A fill or a close came back from the broker.

        TODO: Replace with DTC Client data feed - `report` will be a decoded
        DTC ORDER_UPDATE / POSITION_UPDATE rather than a protobuf
        ProtoOAExecutionEvent. `LiveBroker.handle_execution` is stubbed to
        match; both sides land together when the client is written.
        """
        if isinstance(self.broker, LiveBroker):
            trade = self.broker.handle_execution(report, self.registry)
            if trade is None:
                self.journal.event("execution", detail=str(report))

    def _on_order_error(self, code, description: str, order_id="") -> None:
        log.error("Order rejected: %s %s (order %s)", code, description, order_id)
        self.journal.event("order_error", code=code, description=description)

    # ------------------------------------------------------------- decisioning

    def _maybe_trade(self, state) -> None:
        symbol = state.spec.name
        now = datetime.now(timezone.utc)

        last_evaluated = self._last_evaluation.get(symbol)
        if last_evaluated and (now - last_evaluated).total_seconds() < EVALUATE_EVERY_SECONDS:
            return
        self._last_evaluation[symbol] = now

        signal = self.strategies[symbol].evaluate(state)
        if not signal.tradable:
            # Tally *why* we are not trading. In week one this is the most
            # useful output the engine produces - "no signals" and "every
            # signal vetoed on spread" need completely different fixes.
            for veto in signal.vetoes:
                self._veto_counts[(symbol, _veto_key(veto))] += 1
            return

        if not self.autotrade:
            log.info("[disarmed] would have taken %s", signal.describe())
            self.journal.event("signal_disarmed", **_signal_payload(signal))
            return

        blocked, reason = self.news.blackout(symbol, now)
        if blocked:
            self._note_block(symbol, "blocked_news", reason, now)
            return

        # Ask Claude only when a trade is actually on the table. A timed poll
        # (2 symbols every 10 minutes) would run ~290 calls a day and cost more
        # than this account could plausibly earn; demand-driven cuts that by
        # more than 90% because most evaluations never get this far.
        if self.analyst.needs_refresh(symbol, now):
            self._request_analyst(symbol, now)

        verdict = self.analyst.verdict(symbol)
        veto = verdict.blocks(signal.direction)
        if veto:
            self._note_block(symbol, "blocked_analyst", veto, now)
            return

        config = self.settings.symbols.get(symbol)
        if config is None:
            return
        decision = self.risk.can_trade(
            spec=state.spec,
            stop_distance=config.stop_distance,
            open_positions=self.broker.open_count,
            now=now,
        )
        if not decision.allowed:
            self._note_block(symbol, "blocked_risk", decision.reason, now)
            return

        meta = {
            "confidence": round(signal.confidence, 3),
            "score": round(signal.score, 1),
            "reasons": signal.reasons,
            "analyst": verdict.action,
        }
        result = self.broker.open(
            spec=state.spec,
            side=signal.direction,
            volume=decision.volume,
            stop_distance=config.stop_distance,
            target_distance=config.target_distance,
            state=state,
            meta=meta,
        )
        if result is None:
            return

        self.risk.record_open(symbol, now)
        log.info(
            "OPEN %s | risk %.2f | %d contract(s)",
            signal.describe(),
            decision.risk_amount,
            decision.contracts,
        )
        self.journal.event(
            "trade_opened",
            symbol=symbol,
            side=signal.direction,
            volume=decision.contracts,
            contracts=decision.contracts,
            risk=round(decision.risk_amount, 2),
            **_signal_payload(signal),
        )

    def _note_block(
        self, symbol: str, kind: str, reason: str, now: datetime
    ) -> None:
        """Log and journal a gate block, but only once per distinct reason.

        A blocked signal re-fires on every evaluation - while a position is
        open that is one identical line per second. The reason is always
        counted; it is only *written* when it changes or after a quiet period.
        """
        key = (symbol, kind)
        self._veto_counts[(symbol, _veto_key(reason))] += 1
        previous, last_written = self._block_state.get(key, (None, None))
        normalised = _veto_key(reason)
        if previous == normalised and last_written is not None:
            if (now - last_written).total_seconds() < BLOCK_REPEAT_SECONDS:
                return
        self._block_state[key] = (normalised, now)
        log.info("%s blocked: %s", symbol, reason)
        self.journal.event(kind, symbol=symbol, reason=reason)

    # ------------------------------------------------------------- ICT mode

    def _on_tick_ict(self, symbol: str, moment: datetime, bid: float,
                     ask: float, state) -> None:
        """Every tick updates the ICT trackers; the decision is taken on the
        5m bar close, which is the cadence the features are defined on."""
        extractor = self.ict_extractors.get(symbol)
        if extractor is None:
            return
        try:
            extractor.on_tick(moment, bid, ask)
            bucket = int(moment.timestamp()) // ENTRY_TF
            previous = self._ict_bucket.get(symbol)
            if previous is None:
                self._ict_bucket[symbol] = bucket
                return
            if bucket == previous:
                return
            self._ict_bucket[symbol] = bucket
            extractor.on_bars(state)
            self._maybe_trade_ict(symbol, moment, bid, ask, state)
        except Exception:
            # A fault in the ICT layer must not take the engine down; any open
            # position is still managed by the broker.
            log.exception("ICT evaluation failed for %s", symbol)

    def _maybe_trade_ict(self, symbol: str, moment: datetime, bid: float,
                         ask: float, state) -> None:
        extractor = self.ict_extractors[symbol]
        generator = self.ict_signals[symbol]
        spec = state.spec

        if not state.warmed(26):
            self._veto_counts[(symbol, "warming up")] += 1
            return

        atr = indicators.atr(
            state.highs(ENTRY_TF, 200), state.lows(ENTRY_TF, 200),
            state.closes(ENTRY_TF, 200), 14,
        )
        blocked, news_reason = self.news.blackout(symbol, moment)
        features = extractor.extract(moment, bid, ask, state)

        signal = generator.evaluate(
            features=features, moment=moment, spec=spec, atr=atr or 0.0,
            risk_amount=self.settings.risk_per_trade,
            news_blocked=blocked, news_reason=news_reason,
        )

        # Every failed condition is counted, not just the first, so the journal
        # shows which gate is actually doing the blocking. Skipped gates are
        # counted under their own key: a condition that is permanently skipped
        # means its data source is dead, which looks nothing like a pass.
        for condition in signal.failed:
            self._veto_counts[(symbol, f"{condition.number}.{condition.name}")] += 1
        for condition in signal.skipped:
            self._veto_counts[(symbol, f"{condition.number}.{condition.name} SKIPPED")] += 1

        if not signal.actionable:
            reason = (signal.veto_reasons[0] if signal.veto_reasons
                      else "no structural direction")
            self._note_block(symbol, "blocked_ict", reason, moment)
            return

        # Data-only symbols: everything above this line has already run, so the
        # feature vector, the veto counters and this "would have traded" record
        # all keep feeding the ML training set. Only the position is withheld.
        sym_cfg = self.settings.symbols.get(symbol)
        if sym_cfg is not None and not sym_cfg.tradable:
            log.info("[data-only] %s would have taken %s",
                     symbol, signal.describe())
            self.journal.event("signal_untraded", symbol=symbol,
                               direction=signal.direction,
                               reasons=signal.reasons,
                               why="symbol marked tradable=False in config")
            return

        if not self.autotrade:
            log.info("[disarmed] would have taken %s", signal.describe())
            self.journal.event("signal_disarmed", symbol=symbol,
                               direction=signal.direction,
                               reasons=signal.reasons)
            return

        side = "BUY" if signal.direction == ICT_BUY else "SELL"
        if self.analyst.needs_refresh(symbol, moment):
            self._request_analyst(symbol, moment)
        veto = self.analyst.verdict(symbol).blocks(side)
        if veto:
            self._note_block(symbol, "blocked_analyst", veto, moment)
            return

        # core/risk.py stays authoritative for size and the daily caps, so a
        # strategy module cannot size around them.
        decision = self.risk.can_trade(
            spec=spec, stop_distance=signal.stop_distance,
            open_positions=self.broker.open_count, now=moment,
        )
        if not decision.allowed:
            self._note_block(symbol, "blocked_risk", decision.reason, moment)
            return

        if signal.degraded:
            log.warning(
                "%s trading with %d gate(s) skipped: %s",
                symbol, len(signal.skipped),
                "; ".join(str(c) for c in signal.skipped),
            )

        result = self.broker.open(
            spec=spec, side=side, volume=decision.contracts,
            stop_distance=signal.stop_distance,
            target_distance=signal.target_distance,
            state=state,
            meta={"confidence": round(signal.confidence, 3),
                  "reasons": signal.reasons,
                  "model": signal.prediction.label if signal.prediction else "none"},
        )
        if result is None:
            return
        self.risk.record_open(symbol, moment)
        log.info("OPEN %s | risk %.2f | %d contract(s)", signal.describe(),
                 decision.risk_amount, decision.contracts)
        self.journal.event(
            "trade_opened", symbol=symbol, side=side,
            volume=decision.contracts, contracts=decision.contracts,
            risk=round(decision.risk_amount, 2),
            confidence=round(signal.confidence, 3),
            conditions=signal.reasons,
            model=signal.prediction.label if signal.prediction else "none",
        )

    def _on_trade_closed(self, trade: dict) -> None:
        self.risk.record_close(
            symbol=trade["symbol"],
            pnl=float(trade.get("pnl", 0.0)),
            exit_reason=trade.get("exit_reason", ""),
        )
        trade["balance"] = round(self.risk.balance, 2)
        self.journal.trade(trade)

    # ----------------------------------------------------------- housekeeping

    def _refresh_news(self) -> None:
        _in_thread(
            news_module.fetch_calendar,
            on_success=self.news.update,
            on_error=lambda error: self.news.update_failed(str(error)),
            name="news-fetch",
        )

    def _request_analyst(self, symbol: str, now: datetime) -> None:
        """Kick off a background verdict refresh. Never blocks the decision.

        The current (possibly slightly stale) verdict is used for this tick;
        the fresh one applies from the next signal onward. The analyst can only
        remove trades, so running one tick behind is a safe trade-off against
        adding seconds of latency to every entry.
        """
        if not self.analyst.enabled:
            return
        state = self.market.get(symbol)
        if state is None or state.last_tick is None or not state.warmed(26):
            return
        self.analyst.mark_requested(symbol, now)
        snapshot = self._snapshot(symbol, state)
        _in_thread(
            lambda: self.analyst.analyse(snapshot),
            on_success=lambda verdict, s=symbol: self.analyst.store(s, verdict),
            on_error=lambda error, s=symbol: self._analyst_failed(s, error),
            name=f"analyst-{symbol}",
        )

    def _analyst_failed(self, symbol: str, error) -> None:
        log.warning("Analyst call failed for %s: %s", symbol, error)
        # Failing open: a broken analyst must not silently halt trading, and it
        # can only ever remove trades anyway.
        self.analyst.store(
            symbol,
            Verdict(action="trade", reasoning="analyst unavailable", confidence=0.0),
        )

    def _snapshot(self, symbol: str, state) -> dict:
        signal = self.strategies[symbol].evaluate(state)
        context = signal.context
        upcoming = [
            {
                "in_minutes": round(
                    (event.when - datetime.now(timezone.utc)).total_seconds() / 60
                ),
                "title": event.title,
                "currency": event.currency,
                "forecast": event.forecast,
                "previous": event.previous,
            }
            for event in self.news.upcoming(symbol, hours=4)
        ]
        recent = [
            {"pnl": row["pnl"], "symbol": row["symbol"]}
            for row in self.risk.history[-5:]
        ]
        digits = state.spec.digits
        return {
            "symbol": symbol,
            "utc_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "price": context.get("price"),
            "spread": context.get("spread"),
            "median_spread": context.get("median_spread"),
            # The analyst sees the same three timeframes the strategy trades on,
            # so its verdict is formed from the same evidence.
            "entry_timeframe": tf_name(ENTRY_TF),
            "higher_timeframe_trends": context.get("confirm"),
            "recent_closes": {
                tf_name(tf): [round(c, digits) for c in state.closes(tf, 20)]
                for tf in ALL_TFS
            },
            f"atr_{tf_name(ENTRY_TF)}": context.get("atr"),
            "rsi_14": context.get("rsi"),
            "ema_fast": context.get("ema_fast"),
            "ema_slow": context.get("ema_slow"),
            "order_book_imbalance": context.get("imbalance"),
            "has_level2": context.get("has_depth"),
            "technical_signal": {
                "direction": signal.direction,
                "confidence": round(signal.confidence, 2),
                "reasons": signal.reasons,
                "vetoes": signal.vetoes,
            },
            "upcoming_high_impact_news": upcoming,
            "recent_trades": recent,
            "daily_pnl": self.risk.day.realised,
            "trades_today": self.risk.day.trades,
        }

    # ------------------------------------------------------------- state file

    def snapshot(self) -> dict:
        """Everything the dashboard shows, as plain JSON-safe values.

        Deliberately contains no credentials: the panel process only ever sees
        this dict, so a compromised panel cannot leak the broker token. See
        `panel/server.py` for the assertion that enforces it.
        """
        now = datetime.now(timezone.utc)
        status = self.risk.status()
        settings = self.settings

        symbols = []
        for name in self._active_symbols:
            state = self.market.get(name)
            if state is None:
                continue
            spec = state.spec
            config = settings.symbols.get(name)
            tick = state.last_tick
            signal = (
                self.strategies[name].evaluate(state)
                if name in self.strategies
                else None
            )
            blackout, blackout_reason = self.news.blackout(name, now)
            verdict = self.analyst.verdict(name)
            symbols.append({
                "name": name,
                "digits": spec.digits,
                "bid": tick.bid if tick else None,
                "ask": tick.ask if tick else None,
                "spread": round(tick.spread, spec.digits + 1) if tick else None,
                "median_spread": round(state.median_spread(), spec.digits + 1),
                "max_spread": config.max_spread if config else None,
                "last_tick_age": (
                    round((now - tick.timestamp).total_seconds(), 1) if tick else None
                ),
                "bars": {tf_name(tf): state.bar_count(tf) for tf in state.timeframes},
                "has_depth": state.has_depth(),
                "imbalance": round(state.imbalance(), 3),
                "confirm": (signal.context.get("confirm") if signal else None),
                "signal": {
                    "direction": signal.direction if signal else None,
                    "confidence": round(signal.confidence, 3) if signal else 0.0,
                    "score": round(signal.score, 1) if signal else 0.0,
                    "reasons": signal.reasons if signal else [],
                    "vetoes": signal.vetoes if signal else [],
                } if signal else None,
                "news_blackout": blackout,
                "news_reason": blackout_reason,
                "analyst": {
                    "action": verdict.action,
                    "bias": verdict.bias,
                    "confidence": round(verdict.confidence, 2),
                    "reasoning": verdict.reasoning,
                    "age_minutes": round(
                        (now - verdict.created_at).total_seconds() / 60, 1
                    ),
                },
            })

        positions = []
        for position in self.broker.positions.values():
            state = self.market.get(position.symbol)
            unrealised = None
            if state is not None and state.last_tick is not None:
                exit_price = (
                    state.last_tick.bid if position.is_buy else state.last_tick.ask
                )
                unrealised = round(
                    state.spec.pnl(
                        position.volume, position.entry, exit_price, position.is_buy
                    ) - position.commission,
                    2,
                )
            positions.append({
                "symbol": position.symbol,
                "side": position.side,
                "contracts": (
                    state.spec.contracts(position.volume) if state
                    else int(position.volume)
                ),
                "entry": position.entry,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
                "opened_at": position.opened_at.isoformat(),
                "unrealised": unrealised,
            })

        equity, running = [], self.risk.starting_balance
        for row in self.risk.history:
            running = row["balance"]
            equity.append({"time": row["time"], "balance": round(running, 2)})

        upcoming = []
        for name in self._active_symbols:
            for event in self.news.upcoming(name, hours=12, now=now):
                entry = {
                    "in_minutes": round((event.when - now).total_seconds() / 60),
                    "title": event.title,
                    "currency": event.currency,
                    "when": event.when.isoformat(),
                }
                if entry not in upcoming:
                    upcoming.append(entry)
        upcoming.sort(key=lambda item: item["in_minutes"])

        return {
            "generated_at": now.isoformat(),
            "engine": {
                "mode": settings.execution_mode,
                "feed": self.feed,
                "autotrade": self.autotrade,
                "armed": bool(getattr(self.broker, "armed", False)),
                # TODO: Replace with DTC Client - `self.client.connected` once
                # the client exists. Dry mode is never "connected" and should
                # not claim to be.
                "connected": bool(
                    self.client is not None and getattr(self.client, "connected", False)
                ),
                "broker": "Sierra Chart (DTC)",
                "dtc_address": settings.dtc_address,
                "entry_timeframe": tf_name(ENTRY_TF),
                "timeframes": [tf_name(tf) for tf in ALL_TFS],
                "started_at": self._started_at.isoformat() if self._started_at else None,
            },
            "risk": {
                **status,
                "limits": {
                    "risk_per_trade": settings.risk_per_trade,
                    "max_daily_loss": settings.max_daily_loss,
                    "max_daily_profit": settings.max_daily_profit,
                    "max_daily_trades": settings.max_daily_trades,
                    "max_consecutive_losses": settings.max_consecutive_losses,
                    "max_open_positions": settings.max_open_positions,
                },
            },
            "analyst": {
                "enabled": self.analyst.enabled,
                "model": settings.anthropic_model if self.analyst.enabled else None,
                "interval_minutes": settings.analyst_interval_minutes,
            },
            "news": {
                "loaded_at": (
                    self.news.last_update.isoformat() if self.news.last_update else None
                ),
                "high_impact_this_week": len(self.news.events),
                "error": self.news.last_error,
                "upcoming": upcoming[:8],
            },
            "symbols": symbols,
            "positions": positions,
            "equity": equity[-200:],
            "trades": self.risk.history[-25:],
            "vetoes": [
                {"symbol": symbol, "reason": reason, "count": count}
                for (symbol, reason), count in self._veto_counts.most_common(12)
            ],
            "daily_bias": {
                symbol: extractor.daily.bias_summary()
                for symbol, extractor in self.ict_extractors.items()
            },
        }

    def _write_state(self) -> None:
        """Atomically publish the snapshot for the read-only panel process."""
        path = LOG_DIR / "state.json"
        temporary = path.with_suffix(".json.tmp")
        try:
            payload = json.dumps(self.snapshot(), default=str, indent=1)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, path)  # atomic: readers never see a half file
        except Exception:
            log.exception("Could not write state.json")

    def _log_status(self) -> None:
        status = self.risk.status()
        log.info(
            "STATUS balance=%.2f day=%+.2f trades=%d win=%.0f%% open=%d autotrade=%s",
            status["balance"],
            status["daily_pnl"],
            status["trades_today"],
            status["win_rate"],
            self.broker.open_count,
            self.autotrade,
        )
        for symbol, extractor in self.ict_extractors.items():
            summary = extractor.daily.bias_summary()
            if summary.get("days"):
                log.info("BIAS %s %s", symbol, summary)

        if self._veto_counts:
            top = self._veto_counts.most_common(6)
            log.info(
                "VETOES (cumulative) %s",
                " | ".join(f"{sym} {reason} x{count}" for (sym, reason), count in top),
            )
            self.journal.event(
                "veto_summary",
                counts={f"{sym}:{reason}": count
                        for (sym, reason), count in self._veto_counts.items()},
            )


def _veto_key(reason: str) -> str:
    """Collapse a veto message to a countable category.

    'spread 0.00042 > max 0.00025' and 'spread 0.00051 > max 0.00025' are the
    same problem, so the numbers are stripped before tallying.
    """
    return re.sub(r"[-+]?\d*\.?\d+", "N", reason).strip()


def _signal_payload(signal) -> dict:
    return {
        "direction": signal.direction,
        "confidence": round(signal.confidence, 3),
        "score": round(signal.score, 1),
        "reasons": signal.reasons,
        "context": {
            key: (round(value, 6) if isinstance(value, float) else value)
            for key, value in signal.context.items()
        },
    }
