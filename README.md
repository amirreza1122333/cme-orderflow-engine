# CME futures trading engine (Sierra Chart / DTC)

> ### 📦 What is in this repository
>
> This is the **infrastructure layer** of a live research project, published for
> review. It is **not a runnable trading system**, and it is not open source —
> see [LICENSE](LICENSE).
>
> **Included** — and each of these reads standalone:
>
> | Module | What it is |
> |---|---|
> | [`core/dtc_client.py`](core/dtc_client.py) | Full DTC protocol interface spec for Sierra Chart — message types, data shapes, threading contract |
> | [`core/symbols.py`](core/symbols.py) | Futures contract maths: multipliers, tick grids, whole-contract sizing |
> | [`core/risk.py`](core/risk.py) | Risk manager and circuit breakers. Every gate can only refuse a trade |
> | [`core/marketdata.py`](core/marketdata.py) | Tick → multi-timeframe bar aggregation, incremental L2 book |
> | [`core/broker.py`](core/broker.py) | Pessimistic paper-fill simulator and the gated live-order path |
> | [`core/engine.py`](core/engine.py) | The decision loop wiring it together |
> | [`panel/`](panel/), [`deploy/`](deploy/) | Read-only monitoring dashboard, hardened systemd units |
>
> **Withheld** — the `ict/` package: the ICT signal rules, the 36-feature
> extractor and the LightGBM pipeline. That is the research, and it stays
> private. Only `core/engine.py` and `run.py` import from it, so everything
> above is unaffected and can be read on its own.
>
> That separation is deliberate, not a convenience for publishing: the strategy
> layer depends on nothing but floats and datetimes, which is what let the
> entire data feed be swapped from cTrader to CME futures without touching a
> line of it.

> ## ⚠️ PLATFORM PIVOT — 2026-08-21
>
> **The cTrader Open API is dead for this project.** The engine now targets
> **CME futures via Sierra Chart over the DTC protocol** (localhost:11099), and
> the traded instrument is **MGC (Micro Gold)** instead of spot XAUUSD.
>
> Everything below this banner that mentions cTrader, XAUUSD, lots or fractional
> position sizes is **historical**. What changed:
>
> | Was | Is |
> |---|---|
> | cTrader Open API, Twisted, protobuf | raw `socket` + `struct` DTC client |
> | `core/ctrader.py` | `core/ctrader_DEPRECATED.py` (reference only) |
> | XAUUSD spot | MGC — 10 oz, $10 per $1.00 move, 0.10 tick |
> | 0.01-lot fractional sizing | whole contracts, minimum 1 |
> | `lot_size` / `lots` | `contract_size` / `contracts` |
>
> **Not implemented yet:** `core/dtc_client.py` is an **interface blueprint** -
> every signature the engine and collector call, documented, with no socket code
> behind it. Filling in those bodies is the whole remaining job.
>
> Until then both processes run on the **dry feed**, replaying recorded CSVs
> offline through the identical callbacks the live client will fire:
>
> ```
> python run.py collect --feed dry --source data --out data   # 36-feature CSVs
> python -m ict.prepare --symbol MGC                          # labels
> python -m ict.train                                         # walk-forward
> python run.py run --feed dry                                # engine replay
> ```
>
> `--feed dtc` is refused by both, by design - neither may silently fall back to
> a broker. `calibrate` needs historical bars and is offline until the client
> lands. No order can be sent in any mode.
>
> **`ict/` is fully broker-decoupled.** It imports only stdlib, the ML stack,
> and three platform-agnostic project modules (`config`, `core.marketdata`,
> `core.symbols`). `ict/service.py` was ported off cTrader on 2026-08-21 and is
> the last piece that ever touched a protocol.
>
> **Funding reality:** one MGC contract at the configured 4.30 stop risks $43.
> On a $500 account that is 8.6% per trade and there is nothing smaller to size
> down to — so this is a paper-trading and data-collection configuration, not a
> live one.
>
> **Windows import-order trap:** `ict/train.py` must import LightGBM before
> pandas or the first fit dies with an OpenMP access violation that looks like
> corrupt data. The import is pinned at the top of that file with a comment -
> do not "tidy" it into the alphabetical block.
>
> `HANDOFF.md` has not been rewritten for the pivot and still describes the
> cTrader deployment.

> **Picking this up mid-project?** Read [HANDOFF.md](HANDOFF.md) first — it has
> the current deployment state, verified account and contract facts, known
> blockers, and the rules not to break. This file explains how the system works;
> that one explains what state it is actually in.

An automated intraday trading engine for a cTrader account: live prices and
level-2 depth over the cTrader Open API, a trend-continuation signal, an
economic-calendar blackout filter, an optional Claude review layer, a hard risk
manager, and a paper broker that simulates fills against the real feed.

**Read this first.** This is a complete, working trading system. It is not a
profitable strategy, and nobody can hand you one. The weights in
`core/strategy.py` are a reasonable starting point, not a measured edge — the
only thing that tells you whether they work is weeks of paper trading with the
journal this engine writes. Expect losing days; the risk manager exists to make
sure a losing day stays a losing day rather than a blown account.

