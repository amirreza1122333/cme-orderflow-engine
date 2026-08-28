/* Trading engine panel.
 *
 * Read-only. Everything is built with createElement/textContent - no innerHTML
 * anywhere - so a string that arrives from the engine can never become markup.
 * That pairs with the strict CSP the server sends (no inline script or style).
 *
 * Colour is never the only carrier of meaning: the red/green profit pair fails
 * colourblind separation, so every signed value also gets a glyph and a word.
 */
'use strict';

const REFRESH_MS = 5000;
const $ = (id) => document.getElementById(id);

/* ------------------------------------------------------------- formatting */

/* Fixed locale on purpose: the panel sits beside prices rendered with toFixed
 * and a CSV written with '.', so following the browser's locale would put
 * "479,22" next to "4041.05" on the same screen. */
const LOCALE = 'en-US';

const money = (v, digits = 2) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : Number(v).toLocaleString(LOCALE, {
        minimumFractionDigits: digits, maximumFractionDigits: digits });

const signed = (v, digits = 2) => {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  const n = Number(v);
  return (n > 0 ? '+' : n < 0 ? '−' : '') + money(Math.abs(n), digits);
};

const pct = (v, digits = 0) =>
  v === null || v === undefined ? '—' : `${Number(v).toFixed(digits)}%`;

const price = (v, digits) =>
  v === null || v === undefined ? '—' : Number(v).toFixed(digits ?? 2);

function timeOf(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? '—'
    : d.toISOString().slice(11, 16) + ' UTC';
}

function dateTimeOf(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? '—' : d.toISOString().slice(5, 16).replace('T', ' ');
}

/** Direction as three redundant channels: sign, glyph, colour class. */
function directionParts(value) {
  const n = Number(value) || 0;
  if (n > 0) return { cls: 'delta-up', glyph: '▲', word: 'up' };
  if (n < 0) return { cls: 'delta-down', glyph: '▼', word: 'down' };
  return { cls: 'delta-flat', glyph: '■', word: 'flat' };
}

/* ------------------------------------------------------- element helpers */

function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text !== undefined) node.textContent = opts.text;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  for (const child of children) if (child) node.appendChild(child);
  return node;
}

const NS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

function replace(container, ...nodes) {
  container.replaceChildren(...nodes.filter(Boolean));
}

/* ---------------------------------------------------------------- tooltip */

const tooltip = el('div', { class: 'tooltip', attrs: { role: 'status' } });
document.body.appendChild(tooltip);

function showTip(event, lines) {
  replace(tooltip, ...lines.map((line) => {
    const row = el('div');
    if (line.label) row.appendChild(el('b', { text: line.label + ' ' }));
    row.appendChild(document.createTextNode(line.value));
    return row;
  }));
  tooltip.dataset.visible = 'true';
  const pad = 14;
  const rect = tooltip.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = event.clientY - rect.height - pad;
  tooltip.style.left = `${Math.max(8, x)}px`;
  tooltip.style.top = `${Math.max(8, y)}px`;
}

function hideTip() { tooltip.dataset.visible = 'false'; }

/* ------------------------------------------------------------ equity chart
 * Single series, so no legend: the panel heading names what is plotted.
 * 2px line, 10% area wash, hairline grid, end dot with a 2px surface ring
 * and one direct label at the endpoint (never a label on every point).
 */

