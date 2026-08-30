"""Configuration for the trading engine.

PLATFORM: CME futures via Sierra Chart's DTC server (migrated 2026-08-21 from
the cTrader Open API, which is dead for this project). The primary instrument
is MGC - CME Micro Gold - replacing spot XAUUSD.

Everything comes from .env in this directory. Secrets are never printed:
`Settings.summary()` masks them.

Two independent switches gate real money:

    DTC_HOST / DTC_PORT  -> which Sierra Chart session we talk to
    EXECUTION_MODE       -> paper | live: whether orders leave this machine

Orders can only reach the market when EXECUTION_MODE=live *and* the process is
started with the explicit --i-understand-live flag *and* autotrade is armed. In
every other configuration fills are simulated locally.

Contract specifications live here rather than being downloaded. DTC has a
SECURITY_DEFINITION exchange, but the numbers below are published by the CME,
change essentially never, and are the single most dangerous values in the
system - so they are pinned in source where a diff shows them changing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
# The ICT collector's feature CSVs and their manifests. One place, because
# three separate things have to agree on it: ict/service.py writes here,
# ict/prepare.py reads here, and the engine's dry replay reads here too.
DATA_DIR = BASE_DIR / "data"


def load_env(path: Path = ENV_PATH) -> dict[str, str]:
    """Minimal .env reader. Real environment variables win over the file."""
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.split(" #")[0].strip().strip('"').strip("'")
            values[key] = value
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    for key, value in os.environ.items():
        values.setdefault(key, value)
    return values


def _mask(secret: str) -> str:
    if not secret:
        return "<missing>"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}...{secret[-4:]} ({len(secret)} chars)"


@dataclass
class SymbolConfig:
    """Contract specification plus strategy parameters for one instrument.

    The contract block is exchange fact; the strategy block is our choice.

    Distances are in *price* units of the contract: on MGC 4.30 means $4.30 of
    gold, which at a $10 multiplier is $43 of P&L per contract.
    """

    name: str

    # ---- contract specification (exchange fact - do not tune) --------------
    # contract_size : underlying units in one contract, descriptive only
    # multiplier    : account currency per 1.00 price move, per contract.
    #                 THIS is what every risk and P&L calculation uses.
    # tick_size     : minimum price increment the exchange accepts
    contract_size: float = 1.0
    multiplier: float = 1.0
    tick_size: float = 0.01
    digits: int = 2
    # A futures order is a whole number of contracts. There is no 0.01 lot and
    # nothing below one contract to shrink into, so this floor is hard.
    min_contracts: int = 1
    max_contracts: int = 0           # 0 = no local cap
    step_contracts: int = 1
    exchange: str = "CME"
    base_asset: str = ""
    quote_asset: str = "USD"

    # ---- strategy parameters (ours to tune) --------------------------------
    # enabled  -> subscribe to the feed at all (data, features, veto stats)
    # tradable -> allowed to open positions. Keep enabled=True/tradable=False
    #             for a contract you want to KEEP STUDYING but not trade: the
    #             collector, the 36-feature extractor and the veto counters all
    #             keep running, so the ML training set keeps growing.
    enabled: bool = True
    tradable: bool = True
    stop_distance: float = 0.0
    target_distance: float = 0.0
    max_spread: float = 0.0          # skip the trade when spread exceeds this
    # 0..1. The strategy only proposes a direction above |score| = 30, so a
    # value below 0.30 has no effect; raising it makes the bot more selective.
    min_confidence: float = 0.35
    min_seconds_between_trades: int = 180
    depth_levels: int = 5            # order-book levels used for imbalance

    # ---- derived -----------------------------------------------------------

    @property
    def tick_value(self) -> float:
        """Account-currency value of one tick, per contract. MGC: $1.00."""
        return self.tick_size * self.multiplier

    @property
    def risk_per_contract(self) -> float:
        """What one contract loses if the stop is hit. MGC @ 4.30 -> $43.00."""
        return self.stop_distance * self.multiplier

    def to_spec(self, symbol_id: int = 0):
        """Build the runtime SymbolSpec the engine and risk manager use."""
        from core.symbols import SymbolSpec

        return SymbolSpec(
            symbol_id=symbol_id,
            name=self.name,
            digits=self.digits,
            tick_size=self.tick_size,
            contract_size=self.contract_size,
            multiplier=self.multiplier,
            min_contracts=self.min_contracts,
            max_contracts=self.max_contracts,
            step_contracts=self.step_contracts,
            base_asset=self.base_asset,
            quote_asset=self.quote_asset,
            exchange=self.exchange,
        )


@dataclass
class Settings:
    # ---- DTC / Sierra Chart connection -------------------------------------
    dtc_host: str
    dtc_port: int
    dtc_username: str
    dtc_password: str
    dtc_trade_account: str
    execution_mode: str              # "paper" | "live"
    autotrade_default: bool
    bar_seconds: int
    paper_start_balance: float
    default_symbol: str
    anthropic_api_key: str
    anthropic_model: str
    fred_key: str

    # Round-turn commission per CONTRACT, in account currency. Take this from
    # your broker's commission schedule - leaving it at 0 makes paper results
    # look better than live ones ever will. Typical MGC round turn including
    # exchange and NFA fees runs about $1.00-$1.50.
    commission_per_contract: float = 0.0

    # Risk envelope (account currency, assumed = deposit currency).
    #
    # Sized for ONE MGC contract, which is the smallest position that exists.
    # See `underfunded_warnings()` - on a $500 account these numbers are a
    # paper-trading and data-collection envelope, not a live one.
    risk_per_trade: float = 50.0
    max_daily_loss: float = 100.0
    max_daily_profit: float = 100.0
    max_daily_trades: int = 6
    max_consecutive_losses: int = 2
    max_open_positions: int = 1
    cooldown_after_losses_minutes: int = 30

    news_blackout_before_min: int = 30
    news_blackout_after_min: int = 15
    news_refresh_minutes: int = 30

    analyst_interval_minutes: int = 10

    # "ema" = the original EMA/RSI multi-timeframe strategy.
    # "ict" = the ICT structure rules in ict/signal.py.
    strategy_mode: str = "ict"
    # With this True (the specification's rule: "if predictor says WAIT -> no
    # signal") the engine cannot trade until a trained model exists. Set it
    # False to run the eight ICT conditions on their own in the meantime.
    ict_require_model: bool = True

    symbols: dict[str, SymbolConfig] = field(default_factory=dict)
    # Instruments for the free research feed (--feed bitstamp). Kept apart from
    # `symbols` on purpose: this project is a CME futures engine, and the crypto
    # venue exists so the collector, the feature extractor and the panel can be
    # exercised end-to-end without a Sierra Chart licence. Nothing here should
    # ever be reachable from a production run.
    research_symbols: dict[str, SymbolConfig] = field(default_factory=dict)

    @property
    def live_execution(self) -> bool:
        return self.execution_mode == "live"

    @property
    def dtc_address(self) -> str:
        return f"{self.dtc_host}:{self.dtc_port}"

    def underfunded_warnings(self) -> list[str]:
        """Contracts this account cannot actually afford to trade.

        A futures position floor of one contract means the account either
        clears the bar or does not trade at all - the engine cannot size down
        to fit. Reported at startup so an empty trade log has a stated reason
        rather than looking like a broken strategy.
        """
        problems: list[str] = []
        for config in self.symbols.values():
            if not (config.enabled and config.tradable):
                continue
            per_contract = config.risk_per_contract
            if per_contract <= 0:
                continue
            if per_contract > self.risk_per_trade:
                problems.append(
                    f"{config.name}: one contract at a {config.stop_distance:g} "
                    f"stop risks {per_contract:.2f}, over the "
                    f"{self.risk_per_trade:.2f} per-trade cap - every trade "
                    f"will be refused"
                )
            elif per_contract > self.paper_start_balance * 0.05:
                problems.append(
                    f"{config.name}: one contract risks {per_contract:.2f}, "
                    f"which is {per_contract / self.paper_start_balance * 100:.1f}% "
                    f"of a {self.paper_start_balance:.2f} account - paper and "
                    f"data collection only, not a live risk envelope"
                )
        return problems

    def summary(self) -> str:
        lines = [
            f"  platform      : CME futures via Sierra Chart (DTC)",
            f"  dtc server    : {self.dtc_address}",
            f"  trade account : {self.dtc_trade_account or '<not set>'}",
            f"  dtc password  : {_mask(self.dtc_password)}",
            f"  execution     : {self.execution_mode.upper()}",
            f"  claude        : {'on (' + self.anthropic_model + ')' if self.anthropic_api_key else 'off (no ANTHROPIC_API_KEY)'}",
            f"  strategy      : {self.strategy_mode.upper()}"
            + (" (model required)" if self.strategy_mode == "ict" and self.ict_require_model else ""),
            f"  contracts     : {', '.join(s for s, c in self.symbols.items() if c.enabled) or 'none enabled'}",
        ]
        for name, config in self.symbols.items():
            if config.enabled:
                lines.append(
                    f"    {name:6}: 1 contract = {config.contract_size:g} "
                    f"{config.base_asset or 'units'}, {config.multiplier:g} "
                    f"{config.quote_asset}/1.00 move, tick {config.tick_size:g} "
                    f"= {config.tick_value:g} {config.quote_asset}"
                )
        return "\n".join(lines)


def _default_symbols() -> dict[str, SymbolConfig]:
    """Contract specs and starting parameters, on a 5-minute entry timeframe.

    MGC replaces XAUUSD. It is the same underlying - gold - so the measured
    volatility carries over directly; what changes completely is how a position
    is sized, because the smallest MGC position is 100x the risk of the
    smallest XAUUSD position was.

        XAUUSD  0.01 lot = 1 oz   -> $0.01 per $1.00 move   -> $4.31 per stop
        MGC     1 contract = 10oz -> $10.00 per $1.00 move  -> $43.00 per stop

    Stops are 1.5x the measured 5-minute ATR, targets 1.5x the stop, snapped up
    to the 0.10 tick grid. ATR carried over from the last cTrader calibration
    on 2026-08-02 (XAUUSD 5m 2.87, 15m 6.12, 1h 14.43); re-measure against
    Sierra Chart's own MGC bars once `run.py calibrate` is ported to DTC, since
    the futures session has different open/close volatility than spot did.
    """
    # cTrader-era instruments (XAUUSD, EURUSD, US30, US500) were dropped in the
    # 2026-08-21 pivot; none of them exist on CME. Two findings from that era
    # are worth carrying forward because they are about market structure, not
    # about the broker:
    #
    #   * The ICT stack needs a DIRECTIONAL DAILY BIAS to produce any setup at
    #     all. Measured over 8 sessions, gold was directional 6/8 days (75%)
    #     and EURUSD 2/8 (25%) - a strategy whose precondition is absent three
    #     days in four is inapplicable to that instrument, not mispriced. Gold
    #     was kept for exactly this reason, and MGC is the same underlying.
    #   * Historical XAUUSD collector CSVs remain valid ML training data. The
    #     36-feature vector in ict/features.py is dimensionless (structure,
    #     ratios, ATR multiples), so a model trained on spot gold transfers to
    #     the futures contract. Do not delete logs/ict_XAUUSD_*.csv.

    return {
        # ------------------------------------------------------ CME Micro Gold
        "MGC": SymbolConfig(
            name="MGC",
            # Exchange fact:
            contract_size=10.0,      # 10 troy ounces
            multiplier=10.0,         # $1.00 price move  ->  $10.00 P&L
            tick_size=0.10,          # minimum tick, worth $1.00 per contract
            digits=1,
            min_contracts=1,         # whole contracts only, floor of 1
            step_contracts=1,
            exchange="CME",
            base_asset="XAU",
            quote_asset="USD",
            # Our choice:
            enabled=True,
            tradable=True,
            stop_distance=4.30,      # 1.5x 5m ATR 2.87, snapped to tick = $43
            target_distance=6.50,    # 1.5x stop = $65
            max_spread=0.30,         # 3 ticks; MGC normally quotes 1 tick wide
            min_confidence=0.35,
            min_seconds_between_trades=900,
        ),
    }


def _research_symbols() -> dict[str, SymbolConfig]:
    """Instruments for the free Bitstamp feed. NOT a trading configuration.

    CME depth needs a Sierra Chart licence and a paid Denali subscription.
    Bitstamp publishes full book depth over a public WebSocket for nothing, so
    every layer above the client - bar aggregation, the L2 book, the 36-feature
    extractor, the panel - can be run and measured for free.

    The microstructure is not CME's: 24/7, no auction, no settlement, a
    different participant mix. A model trained on this data does not transfer
    to MGC and is not meant to. This validates the engineering, not the edge.

    The risk numbers below are placeholders sized off nothing in particular.
    They exist so the gates have something to compare against on a paper run;
    set them from measured ATR before drawing any conclusion from a result.
    """
    return {
        # --------------------------------------------------- Bitstamp BTCUSD
        # No separator in the name, deliberately. The name reaches a filename
        # (ict_{symbol}_{date}.csv), a CSV column and a log line, and a "/"
        # turns the first of those into a directory that does not exist.
        # core.bitstamp_client.to_pair() lowercases it for the wire either way.
        "BTCUSD": SymbolConfig(
            name="BTCUSD",
            # Venue fact:
            contract_size=1.0,       # one unit is one BTC
            multiplier=1.0,          # $1.00 price move  ->  $1.00 P&L per unit
            tick_size=0.01,
            digits=2,
            min_contracts=1,         # the codebase dropped fractional sizing in
            step_contracts=1,        # the futures pivot; one unit is the floor
            exchange="bitstamp",
            base_asset="BTC",
            quote_asset="USD",
            # Placeholders - see the docstring:
            enabled=True,
            tradable=False,          # no order path exists on this adapter
            stop_distance=150.0,
            target_distance=225.0,
            max_spread=5.00,
            min_confidence=0.35,
            min_seconds_between_trades=900,
        ),
    }


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = env if env is not None else load_env()

    # No required credentials any more. The DTC server is a local process the
    # user already has logged in to their broker; it defaults to localhost and
    # usually needs no username or password at all. Nothing here should stop
    # the engine from starting in dry/paper mode on a machine with no .env.
    execution_mode = env.get("EXECUTION_MODE", "paper").strip().lower()
    if execution_mode not in {"paper", "live"}:
        raise SystemExit("EXECUTION_MODE must be 'paper' or 'live'")

    settings = Settings(
        dtc_host=env.get("DTC_HOST", "localhost").strip(),
        dtc_port=int(env.get("DTC_PORT", "11099")),
        dtc_username=env.get("DTC_USERNAME", "").strip(),
        dtc_password=env.get("DTC_PASSWORD", "").strip(),
        dtc_trade_account=env.get("DTC_TRADE_ACCOUNT", "").strip(),
        execution_mode=execution_mode,
        autotrade_default=_as_bool(env.get("AUTOTRADE_DEFAULT", "false")),
        # Entry timeframe. Higher confirmation timeframes are set in
        # core/marketdata.py (CONFIRM_TFS) because the strategy's
        # indicator periods are expressed in bars of each.
        bar_seconds=int(env.get("BAR_SECONDS", "300")),
        paper_start_balance=float(env.get("PAPER_START_BALANCE", "500")),
        default_symbol=env.get("DEFAULT_SYMBOL", "MGC").strip().upper(),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", "").strip(),
        anthropic_model=env.get("ANTHROPIC_MODEL", "claude-opus-5").strip(),
        fred_key=env.get("OPENBB_FRED_KEY", "").strip(),
        commission_per_contract=float(
            env.get("COMMISSION_PER_CONTRACT", "0") or 0
        ),
        risk_per_trade=float(env.get("RISK_PER_TRADE", "50") or 50),
        max_daily_loss=float(env.get("MAX_DAILY_LOSS", "100") or 100),
        max_daily_profit=float(env.get("MAX_DAILY_PROFIT", "100") or 100),
        max_daily_trades=int(env.get("MAX_DAILY_TRADES", "6") or 6),
        strategy_mode=env.get("STRATEGY_MODE", "ict").strip().lower(),
        ict_require_model=_as_bool(env.get("ICT_REQUIRE_MODEL", "true"), True),
        symbols=_default_symbols(),
        research_symbols=_research_symbols(),
    )

    if settings.strategy_mode not in {"ema", "ict"}:
        raise SystemExit("STRATEGY_MODE must be 'ema' or 'ict'")

    # Live execution never arms itself at startup, whatever the .env says.
    if settings.live_execution:
        settings.autotrade_default = False

    LOG_DIR.mkdir(exist_ok=True)
    return settings