A note on the target you started from: $10/day on $500 is 2% per day, roughly
1,000× per year compounded. That is not a target any strategy sustains. Aiming
for it forces oversized positions, which is the fastest known way to lose the
$500. The defaults here risk $5 per trade with a $15 daily stop, and the honest
expectation is a mix of green and red days that you judge over months.

## Layout

```
run.py              entry point: check / selftest / run
daily_report.py     end-of-day review of the journal
config.py           settings, per-symbol parameters, secret masking
panel/              read-only monitoring dashboard (separate process)
  server.py         FastAPI: three GET routes, no mutating path exists
  static/           self-contained HTML/CSS/JS, no external origins
deploy/             hardened systemd units for engine and panel
core/
  ctrader.py        Open API client - connect, auth, subscribe, order entry
  symbols.py        symbol specs and the price/volume/money conversions
  marketdata.py     ticks, bars, level-2 order book
  indicators.py     EMA / RSI / ATR (pure functions)
  strategy.py       signal generation
  news.py           ForexFactory calendar blackout
  analyst.py        optional Claude review layer
  risk.py           position sizing and circuit breakers
  broker.py         PaperBroker (simulated) and LiveBroker (real orders)
  journal.py        JSONL event log + CSV trade log
  engine.py         wires it together, runs the decision loop
logs/               engine log, events-*.jsonl, trades-*.csv
```

## Install

```bash
pip install -r requirements.txt
```

## First run

```bash
python run.py selftest
```

Offline. Replays synthetic ticks through the whole pipeline and asserts that
position sizing respects the risk cap and that no simulated loss exceeds the
stop. Touches no network and no account.

```bash
python run.py check
```

Read-only. Connects, authenticates, prints the accounts on your token, the
balance, the contract specification for each symbol, and 20 seconds of live
spreads. It sends no orders. Run this before anything else — it tells you
whether your configured `max_spread` is realistic for your broker and whether
the smallest tradable lot already risks more than your per-trade cap.

```bash
python run.py calibrate
```

Read-only. Measures ATR(14) on 5m/15m/1h from your broker's own bars and prints
the stop and target those imply, the resulting position size, and whether your
risk cap can afford the symbol at all. The shipped distances are estimates
until you run this.

```bash
python run.py run           # observes and logs signals, never trades
python run.py run --arm     # paper trading with simulated fills
```

## The four gates before real money

Live orders require **all** of:

1. `EXECUTION_MODE=live` in `.env`
2. `--arm` on the command line
3. `--i-understand-live` on the command line
4. typing `LIVE` at the confirmation prompt

Miss any one and fills stay simulated. `AUTOTRADE_DEFAULT=true` only ever arms
paper mode; live mode always starts disarmed regardless of `.env`.

## Configuration

Secrets live in `.env` (never commit it). Strategy and risk parameters live in
`config.py` — they are code, not environment variables, because they are the
part you will actually iterate on.

| Setting | Default | What it does |
|---|---|---|
| `risk_per_trade` | 5.00 | Cash risked per trade. Position size is derived from this and the stop, never chosen directly. |
| `max_daily_loss` | 15.00 | Trading stops for the UTC day at this loss. |
| `max_daily_profit` | 15.00 | Trading also stops after this gain — protecting a good day is a real edge. |
| `max_daily_trades` | 10 | Overtrading cap. |
| `max_consecutive_losses` | 3 | Three in a row triggers a 30-minute cooldown. |
| `max_open_positions` | 1 | One position at a time across all symbols. |
| `commission_per_lot` | 0.00 | **Set this from your broker's contract spec.** Leaving it at zero makes paper results look better than live ones ever will. |
| `stop_distance` / `target_distance` | per symbol | In price units, sized for the 5-minute chart. 1:1.5 reward:risk. **Run `run.py calibrate` to replace these with measured values.** |
| `max_spread` | per symbol | Hard veto. Check it against what `run.py check` reports. |
| `min_confidence` | 0.35–0.40 | Signal strength floor. Raise to trade less. |

`PAPER_START_BALANCE` in `.env` is currently 10000. Set it to the balance you
actually intend to trade (e.g. 500) or the paper results will not reflect the
position sizes and drawdowns you would really experience.

## How a trade is decided

Every gate can only *block*. Nothing in the chain can make the engine trade
more than the strategy proposed.

1. **Strategy — multi-timeframe.** The 1-hour and 15-minute EMA(9/21) trends
   decide which direction is permitted at all; both must agree and both must
   actually be trending (EMAs separated by at least 0.15 ATR), or nothing
   trades. The 5-minute chart then decides timing: entry only on a pullback
   toward the fast EMA, never on an extension. RSI is an exhaustion brake;
   order-book imbalance and tick momentum are minor confirmation. Vetoes on
   wide spread, on a target more than ~6 bars of 5m ATR away, and on any
   5-minute trend that opposes the higher timeframes.