function renderEquity(points, startBalance) {
  const host = $('equity-chart');
  if (!points.length) {
    replace(host, el('p', { class: 'empty', text: 'No closed trades yet.' }));
    return;
  }

  const W = 900, H = 260;
  const m = { top: 16, right: 74, bottom: 26, left: 8 };
  const innerW = W - m.left - m.right;
  const innerH = H - m.top - m.bottom;

  const series = [{ balance: startBalance, time: null, pnl: null, index: 0 }]
    .concat(points.map((p, i) => ({ ...p, index: i + 1 })));

  const values = series.map((p) => p.balance);
  let lo = Math.min(...values, startBalance);
  let hi = Math.max(...values, startBalance);
  if (hi === lo) { hi += 1; lo -= 1; }
  const padY = (hi - lo) * 0.12;
  lo -= padY; hi += padY;

  const x = (i) => m.left + (series.length === 1 ? innerW / 2
    : (i / (series.length - 1)) * innerW);
  const y = (v) => m.top + innerH - ((v - lo) / (hi - lo)) * innerH;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': `Equity curve over ${points.length} closed trades`,
  });

  // Gridlines with clean tick values.
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const value = lo + ((hi - lo) * i) / ticks;
    const yy = y(value);
    svg.appendChild(svgEl('line', {
      class: 'grid-line', x1: m.left, x2: m.left + innerW, y1: yy, y2: yy,
    }));
    const label = svgEl('text', {
      class: 'axis-text', x: m.left + innerW + 8, y: yy + 4,
    });
    label.textContent = money(value, 0);
    svg.appendChild(label);
  }

  // Starting balance reference.
  const yStart = y(startBalance);
  svg.appendChild(svgEl('line', {
    class: 'base-line', x1: m.left, x2: m.left + innerW, y1: yStart, y2: yStart,
  }));

  const line = series.map((p, i) => `${i ? 'L' : 'M'}${x(i)},${y(p.balance)}`).join(' ');
  svg.appendChild(svgEl('path', {
    class: 'series-area',
    d: `${line} L${x(series.length - 1)},${m.top + innerH} L${x(0)},${m.top + innerH} Z`,
  }));
  svg.appendChild(svgEl('path', { class: 'series-line', d: line }));

  // One direct label: the endpoint.
  const last = series[series.length - 1];
  svg.appendChild(svgEl('circle', {
    class: 'end-dot', cx: x(series.length - 1), cy: y(last.balance), r: 4.5,
  }));

  // Hover: crosshair + tooltip, hit target wider than the mark.
  const crosshair = svgEl('line', {
    class: 'crosshair', y1: m.top, y2: m.top + innerH, x1: 0, x2: 0,
    opacity: 0,
  });
  svg.appendChild(crosshair);
  const overlay = svgEl('rect', {
    class: 'hover-target', x: m.left, y: m.top, width: innerW, height: innerH,
  });
  svg.appendChild(overlay);

  overlay.addEventListener('mousemove', (event) => {
    const box = svg.getBoundingClientRect();
    const rel = ((event.clientX - box.left) / box.width) * W;
    const ratio = (rel - m.left) / innerW;
    const idx = Math.max(0, Math.min(series.length - 1,
      Math.round(ratio * (series.length - 1))));
    const point = series[idx];
    crosshair.setAttribute('x1', x(idx));
    crosshair.setAttribute('x2', x(idx));
    crosshair.setAttribute('opacity', 1);
    const lines = [{ label: 'Balance', value: money(point.balance) }];
    if (point.index === 0) lines.push({ value: 'starting balance' });
    else {
      lines.unshift({ value: `Trade ${point.index}` });
      if (point.time) lines.push({ label: 'Closed', value: dateTimeOf(point.time) });
    }
    showTip(event, lines);
  });
  overlay.addEventListener('mouseleave', () => {
    crosshair.setAttribute('opacity', 0);
    hideTip();
  });

  replace(host, svg);
}

function renderEquityTable(points, startBalance) {
  const rows = points.map((p, i) => el('tr', {}, [
    el('td', { text: String(i + 1) }),
    el('td', { text: dateTimeOf(p.time) }),
    el('td', { class: 'num', text: money(p.balance) }),
  ]));
  rows.unshift(el('tr', {}, [
    el('td', { text: '0' }),
    el('td', { text: 'start' }),
    el('td', { class: 'num', text: money(startBalance) }),
  ]));
  replace($('equity-table'), el('table', {}, [
    el('thead', {}, [el('tr', {}, [
      el('th', { text: '#' }),
      el('th', { text: 'Closed (UTC)' }),
      el('th', { class: 'num', text: 'Balance' }),
    ])]),
    el('tbody', {}, rows),
  ]));
}

/* --------------------------------------------------------------- KPI row */

function kpi(label, value, note, deltaValue) {
  const parts = deltaValue === undefined ? null : directionParts(deltaValue);
  const valueNode = el('p', { class: 'kpi-value' });
  if (parts) {
    valueNode.classList.add(parts.cls);
    valueNode.appendChild(el('span', { class: 'delta-glyph', text: parts.glyph + ' ' }));
  }
  valueNode.appendChild(document.createTextNode(value));
  return el('div', { class: 'kpi' }, [
    el('p', { class: 'label', text: label }),
    valueNode,
    el('p', { class: 'kpi-note', text: note }),
  ]);
}

