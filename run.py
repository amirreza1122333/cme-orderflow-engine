#!/usr/bin/env python
"""Entry point.

    python run.py check                 read-only connection + feed test
    python run.py selftest              offline pipeline test, no network
    python run.py calibrate             measure real ATR, recommend stop/target
    python run.py collect               ICT feature collector (own process)
    python run.py status                what the running engine is doing
    python run.py analyst-test          one real Claude call (needs an API key)
    python run.py run                   start the engine (disarmed by default)
    python run.py run --arm             start with autotrade armed (paper)
    python run.py run --arm --i-understand-live    real orders (see below)

Live orders require all four of:
    EXECUTION_MODE=live in .env, --arm, --i-understand-live, and a confirmation
    typed at the prompt. Any one missing and fills stay simulated.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DATA_DIR, LOG_DIR, load_settings  # noqa: E402


def setup_logging(verbose: bool = False) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    # Force UTF-8 on the console. On a non-English Windows the OS hands back
    # localised socket errors, and the default cp1252 console encoding raises
    # UnicodeEncodeError *inside the logging call* - so the first thing that
    # goes wrong is also the thing you cannot read.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"engine-{stamp}.log", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-9s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ----------------------------------------------------------------- check mode


def command_check(settings) -> int:
    """Audit the configuration offline, then probe the DTC port. Never trades.

    Rewritten for the DTC pivot. The cTrader version authenticated, downloaded
    symbol specifications and sampled live spreads. None of that is available
    until core/dtc_client.py exists, and the parts that mattered most - is the
    contract spec right, and can this account actually afford one contract -
    never needed a broker at all: they are arithmetic over config.py.
    """
    import socket

    log = logging.getLogger("check")
    problems = 0

    log.info("Contract specifications (from config.py, not downloaded):")
    for name, config in settings.symbols.items():
        if not config.enabled:
            continue
        spec = config.to_spec()
        log.info("  %s", spec.describe())
        log.info(
            "    stop %g (%.0f ticks) -> %.2f risk per contract | target %g "
            "-> %.2f per contract | R:R %.2f",
            config.stop_distance,
            spec.ticks(config.stop_distance),
            config.risk_per_contract,
            config.target_distance,
            config.target_distance * config.multiplier,
            (config.target_distance / config.stop_distance
             if config.stop_distance else 0.0),
        )

        # The stop has to sit on the tick grid or the exchange will reprice it,
        # and then the risk we sized against is not the risk we took.
        snapped = spec.snap_to_tick(config.stop_distance)
        if abs(snapped - config.stop_distance) > 1e-9:
            log.warning(
                "    ^ stop %g is not a whole number of %g ticks - it would "
                "become %g. Fix stop_distance in config.py.",
                config.stop_distance, spec.tick_size, snapped,
            )
            problems += 1

        if not config.tradable:
            log.info("    data-only: enabled for collection, not for trading")
            continue

        contracts = spec.volume_for_risk(settings.risk_per_trade, config.stop_distance)
        if contracts <= 0:
            log.error(
                "    ^ ONE contract risks %.2f, over the %.2f per-trade cap. "
                "There is nothing smaller than one contract, so the engine "
                "will refuse every %s trade. Raise RISK_PER_TRADE, tighten the "
                "stop, or accept that this is a data-collection session.",
                config.risk_per_contract, settings.risk_per_trade, name,
            )
            problems += 1
        else:
            log.info(
                "    sizes to %d contract(s) at a %.2f cap, risking %.2f",
                contracts, settings.risk_per_trade,
                spec.risk_for_volume(contracts, config.stop_distance),
            )

    log.info("Risk envelope against a %.2f account:", settings.paper_start_balance)
    balance = settings.paper_start_balance
    if balance > 0:
        risk_pct = settings.risk_per_trade / balance * 100
        daily_pct = settings.max_daily_loss / balance * 100
        log.info(
            "  %.2f per trade (%.1f%%) | %.2f daily stop (%.1f%%) | "
            "%d trades/day | %d loss(es) then cooldown",
            settings.risk_per_trade, risk_pct,
            settings.max_daily_loss, daily_pct,
            settings.max_daily_trades, settings.max_consecutive_losses,
        )
        if risk_pct > 2.5 or daily_pct > 6:
            log.warning(
                "  ^ sustainable sizing is 1-2% per trade. These numbers are a "
                "PAPER and data-collection envelope, not a live one. Do not set "
                "EXECUTION_MODE=live on this balance."
            )
    for warning in settings.underfunded_warnings():
        log.warning("  UNDERFUNDED  %s", warning)

    # A plain TCP connect, no DTC handshake - enough to tell "Sierra Chart is
    # listening" from "nothing is there", which is the usual first question.
    log.info("Probing DTC server at %s ...", settings.dtc_address)
    try:
        with socket.create_connection(
            (settings.dtc_host, settings.dtc_port), timeout=5
        ):
            log.info("  port is open - Sierra Chart's DTC server is listening")
    except OSError as error:
        log.warning(
            "  cannot reach %s: %s. Start Sierra Chart and enable "
            "File > Global Settings > Data/Trade Service Settings > DTC "
            "Protocol Server.", settings.dtc_address, error,
        )
        problems += 1

    # TODO: Replace with DTC Client - once it exists, extend this command to
    # log on, pull SECURITY_DEFINITION for each contract and cross-check it
    # against the pinned numbers above, then sample live spreads for 20s the
    # way the cTrader version did. A spec mismatch is exactly the kind of
    # error that only shows up as a position sized 10x wrong.

    if problems:
        log.error("Check finished with %d problem(s).", problems)
        return 1
    log.info("Check passed.")
    return 0


# -------------------------------------------------------------- selftest mode


def command_selftest(settings) -> int:
    """Drive the full decision pipeline with synthetic data, offline.

    Proves the wiring (indicators -> strategy -> risk sizing -> paper fills ->
    journal) works without touching the broker. It says nothing about whether
    the strategy is profitable.
    """
    import math
    import random

    from core.broker import PaperBroker
    from core.marketdata import ENTRY_TF, MarketData
    from core.risk import RiskManager
    from core.strategy import Strategy
    from core.symbols import SymbolSpec

    log = logging.getLogger("selftest")
    random.seed(7)

    # Built from config.py so the selftest exercises the real contract
    # specification. Getting MGC's multiplier wrong here would make the test
    # pass on numbers the live engine never uses.
    config = settings.symbols[settings.default_symbol]
    spec = config.to_spec(symbol_id=1)
    market = MarketData()
    state = market.register(spec)
    strategy = Strategy(config)
    risk = RiskManager(settings, starting_balance=settings.paper_start_balance)
    broker = PaperBroker(commission_per_contract=settings.commission_per_contract)

    closed: list[dict] = []

    def on_closed(trade: dict) -> None:
        closed.append(trade)
        # Feed the result straight back into the risk manager so the daily
        # limits and cooldowns are exercised too, not just the fills.
        risk.record_close(
            trade["symbol"],
            trade["pnl"],
            now=datetime.fromisoformat(trade["closed_at"]),
            exit_reason=trade["exit_reason"],
        )

    broker.on_closed = on_closed

    # Sizing check first: it is the single most dangerous number in the system.
    volume = spec.volume_for_risk(settings.risk_per_trade, config.stop_distance)
    sized_risk = spec.risk_for_volume(volume, config.stop_distance)
    log.info(
        "Sizing: %s, risk cap %.2f over a %g stop -> %d contract(s), "
        "actual risk %.2f",
        spec.describe(),
        settings.risk_per_trade,
        config.stop_distance,
        volume,
        sized_risk,
    )
    assert volume == int(volume), "contract count must be a whole number"
    assert volume > 0, (
        f"sizing produced no tradable position: one {spec.name} contract "
        f"risks {config.risk_per_contract:.2f} at a {config.stop_distance:g} "
        f"stop, over the {settings.risk_per_trade:.2f} cap"
    )
    assert sized_risk <= settings.risk_per_trade + 1e-9, "sizing exceeded the risk cap"

    # A trending random walk in gold, one tick per second.
    price = 2400.0
    start = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
    trend = 0.0
    opened = 0
    # `warmed(26)` requires 26 bars on EVERY timeframe, and the slowest is the
    # 1-hour - so the walk has to cover at least 26 hours before the strategy
    # evaluates anything at all. The old bound of 6000 one-second ticks was
    # 1h40m: the selftest passed without ever reaching a single decision, which
    # is a green light that proves nothing. 100_800 is 28 hours and actually
    # exercises sizing -> fill -> stop/target -> journal.
    for step in range(100_800):
        trend = 0.9 * trend + random.gauss(0, 0.02)
        drift = 0.35 * math.sin(step / 900.0)
        price = max(1.0, price + trend + drift + random.gauss(0, 0.05))
        # MGC quotes one tick wide most of the time and occasionally two.
        # This was 0.30 for spot XAUUSD, which is now equal to MGC's
        # max_spread and would have every signal vetoed on spread alone -
        # the test would pass while exercising none of the fill path.
        spread = spec.tick_size + max(0.0, random.gauss(0, 0.03))
        timestamp = datetime.fromtimestamp(start.timestamp() + step, tz=timezone.utc)
        state.add_tick(price - spread / 2, price + spread / 2, timestamp)

        broker.on_tick(spec, state)

        if step % 5 or not state.warmed(26):
            continue
        signal = strategy.evaluate(state)
        if not signal.tradable:
            continue
        decision = risk.can_trade(
            spec, config.stop_distance, broker.open_count, now=timestamp
        )
        if not decision.allowed:
            continue
        position = broker.open(
            spec,
            signal.direction,
            decision.volume,
            config.stop_distance,
            config.target_distance,
            state,
            meta={"confidence": signal.confidence, "score": signal.score},
        )
        if position:
            opened += 1
            risk.record_open(spec.name, timestamp)

    wins = sum(1 for t in closed if t["pnl"] > 0)
    log.info(
        "Replay done: %d bars, %d positions opened, %d closed (%d wins)",
        state.bar_count(ENTRY_TF),
        opened,
        len(closed),
        wins,
    )
    for trade in closed[:5]:
        log.info(
            "  %s %s entry %.2f exit %.2f (%s) pnl %+.2f",
            trade["symbol"],
            trade["side"],
            trade["entry"],
            trade["exit"],
            trade["exit_reason"],
            trade["pnl"],
        )
    log.info("Risk status: %s", risk.status())

    # Every simulated loss must respect the configured stop, allowing for the
    # spread paid on entry.
    worst = min((t["pnl"] for t in closed), default=0.0)
    limit = -(settings.risk_per_trade + sized_risk * 0.5) - 0.01
    assert worst >= limit, f"a simulated loss ({worst:.2f}) blew past the stop"

    if opened == 0:
        log.warning(
            "No trades were triggered on synthetic data. The pipeline is wired "
            "correctly but the filters are strict - expect few signals."
        )
    log.info("Selftest passed.")
    return 0


# -------------------------------------------------------------- status mode


def command_status(settings=None) -> int:
    """Print what the running engine is doing, from its published snapshot.

    Reads logs/state.json only - it does not connect to the broker, so it is
    safe to run at any time and cannot disturb the engine.
    """
    import json

    log = logging.getLogger("status")
    path = LOG_DIR / "state.json"
    if not path.exists():
        log.error("No %s - is the engine running?", path)
        return 1

    age = time.time() - path.stat().st_mtime
    state = json.loads(path.read_text(encoding="utf-8"))
    engine = state.get("engine", {})
    risk = state.get("risk", {})

    print()
    print(f"  snapshot age  : {age:.0f}s" + ("  STALE" if age > 60 else ""))
    print(f"  mode          : {engine.get('mode')} | strategy "
          f"{engine.get('strategy', '?')} | "
          f"autotrade={engine.get('autotrade')}")
    print(f"  connected     : {engine.get('connected')}")
    print(f"  balance       : {risk.get('balance')}  "
          f"(day {risk.get('daily_pnl'):+}, {risk.get('trades_today')} trades)")

    positions = state.get("positions", [])
    print()
    print(f"  OPEN POSITIONS ({len(positions)})")
    if not positions:
        print("      none")
    else:
        now = datetime.now(timezone.utc)
        header = (f"      {'symbol':9}{'side':6}{'ctr':>5}{'entry':>11}"
                  f"{'stop':>11}{'target':>11}{'P&L':>9}{'open for':>11}")
        print(header)
        print("      " + "-" * (len(header) - 6))
        for position in positions:
            opened = position.get("opened_at")
            held = ""
            if opened:
                try:
                    delta = now - datetime.fromisoformat(opened)
                    minutes = int(delta.total_seconds() // 60)
                    held = f"{minutes // 60}h{minutes % 60:02d}m"
                except ValueError:
                    held = "?"
            pnl = position.get("unrealised")
            pnl_text = f"{pnl:+.2f}" if isinstance(pnl, (int, float)) else "?"
            print(f"      {position.get('symbol',''):9}{position.get('side',''):6}"
                  f"{position.get('contracts', 0):>5d}{position.get('entry', 0):>11.2f}"
                  f"{position.get('stop_loss', 0):>11.5f}"
                  f"{position.get('take_profit', 0):>11.5f}"
                  f"{pnl_text:>9}{held:>11}")

    trades = state.get("trades", [])
    if trades:
        print()
        print(f"  LAST {min(len(trades), 5)} CLOSED")
        for row in trades[-5:]:
            print(f"      {row.get('time','')[:16]}  {row.get('symbol',''):9}"
                  f"{row.get('pnl', 0):+8.2f}  balance {row.get('balance', 0):.2f}")

    bias = state.get("daily_bias", {})
    if bias:
        print()
        print("  DAILY BIAS (strict rule: higher H+L, or lower H+L)")
        for symbol, summary in bias.items():
            if not summary.get("days"):
                print(f"      {symbol:9} no completed days recorded yet")
                continue
            print(f"      {symbol:9} {summary['directional']}/{summary['days']} days "
                  f"directional ({summary['directional_pct']}%)")
            reasons = summary.get("neutral_reasons") or {}
            if reasons:
                print(f"      {'':9} neutral because: "
                      + ", ".join(f"{k} x{v}" for k, v in sorted(reasons.items())))
            print(f"      {'':9} the looser body rule would have traded "
                  f"{summary.get('would_trade_under_body_rule', 0)} of those "
                  f"{summary.get('neutral', 0)} neutral days")

    vetoes = state.get("vetoes", [])
    if vetoes:
        print()
        print("  TOP VETOES (cumulative since start)")
        for entry in vetoes[:10]:
            print(f"      {entry.get('count', 0):>7}  {entry.get('symbol','')} "
                  f"{entry.get('reason','')}")
    print()
    return 0


# ------------------------------------------------------------- collect mode


def command_collect(
    settings, feed: str, source: Path, out: Path, research: bool = False
) -> int:
    """Run the ICT data collector as its own process.

    Separate from the engine on purpose: neither can crash the other. It has
    its own feed session, subscribes to market data, and writes one CSV row per
    completed 5m bar. There is no order-sending code path in this process.

    On the dry feed it replays recorded tick CSVs from `source` through the
    real ICTFeatureExtractor and writes fresh feature rows to `out`. That is
    the offline verification path for the 36-column contract, and it is what
    keeps `ict/prepare.py` and `ict/train.py` runnable with no broker.
    """
    from ict.service import CollectorService

    log = logging.getLogger("collect")

    # The collector writes ict_{symbol}_{date}.csv - the exact glob the dry
    # replay reads its input with. Sharing one directory therefore overwrites
    # the source ticks mid-replay, and the run then re-reads its own feature
    # rows as if they were quotes: every row closes a bar, bars_seen runs into
    # the hundreds of thousands, and the day it clobbered is gone.
    #
    # The --out help text has warned about this since the flag was added. A
    # warning in help text is not a guard; this is.
    if feed == "dry" and out.resolve() == source.resolve():
        log.error("--out and --source are the same directory: %s", out.resolve())
        log.error(
            "The collector writes ict_<SYMBOL>_<DATE>.csv, which is what the "
            "dry replay globs for input. Sharing a directory makes the run "
            "overwrite the ticks it is replaying and then read its own feature "
            "rows back as quotes."
        )
        log.error("Re-run with a separate output, e.g.  --source data --out features")
        return 2

    # The collector appends. On a live feed that is what you want - a restart
    # continues the day's file. On a dry replay it can only ever duplicate:
    # the same input produces the same rows, so a second run into the same
    # directory writes every row twice. The count doubles (looks like more
    # data) while every mean and rate is unchanged (looks like a stable
    # result), and the copies are indistinguishable from real samples once
    # they reach training.
    if feed == "dry":
        existing = sorted(out.glob("ict_*.csv"))
        if existing:
            log.error(
                "%s already holds %d ict_*.csv file(s), starting with %s",
                out, len(existing), existing[0].name,
            )
            log.error(
                "The collector appends, so replaying into a directory that "
                "already has output duplicates every row. A dry replay is "
                "deterministic - there is no version of this that adds "
                "information."
            )
            log.error("Clear that directory, or pass a different --out.")
            return 2
    service = CollectorService(
        settings, directory=out, feed=feed, source=source, research=research
    )
    if not service.start():
        log.error("Collector did not start.")
        return 1

    try:
        if feed == "dry":
            replayed = service.replay()
            if not replayed:
                log.error(
                    "Nothing to replay: no ict_<SYMBOL>_*.csv in %s. Point "
                    "--source at a directory of recorded ticks (any CSV with "
                    "timestamp, bid and ask columns will do).", source,
                )
                return 1
            for name in service._symbols:
                collector = service.collectors.get(name)
                if collector is not None:
                    log.info("RESULT %s", collector.status())
        elif feed == "bitstamp":
            # The client owns a reader thread; this loop only has to stay alive
            # and report. Ctrl-C or a dropped connection ends it, and the
            # `finally` below flushes whatever each collector has buffered.
            log.info("Collecting from Bitstamp. Ctrl-C to stop and flush.")
            last_report = 0.0
            while service.client is not None and service.client.connected:
                time.sleep(0.5)
                now = time.time()
                if now - last_report >= 30:
                    last_report = now
                    for name in service._symbols:
                        collector = service.collectors.get(name)
                        if collector is not None:
                            log.info("%s", collector.status())
            log.warning("Feed disconnected; stopping.")
        else:
            # TODO: Replace with DTC Client data feed - block here while the
            # client's reader thread delivers ticks, e.g.
            #     while service.client.connected: time.sleep(0.5)
            log.error("Live collection loop not implemented - see core/dtc_client.py")
            return 1
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        service.stop()

    if service.failed:
        log.error("Exiting non-zero so the supervisor restarts us.")
        return 1
    return 0


# ------------------------------------------------------------ calibrate mode


def command_calibrate(settings) -> int:
    """Measure real ATR per timeframe and recommend stop/target/sizing.

    OFFLINE pending core/dtc_client.py - it needs historical bars, which only
    the broker connection can supply.

    TODO: Replace with DTC Client - reissue the per-timeframe bar requests as
    HISTORICAL_PRICE_DATA_REQUEST against Sierra Chart, then keep the rest of
    the original logic unchanged. Two things must change in the arithmetic
    when it is ported:

      * sizing is now `floor(budget / (stop x multiplier))` whole contracts,
        so the recommendation is "this timeframe needs a $X budget to trade at
        all", not "this timeframe sizes to 0.03 lots";
      * stops must be snapped to the tick grid with `spec.snap_to_tick`.

    The shipped MGC stop of 4.30 is 1.5x the LAST cTrader-measured gold 5m ATR
    (2.87 on 2026-08-02), carried over because it is the same underlying. It is
    an estimate until this command runs against Sierra Chart's own MGC bars -
    the futures session has different open and close volatility than spot did.
    """
    log = logging.getLogger("calibrate")
    log.error(
        "Calibration unavailable: it needs historical bars and "
        "core/dtc_client.py is not implemented yet."
    )
    log.info(
        "Current MGC parameters carried over from spot gold: stop %g "
        "(%.2f per contract), target %g. Re-measure before trusting them.",
        settings.symbols["MGC"].stop_distance,
        settings.symbols["MGC"].risk_per_contract,
        settings.symbols["MGC"].target_distance,
    )
    return 1


# --------------------------------------------------------- analyst test mode


def command_analyst_test(settings) -> int:
    """One real Claude call against a representative snapshot.

    Verifies the model ID, the structured-output shape and the JSON parse, and
    reports latency and token cost so you can decide whether the layer is worth
    running before it is wired into live decisions.
    """
    import time

    from core.analyst import Analyst

    log = logging.getLogger("analyst")

    if not settings.anthropic_api_key:
        log.error("No ANTHROPIC_API_KEY in .env - nothing to test.")
        log.info("The engine runs fine without it; the analyst stays neutral.")
        return 2

    analyst = Analyst(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        interval_minutes=settings.analyst_interval_minutes,
    )
    if not analyst.enabled:
        log.error("Analyst failed to initialise: %s", analyst.last_error)
        return 1

    snapshot = {
        "symbol": "XAUUSD",
        "utc_time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price": 4041.20, "spread": 0.32, "median_spread": 0.30,
        "atr_1m": 0.85, "rsi_14": 58.2,
        "ema_fast": 4040.80, "ema_slow": 4039.10,
        "order_book_imbalance": 0.18, "has_level2": True,
        "recent_closes": [4039.1, 4039.6, 4040.2, 4040.0, 4040.9, 4041.2],
        "technical_signal": {
            "direction": "BUY", "confidence": 0.51,
            "reasons": ["EMA9/21 up (sep 0.62)", "pullback into trend"],
            "vetoes": [],
        },
        "upcoming_high_impact_news": [
            {"in_minutes": 47, "title": "Core CPI m/m", "currency": "USD",
             "forecast": "0.3%", "previous": "0.2%"},
        ],
        "recent_trades": [{"pnl": -5.02, "symbol": "XAUUSD"}],
        "daily_pnl": -5.02, "trades_today": 1,
    }

    log.info("Calling %s ...", settings.anthropic_model)
    started = time.perf_counter()
    try:
        verdict = analyst.analyse(snapshot)
    except Exception as error:
        log.error("Call failed: %s: %s", type(error).__name__, error)
        return 1
    elapsed = time.perf_counter() - started

    log.info("Latency      : %.1f s", elapsed)
    log.info("Action       : %s", verdict.action)
    log.info("Bias         : %s (confidence %.2f)", verdict.bias, verdict.confidence)
    log.info("Reasoning    : %s", verdict.reasoning)
    log.info("Blocks a BUY : %s", verdict.blocks("BUY") or "no")
    log.info("Blocks a SELL: %s", verdict.blocks("SELL") or "no")

    # Claude Opus 5: $5 per 1M input tokens, $25 per 1M output.
    usage = analyst.last_usage
    if usage:
        cost = (usage["input_tokens"] * 5.0 + usage["output_tokens"] * 25.0) / 1e6
        log.info(
            "Tokens       : %d in / %d out  ->  $%.4f per call",
            usage["input_tokens"], usage["output_tokens"], cost,
        )
        log.info(
            "At ~30 calls/day that is about $%.2f/day, $%.2f/month (22 days).",
            cost * 30, cost * 30 * 22,
        )
        if cost * 30 > 1.0:
            log.warning(
                "That is a meaningful share of a small account's target. Raise "
                "analyst_interval_minutes in config.py to call it less often.",
            )

    if elapsed > 20:
        log.warning(
            "That took %.0f s. The analyst runs off the hot path so it will not "
            "delay entries, but a verdict this slow is often one tick stale.",
            elapsed,
        )
    log.info("Analyst test passed.")
    return 0


# ------------------------------------------------------------------- run mode


def command_run(settings, arm: bool, understand_live: bool, feed: str) -> int:
    """Start the engine.

    No reactor any more - the Twisted event loop left with the cTrader SDK.
    On the dry feed the engine is driven by `engine.replay()`, which pushes the
    collector's recorded ticks through the same callback the DTC client will
    fire. On the dtc feed it refuses until core/dtc_client.py exists.
    """
    from core.broker import LiveBroker
    from core.engine import TradingEngine

    log = logging.getLogger("run")
    engine = TradingEngine(settings, feed=feed)

    if settings.live_execution:
        # Kept as the outer gate even though the order path is offline: the day
        # dtc_client.py starts sending orders, this must already be here.
        if not (arm and understand_live):
            log.warning(
                "EXECUTION_MODE=live but the required flags are missing. "
                "Running DISARMED - no orders will be sent."
            )
        else:
            print()
            print("!" * 66)
            print("  LIVE EXECUTION on a REAL futures account.")
            print(f"  Server  : {settings.dtc_address} (Sierra Chart DTC)")
            print(f"  Account : {settings.dtc_trade_account or '<not set>'}")
            print(f"  Risk    : {settings.risk_per_trade:.2f} per trade, "
                  f"{settings.max_daily_loss:.2f} max daily loss")
            for warning in settings.underfunded_warnings():
                print(f"  WARNING : {warning}")
            print("  NOTE    : the DTC order path is NOT implemented. Every")
            print("            order will be refused by core/broker.py.")
            print("!" * 66)
            answer = input("Type LIVE to confirm, anything else to stay simulated: ")
            if answer.strip() == "LIVE":
                if isinstance(engine.broker, LiveBroker):
                    engine.broker.armed = True
                engine.autotrade = True
                log.warning("LIVE TRADING ARMED")
            else:
                log.info("Not confirmed - staying disarmed.")
    elif arm:
        engine.autotrade = True
        log.info("Paper autotrade armed.")

    if not engine.start():
        log.error("Engine did not start.")
        return 1

    try:
        if feed == "dry":
            replayed = engine.replay()
            if not replayed:
                log.warning(
                    "Nothing to replay: no collector CSVs in %s. The engine is "
                    "wired and started, but with no data it cannot evaluate "
                    "anything. Run `python run.py collect` to produce some, or "
                    "drop recorded ict_*.csv files there.", DATA_DIR,
                )
        else:
            # TODO: Replace with DTC Client data feed - block here while the
            # client's reader thread delivers ticks, e.g.
            #     while engine.client.connected: time.sleep(0.5)
            log.error("Live feed loop not implemented - see core/dtc_client.py")
            return 1
    except KeyboardInterrupt:
        log.info("Interrupted.")
    finally:
        engine.stop()

    if engine.failed:
        log.error("Exiting non-zero so the supervisor restarts us.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="CME futures trading engine (Sierra Chart / DTC)"
    )
    parser.add_argument(
        "command",
        choices=["check", "selftest", "calibrate", "analyst-test",
                 "collect", "status", "run"],
        help="what to do"
    )
    parser.add_argument(
        "--feed",
        choices=["dry", "dtc", "bitstamp"],
        default="dry",
        help="dry = replay recorded CSVs offline (default); "
             "dtc = live Sierra Chart feed (not implemented yet), "
             "bitstamp = free public crypto depth, for exercising the "
             "pipeline without a Sierra Chart licence",
    )
    parser.add_argument(
        "--research",
        action="store_true",
        help="use the research instrument set from config.py (XAUUSD, BTCUSD) "
             "instead of the production CME contracts",
    )
    parser.add_argument(
        "--asian-preset",
        choices=["spec", "tokyo"],
        default=None,
        help="Asian accumulation window: 'spec' is 20:00-07:00 UTC as the "
             "specification literally reads, 'tokyo' is 00:00-07:00 UTC, the "
             "real Tokyo session. Both end at London open. This changes every "
             "asian_* feature, so never mix presets within one dataset. "
             "Omit to use whatever ict/sessions.py defaults to.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DATA_DIR,
        help="collect --feed dry: directory of recorded tick CSVs to replay "
             f"(default {DATA_DIR.name}/)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DATA_DIR,
        help=f"collect: where feature CSVs are written (default {DATA_DIR.name}/). "
             "MUST differ from --source on the dry feed: output and input share "
             "the ict_<SYMBOL>_<DATE>.csv naming, so one directory means the "
             "replay overwrites the ticks it is reading. Enforced, not advisory.",
    )
    parser.add_argument("--arm", action="store_true", help="arm autotrade at startup")
    parser.add_argument(
        "--i-understand-live",
        action="store_true",
        help="required (with --arm) before real orders can be sent",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # Imported lazily: run.py must stay importable without the private ict
    # package, and `status` in particular has to work on a machine that only
    # has the logs.
    if args.asian_preset:
        from ict.sessions import configure_asian

        configure_asian(args.asian_preset)

    # `status` only reads logs/state.json, so it must work without any
    # connection - on a machine that has the logs but no Sierra Chart, say.
    if args.command == "status":
        return command_status(None)

    settings = load_settings()

    log = logging.getLogger("main")
    log.info("Configuration:")
    for line in settings.summary().splitlines():
        log.info(line)

    if args.command == "check":
        return command_check(settings)
    if args.command == "selftest":
        return command_selftest(settings)
    if args.command == "collect":
        return command_collect(
            settings, args.feed, args.source, args.out, args.research
        )
    if args.command == "calibrate":
        return command_calibrate(settings)
    if args.command == "analyst-test":
        return command_analyst_test(settings)
    return command_run(settings, args.arm, args.i_understand_live, args.feed)


if __name__ == "__main__":
    raise SystemExit(main())