2. **News blackout** — no trading from 30 minutes before to 15 minutes after a
   high-impact release for the relevant currency.
3. **Claude analyst** (optional) — a snapshot every 10 minutes; `avoid`/`wait`
   or a confident opposing bias blocks the trade. Agreement changes nothing.
4. **Risk manager** — sizes the position from the stop, then checks the daily
   loss, daily profit, trade count, consecutive losses, cooldown, open-position
   cap and per-symbol re-entry delay.
5. **Broker** — paper by default; live only behind the four gates above.

### Why Claude is not choosing entries

An API round trip takes seconds. The setups here last minutes. By the time a
reply arrives, the tick that prompted it is history — so Claude runs on a slow
loop as a veto, never as a trigger. A language model's market opinion is not an
edge; it is a reasonable check for "conditions look abnormal". To enable it,
add `ANTHROPIC_API_KEY=...` to `.env`. Without it the engine runs unchanged.

## Reading the output

- `logs/engine-YYYYMMDD.log` — everything, including why each signal was
  rejected. The rejection lines are the useful part.
- `logs/events-YYYYMMDD.jsonl` — every signal, block and fill with full context.
- `logs/trades-YYYYMMDD.csv` — closed trades: entry, exit, reason, P&L.

After a couple of weeks the CSV answers the only questions that matter: win
rate, average win vs average loss, and whether the losers cluster around
particular hours or spread conditions.

## Monitoring panel

```bash
python -m panel.server
```

A read-only dashboard on `http://127.0.0.1:8787`: balance and equity curve,
the risk envelope as meters against each limit, live spread and signal state
per instrument, the Claude analyst's current verdict, upcoming high-impact
news, open positions, closed trades, and — most useful in week one — a ranked
breakdown of *why the engine is not trading*.

### Security model

The panel is a **reader**, and that is enforced structurally rather than by
convention:

| Decision | Why |
|---|---|
| **No mutating routes exist.** The whole API is three GETs, and non-GET verbs are rejected in middleware. | Compromising the panel cannot place an order, because no code path places one. |
| **Separate process.** It never imports the engine or touches the broker socket — it reads `logs/state.json`, which the engine publishes atomically via `os.replace`. | A hang, crash or request flood in the web layer cannot slow or kill the trading loop. |
| **No credentials in the snapshot**, and every response is re-checked against the live `.env` values before it is served. | A future change that widens the snapshot fails loudly instead of leaking quietly. |
| **Localhost by default.** A public bind requires `--i-understand-exposure` *and* a 24+ character `PANEL_TOKEN`. | Exposing an account dashboard to the internet cannot happen by accident. |
| **Strict CSP** (`default-src 'none'`, no inline script or style), plus `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. | A string that reaches the page cannot become executable. |
| **DOM built with `createElement`/`textContent`** — no `innerHTML` anywhere. | Engine-supplied strings are data, never markup. |
| **Fixed static allowlist**; no filesystem path is ever built from user input. | Directory traversal is structurally impossible, not filtered. |

View it remotely over an SSH tunnel rather than opening a port:

```bash
ssh -N -L 8787:127.0.0.1:8787 ubuntu@<vps-ip>
```

### Design notes

Colour never carries meaning alone. The red/green profit pair fails colourblind
separation — measured at ΔE 4.1 under deuteranopia against an ≥8 threshold — so
every signed value also carries a sign, an arrow glyph and, in the trades table,
the word "win" or "loss". Light and dark are separately chosen steps of the same
palette, not an inverted flip. Charts use a single series (no categorical
palette to get wrong), a 2px line, a 10% area wash, hairline gridlines, one
direct label at the endpoint, and a hover crosshair with a tooltip; the equity
curve has a table view for anyone who cannot use the chart.

## Known limitations

- P&L assumes the quote currency equals the account's deposit currency. True
  for XAUUSD, EURUSD, US30 and US500 on a USD account; the engine warns at
  startup if you enable a symbol where it does not hold.
- Paper fills use the real bid/ask and charge spread and commission, but cannot
  model slippage on a fast market or a stop gapping through a level. Live
  results will be worse than paper. `PaperBroker` accepts `slippage_price` and
  `stop_slippage_price` if you want to make it pessimistic deliberately.
- Level-2 depth is a retail feed, showing a fraction of real liquidity. Resting
  size is not intent. It is weighted at 15 of 100 points for that reason.
- The engine holds at most one position and does not trail stops, scale in or
  hedge.
- Reconnects re-subscribe, but a position opened live and closed while the
  process was down will be seen only at the next `reconcile()`.

## Suggested path

1. `selftest`, then `check`, and fix anything either one warns about.
2. Run disarmed for a day. Read the log. Do the signals appear where you would
   have taken them?
3. Switch `CTRADER_HOST_TYPE=demo` with a demo account and run `--arm` for at
   least two weeks. Do not change parameters mid-run; you need a clean sample.
4. Review the CSV. If it is not profitable after commission on demo, it will
   not be profitable live. Change one thing, run another two weeks.
5. Only then consider live, at the smallest size your broker allows.