function renderHero(state) {
  const risk = state.risk || {};
  const total = Number(risk.total_pnl || 0);
  const parts = directionParts(total);

  $('balance').textContent = money(risk.balance);
  const sub = $('balance-sub');
  replace(sub,
    el('span', { class: parts.cls }, [
      el('span', { class: 'delta-glyph', text: parts.glyph + ' ' }),
      document.createTextNode(`${signed(total)} ${parts.word}`),
    ]),
    document.createTextNode(` since ${money(risk.starting_balance)} start`),
  );

  const pf = risk.profit_factor;
  const pfNote = risk.trades_today >= 30
    ? 'above 1.3 is an edge'
    : 'needs 30+ trades to mean anything';

  replace($('kpis'),
    kpi('P&L today', signed(risk.daily_pnl), `limit ${money(risk.limits?.max_daily_loss)}`,
        risk.daily_pnl),
    kpi('Trades today', String(risk.trades_today ?? 0),
        `cap ${risk.limits?.max_daily_trades ?? '—'}`),
    kpi('Win rate', pct(risk.win_rate, 1),
        `${risk.wins ?? 0} won · ${risk.losses ?? 0} lost`),
    kpi('Profit factor', pf ? Number(pf).toFixed(2) : '—', pfNote),
  );
}

/* ----------------------------------------------------------- risk meters */

function severityFor(ratio) {
  if (ratio >= 1) return 'critical';
  if (ratio >= 0.85) return 'serious';
  if (ratio >= 0.6) return 'warning';
  return 'normal';
}

function meter(name, used, limit, formatter = money) {
  const ratio = limit > 0 ? Math.min(used / limit, 1) : 0;
  const severity = severityFor(limit > 0 ? used / limit : 0);
  const fill = el('div', { class: 'meter-fill' });
  fill.style.width = `${(ratio * 100).toFixed(1)}%`;
  if (severity !== 'normal') fill.dataset.severity = severity;

  const stateWord = { critical: '⚠ at limit', serious: '⚠ close to limit',
                      warning: 'over half used', normal: '' }[severity];

  return el('div', { class: 'meter' }, [
    el('div', { class: 'meter-head' }, [
      el('span', { class: 'meter-name', text: name }),
      el('span', { class: 'meter-num',
                   text: `${formatter(used)} / ${formatter(limit)}` }),
    ]),
    el('div', { class: 'meter-track' }, [fill]),
    stateWord ? el('p', { class: 'muted', text: stateWord }) : null,
  ]);
}

function renderRisk(state) {
  const risk = state.risk || {};
  const limits = risk.limits || {};
  const lossUsed = Math.max(0, -Number(risk.daily_pnl || 0));
  const profitUsed = Math.max(0, Number(risk.daily_pnl || 0));

  const nodes = [
    meter('Daily loss budget', lossUsed, limits.max_daily_loss || 0),
    meter('Daily profit target', profitUsed, limits.max_daily_profit || 0),
    meter('Trades today', risk.trades_today || 0, limits.max_daily_trades || 0,
          (v) => String(Math.round(v))),
    meter('Consecutive losses', risk.consecutive_losses || 0,
          limits.max_consecutive_losses || 0, (v) => String(Math.round(v))),
  ];
  if (risk.cooling_down) {
    nodes.push(el('p', { class: 'muted',
      text: '⚠ Cooling down after consecutive losses — new entries are blocked.' }));
  }
  replace($('risk-meters'), ...nodes);
}

/* --------------------------------------------------------------- analyst */

function renderAnalyst(state) {
  const analyst = state.analyst || {};
  const pill = $('analyst-pill');
  const body = $('analyst-body');

  if (!analyst.enabled) {
    pill.dataset.status = 'neutral';
    $('analyst-state').textContent = 'disabled';
    replace(body, el('p', { class: 'muted',
      text: 'No ANTHROPIC_API_KEY set. The engine trades on technicals, order '
          + 'book and the news filter alone — this is the week-one baseline.' }));
    return;
  }

  pill.dataset.status = 'good';
  $('analyst-state').textContent = analyst.model || 'enabled';

  const rows = (state.symbols || []).map((symbol) => {
    const verdict = symbol.analyst || {};
    const status = verdict.action === 'avoid' ? 'critical'
      : verdict.action === 'wait' ? 'warning' : 'good';
    return el('div', { class: 'symbol' }, [
      el('div', { class: 'symbol-name', text: symbol.name }),
      el('div', {}, [
        el('span', { class: 'pill', attrs: { 'data-status': status } }, [
          el('span', { class: 'pill-glyph', attrs: { 'aria-hidden': 'true' }, text: '●' }),
          el('span', { text: `${verdict.action || '—'} · ${verdict.bias || '—'}` }),
        ]),
      ]),
      el('div', { class: 'symbol-reasons', text: verdict.reasoning || '—' }),
      el('div', { class: 'muted',
                  text: verdict.age_minutes !== undefined
                    ? `${verdict.age_minutes} min ago` : '' }),
    ]);
  });
  replace(body, ...(rows.length ? rows
    : [el('p', { class: 'empty', text: 'No verdicts yet.' })]));
}

/* --------------------------------------------------------------- symbols */

function renderSymbols(state) {
  const rows = (state.symbols || []).map((symbol) => {
    const signal = symbol.signal || {};
    const tags = [];
    if (symbol.news_blackout) tags.push('news blackout');
    if (!symbol.has_depth) tags.push('no L2');
    if (symbol.spread !== null && symbol.max_spread
        && symbol.spread > symbol.max_spread) tags.push('spread over limit');

    const status = signal.direction && !(signal.vetoes || []).length ? 'good'
      : symbol.news_blackout ? 'critical' : 'neutral';

    const detail = (signal.vetoes && signal.vetoes.length)
      ? signal.vetoes : (signal.reasons || []);

    return el('div', { class: 'symbol' }, [
      el('div', {}, [
        el('div', { class: 'symbol-name', text: symbol.name }),
        el('div', { class: 'muted', text: `${symbol.bars} bars` }),
      ]),
      el('div', {}, [
        el('div', { class: 'symbol-price',
                    text: `${price(symbol.bid, symbol.digits)} / ${price(symbol.ask, symbol.digits)}` }),
        el('div', { class: 'muted',
                    text: `spread ${price(symbol.spread, symbol.digits + 1)}`
                        + ` · median ${price(symbol.median_spread, symbol.digits + 1)}` }),
      ]),
      el('div', {}, [
        el('span', { class: 'pill', attrs: { 'data-status': status } }, [
          el('span', { class: 'pill-glyph', attrs: { 'aria-hidden': 'true' }, text: '●' }),
          el('span', { text: signal.direction
            ? `${signal.direction} ${(signal.confidence * 100).toFixed(0)}%`
            : 'no signal' }),
        ]),
        el('ul', { class: 'reason-list symbol-reasons' },
          detail.slice(0, 3).map((line) => el('li', { text: line }))),
      ]),
      el('div', {}, tags.map((tag) => el('span', { class: 'tag', text: tag }))),
    ]);
  });
  replace($('symbols'), ...(rows.length ? rows
    : [el('p', { class: 'empty', text: 'No instruments active.' })]));
}

/* ------------------------------------------------------------ veto chart
 * One series, so every bar takes slot 1 - never a value ramp, which would
 * double-encode length as hue. Bars are 16px (cap is 24), separated by a 2px
 * surface gap, with a 4px rounded data-end and a square baseline end.
 */

function renderVetoes(vetoes) {
  const host = $('veto-chart');
  if (!vetoes.length) {
    replace(host, el('p', { class: 'empty',
      text: 'Nothing blocked yet — the engine has not evaluated a signal.' }));
    return;
  }

  const rows = vetoes.slice(0, 8);
  const max = Math.max(...rows.map((r) => r.count));
  const barH = 16, gap = 2, labelH = 17, rowH = barH + labelH + gap + 8;
  const W = 640, valueW = 56;
  const H = rows.length * rowH + 6;
  const innerW = W - valueW;

  const svg = svgEl('svg', {
    viewBox: `0 0 ${W} ${H}`, role: 'img',
    'aria-label': 'Reasons the engine did not trade, by count',
  });

  rows.forEach((row, i) => {
    const top = i * rowH;
    const label = svgEl('text', { class: 'bar-label', x: 0, y: top + 12 });
    const full = `${row.symbol} · ${row.reason}`;
    label.textContent = full;
    svg.appendChild(label);
    // Measure, then trim - never let a long reason run past the chart or get
    // clipped mid-character. The untruncated text stays in the tooltip.
    if (label.getComputedTextLength() > innerW) {
      let text = full;
      while (text.length > 4 && label.getComputedTextLength() > innerW) {
        text = text.slice(0, -2);
        label.textContent = text + '…';
      }
    }

    const width = Math.max(2, (row.count / max) * innerW);
    const y = top + labelH;
    const r = Math.min(4, width);
    // Square at the baseline (left), 4px rounded at the data end (right).
    const path = `M0,${y} H${width - r} A${r},${r} 0 0 1 ${width},${y + r}`
               + ` V${y + barH - r} A${r},${r} 0 0 1 ${width - r},${y + barH}`
               + ` H0 Z`;
    const bar = svgEl('path', { class: 'bar-mark', d: path });
    svg.appendChild(bar);

    const value = svgEl('text', {
      class: 'bar-value', x: width + 8, y: y + barH - 3,
    });
    value.textContent = row.count.toLocaleString(LOCALE);
    svg.appendChild(value);

    // Hit target spans the whole row, not just the bar.
    const hit = svgEl('rect', {
      class: 'hover-target', x: 0, y: top, width: W, height: rowH,
    });
    hit.addEventListener('mousemove', (event) => showTip(event, [
      { value: row.reason },
      { label: 'Symbol', value: row.symbol },
      { label: 'Times', value: row.count.toLocaleString(LOCALE) },
    ]));
    hit.addEventListener('mouseleave', hideTip);
    svg.appendChild(hit);
  });

  replace(host, svg);
}

/* ------------------------------------------------------------------ news */

function renderNews(state) {
  const news = state.news || {};
  const upcoming = news.upcoming || [];
  const head = el('p', { class: 'muted',
    text: news.loaded_at
      ? `${news.high_impact_this_week} high-impact events this week · loaded ${timeOf(news.loaded_at)}`
      : 'Calendar not loaded yet.' });

  if (!upcoming.length) {
    replace($('news-body'), head,
      el('p', { class: 'empty', text: 'Nothing high-impact in the next 12 hours.' }));
    return;
  }

  const table = el('table', {}, [
    el('thead', {}, [el('tr', {}, [
      el('th', { text: 'In' }),
      el('th', { text: 'Currency' }),
      el('th', { text: 'Event' }),
    ])]),
    el('tbody', {}, upcoming.map((event) => el('tr', {}, [
      el('td', { class: 'num', text: `${event.in_minutes} min` }),
      el('td', { text: event.currency }),
      el('td', { text: event.title }),
    ]))),
  ]);
  replace($('news-body'), head, el('div', { class: 'table-wrap' }, [table]));
}

/* ------------------------------------------------------------- positions */

function renderPositions(state) {
  const positions = state.positions || [];
  if (!positions.length) {
    replace($('positions'), el('p', { class: 'empty', text: 'Flat — no open positions.' }));
    return;
  }
  const table = el('table', {}, [
    el('thead', {}, [el('tr', {}, [
      el('th', { text: 'Symbol' }), el('th', { text: 'Side' }),
      el('th', { class: 'num', text: 'Ctr' }), el('th', { class: 'num', text: 'Entry' }),
      el('th', { class: 'num', text: 'Stop' }), el('th', { class: 'num', text: 'Target' }),
      el('th', { class: 'num', text: 'Unrealised' }),
    ])]),
    el('tbody', {}, positions.map((position) => {
      const parts = directionParts(position.unrealised);
      return el('tr', {}, [
        el('td', { text: position.symbol }),
        el('td', { text: position.side }),
        el('td', { class: 'num', text: String(position.contracts ?? 0) }),
        el('td', { class: 'num', text: money(position.entry, 5) }),
        el('td', { class: 'num', text: money(position.stop_loss, 5) }),
        el('td', { class: 'num', text: money(position.take_profit, 5) }),
        el('td', { class: `num ${parts.cls}`,
                   text: `${parts.glyph} ${signed(position.unrealised)}` }),
      ]);
    })),
  ]);
  replace($('positions'), el('div', { class: 'table-wrap' }, [table]));
}

/* ---------------------------------------------------------------- trades */

function renderTrades(trades) {
  const host = $('trades');
  if (!trades.length) {
    replace(host, el('p', { class: 'empty', text: 'No closed trades yet.' }));
    $('trades-sub').textContent = '';
    return;
  }
  const recent = trades.slice(-25).reverse();
  const wins = trades.filter((t) => Number(t.pnl) > 0).length;
  $('trades-sub').textContent =
    `${trades.length} total · ${wins} won · ${trades.length - wins} lost`;

  const table = el('table', {}, [
    el('thead', {}, [el('tr', {}, [
      el('th', { text: 'Closed (UTC)' }), el('th', { text: 'Symbol' }),
      el('th', { text: 'Side' }), el('th', { class: 'num', text: 'Ctr' }),
      el('th', { class: 'num', text: 'Entry' }), el('th', { class: 'num', text: 'Exit' }),
      el('th', { text: 'Reason' }), el('th', { text: 'Result' }),
      el('th', { class: 'num', text: 'P&L' }),
    ])]),
    el('tbody', {}, recent.map((trade) => {
      const pnl = Number(trade.pnl);
      const parts = directionParts(pnl);
      return el('tr', {}, [
        el('td', { text: dateTimeOf(trade.closed_at) }),
        el('td', { text: trade.symbol }),
        el('td', { text: trade.side }),
        el('td', { class: 'num', text: String(trade.contracts ?? 0) }),
        el('td', { class: 'num', text: trade.entry }),
        el('td', { class: 'num', text: trade.exit }),
        el('td', { text: (trade.exit_reason || '').replace('_', ' ') }),
        // The word, not just the colour.
        el('td', { class: parts.cls, text: pnl > 0 ? 'win' : pnl < 0 ? 'loss' : 'flat' }),
        el('td', { class: `num ${parts.cls}`,
                   text: `${parts.glyph} ${signed(pnl)}` }),
      ]);
    })),
  ]);
  replace(host, table);
}

/* ----------------------------------------------------------------- shell */

function renderHeader(state) {
  const engine = state.engine || {};
  const modePill = $('mode-pill');
  const live = engine.mode === 'live';
  modePill.dataset.status = live ? 'critical' : 'good';
  $('mode-text').textContent = live
    ? (engine.armed ? 'LIVE · armed' : 'LIVE · disarmed')
    : `paper · ${engine.autotrade ? 'armed' : 'observing'}`;

  const connPill = $('conn-pill');
  const age = Number(state.state_age_seconds ?? 999);
  const connected = engine.connected && age < 30;
  connPill.dataset.status = connected ? 'good' : 'critical';
  $('conn-text').textContent = connected ? 'broker connected' : 'no feed';

  $('updated').textContent = `updated ${timeOf(state.generated_at)}`;
}

function renderStale(seconds) {
  const banner = el('div', { class: 'stale-banner' }, [
    el('strong', { text: '⚠ Stale data. ' }),
    document.createTextNode(
      `The engine has not published a snapshot for ${Math.round(seconds)}s. `
      + 'Check that the trading service is running.'),
  ]);
  const main = document.querySelector('main');
  const existing = document.querySelector('.stale-banner');
  if (existing) existing.replaceWith(banner);
  else main.prepend(banner);
}

function clearStale() {
  const existing = document.querySelector('.stale-banner');
  if (existing) existing.remove();
}

async function tick() {
  try {
    const response = await fetch('/api/state', { headers: { accept: 'application/json' } });
    if (!response.ok) throw new Error(`state ${response.status}`);
    const state = await response.json();

    if (Number(state.state_age_seconds ?? 0) > 30) renderStale(state.state_age_seconds);
    else clearStale();

    renderHeader(state);
    renderHero(state);
    renderRisk(state);
    renderAnalyst(state);
    renderSymbols(state);
    renderVetoes(state.vetoes || []);
    renderNews(state);
    renderPositions(state);
    renderEquity(state.equity || [], Number(state.risk?.starting_balance || 0));
    renderEquityTable(state.equity || [], Number(state.risk?.starting_balance || 0));
  } catch (error) {
    renderStale(999);
    $('conn-pill').dataset.status = 'critical';
    $('conn-text').textContent = 'panel offline';
  }

  try {
    const response = await fetch('/api/trades?limit=500');
    if (response.ok) {
      const data = await response.json();
      renderTrades(data.trades || []);
    }
  } catch (error) { /* the trades table simply keeps its last content */ }
}

/* Theme toggle: explicit choice wins over the OS setting, both ways. */
function initTheme() {
  const stored = localStorage.getItem('panel-theme');
  if (stored === 'light' || stored === 'dark') {
    document.documentElement.dataset.theme = stored;
  }
  $('theme-toggle').addEventListener('click', () => {
    const current = document.documentElement.dataset.theme;
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const effective = current === 'light' || current === 'dark'
      ? current : (prefersDark ? 'dark' : 'light');
    const next = effective === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('panel-theme', next);
  });
}

function initEquityTable() {
  const button = $('equity-table-toggle');
  const table = $('equity-table');
  button.addEventListener('click', () => {
    const open = table.hasAttribute('hidden');
    if (open) table.removeAttribute('hidden'); else table.setAttribute('hidden', '');
    button.setAttribute('aria-expanded', String(open));
    button.textContent = open ? 'Hide table' : 'Table view';
  });
}

initTheme();
initEquityTable();
tick();
setInterval(tick, REFRESH_MS);
