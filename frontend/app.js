/* ============================================================
   SolSnipBot — dashboard logic
   ============================================================ */
'use strict';

const state = {
  settings: {},
  runtime: {},
  positions: [],
  calls: [],
  logs: [],
  stats: {},
  feed: { rows: [] },      // live Dexscreener tracking (shared WS feed)
  fees: {},                // live cost of a round trip (amount + priority fee)
  rpc: {},                 // node health, socket state, fee-cap adequacy
  credentials: { items: [] },  // masked state of the managed secrets
  selected: null,        // address of the token shown in the chart panel
  tf: '1m',
  ws: null,
  chart: null,
  candleSeries: null,
  volumeSeries: null,
  liveCandle: null,      // current, still-forming candle fed by live ticks
  chartGap: 0,           // how far the live tick is from the charted series
  tg: { step: 'idle' },
  lastKline: { address: null, tf: null, at: 0 },
};

// Seconds per chart resolution: used to bucket a live tick into the candle it
// belongs to, so the chart keeps growing in real time between two kline pulls.
const TF_SECONDS = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600, '4h': 14400, '1d': 86400 };

/* ---------------------------------------------------------- formatters */
const nf = new Intl.NumberFormat('en-US');

function fmtUsd(n) {
  n = Number(n) || 0;
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a >= 1e9) return sign + '$' + (a / 1e9).toFixed(2) + 'B';
  if (a >= 1e6) return sign + '$' + (a / 1e6).toFixed(2) + 'M';
  if (a >= 1e4) return sign + '$' + (a / 1e3).toFixed(1) + 'K';
  if (a >= 1e3) return sign + '$' + a.toFixed(2).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  if (a >= 1) return sign + '$' + a.toFixed(2);
  return sign + '$' + a.toFixed(4);
}

function fmtPrice(p) {
  p = Number(p) || 0;
  if (p <= 0) return '$0';
  if (p >= 1000) return '$' + Math.round(p).toLocaleString('en-US');
  if (p >= 1) return '$' + p.toFixed(2);
  if (p >= 0.01) return '$' + p.toFixed(4);
  let s = p.toPrecision(3);
  if (s.includes('e')) s = p.toFixed(12);
  if (s.includes('.')) s = s.replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
  return '$' + s;
}

function fmtPct(n) {
  n = Number(n) || 0;
  return (n >= 0 ? '+' : '') + n.toFixed(2) + '%';
}

/* A ticker already starting with "$" (most pump.fun calls) must not become
 * "$$GTA6": the dollar sign is a prefix, not part of the symbol. */
function symLabel(sym) {
  const s = String(sym || '?').trim();
  return s.startsWith('$') ? s : '$' + s;
}

function shortAddr(a) {
  if (!a) return '—';
  const s = String(a);
  if (s.length <= 9) return s;
  // Every pump.fun contract ends in "pump", so the classic last-3 characters are
  // identical noise: two *different* tokens published under the same name both
  // read as "7ZY...ump" and "FTM...ump". Keep a longer head, and for those
  // contracts take the four characters *before* the constant suffix instead.
  const tail = s.endsWith('pump') ? s.slice(-8, -4) : s.slice(-3);
  return s.slice(0, 6) + '…' + tail;
}

function fmtDuration(ms) {
  const s = Math.max(0, Math.floor(ms / 1000));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + (m % 60) + 'm';
  return Math.floor(h / 24) + 'j';
}

function clockTime(ms) {
  const d = new Date(ms);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function clockTimeS(ms) {
  const d = new Date(ms);
  return clockTime(ms) + ':' + String(d.getSeconds()).padStart(2, '0');
}

function coinHtml(symbol, logo, cls) {
  cls = cls || '';
  if (logo) return `<div class="coin ${cls}"><img src="${logo}" alt="" onerror="this.parentNode.textContent='${(symbol || '?').slice(0, 2).toUpperCase()}'"></div>`;
  const hue = [...(symbol || 'X')].reduce((a, c) => a + c.charCodeAt(0), 0) % 360;
  return `<div class="coin ${cls}" style="background:linear-gradient(135deg,hsl(${hue} 55% 32%),hsl(${(hue + 40) % 360} 55% 20%))">${(symbol || '?').slice(0, 2).toUpperCase()}</div>`;
}

/* ---------------------------------------------------------- websocket */
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => { state.connected = true; setConnBadge(true); };
  ws.onclose = () => { state.connected = false; setConnBadge(false); setTimeout(connectWS, 1800); };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    handleEvent(msg);
  };
}

function handleEvent(msg) {
  switch (msg.type) {
    case 'snapshot':
      state.positions = msg.positions || [];
      state.calls = msg.calls || [];
      state.logs = msg.logs || [];
      state.stats = msg.stats || {};
      state.runtime = msg.runtime || {};
      state.settings = msg.settings || {};
      state.feed = msg.feed || { rows: [] };
      state.fees = msg.fees || {};
      if (!Array.isArray(state.feed.rows)) state.feed.rows = [];
      ensureSelection();
      renderAll();
      break;
    case 'prices':
      // One tick per tracked token, pushed by the backend's single Dexscreener
      // loop. Nothing here is simulated: only received prices are displayed.
      mergeTicks(msg.data || {}, msg.feed || {});
      break;
    case 'feed_status':
      state.feed = Object.assign({}, state.feed, msg.feed || {});
      renderFeed();
      break;
    case 'call':
      state.calls.unshift(msg.call);
      if (state.calls.length > 300) state.calls.pop();
      renderCalls(); renderActivity(); renderStats();
      if (!state.selected) { selectToken(msg.call.address); }
      break;
    case 'position_open':
      state.positions.unshift(msg.position);
      renderPositions(); renderAllPositions(); renderStats(); ensureSelection();
      break;
    case 'position_update':
    case 'position_close': {
      const p = msg.position;
      const idx = state.positions.findIndex(x => x.id === p.id);
      if (idx >= 0) state.positions[idx] = p; else state.positions.unshift(p);
      renderPositions(); renderAllPositions(); renderStats();
      const c = state.calls.find(x => x.address === p.address);
      if (c) { c.pnl_percent = p.pnl_percent; }
      break;
    }
    case 'log':
      state.logs.unshift(msg.log);
      if (state.logs.length > 500) state.logs.pop();
      renderLogs();
      if (msg.log.level === 'error') toast(msg.log.message, 'err');
      break;
    case 'settings':
      state.settings = msg.settings;
      if (msg.fees) state.fees = msg.fees;
      renderSettingsForms();
      renderConfigPanel();
      renderPaper();
      break;
    case 'credentials':
      // Only the mask travels: another tab sees that a key changed, never its
      // value.
      state.credentials = msg.credentials || state.credentials;
      renderSecrets();
      break;
    case 'status':
      refreshStatus();
      break;
  }
}

function setConnBadge(ok) {
  const el = document.getElementById('account-sub');
  if (!ok) { el.textContent = 'reconnexion…'; }
}

// The bot pushes a `status` event for every analysed call, and each one used to
// trigger a full /api/state poll. Coalesce them so a busy channel cannot turn
// into a request flood: at most one poll every 2.5s, plus a trailing poll so the
// very last update always lands.
const STATUS_MIN_GAP = 2500;
let _statusLast = 0;
let _statusTimer = null;

function refreshStatus() {
  const wait = STATUS_MIN_GAP - (Date.now() - _statusLast);
  if (wait <= 0) { _statusNow(); return; }
  if (_statusTimer) return;
  _statusTimer = setTimeout(() => { _statusTimer = null; _statusNow(); }, wait);
}

async function _statusNow() {
  _statusLast = Date.now();
  try {
    const r = await fetch('/api/state');
    const j = await r.json();
    state.runtime = j.runtime;
    state.stats = j.stats;
    state.positions = j.positions;
    state.calls = j.calls;
    if (j.feed && Array.isArray(j.feed.rows)) state.feed = j.feed;
    if (j.rpc) state.rpc = j.rpc;
    renderStatus(); renderStats(); renderPositions(); renderAllPositions();
    renderCalls(); renderActivity(); renderTradeBanner(); renderFeed();
    renderPaper(); renderRpcLine();
  } catch (e) {}
}

/* ---------------------------------------------------------- rendering */
function ensureSelection() {
  if (state.selected) return;
  const open = state.positions.find(p => p.status === 'open');
  if (open) { state.selected = open.address; return; }
  const call = state.calls.find(c => c.address);
  if (call) state.selected = call.address;
}

function renderAll() {
  renderChannels(); renderStatus(); renderStats(); renderPositions();
  renderAllPositions(); renderActivity(); renderCalls(); renderLogs();
  renderTokenDetails(); renderChart(); renderSettingsForms(); renderAbout();
  renderTradeBanner(); renderFeed(); renderPaper();
}

// Paper mode must never be ambiguous: a banner states the mode, and the virtual
// scoreboard is shown apart from the real statistics — the two are never mixed.
function renderPaper() {
  const on = !!(state.settings && state.settings.paper_trading);
  const banner = document.getElementById('paper-banner');
  if (banner) {
    banner.classList.toggle('is-hidden', !on);
    if (on) {
      const amount = Number(state.settings.trade_amount_sol) || 0;
      const feeds = state.settings.price_feed_enabled ? 'prix Dexscreener réels' : 'prix Dexscreener';
      document.getElementById('paper-banner-text').textContent =
        `Les appels Telegram, l'analyse GMGN, les ${feeds} et le devis/route Jupiter ` +
        `sont réels — mais aucune transaction n'est signée ni diffusée. ` +
        `Chaque achat virtuel de ${amount} SOL est pris au prix réellement exécutable ` +
        `(impact de prix inclus) ; sa route est validée par une simulation quand le solde ` +
        `du wallet peut la couvrir, et marquée « route non validée » sinon. Les frais sont ` +
        `comptés au barème du critère « frais » (base ×3 + priority ×2). « Trading réel » ` +
        `est ignoré tant que le mode papier est actif.`;
    }
  }
  const block = document.getElementById('paper-stats');
  if (!block) return;
  const ps = (state.stats && state.stats.paper) || null;
  const has = !!ps && (ps.trades > 0 || ps.open_count > 0 || ps.sniped > 0);
  block.classList.toggle('is-hidden', !(on && has));
  if (!ps || !on) return;
  const pnl = Number(ps.total_pnl) || 0;
  const el = document.getElementById('ps-pnl');
  el.textContent = (pnl >= 0 ? '+' : '-') + fmtUsd(Math.abs(pnl));
  el.className = 'ps-value ' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '');
  document.getElementById('ps-trades').textContent = ps.trades || 0;
  document.getElementById('ps-winrate').textContent =
    (Number(ps.win_rate) || 0).toFixed(1).replace(/\.0$/, '') + '%';
  document.getElementById('ps-open').textContent = ps.open_count || 0;
  document.getElementById('ps-calls').textContent = `${ps.sniped || 0} / ${ps.rejected || 0}`;
}

/* ------------------------------------------------- suivi de prix en direct */
// The backend polls Dexscreener once for everything it watches and pushes a
// `prices` event per tick. The dashboard only ever displays what it received:
// a token without a fresh tick keeps showing its last price *with its age*, so
// a stalled feed can never look like a calm market.

function sparkSvg(points) {
  const pts = (points || []).map(Number).filter(v => v > 0);
  if (pts.length < 2) return '<span class="feed-dim">—</span>';
  const w = 68, h = 18;
  const min = Math.min(...pts), max = Math.max(...pts);
  const span = (max - min) || 1;
  const line = pts.map((v, i) =>
    `${((i / (pts.length - 1)) * w).toFixed(1)},${(h - ((v - min) / span) * (h - 2) - 1).toFixed(1)}`
  ).join(' ');
  const dir = pts[pts.length - 1] > pts[0] ? 'up' : pts[pts.length - 1] < pts[0] ? 'down' : 'flat';
  return `<svg class="spark spark--${dir}" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" preserveAspectRatio="none"><polyline points="${line}"></polyline></svg>`;
}

const FEED_KINDS = { position: 'position', select: 'sélection', watch: 'suivi', call: 'call' };

function renderFeed() {
  const body = document.getElementById('feed-body');
  if (!body) return;
  const f = state.feed || {};
  const rows = Array.isArray(f.rows) ? f.rows : [];
  const now = Date.now();
  const interval = Number(f.interval_s) || 2;

  const empty = document.getElementById('feed-empty');
  if (empty) empty.classList.toggle('is-hidden', rows.length > 0);

  const live = document.getElementById('feed-live');
  if (live) {
    const off = f.enabled === false;
    const stale = !f.last_ok_ms || (now - f.last_ok_ms) > Math.max(8000, interval * 5000);
    live.className = 'tag-live' + (off ? ' tag-live--off' : stale ? ' tag-live--warn' : '');
    live.textContent = off ? 'désactivé'
      : f.backoff_s ? `pause ${f.backoff_s} s`
      : !f.last_ok_ms ? 'en attente'
      : stale ? 'flux en retard'
      : `actif · ${interval.toFixed(interval < 1 ? 1 : 0)} s`;
  }
  const dot = document.getElementById('feed-dot');
  if (dot) dot.style.animationPlayState = f.enabled === false ? 'paused' : 'running';

  const stat = document.getElementById('feed-stat');
  if (stat) {
    const age = f.last_ok_ms ? Math.max(0, Math.round((now - f.last_ok_ms) / 1000)) : null;
    stat.textContent = `${rows.length} token${rows.length > 1 ? 's' : ''} · ${f.requests || 0} requêtes`
      + (age === null ? '' : ` · dernier tick il y a ${age} s`)
      + (f.latency_ms ? ` · ${f.latency_ms} ms` : '')
      + (f.last_error && f.errors ? ` · ${f.errors} erreur(s)` : '');
  }

  body.innerHTML = rows.map(r => {
    const price = Number(r.price_usd) || 0;
    const chg = Number(r.change_m5);
    const kinds = (r.kinds || []).map(k => `<span class="feed-kind feed-kind--${k}">${FEED_KINDS[k] || k}</span>`).join('');
    // The one token on screen: say when its Dexscreener tick sits too far from
    // the GMGN candles to be drawn (a fake wick would be invented otherwise).
    const gap = (r.address === state.selected && state.chartGap)
      ? `<span class="feed-kind feed-kind--gap" title="Le prix Dexscreener s'écarte de x${state.chartGap} du dernier chandelier GMGN : ce tick n'est pas dessiné sur le graphique (les deux sources sont indépendantes).">graphique x${state.chartGap}</span>`
      : '';
    const ageS = r.ts ? Math.max(0, Math.round((now - r.ts) / 1000)) : null;
    const staleTick = ageS === null ? null : ageS > Math.max(8, interval * 4);
    // A token Dexscreener has not indexed yet (a fresh launchpad token) has a
    // price of its own nowhere: it is reported as such, never invented.
    const misses = Number(r.errors) || 0;
    const tickCell = ageS === null
      ? `<span class="feed-dim" title="Dexscreener ne publie pas encore de paire pour ce token (lancement récent) : aucun prix n'est affiché, et aucun n'est inventé.${misses ? ' ' + misses + ' tentative(s) sans prix.' : ''}">aucun tick</span>`
      : `<span class="${staleTick ? 'feed-stale' : 'feed-dim'}" title="${staleTick ? 'Aucun tick reçu depuis plus de ' + Math.round(Math.max(8, interval * 4)) + ' s' : 'Dernier tick reçu'}">il y a ${ageS} s</span>`;
    return `<tr data-select="${r.address}">
      <td>
        <div class="token-cell">
          ${coinHtml(r.symbol, '')}
          <div>
            <div class="token-name">${escapeHtml(symLabel(r.symbol))}</div>
            <div class="token-addr" title="${r.address}">${shortAddr(r.address)}</div>
          </div>
        </div>
        <div style="margin-top:5px">${kinds}${gap}</div>
      </td>
      <td class="num">${price ? fmtPrice(price) : '<span class="feed-dim">—</span>'}</td>
      <td class="num ${chg >= 0 ? 'pos' : 'neg'}">${isFinite(chg) && chg ? fmtPct(chg) : '<span class="feed-dim">—</span>'}</td>
      <td class="num">${r.liquidity_usd ? fmtUsd(r.liquidity_usd) : '<span class="feed-dim">—</span>'}</td>
      <td class="ta-c">${sparkSvg(r.spark)}</td>
      <td class="ta-r">${tickCell}</td>
      <td class="ta-r"><button class="btn-xs" data-feed-untrack="${r.address}" title="Ne plus suivre ce token">Retirer</button></td>
    </tr>`;
  }).join('');
}

// Merge a batch of ticks into the feed rows (creating rows for tokens the
// snapshot did not know yet) and refresh everything that shows a price.
function mergeTicks(data, counters) {
  state.feed = Object.assign({}, state.feed, counters || {});
  const rows = state.feed.rows || (state.feed.rows = []);
  Object.keys(data).forEach(addr => {
    const t = data[addr];
    let row = rows.find(r => r.address === addr);
    if (!row) {
      row = { address: addr, kinds: ['watch'], spark: [] };
      rows.push(row);
    }
    Object.assign(row, t, { age_ms: 0 });
    row.spark = (row.spark || []).concat([Number(t.price_usd) || 0]).slice(-40);
  });
  applyLivePrice(data);
  renderFeed();
}

// Feed the selected token's still-forming candle with the live tick, so the
// chart moves between two kline pulls instead of waiting for the next refresh.
//
// The candles come from GMGN and the ticks from Dexscreener: two independent
// sources that can disagree by a large factor on a brand new token (measured:
// ×5,7 on one call). A tick far outside the range the candle already shows is
// therefore NOT drawn — that would paint a wick no trade produced — it is
// reported instead, so the disagreement is visible rather than silently drawn.
function applyLivePrice(data) {
  const addr = state.selected;
  if (!addr || !data[addr] || !state.candleSeries) return;
  const price = Number(data[addr].price_usd) || 0;
  if (price <= 0) return;

  const secs = TF_SECONDS[state.tf] || 60;
  const bucket = Math.floor(Date.now() / 1000 / secs) * secs;
  const c = state.liveCandle;
  const high = c ? Math.max(c.high || 0, c.close || 0) : 0;
  const low = c ? Math.min(c.low || Infinity, c.close || Infinity) : 0;
  const within = !!c && price <= high * 1.35 && price >= low * 0.65;

  if (within) {
    if (c.time !== bucket) {
      state.liveCandle = { time: bucket, open: price, high: price, low: price, close: price };
    } else {
      c.high = Math.max(c.high, price);
      c.low = Math.min(c.low, price);
      c.close = price;
    }
    state.chartGap = 0;
    try { state.candleSeries.update(state.liveCandle); } catch (e) {}
  } else if (c) {
    const ref = c.close || c.open || 0;
    state.chartGap = ref > 0 ? Number((price / ref).toFixed(2)) : 0;
  }

  const priceEl = document.getElementById('chart-price');
  if (priceEl) priceEl.textContent = fmtPrice(price);
  const mc = document.getElementById('chart-mc');
  if (mc && data[addr].market_cap) mc.textContent = fmtUsd(data[addr].market_cap);
  const liq = document.getElementById('chart-liq');
  if (liq && data[addr].liquidity_usd) liq.textContent = fmtUsd(data[addr].liquidity_usd);
}

async function feedTrack(address, kind, action) {
  try {
    const r = await fetch('/api/feed/track', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ address, kind, action }),
    });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || 'Adresse invalide', 'err'); return false; }
    return true;
  } catch (e) { toast('Erreur réseau', 'err'); return false; }
}


/* Channel links exactly as they appear in the source Telegram messages. */
const CHANNEL_LABELS = { gmgnsignals: 'gmgnsignals/4437230' };

function renderChannels() {
  const chips = document.getElementById('channel-chips');
  const channels = state.settings.channels || [];
  const resolved = state.runtime.telegram_channels || [];
  document.getElementById('channel-count').textContent = channels.length;
  chips.innerHTML = channels.map(c => {
    const live = resolved.includes(c);
    const label = CHANNEL_LABELS[c] || c;
    return `<span class="chip"><span class="chip-dot" style="${live ? '' : 'background:#5c6674'}"></span>
      <svg viewBox="0 0 24 24"><path d="M21.5 4.3 18.4 19c-.2 1-.9 1.3-1.7.8l-4.6-3.4-2.2 2.1c-.24.24-.44.44-.9.44l.32-4.6L18 6.9c.36-.32-.08-.5-.56-.18L7.1 13.6l-4.5-1.4c-1-.3-1-1 .2-1.5L20 4.1c.8-.3 1.5.2 1.5.2z"/></svg>
      t.me/${label}</span>`;
  }).join('');
}

function renderTradeBanner() {
  const el = document.getElementById('trade-banner');
  if (!el) return;
  const rt = state.runtime || {};
  const blocked = !!rt.trading_blocked;
  el.classList.toggle('is-hidden', !blocked);
  if (!blocked) return;
  // GMGN 403 PLAN_UPGRADE_REQUIRED: deterministic, actionable, not a bot bug.
  const text = String(rt.trading_blocked_reason || 'Plan GMGN insuffisant pour trader via l\'API.')
    .replace(/\s*https?:\/\/\S+\s*$/, '');
  document.getElementById('trade-banner-text').textContent = text;
  const link = document.getElementById('trade-banner-link');
  link.href = rt.trading_blocked_url || 'https://gmgn.ai/ai?chain=sol&tab=paid_plans';
}

function renderStatus() {
  const rt = state.runtime || {};
  const tg = rt.telegram || 'disconnected';
  const titleEl = document.getElementById('bot-status-title');
  const subEl = document.getElementById('bot-status-sub');
  const dot = document.getElementById('bot-dot');
  const card = dot.closest('.status-card');

  const labels = {
    listening: ['Bot Running', `Listening to ${(rt.telegram_channels || []).length} Telegram channel(s)…`],
    waiting: ['Bot en veille', 'En attente de la connexion Telegram'],
    need_login: ['Connexion requise', 'Connectez Telegram dans Settings'],
    awaiting_code: ['Code requis', 'Saisissez le code Telegram'],
    awaiting_password: ['Mot de passe requis', 'Validation 2FA en attente'],
    error: ['Erreur Telegram', 'Voir les logs'],
    disconnected: ['Bot arrêté', 'Telegram déconnecté'],
  };
  const [t, s] = labels[tg] || labels.disconnected;
  titleEl.textContent = t; subEl.textContent = s;

  card.classList.toggle('status-card--warn', ['need_login', 'awaiting_code', 'awaiting_password', 'waiting'].includes(tg));
  card.classList.toggle('status-card--off', ['error', 'disconnected'].includes(tg));

  document.getElementById('account-phone').textContent = rt.telegram_phone || '—';
  document.getElementById('account-sub').textContent = rt.telegram_user
    ? `${rt.telegram_user} · ${tg === 'listening' ? 'connecté' : tg.replace('_', ' ')}`
    : (tg === 'listening' ? 'connecté' : (state.connected ? tg.replace('_', ' ') : 'hors ligne'));

  const drift = rt.clock_drift;
  document.getElementById('chain-status').textContent = drift !== undefined && Math.abs(drift) > 4
    ? `Connected · skew ${drift > 0 ? '+' : ''}${drift}s` : 'Connected';
}

function renderStats() {
  const rt = state.runtime || {};
  const st = state.stats || {};
  const solPrice = Number(rt.sol_price) || 0;
  const balance = Number(rt.wallet_balance_sol) || 0;

  const pnl = Number(st.total_pnl) || 0;
  const openValue = (state.positions || []).filter(p => p.status === 'open')
    .reduce((a, p) => a + (Number(p.tokens) || 0) * (Number(p.current_price) || 0), 0);
  const capital = balance * solPrice + openValue;
  const baseline = capital - pnl;
  const pnlPct = baseline > 0 ? (pnl / baseline) * 100 : 0;

  const pnlEl = document.getElementById('stat-pnl');
  pnlEl.textContent = (pnl >= 0 ? '+' : '-') + fmtUsd(Math.abs(pnl));
  pnlEl.className = 'stat-value ' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '');
  const pctEl = document.getElementById('stat-pnl-pct');
  pctEl.textContent = fmtPct(pnlPct);
  pctEl.className = 'stat-sub ' + (pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '');

  document.getElementById('stat-trades').textContent = st.trades || 0;
  document.getElementById('stat-trades-sub').textContent = `(${st.wins || 0} succès / ${st.losses || 0} échecs)`;
  document.getElementById('stat-winrate').textContent = (st.win_rate || 0).toFixed(1).replace(/\.0$/, '') + '%';
  document.getElementById('stat-capital').textContent = fmtUsd(capital);
  document.getElementById('stat-balance').textContent = balance.toFixed(3) + ' SOL';
  document.getElementById('stat-balance-usd').textContent = '≈ ' + fmtUsd(balance * solPrice);
}

function renderPositions() {
  const body = document.getElementById('positions-body');
  const open = (state.positions || []).filter(p => p.status === 'open');
  document.getElementById('open-count').textContent = open.length;
  document.getElementById('positions-empty').classList.toggle('is-hidden', open.length > 0);
  const now = Date.now();
  body.innerHTML = open.slice(0, 8).map(p => {
    const up = (p.pnl_percent || 0) >= 0;
    const rk = exitRecheck(p);
    return `<tr data-select="${p.address}">
      <td><div class="token-cell">${coinHtml(p.symbol, p.logo)}
        <div><div class="token-name">${symLabel(p.symbol)}${p.paper ? ` <span class="badge badge--paper" title="Position papier : aucun ordre réel n'a été envoyé.${p.paper_funding_only ? ' Attention : la simulation a été refusée faute de SOL, le prix retenu est celui du devis (exécution non validée).' : ''}">PAPIER</span>` : ''}${p.no_route_failures ? ` <span class="badge badge--warn" title="Jupiter ne trouve plus aucune route de vente pour ce token (pool vidé). Si cela persiste, la position papier sera soldée au dernier prix connu — un prix non exécutable, signalé comme tel.">aucune route ×${p.no_route_failures}</span>` : ''}</div><div class="token-addr" title="${p.address}">${shortAddr(p.address)}</div></div></div></td>
      <td class="num">${fmtPrice(p.entry_price)}</td>
      <td class="num">${fmtPrice(p.current_price)}</td>
      <td class="num ${up ? 'pos' : 'neg'}">${up ? '+' : '-'}${fmtUsd(Math.abs(p.pnl_usd || 0))}</td>
      <td class="num ${up ? 'pos' : 'neg'}">${fmtPct(p.pnl_percent || 0)}${rk ? `<div class="pnl-exec"><span class="badge badge--warn" title="${rk.title}">${rk.text}</span>${rk.exec_pct === null ? '' : ` exéc. ${fmtPct(rk.exec_pct)}`}</div>` : ''}</td>
      <td class="num" style="color:var(--muted)">${fmtDuration(now - p.opened_at)}</td>
      <td><div class="row-actions">
        <button class="btn-xs" data-tp="${p.id}">TP/SL</button>
        <button class="icon-btn" data-close="${p.id}" title="Fermer la position — confirmation, vente partielle possible (25 / 50 / 100 %)">
          <svg viewBox="0 0 24 24" width="15" height="15"><path d="M6 6l12 12M18 6 6 18"/></svg></button>
      </div></td>
    </tr>`;
  }).join('');

  // Le panneau montre les 8 plus récentes : le dire, plutôt que de laisser croire
  // que les autres n'existent pas. Toutes sont dans l'onglet Positions, chacune
  // avec son bouton « Fermer ».
  const more = document.getElementById('positions-more');
  if (more) {
    const reste = open.length - 8;
    more.classList.toggle('is-hidden', reste <= 0);
    if (reste > 0) {
      more.innerHTML = `+ ${reste} autre${reste > 1 ? 's' : ''} position${reste > 1 ? 's' : ''} `
        + `ouverte${reste > 1 ? 's' : ''} — <b>toutes</b> sont dans l'onglet <b>Positions</b>, `
        + `chacune avec son bouton « Fermer ».`;
    }
  }
}

// The row shows the Dexscreener PnL. Before executing an exit the bot asks a real
// Jupiter quote for the amount held; when the market does not pay what the
// display says, a take-profit is cancelled and a protective stop is recalculated.
// Without this the row showed "+39 %" and no sale, which reads as a broken
// take-profit instead of a price nobody was paying — so say it, and show the PnL
// at the executable price next to the displayed one.
const EXIT_RECHECK_NAMES = {
  take_profit: 'TP atteint, non exécutable',
  stop_loss: 'SL non exécutable',
  trailing_stop: 'Trailing non exécutable',
};

function exitRecheck(p) {
  const c = p.exit_recheck;
  if (!c || !c.display || !c.exec) return null;
  const gap = (c.exec / c.display - 1) * 100;
  const entry = Number(p.entry_price || 0);
  const exec_pct = entry ? (c.exec / entry - 1) * 100 : null;
  return {
    text: EXIT_RECHECK_NAMES[c.reason] || `sortie « ${c.reason} » non exécutable`,
    exec_pct,
    title: `Un devis de vente réel a été demandé avant d'exécuter « ${c.reason} » : `
      + `prix affiché ${fmtPrice(c.display)}, prix exécutable (ce que le marché paie) `
      + `${fmtPrice(c.exec)}, soit ${gap >= 0 ? '+' : ''}${gap.toFixed(1)} %. `
      + `Règle appliquée : ${c.real ? `« ${c.real} »` : 'aucune, la sortie est annulée'}.`
      + (exec_pct === null ? '' : ` PnL au prix exécutable : ${fmtPct(exec_pct)}.`),
  };
}

// A rule name can contradict the money that was made: a take-profit is only// named that way because of the price it saw, and fees (or a fill that came in
// lower) can still leave the trade at a loss. Say so instead of letting the
// label lie.
function exitOutcomeFlag(p) {
  const reason = p.exit_reason || '';
  const net = Number(p.pnl_percent || 0);
  if (reason === 'take_profit' && net <= 0) {
    return { text: 'net négatif', title: "Take Profit déclenché, mais résultat net négatif tous frais inclus : les frais de transaction ont dépassé le gain, ou l'exécution a été moins bonne que le prix affiché. Un Take Profit n'est désormais honoré que si le prix exécutable est au niveau demandé." };
  }
  if ((reason === 'stop_loss' || reason === 'trailing_stop') && net > 0) {
    return { text: 'net positif', title: "Sortie protectrice déclenchée, mais le prix s'est rétabli avant l'exécution : résultat net positif, tous frais inclus." };
  }
  return null;
}

function renderAllPositions() {
  const body = document.getElementById('positions-all');
  const list = state.positions || [];
  document.getElementById('positions-all-empty').classList.toggle('is-hidden', list.length > 0);
  const now = Date.now();
  body.innerHTML = list.map(p => {
    const up = (p.pnl_percent || 0) >= 0;
    const dur = fmtDuration((p.closed_at || now) - p.opened_at);
    const flag = exitOutcomeFlag(p);
    const rk = exitRecheck(p);
    const fees = Number(p.fees_sol || 0);
    const pnlTitle = p.status === 'closed'
      ? `Réalisé net de tous les frais de transaction (réseau + priorité)${fees ? ` : ${fees.toFixed(9)} SOL` : ''}. Loyer remboursé exclu.`
      : '';
    return `<tr data-select="${p.address}">
      <td><div class="token-cell">${coinHtml(p.symbol, p.logo)}
        <div><div class="token-name">${symLabel(p.symbol)}</div><div class="token-addr" title="${p.address}">${shortAddr(p.address)}</div></div></div></td>
      <td class="num">${fmtPrice(p.entry_price)}</td>
      <td class="num">${fmtPrice(p.status === 'closed' && p.exit_price ? p.exit_price : p.current_price)}</td>
      <td class="num">${(p.amount_sol || 0)} SOL</td>
      <td class="num ${up ? 'pos' : 'neg'}"${pnlTitle ? ` title="${pnlTitle}"` : ''}>${up ? '+' : '-'}${fmtUsd(Math.abs(p.pnl_usd || 0))} <span style="opacity:.65">${fmtPct(p.pnl_percent || 0)}</span>${rk && rk.exec_pct !== null ? `<div class="pnl-exec" title="${rk.title}">exéc. ${fmtPct(rk.exec_pct)}</div>` : ''}</td>
      <td><span class="badge ${p.status === 'open' ? 'badge--open' : 'badge--closed'}"${p.status === 'open' ? '' : ` title="Trade terminé : le PnL affiché est le réalisé net de frais, figé à la clôture."`}>${p.status === 'open' ? 'Ouverte' : `Fermée · ${p.exit_reason || 'clôturée'}`}</span>${p.paper ? ` <span class="badge badge--paper" title="Position papier : aucune transaction n'a été envoyée. Entrée au prix exécutable d'un devis Jupiter réel${p.paper_price_impact ? ` (impact ${(p.paper_price_impact * 100).toFixed(2)} %)` : ''}, frais modélisés.${p.paper_funding_only ? ' La simulation a été refusée faute de SOL : le prix retenu est celui du devis, l\'exécution n\'a pas pu être validée.' : ''}">PAPIER</span>` : ''}${p.exit_unpriced ? ` <span class="badge badge--warn" title="Plus aucune route de vente n'existait (le pool ne paie plus rien) : la position a été soldée au dernier prix Dexscreener, qui n'était PAS exécutable. Le PnL est une estimation de ce que le sac valait, pas le produit d'une vente — aucune transaction n'a eu lieu.">prix non exécutable</span>` : ''}${flag ? ` <span class="badge badge--warn" title="${flag.title}">${flag.text}</span>` : ''}${rk ? ` <span class="badge badge--warn" title="${rk.title}">${rk.text}</span>` : ''}${p.recovered ? ' <span class="badge badge--open" title="Token détenu sans position : adopté au prix courant, TP/SL appliqués">Récupérée</span>' : ''}</td>
      <td class="num" style="color:var(--muted)">${dur}</td>
      <td><div class="row-actions">${p.status === 'open'
        ? `<button class="btn-xs danger" data-close="${p.id}" title="Fermer la position — confirmation, vente partielle possible (25 / 50 / 100 %)">Fermer</button>` : ''}</div></td>
    </tr>`;
  }).join('');
}

function renderActivity() {
  const body = document.getElementById('activity-body');
  body.innerHTML = (state.calls || []).slice(0, 6).map(c => {
    const sniped = c.decision === 'sniped';
    const detail = sniped
      ? `MC: ${fmtUsd(c.market_cap)} | Top10: ${(c.top10_pct || 0).toFixed(1)}% | Âge: ${(c.age_minutes || 0).toFixed(0)}m${c.pnl_percent !== undefined ? ' | PnL: ' + fmtPct(c.pnl_percent) : ''}`
      : (c.reasons && c.reasons[0] ? c.reasons[0] : 'critères non remplis');
    return `<tr data-select="${c.address}">
      <td class="num" style="color:var(--muted)">${clockTimeS(c.ts)}</td>
      <td><div class="token-cell">${coinHtml(c.symbol, c.logo)}<div class="token-name">${symLabel(c.symbol)}</div></div></td>
      <td><span class="badge ${sniped ? 'badge--sniped' : 'badge--rejected'}">${sniped ? 'Sniped' : 'Rejected'}</span></td>
      <td style="color:var(--muted);max-width:340px;overflow:hidden;text-overflow:ellipsis">${detail}</td>
    </tr>`;
  }).join('');
}

function renderCalls() {
  const feed = document.getElementById('calls-feed');
  feed.innerHTML = (state.calls || []).slice(0, 22).map(c => {
    const sniped = c.decision === 'sniped';
    const sel = c.address === state.selected ? ' selected' : '';
    return `<div class="call${sel}" data-select="${c.address}">
      ${coinHtml(c.symbol, c.logo)}
      <div class="call-main">
        <div class="call-top">
          <span class="call-sym">${symLabel(c.symbol)}</span>
          <span class="call-badge badge ${sniped ? 'badge--sniped' : 'badge--rejected'}">${sniped ? 'Sniped' : 'Rejected'}</span>
          <span class="call-time">${clockTime(c.ts)}</span>
        </div>
        <div class="call-addr">${shortAddr(c.address)}</div>
        <div class="call-metrics">
          <span>MarketCap: <b>${fmtUsd(c.market_cap)}</b></span>
          <span>Age: <b>${(c.age_minutes || 0).toFixed(0)}m</b></span>
          <span>Top10: <b>${(c.top10_pct || 0).toFixed(0)}%</b></span>
        </div>
      </div>
    </div>`;
  }).join('');
  if (!state.calls.length) feed.innerHTML = '<div class="empty">En attente de calls Telegram…</div>';
}

function renderLogs() {
  const el = document.getElementById('log-list');
  el.innerHTML = (state.logs || []).slice(0, 300).map(l => {
    const lv = l.level === 'success' ? 'lv-success' : l.level === 'warn' ? 'lv-warn' : l.level === 'error' ? 'lv-error' : '';
    return `<div class="log-item ${lv}"><span class="log-time">${clockTimeS(l.ts)}</span><span class="log-msg">${l.message}</span></div>`;
  }).join('');
}

function renderAbout() {
  const rt = state.runtime || {};
  const rows = [
    ['GMGN API', rt.gmgn === 'ok' ? '<span class="pill-ok">connecté</span>' : '<span class="pill-no">indisponible</span>'],
    ['Dexscreener', '<span class="pill-ok">connecté</span>'],
    ['Wallet Solana', rt.wallet ? `<span class="mono">${rt.wallet}</span>` : '—'],
    ['Dérive horloge', (rt.clock_drift !== undefined ? (rt.clock_drift > 0 ? '+' : '') + rt.clock_drift + 's' : '—')],
    ['Telegram', rt.telegram || '—'],
    ['Canaux suivis', (rt.telegram_channels || []).length + ' / ' + (state.settings.channels || []).length],
    ['Prix SOL', fmtUsd(rt.sol_price)],
  ];
  document.getElementById('about-system').innerHTML = rows.map(([k, v]) =>
    `<div class="kv-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('');
}

/* ---------------------------------------------------- token details */
function currentTokenData() {
  const addr = state.selected;
  if (!addr) return null;
  const call = (state.calls || []).find(c => c.address === addr);
  const pos = (state.positions || []).find(p => p.address === addr);
  return { addr, call, pos };
}

function renderTokenDetails() {
  const data = currentTokenData();
  const el = document.getElementById('token-details');
  if (!data || !data.call) {
    el.innerHTML = '<div class="empty">Sélectionnez un token</div>';
    return;
  }
  const c = data.call, p = data.pos;
  const mc = p && p.market_cap ? p.market_cap : c.market_cap;
  const vol = p && p.volume_24h ? p.volume_24h : c.volume_24h;
  const liq = p && p.liquidity ? p.liquidity : c.liquidity;
  const socials = (c.socials || []).map(s => socialIcon(s, c)).join('') || '<span class="pill-no">aucun</span>';
  const rows = [
    ['Nom', escapeHtml(c.name || '—')],
    ['Symbole', escapeHtml(c.symbol || '—')],
    ['Adresse', `<span class="mono">${shortAddr(c.address)}</span><button class="copy" data-copy="${c.address}"><svg viewBox="0 0 24 24" width="12" height="12"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5h10"/></svg></button>`],
    ['MarketCap', fmtUsd(mc)],
    ['MarketCap ATH', c.ath_market_cap ? fmtUsd(c.ath_market_cap) : '—'],
    ['Volume (24h)', fmtUsd(vol)],
    ['Liquidité', fmtUsd(liq)],
    ['Top 10 Holders', (c.top10_pct || 0).toFixed(1) + '%'],
    ['Acheteurs / Vendeurs', `${nf.format(c.buyers || 0)} / ${nf.format(c.sellers || 0)}`],
    ['Âge du Token', (c.age_minutes || 0).toFixed(0) + 'm'],
    ['Site Web', c.has_website ? '<span class="pill-ok">Oui</span>' : '<span class="pill-no">Non</span>'],
    ['Réseaux sociaux', socials],
    ['KOLs', c.kols > 0 ? `<span class="pill-ok">Oui · ${c.kols}</span>` : '<span class="pill-no">Non</span>'],
    ['Smart Wallets', c.smart_wallets > 0 ? `<span class="pill-ok">Oui · ${c.smart_wallets}</span>` : '<span class="pill-no">Non</span>'],
    ...(c.prefiltered ? [['Décision', 'Pré-filtre alerte (0 requête GMGN)']] : []),
  ];
  el.innerHTML = rows.map(([k, v]) => `<div class="kv-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join('') + checksBlock(c);
}

/* Compact pass/fail breakdown of every snipe criterion for this token. */
function checksBlock(c) {
  const checks = c.checks || [];
  if (!checks.length) return '';
  const pass = checks.filter(x => x.passed).length;
  return `<div class="checks">
    <div class="checks-head">
      <span>Critères de snipe</span>
      <span class="checks-score ${pass === checks.length ? 'ok' : 'ko'}">${pass}/${checks.length}</span>
    </div>
    ${checks.map(x => `<div class="check ${x.passed ? 'ok' : 'ko'}">
      <span class="check-dot"></span>
      <span class="check-label">${escapeHtml(x.label)}</span>
      <span class="check-detail">${escapeHtml(x.detail || '')}</span>
    </div>`).join('')}
  </div>`;
}

function socialIcon(s, c) {
  const map = {
    twitter_username: ['X', c.twitter ? (c.twitter.startsWith('http') ? c.twitter : 'https://x.com/' + c.twitter) : ''],
    telegram: ['Telegram', c.telegram ? (c.telegram.startsWith('http') ? c.telegram : 'https://t.me/' + c.telegram) : ''],
    website: ['Site', c.website],
    discord: ['Discord', '#'],
  };
  const [label, href] = map[s] || [s, '#'];
  return `<a href="${href || '#'}" target="_blank" rel="noopener" title="${label}">${label === 'X' ? '<svg viewBox="0 0 24 24"><path d="M4 4l16 16M20 4 4 20"/></svg>' : label === 'Telegram' ? '<svg viewBox="0 0 24 24"><path d="M21.5 4.3 18.4 19c-.2 1-.9 1.3-1.7.8l-4.6-3.4-2.2 2.1c-.24.24-.44.44-.9.44l.32-4.6L18 6.9"/></svg>' : '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3.5 3 14 0 18M12 3c-3 3.5-3 14 0 18"/></svg>'}</a>`;
}

function escapeHtml(s) { return String(s).replace(/[&<>"]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m])); }

/* ---------------------------------------------------------- chart */
// Lightweight-Charts v4 renders intraday axis labels in UTC; format them in
// the visitor's local timezone so the chart matches the wall clock.
function localHm(ts) {
  const d = new Date(ts * 1000);
  return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
}
function localTick(ts, type) {
  const d = new Date(ts * 1000);
  const T = (window.LightweightCharts && window.LightweightCharts.TickMarkType) || { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 };
  if (type === T.Year) return String(d.getFullYear());
  if (type === T.Month) return d.toLocaleDateString('fr-FR', { month: 'short' });
  if (type === T.DayOfMonth) return String(d.getDate());
  const hm = localHm(ts);
  return type === T.TimeWithSeconds ? hm + ':' + String(d.getSeconds()).padStart(2, '0') : hm;
}

function initChart() {
  const el = document.getElementById('chart');
  if (!el || !window.LightweightCharts) return;
  el.innerHTML = '';
  const chart = LightweightCharts.createChart(el, {
    layout: { background: { color: 'transparent' }, textColor: '#79838f', fontSize: 11, fontFamily: 'Inter' },
    localization: { locale: 'fr-FR', timeFormatter: localHm },
    grid: { vertLines: { color: 'rgba(26,33,48,.5)' }, horzLines: { color: 'rgba(26,33,48,.5)' } },
    rightPriceScale: { borderColor: '#1a2130' },
    timeScale: {
      borderColor: '#1a2130', timeVisible: true, secondsVisible: false,
      tickMarkFormatter: (time, type) => localTick(time, type),
    },
    crosshair: { mode: 0, vertLine: { color: '#2a3a4d', labelBackgroundColor: '#1a2432' }, horzLine: { color: '#2a3a4d', labelBackgroundColor: '#1a2432' } },
    handleScale: { axisPressedMouseMove: true },
    autoSize: true,
  });
  const candles = chart.addCandlestickSeries({
    upColor: '#23e36b', downColor: '#ff5364', borderUpColor: '#23e36b', borderDownColor: '#ff5364',
    wickUpColor: '#23e36b', wickDownColor: '#ff5364',
  });
  const volume = chart.addHistogramSeries({ priceFormat: { type: 'volume' }, priceScaleId: '', color: 'rgba(35,227,107,.35)' });
  volume.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  state.chart = chart; state.candleSeries = candles; state.volumeSeries = volume;
  window.addEventListener('resize', () => chart.applyOptions({}));
}

async function renderChart(force) {
  const data = currentTokenData();
  const addr = data ? data.addr : null;
  const coinWrap = document.getElementById('chart-coin-wrap');
  const symEl = document.getElementById('chart-symbol');
  const addrEl = document.getElementById('chart-addr-text');
  const priceEl = document.getElementById('chart-price');
  const changeEl = document.getElementById('chart-change');
  const copyBtn = document.getElementById('chart-copy');

  if (!addr) { symEl.textContent = '$—'; addrEl.textContent = '—'; priceEl.textContent = '$0'; return; }
  const c = data.call || {};
  const p = data.pos || {};
  coinWrap.innerHTML = coinHtml(c.symbol, c.logo, 'coin--lg');
  symEl.textContent = '$' + (c.symbol || '—');
  addrEl.textContent = shortAddr(addr);
  copyBtn.dataset.copy = addr;

  const price = (p.current_price || c.price_usd || 0);
  priceEl.textContent = fmtPrice(price);
  const pct = c.pnl_percent !== undefined && c.decision === 'sniped' ? c.pnl_percent : null;
  const change = pct !== null ? pct : (c.change_h24 || 0);
  changeEl.textContent = fmtPct(change);
  changeEl.style.color = change >= 0 ? 'var(--green)' : 'var(--red)';

  document.getElementById('chart-mc').textContent = fmtUsd(p.market_cap || c.market_cap);
  document.getElementById('chart-vol').textContent = fmtUsd(p.volume_24h || c.volume_24h);
  document.getElementById('chart-liq').textContent = fmtUsd(p.liquidity || c.liquidity);

  const key = addr + '|' + state.tf;
  const now = Date.now();
  if (!force && state.lastKline.address === addr && state.lastKline.tf === state.tf && now - state.lastKline.at < 15000) return;
  state.lastKline = { address: addr, tf: state.tf, at: now };

  try {
    const r = await fetch(`/api/kline/${addr}?resolution=${state.tf}&limit=300`);
    const j = await r.json();
    const candles = (j.candles || []).map(k => ({
      time: Math.floor((k.time > 1e12 ? k.time : k.time * 1000) / 1000),
      open: +k.open, high: +k.high, low: +k.low, close: +k.close,
      volume: +(k.volume || 0),
    })).filter(k => k.time && !isNaN(k.close));
    if (state.candleSeries) {
      const maxP = candles.reduce((m, c) => Math.max(m, c.high), 0) || price || 1;
      const precision = maxP >= 1 ? 2 : maxP >= 0.01 ? 4 : Math.min(10, Math.max(2, Math.ceil(-Math.log10(maxP)) + 3));
      state.candleSeries.applyOptions({ priceFormat: { type: 'price', precision, minMove: Math.pow(10, -precision) } });
      state.candleSeries.setData(candles);
      state.volumeSeries.setData(candles.map(k => ({ time: k.time, value: k.volume, color: k.close >= k.open ? 'rgba(35,227,107,.35)' : 'rgba(255,83,100,.35)' })));
      // The candle the live ticks will keep growing until the next pull.
      state.liveCandle = candles.length
        ? { ...candles[candles.length - 1] }
        : null;
      state.chart.timeScale().fitContent();
    }
  } catch (e) {}
}

/* ---------------------------------------------------------- settings */
const FIELD_GROUPS = {
  exec: [
    ['paper_trading', 'Mode papier (aucun ordre réel)', 'bool'],
    ['trade_amount_sol', 'Montant par trade (SOL)', 'number', { step: 0.01 }],
    ['execution_backend', "Moteur d'exécution", 'select', { options: ['jupiter', 'gmgn'] }],
    ['solana_rpc_url', 'RPC Solana', 'text'],
    ['solana_rpc_fallback_url', 'RPC de secours (lectures)', 'text'],
    ['tp_percent', 'Take Profit (%)', 'number', { step: 1 }],
    ['sl_percent', 'Stop Loss (%)', 'number', { step: 1 }],
    ['trailing_percent', 'Trailing Stop (% du pic)', 'number', { step: 1 }],
    ['slippage', 'Slippage (%)', 'number', { step: 1 }],
    ['priority_fee', 'Priority fee (SOL)', 'number', { step: 0.0001 }],
    ['max_fee_percent', 'Frais max (% du trade, 0 = désactivé)', 'number', { step: 0.5 }],
    ['max_concurrent_positions', 'Positions simultanées max', 'number', { step: 1 }],
    ['price_poll_seconds', "Intervalle d'évaluation des sorties (s)", 'number', { step: 1 }],
    ['price_feed_enabled', 'Suivi de prix continu (Dexscreener)', 'bool'],
    ['price_feed_seconds', 'Intervalle du suivi en direct (s)', 'number', { step: 0.5 }],
    ['price_feed_calls', 'Calls récents suivis en direct', 'number', { step: 1 }],
    ['live_trading', 'Trading réel activé', 'bool'],
    ['auto_snipe', 'Sniper automatique', 'bool'],
    ['snipe_once_per_token', 'Un seul achat par token (jamais de rachat)', 'bool'],
    ['trailing_enabled', 'Trailing stop activé', 'bool'],
    ['anti_mev', 'Protection anti-MEV (GMGN uniquement)', 'bool'],
  ],
  criteria: [
    ['max_market_cap', 'MarketCap ATH max (USD)', 'number', { step: 1000 }],
    ['max_age_minutes', 'Âge max (min)', 'number', { step: 1 }],
    ['max_top10_percent', 'Top 10 holders max (%)', 'number', { step: 1 }],
    ['buyer_seller_window', 'Fenêtre acheteurs/vendeurs', 'select', { options: ['1m', '5m', '1h', '6h', '24h'] }],
    ['require_mc_gt_volume', 'Rejeter si MarketCap > Volume', 'bool'],
    ['require_more_buyers', 'Rejeter si vendeurs > acheteurs', 'bool'],
    ['require_website', 'Exiger un site web', 'bool'],
    ['require_social', 'Exiger des réseaux sociaux', 'bool'],
    ['require_description', 'Exiger une description', 'bool'],
    ['require_social_engagement', 'Exiger un engagement social', 'bool'],
    ['max_social_dup', 'Duplication sociale max', 'number', { step: 1 }],
    ['require_kol', 'Exiger des KOLs', 'bool'],
    ['require_smart_wallet', 'Exiger des smart wallets', 'bool'],
    ['alert_prefilter', "Pré-filtre sur le message d'alerte", 'bool'],
  ],
  system: [
    ['channels', 'Canaux Telegram (virgules)', 'textlist'],
    ['secrets_local_only', 'Verrouiller les secrets à cette machine', 'bool'],
  ],
};

function renderSettingsForms() {
  for (const [group, fields] of Object.entries(FIELD_GROUPS)) {
    const el = document.getElementById('settings-' + group);
    if (!el) continue;
    el.innerHTML = fields.map(([key, label, type, opt]) => fieldHtml(key, label, type, opt || {})).join('')
      + (group === 'exec' ? '<button class="btn-primary" data-save-group="exec">Sauvegarder</button>' : '')
      + (group === 'criteria' ? '<button class="btn-primary" data-save-group="criteria">Sauvegarder les critères</button>' : '')
      + (group === 'system'
        ? `<label class="field"><span>Sniper manuel (adresse de contrat)</span><input type="text" id="manual-ca" placeholder="Ex: 9xQ...a3f"></label>
           <button class="btn-ghost" id="manual-snipe-btn" title="Un contrat déjà traité est refusé : un seul achat par token.">Analyser &amp; sniper</button>
           <button class="btn-primary" data-save-group="system">Sauvegarder</button>`
        : '');
  }
  const tg = document.getElementById('tg-login');
  if (tg) {
    const st = state.runtime.telegram || 'disconnected';
    tg.innerHTML = `<div class="kv-row"><span class="k">Statut Telegram</span><span class="v">${tgLabel(st)}</span></div>
      <button class="btn-ghost" id="tg-open" style="margin-top:10px;width:100%">${st === 'listening' ? 'Reconnecter Telegram' : 'Connecter Telegram'}</button>`;
  }
}

function tgLabel(st) {
  return ({ listening: '<span class="pill-ok">connecté</span>', need_login: '<span class="pill-no">connexion requise</span>', awaiting_code: '<span class="pill-no">code requis</span>', awaiting_password: '<span class="pill-no">2FA requis</span>', error: '<span class="pill-no">erreur</span>' }[st]) || st;
}

/* ------------------------------------------------- secrets (clés & API)
 * The values live outside the project and never come back to the browser: the
 * server answers with a mask, the layer the value in force comes from, and
 * whether an environment variable is overriding what was saved here. A wallet
 * key is refused unless its address can be derived — storing a key that cannot
 * sign would only be discovered at the first trade. */
function renderSecrets() {
  const host = document.getElementById('settings-secrets');
  if (!host) return;
  const c = state.credentials || {};
  const fileEl = document.getElementById('secrets-file');
  if (fileEl) { fileEl.textContent = c.file || '—'; fileEl.title = c.file || ''; }
  const note = document.getElementById('secrets-note');
  if (note) note.innerHTML = secretsNote(c);
  const items = c.items || [];
  host.innerHTML = items.length
    ? items.map(secretRowHtml).join('')
    : '<div class="secret-hint">Aucun secret géré.</div>';
}

function secretsNote(c) {
  const lines = [];
  lines.push(`Enregistrés dans <code>${escapeHtml(c.file || '—')}</code>, hors du dossier du bot. `
    + `La valeur stockée n'est jamais renvoyée au navigateur : seul un aperçu masqué est affiché.`);
  lines.push(`Priorité de lecture : variable d'environnement &gt; fichier du tableau de bord &gt; anciens .env.`);
  if (c.helius_in_use === false) {
    lines.push(`<span class="warn">L'URL RPC actuelle n'utilise aucune clé Helius.</span> `
      + `Enregistrer une clé Helius réécrit l'URL pour la lire depuis ce fichier.`);
  }
  if ((c.inline_keys || []).length) {
    lines.push(`<span class="warn">Une clé est encore écrite en clair dans data/settings.json</span> `
      + `(<code>${c.inline_keys.map(k => escapeHtml(k)).join('</code>, <code>')}</code>).`);
  }
  if (c.client_local === false) {
    lines.push(`<span class="warn">Connexion depuis ${escapeHtml(c.client_ip || 'une autre machine')} :</span> `
      + `quiconque peut atteindre ce tableau de bord peut remplacer la clé privée du wallet.`);
  }
  lines.push(c.local_only
    ? `Écriture verrouillée à la machine locale (<code>secrets_local_only</code>).`
    : `Écriture acceptée depuis toute machine pouvant atteindre le tableau de bord ; cocher `
      + `« Verrouiller les secrets à cette machine » dans les réglages pour la limiter.`);
  return lines.map(l => `<div>${l}</div>`).join('');
}

function secretRowHtml(it) {
  // A PEM mask already carries its size ("PEM (120 caractères)"): don't repeat it.
  const maskText = it.mask.endsWith(')') ? it.mask : `${it.mask} (${it.length} car.)`;
  const state = it.set
    ? `<span class="ok">défini</span><span>· ${escapeHtml(it.source_label)}</span>`
      + `<span>· ${escapeHtml(maskText)}</span>`
    : '<span class="no">absent</span>';
  const shadow = it.shadowed
    ? `<span class="secret-hint warn">Une variable d'environnement impose déjà une autre valeur : `
      + `ce que vous enregistrez ici ne sera pas utilisé tant qu'elle est définie.</span>`
    : '';
  const placeholder = it.set ? 'remplacer la valeur actuelle' : 'coller la valeur';
  const field = it.multiline
    ? `<textarea rows="3" spellcheck="false" data-secret-input="${it.key}" placeholder="${placeholder}"></textarea>`
    : `<input type="${it.type}" autocomplete="new-password" spellcheck="false" data-secret-input="${it.key}" placeholder="${placeholder}">`;
  return `<div class="secret-row" data-secret="${it.key}">
      <div class="secret-meta">
        <span class="secret-label">${escapeHtml(it.label)}</span>
        <span class="secret-hint">${escapeHtml(it.hint)}</span>
        ${it.legacy ? `<span class="secret-hint">Source d'origine : <code>${escapeHtml(it.legacy)}</code></span>` : ''}
        <span class="secret-state">${state}</span>
        ${shadow}
      </div>
      <div class="secret-field">
        <label class="field"><span>Nouvelle valeur</span>${field}</label>
      </div>
      <div class="secret-actions">
        <button class="btn-primary" data-secret-save="${it.key}">Enregistrer</button>
        ${it.testable ? `<button class="btn-ghost" data-secret-test="${it.key}">Tester</button>` : ''}
        ${it.in_file ? `<button class="btn-ghost danger" data-secret-clear="${it.key}">Retirer</button>` : ''}
      </div>
    </div>`;
}

async function loadCredentials() {
  try {
    const r = await fetch('/api/credentials');
    state.credentials = await r.json();
  } catch (e) {
    state.credentials = { items: [] };
  }
  renderSecrets();
}

async function saveSecret(key) {
  const input = document.querySelector(`[data-secret-input="${key}"]`);
  const value = input ? input.value : '';
  if (!value || !value.trim()) {
    toast('Rien à enregistrer : le champ est vide.', 'err');
    return;
  }
  try {
    const r = await fetch('/api/credentials', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, value }),
    });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || 'Valeur refusée', 'err'); return; }
    if (input) input.value = '';
    state.credentials = j.credentials || state.credentials;
    renderSecrets();
    toast(j.message || 'Secret enregistré');
    (j.notes || []).forEach(n => toast(n));
    refreshStatus();
  } catch (e) { toast("Échec de l'enregistrement", 'err'); }
}

async function testSecret(key) {
  try {
    const r = await fetch('/api/credentials/test', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || 'Test impossible', 'err'); return; }
    toast(j.detail || (j.ok ? 'Test réussi' : 'Test échoué'), j.ok ? '' : 'warn');
  } catch (e) { toast('Test impossible', 'err'); }
}

async function clearSecret(key) {
  const sure = window.confirm('Retirer cette valeur du fichier des secrets ?\n\n'
    + "La source précédente (variable d'environnement ou ancien fichier .env) reprendra la main.");
  if (!sure) return;
  try {
    const r = await fetch('/api/credentials/' + encodeURIComponent(key), { method: 'DELETE' });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || 'Suppression refusée', 'err'); return; }
    state.credentials = j.credentials || state.credentials;
    renderSecrets();
    toast(j.message || 'Valeur retirée');
    refreshStatus();
  } catch (e) { toast('Échec de la suppression', 'err'); }
}

function fieldHtml(key, label, type, opt) {
  const v = state.settings[key];
  if (type === 'bool') {
    return `<div class="toggle-row"><span>${label}</span><label class="switch"><input type="checkbox" data-key="${key}" ${v ? 'checked' : ''}><i></i></label></div>`;
  }
  if (type === 'select') {
    return `<label class="field"><span>${label}</span><select data-key="${key}">${opt.options.map(o => `<option value="${o}" ${String(v) === String(o) ? 'selected' : ''}>${o}</option>`).join('')}</select></label>`;
  }
  if (type === 'textlist') {
    const val = Array.isArray(v) ? v.join(', ') : (v || '');
    return `<label class="field"><span>${label}</span><input type="text" data-key="${key}" value="${escapeHtml(val)}"></label>`;
  }
  if (type === 'text') {
    return `<label class="field"><span>${label}</span><input type="text" data-key="${key}" value="${escapeHtml(v || '')}"></label>`;
  }
  return `<label class="field"><span>${label}</span><input type="number" data-key="${key}" value="${v}" step="${opt.step || 1}"></label>`;
}

function collectGroup(group) {
  const el = document.getElementById('settings-' + group);
  const patch = {};
  const invalid = [];
  el.querySelectorAll('[data-key]').forEach(inp => {
    const key = inp.dataset.key;
    if (inp.type === 'checkbox') patch[key] = inp.checked;
    else if (key === 'channels') patch[key] = inp.value.split(',').map(s => s.trim().replace(/^.*t\.me\//, '')).filter(Boolean);
    else if (inp.type === 'number') {
      const n = parseFloat(inp.value);
      if (!isFinite(n)) { invalid.push(key); return; }
      patch[key] = n;
    }
    else patch[key] = inp.value;
  });
  return { patch, invalid };
}

async function saveGroup(group) {
  const { patch, invalid } = collectGroup(group);
  if (invalid.length) {
    toast('Valeur numérique manquante : ' + invalid.join(', '), 'err');
    return;
  }
  try {
    const r = await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || 'Valeur refusée par le serveur', 'err'); return; }
    state.settings = j;
    renderSettingsForms(); renderConfigPanel();
    if (group === 'system') loadCredentials();
    toast('Configuration sauvegardée');
  } catch (e) { toast('Échec de la sauvegarde', 'err'); }
}

/* dashboard config panel (sync with settings) */
function renderConfigPanel() {
  const s = state.settings;
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val; };
  set('cfg-tp', s.tp_percent); set('cfg-sl', s.sl_percent); set('cfg-trail', s.trailing_percent);
  const amt = document.getElementById('cfg-amount');
  if (amt) {
    let opts = [...amt.options].map(o => o.value);
    if (!opts.includes(String(s.trade_amount_sol))) { amt.add(new Option(`${s.trade_amount_sol} SOL`, String(s.trade_amount_sol))); }
    amt.value = String(s.trade_amount_sol);
  }
  const t = document.getElementById('cfg-trail-on'); if (t) t.checked = !!s.trailing_enabled;
  const a = document.getElementById('cfg-auto'); if (a) a.checked = !!s.auto_snipe;
  renderModeLine();
  renderFeeLine();
  renderRpcLine();
}

/* The mode, derived in exactly one place on this side of the wire. The server
 * states it too (/api/state → mode, /api/health → trading, /api/mode), and
 * tools/verify_mode.py checks the two agree on every combination — but the switch
 * re-reads the saved settings rather than the last snapshot, so flipping it is
 * immediate and can never show a stale answer while the socket catches up.
 * "Papier" means no order is ever sent, whatever "trading réel" says; the third
 * state is the one that is easy to get wrong, so it is named separately instead
 * of being folded into "réel". */
function modeInfo() {
  const s = state.settings || {};
  if (s.paper_trading) {
    return { key: 'paper', name: 'PAPIER', canSendOrders: false,
             label: 'PAPIER — appels, analyses et prix réels, aucun ordre envoyé' };
  }
  if (s.live_trading) {
    return { key: 'live', name: 'RÉEL', canSendOrders: true,
             label: s.auto_snipe ? 'RÉEL — snipes automatiques activés'
                                 : 'RÉEL — sniper automatique désactivé' };
  }
  return { key: 'observation', name: 'OBSERVATION', canSendOrders: false,
           label: 'OBSERVATION — aucun ordre automatique' };
}

function renderModeLine() {
  const info = modeInfo();
  const el = document.getElementById('cfg-mode');
  if (el) {
    el.textContent = info.label;
    el.className = 'v ' + (info.key === 'paper' ? 'fee-ok' : info.key === 'live' ? 'fee-bad' : '');
  }
  renderModeSwitch(info);
}

/* The top-bar switch. Two buttons and one data-state, so the colour and the words
 * can never disagree about which mode is on. */
function renderModeSwitch(info) {
  const box = document.getElementById('mode-switch');
  if (!box) return;
  info = info || modeInfo();
  box.dataset.state = info.key;
  box.title = info.label;
  box.querySelectorAll('button').forEach(b => {
    const on = b.dataset.mode === 'paper' ? info.key === 'paper' : info.key !== 'paper';
    b.classList.toggle('is-on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

/* Switching mode is the one click here that can put real money at risk, so it is
 * the one click that asks first — in words, with the live balance. It never arms
 * the "Trading réel" master switch by itself: that box has to be ticked, so a
 * stray click can leave the bot in observation but never in the market. */
async function switchMode(target) {
  const info = modeInfo();
  if (target === info.key) return;
  const s = state.settings || {};
  if (target === 'paper') {
    // Going back to paper only ever removes the ability to send an order, so it
    // is applied at once: a brake that demands a second confirmation is a brake
    // people learn not to use.
    await postMode({ mode: 'paper' });
    return;
  }
  const amount = Number(s.trade_amount_sol) || 0;
  const balance = Number((state.runtime || {}).wallet_balance_sol);
  // The address is only filled once the wallet has really been read, so a
  // genuine 0 SOL is announced like any other shortfall instead of being hidden
  // behind "the balance is not known yet".
  const known = !!((state.runtime || {}).wallet) && !isNaN(balance);
  const dot = v => String(v).replace('.', ',');
  const facts = ['Chaque snipe accepté engagera de vrais SOL depuis le wallet configuré.',
                 `Montant par trade : ${dot(amount)} SOL.`];
  if (!s.live_trading) {
    facts.push('« Trading réel » est désactivé : sans lui, quitter le mode papier '
             + 'laisserait le bot en observation, sans qu’aucun ordre ne parte.');
  }
  if (known) {
    facts.push(balance < amount
      ? `Solde du wallet : ${dot(balance.toFixed(4))} SOL — un achat de `
        + `${dot(amount)} SOL serait refusé (fonds insuffisants).`
      : `Solde du wallet : ${dot(balance.toFixed(4))} SOL.`);
  }
  openModal(`
    <p>Le mode papier n'envoie aucun ordre : les achats sont virtuels, mais pris au
       prix réellement exécutable. Le mode réel, lui, signe et diffuse des
       transactions.</p>
    <ul class="mode-list">${facts.map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul>
    ${s.live_trading ? '' : `<label class="mode-arm"><input type="checkbox" id="mode-arm-live" checked>
       Activer aussi « Trading réel »</label>`}
    <button class="btn-primary" id="mode-confirm">Passer en mode réel</button>
    <button class="btn-ghost" id="mode-cancel">Rester en mode papier</button>
  `, 'Passer en mode RÉEL ?');
  document.getElementById('mode-cancel').onclick = closeModal;
  document.getElementById('mode-confirm').onclick = async () => {
    const arm = document.getElementById('mode-arm-live');
    const body = { mode: 'live' };
    if (arm) body.live_trading = !!arm.checked;
    closeModal();
    await postMode(body);
  };
}

/* The server decides, so its answer is what gets announced — including when it
 * did *less* than asked, such as leaving the master switch off. */
async function postMode(body) {
  try {
    const r = await fetch('/api/mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || 'Changement de mode refusé', 'err'); return null; }
    if (j.settings) state.settings = j.settings;
    renderSettingsForms(); renderConfigPanel(); renderPaper();
    const info = j.mode || {};
    const kind = info.can_send_orders ? 'warn' : undefined;
    toast(`Mode ${info.name || ''}`, kind);
    (j.notes || []).forEach(n => toast(n, kind));
    return j;
  } catch (e) {
    toast('Échec du changement de mode', 'err');
    return null;
  }
}

/* What a round trip costs right now. Fees are a fixed SOL amount, so their
 * share of the trade is what decides whether the trade can pay for itself:
 * measured on this bot's own fills, a 0,001 SOL trade paid 2,35 % to 15,58 %. */
function renderFeeLine() {
  const line = document.getElementById('cfg-fee-line');
  const el = document.getElementById('cfg-fee');
  if (!el) return;
  const f = state.fees || {};
  if (!f.estimated_sol && f.estimated_sol !== 0) {
    el.textContent = '—';
    el.className = 'v';
    return;
  }
  const share = Number(f.share_percent) || 0;
  const ok = !!f.passed;
  const cap = Number(f.cap_percent) || 0;
  el.textContent = `${f.estimated_sol.toFixed(6)} SOL = ${share.toFixed(1)} % d'un trade de `
    + `${f.amount_sol} SOL`
    + (f.enabled ? (ok ? ` · sous le plafond de ${cap} %` : ` · au-dessus du plafond de ${cap} % — les snipes seront refusés`)
                 : ' · plafond désactivé');
  el.className = 'v ' + (f.enabled && !ok ? 'fee-bad' : 'fee-ok');
  if (line) line.classList.toggle('is-bad', f.enabled && !ok);
}

/* The infrastructure, stated honestly.
 *
 * Which node is configured (the server masks the API key — it must never reach
 * a browser), whether that node is really answering or merely absent, whether
 * the live socket is up, and whether the priority-fee cap sits above or below
 * the going rate. A node that answers "200" with nothing in it is the failure
 * this exists to make visible: for a while this bot only survived that because
 * reads fell back and sends did not. */
function renderRpcLine() {
  const r = state.rpc || {};
  const rpcEl = document.getElementById('cfg-rpc');
  const rpcLine = document.getElementById('cfg-rpc-line');
  if (rpcEl) {
    const host = String(r.primary || '—')
      .replace(/^https?:\/\//, '').replace(/\?.*$/, '').replace(/\/+$/, '');
    const entry = (r.endpoints || {})[r.primary] || null;
    const ok = entry ? entry.ok : 0;
    const fail = entry ? entry.fail : 0;
    const ms = (entry && entry.median_ms != null) ? Math.round(entry.median_ms) : null;
    const ws = r.websocket || {};
    const parts = [`${host} · ${ok} lecture(s) ok` + (fail ? ` / ${fail} échec(s)` : '')];
    if (ms != null) parts.push(`${ms} ms médian`);
    parts.push(ws.connected
      ? `WebSocket actif${ws.last_slot ? ` · slot ${ws.last_slot}` : ''}`
      : 'WebSocket inactif (sondage utilisé)');
    rpcEl.textContent = parts.join(' · ');
    const dead = ok === 0 && fail > 0;
    rpcEl.className = 'v ' + (dead ? 'fee-bad' : (ok ? 'fee-ok' : ''));
    if (rpcLine) rpcLine.classList.toggle('is-bad', dead);
  }

  const prioEl = document.getElementById('cfg-priority');
  const prioLine = document.getElementById('cfg-priority-line');
  if (!prioEl) return;
  const p = r.priority_fee || {};
  const cap = Number(p.cap_lamports) || 0;
  const allowed = Number(p.cap_price_microlamports_per_cu) || 0;
  const high = ((p.implied_lamports || {}).high) || null;
  const levels = p.levels_microlamports_per_cu || {};
  const fmt = n => Number(n).toLocaleString('fr-FR');
  if (!cap) {
    prioEl.textContent = '—';
    prioEl.className = 'v';
    return;
  }
  /* Deliberately no pass/fail colour here. The node's estimate is an *opinion*,
   * and it swings by an order of magnitude within minutes — it said "above the
   * market" while this wallet's own last 19 swaps had their price clamped 14
   * times. A green tick from a number like that is a false reassurance, which is
   * worse than no tick. The objective comparison (what Jupiter actually asked
   * for, decoded from the chain) lives in tools/priority_fee.py. */
  const nodeHigh = Number(levels.high);
  let text = `${fmt(cap)} lamports (≈ ${fmt(allowed)} µLam/CU à ${fmt(p.cu_budget)} CU)`;
  if (Number.isFinite(nodeHigh) && nodeHigh > 0) {
    text += ` · nœud « high » : ${fmt(nodeHigh)} µLam/CU (estimation)`;
  } else if (high) {
    text += ` · nœud « high » : ${fmt(high)} lamports (estimation)`;
  }
  prioEl.textContent = text;
  prioEl.className = 'v';
  if (prioLine) prioLine.classList.remove('is-bad');
}

async function saveConfigPanel() {
  const num = id => parseFloat(document.getElementById(id).value);
  const patch = {
    trade_amount_sol: num('cfg-amount'),
    tp_percent: num('cfg-tp'),
    sl_percent: num('cfg-sl'),
    trailing_percent: num('cfg-trail'),
    trailing_enabled: document.getElementById('cfg-trail-on').checked,
    auto_snipe: document.getElementById('cfg-auto').checked,
  };
  const invalid = Object.keys(patch).filter(k => typeof patch[k] === 'number' && !isFinite(patch[k]));
  if (invalid.length) { toast('Valeur numérique manquante : ' + invalid.join(', '), 'err'); return; }
  try {
    const r = await fetch('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
    const j = await r.json();
    if (!r.ok) { toast(j.detail || 'Valeur refusée par le serveur', 'err'); return; }
    state.settings = j;
    renderConfigPanel(); renderSettingsForms();
    toast('Configuration de trading sauvegardée');
  } catch (e) { toast('Échec de la sauvegarde', 'err'); }
}

/* ---------------------------------------------------------- toasts */
function toast(message, kind) {
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity .3s'; setTimeout(() => el.remove(), 320); }, 3600);
}

/* ---------------------------------------------------------- modal / telegram */
function openModal(html, title) {
  const heading = document.getElementById('modal-title');
  if (heading) heading.textContent = title || 'Connexion Telegram';
  document.getElementById('tg-modal-body').innerHTML = html;
  document.getElementById('tg-modal').classList.remove('is-hidden');
}
function closeModal() { document.getElementById('tg-modal').classList.add('is-hidden'); }

function tgModalStart() {
  state.tg.step = 'phone';
  openModal(`
    <p>Connectez votre compte Telegram pour écouter les canaux en direct. Un code de confirmation sera envoyé dans votre application Telegram.</p>
    <label class="field"><span>Numéro de téléphone</span><input type="text" id="tg-phone" value="${escapeHtml(state.settings.__phone || '')}" placeholder="+33 6 12 34 56 78"></label>
    <button class="btn-primary" id="tg-send">Envoyer le code</button>
  `);
  document.getElementById('tg-send').onclick = tgSendCode;
}
async function tgSendCode() {
  const phone = document.getElementById('tg-phone').value.trim();
  const btn = document.getElementById('tg-send'); btn.disabled = true; btn.textContent = 'Envoi…';
  try {
    const r = await fetch('/api/telegram/send_code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ phone }) });
    const j = await r.json();
    if (!j.ok) { toast(j.error || 'Échec', 'err'); btn.disabled = false; btn.textContent = 'Envoyer le code'; return; }
    tgModalCode(phone);
  } catch (e) { toast('Erreur réseau', 'err'); btn.disabled = false; btn.textContent = 'Envoyer le code'; }
}
function tgModalCode(phone) {
  state.tg.step = 'code'; state.tg.phone = phone;
  openModal(`
    <p>Un code a été envoyé sur Telegram pour <b>${escapeHtml(phone)}</b>. Saisissez-le ci-dessous.</p>
    <label class="field"><span>Code de confirmation</span><input type="text" id="tg-code" placeholder="12345" autocomplete="off"></label>
    <button class="btn-primary" id="tg-signin">Valider</button>
  `);
  document.getElementById('tg-code').focus();
  document.getElementById('tg-signin').onclick = tgSignIn;
}
async function tgSignIn() {
  const code = document.getElementById('tg-code').value.trim();
  const btn = document.getElementById('tg-signin'); btn.disabled = true; btn.textContent = 'Vérification…';
  try {
    const r = await fetch('/api/telegram/sign_in', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
    const j = await r.json();
    if (j.need_password) { tgModalPassword(); return; }
    if (!j.ok) { toast(j.error || 'Code invalide', 'err'); btn.disabled = false; btn.textContent = 'Valider'; return; }
    closeModal(); toast('Telegram connecté'); refreshStatus();
  } catch (e) { toast('Erreur réseau', 'err'); btn.disabled = false; btn.textContent = 'Valider'; }
}
function tgModalPassword() {
  openModal(`
    <p>Votre compte est protégé par la validation en deux étapes. Saisissez votre mot de passe Telegram.</p>
    <label class="field"><span>Mot de passe 2FA</span><input type="password" id="tg-pw"></label>
    <button class="btn-primary" id="tg-pw-ok">Valider</button>
  `);
  document.getElementById('tg-pw').focus();
  document.getElementById('tg-pw-ok').onclick = async () => {
    const password = document.getElementById('tg-pw').value;
    const btn = document.getElementById('tg-pw-ok'); btn.disabled = true; btn.textContent = '…';
    const r = await fetch('/api/telegram/password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password }) });
    const j = await r.json();
    if (!j.ok) { toast(j.error || 'Mot de passe invalide', 'err'); btn.disabled = false; btn.textContent = 'Valider'; return; }
    closeModal(); toast('Telegram connecté'); refreshStatus();
  };
}

/* ---------------------------------------------------------- actions */
async function manualSnipe(address) {
  if (!address) return;
  /* Le bouton contourne la file d'analyse : deux clics lançaient deux analyses
   * en parallèle sur le même contrat. Le serveur refuse désormais le second
   * (« in-progress ») ; désactiver le bouton pendant la requête évite même de le
   * lui demander. */
  const btn = document.getElementById('manual-snipe-btn');
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  toast('Vérification de ' + shortAddr(address) + '…');
  try {
    const r = await fetch('/api/snipe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ address }) });
    const j = await r.json();
    if (j.decision === 'sniped') toast('Token validé' + (j.executed ? ' et acheté' : ''), 'warn');
    else if (j.reason === 'already-traded') toast((j.reasons || [])[0] || 'Token déjà traité — un seul achat par token', 'err');
    else if (j.reason === 'in-progress') toast((j.reasons || [])[0] || 'Snipe déjà en cours', 'warn');
    else toast('Token rejeté: ' + ((j.reasons || [])[0] || ''), 'err');
  } catch (e) { toast('Erreur', 'err'); }
  finally { if (btn) btn.disabled = false; }
}

/* ------------------------------------------------- fermeture manuelle
 * Closing a position is a sale, so nothing leaves before the confirmation, and
 * the amount is a choice: a quarter, half, or the whole position. The ✕ used to
 * send 100 % on a single click with no further question — on a real position
 * that is the expensive kind of mistake, and a partial exit is an ordinary
 * thing to want. A refused sale (a rate-limited quote, say) keeps the position
 * open and says so *inside the dialog*, where the click was made, instead of in
 * a toast that has already faded by the time the table is read. */
function closePosition(id) {
  const p = (state.positions || []).find(x => x.id === id);
  if (!p) { toast('Position introuvable', 'err'); return; }
  if (p.status !== 'open') {
    toast(`Déjà fermée · ${p.exit_reason || 'clôturée'}`, 'warn');
    return;
  }
  openModal(closeDialogHtml(p), 'Fermer la position ?');
  document.querySelectorAll('#tg-modal-body .close-pct').forEach(btn => {
    btn.onclick = () => {
      document.querySelectorAll('#tg-modal-body .close-pct')
        .forEach(x => x.classList.toggle('is-on', x === btn));
      renderCloseAnswer(p);
    };
  });
  document.getElementById('close-cancel').onclick = closeModal;
  document.getElementById('close-confirm').onclick = () => doClose(p);
  renderCloseAnswer(p);
}

function closeDialogHtml(p) {
  const up = (p.pnl_percent || 0) >= 0;
  return `
    <div class="close-head">
      <span class="close-sym">${escapeHtml(symLabel(p.symbol))}</span>
      <span class="token-addr" title="${p.address}">${shortAddr(p.address)}</span>
    </div>
    <div class="close-pnl">PnL suivi <span class="${up ? 'pos' : 'neg'}">${fmtPct(p.pnl_percent || 0)}</span>
      · ${fmtUsd(p.pnl_usd || 0)}${p.paper ? ' · position papier' : ' · position réelle'}</div>
    <div class="close-pcts">
      <span class="close-pcts-label">Part à vendre</span>
      <div class="close-pcts-row">
        ${[25, 50, 100].map(v => `<button class="close-pct${v === 100 ? ' is-on' : ''}" data-pct="${v}">${v} %</button>`).join('')}
      </div>
    </div>
    <div class="close-answer" id="close-answer"></div>
    <ul class="mode-list">${closeFacts(p).map(f => `<li>${escapeHtml(f)}</li>`).join('')}</ul>
    <div id="close-error"></div>
    <button class="btn-primary${p.paper ? '' : ' danger'}" id="close-confirm"></button>
    <button class="btn-ghost" id="close-cancel">Annuler</button>`;
}

/* What really happens, in the user's words rather than the code's. */
function closeFacts(p) {
  if (p.paper) {
    return ["Position papier : la vente est cotée sur le marché réel, à la même route "
            + "et au même prix qu'une vraie vente, mais aucun ordre n'est signé ni envoyé.",
            "Le PnL est calculé comme pour une vraie vente, frais de transaction compris."];
  }
  return ["Position réelle : la vente signe et diffuse une transaction. Elle est définitive.",
          "Aucune autre confirmation ne sera demandée après celle-ci."];
}

function closePercent() {
  const on = document.querySelector('#tg-modal-body .close-pct.is-on');
  return on ? Number(on.dataset.pct) : 100;
}

/* The sentence has to follow the choice, because the two acts differ: a partial
 * sale leaves a position running, a full one freezes its PnL for good. */
function renderCloseAnswer(p) {
  const pct = closePercent();
  const answer = document.getElementById('close-answer');
  const confirm = document.getElementById('close-confirm');
  if (!answer || !confirm) return;
  const rest = 100 - pct;
  answer.innerHTML = pct >= 100
    ? "Vente de <b>la totalité</b> : la position est fermée, son PnL réalisé est figé "
      + "et n'évoluera plus."
    : `Vente de <b>${pct} %</b> : la position reste <b>ouverte</b> pour les ${rest} % restants, `
      + `dont le PnL continue de suivre le prix.`;
  confirm.textContent = pct >= 100
    ? (p.paper ? 'Fermer la position' : 'Vendre la totalité (réel)')
    : `Vendre ${pct} %`;
}

async function doClose(p) {
  const pct = closePercent();
  const confirm = document.getElementById('close-confirm');
  const box = document.getElementById('close-error');
  if (box) box.innerHTML = '';
  if (confirm) { confirm.disabled = true; confirm.textContent = 'Vente en cours…'; }
  try {
    const r = await fetch(`/api/positions/${p.id}/close`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ percent: pct }),
    });
    const j = await r.json();
    if (j.ok) {
      closeModal();
      /* A liquidation is a book-keeping exit, not a sale: never announce it with
       * the same word as a real close, or the PnL reads as money received. */
      if (j.liquidation === 'unpriced') {
        toast(`${symLabel(p.symbol)} soldée au dernier prix connu (non exécutable)`, 'warn');
      } else {
        toast(pct >= 100 ? `${symLabel(p.symbol)} fermée` : `${pct} % de ${symLabel(p.symbol)} vendus`);
      }
      return;
    }
    if (box) {
      box.innerHTML = `<div class="close-error">${escapeHtml(j.error || j.reason || 'Vente refusée')}`
        + `<span class="untouched">La position est inchangée.</span></div>`;
    }
    if (confirm) confirm.disabled = false;
    renderCloseAnswer(p);
    toast('Vente refusée', 'err');
  } catch (e) {
    if (box) {
      box.innerHTML = '<div class="close-error">Communication interrompue avec le bot.'
        + '<span class="untouched">La position est inchangée.</span></div>';
    }
    if (confirm) confirm.disabled = false;
    renderCloseAnswer(p);
    toast('Erreur', 'err');
  }
}

/* ---------------------------------------------------------- view switch */
function switchView(view) {
  document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.dataset.view === view));
  const dash = document.getElementById('view-dashboard');
  dash.classList.toggle('is-hidden', view !== 'dashboard');
  ['positions', 'settings', 'logs', 'about'].forEach(v => {
    document.getElementById('view-' + v).classList.toggle('is-hidden', v !== view);
  });
}

/* ---------------------------------------------------------- events */
function bindEvents() {
  document.getElementById('nav').addEventListener('click', e => {
    const btn = e.target.closest('.nav-item'); if (btn) switchView(btn.dataset.view);
  });
  document.addEventListener('click', e => {
    const viewLink = e.target.closest('[data-view-link]');
    if (viewLink) { switchView(viewLink.dataset.viewLink); return; }

    // Secrets: the buttons are re-rendered on every change, so delegation from
    // the document keeps them working without rebinding.
    const secretSave = e.target.closest('[data-secret-save]');
    if (secretSave) { saveSecret(secretSave.dataset.secretSave); return; }

    const secretTest = e.target.closest('[data-secret-test]');
    if (secretTest) { testSecret(secretTest.dataset.secretTest); return; }

    const secretClear = e.target.closest('[data-secret-clear]');
    if (secretClear) { clearSecret(secretClear.dataset.secretClear); return; }

    // Must come before the row's [data-select]: the button lives inside the row.
    const untrack = e.target.closest('[data-feed-untrack]');
    if (untrack) {
      e.stopPropagation();
      feedTrack(untrack.dataset.feedUntrack, 'watch', 'remove');
      const rows = (state.feed.rows || []).filter(r => r.address !== untrack.dataset.feedUntrack);
      state.feed.rows = rows;
      renderFeed();
      return;
    }

    const copy = e.target.closest('[data-copy]');
    if (copy) { navigator.clipboard.writeText(copy.dataset.copy); toast('Adresse copiée'); return; }

    // These two buttons live **inside** a `<tr data-select>`, so they must be
    // tested before `[data-select]`: the row is clickable to load its chart, and
    // it used to swallow the clicks meant for « Fermer » and « TP/SL » — the ✕
    // did nothing but change the selected token. Order is the whole fix; keep
    // these two checks above the `[data-select]` one.
    const close = e.target.closest('[data-close]');
    if (close) { e.stopPropagation(); closePosition(close.dataset.close); return; }

    const tp = e.target.closest('[data-tp]');
    if (tp) {
      e.stopPropagation();
      const pos = state.positions.find(p => p.id === tp.dataset.tp);
      if (pos) toast(`TP ${pos.tp_percent}% · SL ${pos.sl_percent}% · Trailing ${pos.trailing_percent}%`, 'warn');
      switchView('settings'); return;
    }

    const select = e.target.closest('[data-select]');
    if (select) { selectToken(select.dataset.select); return; }

    const modeBtn = e.target.closest('#mode-switch [data-mode]');
    if (modeBtn) { switchMode(modeBtn.dataset.mode); return; }

    const saveGroupBtn = e.target.closest('[data-save-group]');
    if (saveGroupBtn) { saveGroup(saveGroupBtn.dataset.saveGroup); return; }

    const tgOpen = e.target.closest('#tg-open');
    if (tgOpen) { tgModalStart(); return; }

    const manualBtn = e.target.closest('#manual-snipe-btn');
    if (manualBtn) { manualSnipe(document.getElementById('manual-ca').value.trim()); return; }

    if (e.target.id === 'tg-modal' || e.target.id === 'tg-close') closeModal();
  });

  document.getElementById('cfg-save').addEventListener('click', saveConfigPanel);

  document.getElementById('trade-banner-retry').addEventListener('click', async () => {
    try { await fetch('/api/trading/reset', { method: 'POST' }); } catch (e) {}
    await _statusNow();
  });

  document.getElementById('tf-row').addEventListener('click', e => {
    const tf = e.target.closest('.tf'); if (!tf) return;
    state.tf = tf.dataset.tf;
    state.liveCandle = null;
    document.querySelectorAll('.tf').forEach(b => b.classList.toggle('active', b === tf));
    renderChart(true);
  });

  // Follow a contract typed by hand (the row itself only needs to be selected).
  const feedAdd = document.getElementById('feed-add-btn');
  if (feedAdd) {
    const follow = async () => {
      const input = document.getElementById('feed-input');
      const address = (input.value || '').trim();
      if (!address) return;
      if (await feedTrack(address, 'watch', 'add')) {
        input.value = '';
        toast('Token suivi en direct');
      }
    };
    feedAdd.addEventListener('click', follow);
    const feedInput = document.getElementById('feed-input');
    if (feedInput) feedInput.addEventListener('keydown', e => { if (e.key === 'Enter') follow(); });
  }

  // Manual snipe input (in About/system via settings)
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });
}

function selectToken(address) {
  state.selected = address;
  state.liveCandle = null;
  state.chartGap = 0;
  state.lastKline = { address: null, tf: null, at: 0 };
  renderCalls(); renderTokenDetails(); renderChart(true);
  // The backend follows whatever is on screen: its price then streams in even
  // when the token is neither held nor recently called.
  if (address) feedTrack(address, 'select', 'add');
  if (document.getElementById('view-positions').classList.contains('is-hidden') === false) {
    /* keep positions view as-is */
  }
}

/* ---------------------------------------------------------- boot */
async function boot() {
  bindEvents();
  initChart();
  try {
    const r = await fetch('/api/state');
    const j = await r.json();
    state.settings = j.settings || {};
    state.runtime = j.runtime || {};
    state.positions = j.positions || [];
    state.calls = j.calls || [];
    state.logs = j.logs || [];
    state.stats = j.stats || {};
    state.feed = j.feed || { rows: [] };
    state.fees = j.fees || {};
    state.rpc = j.rpc || {};
    if (!Array.isArray(state.feed.rows)) state.feed.rows = [];
  } catch (e) {}
  ensureSelection();
  renderAll();
  renderConfigPanel();
  connectWS();
  loadCredentials();
  setInterval(() => { renderPositions(); renderAllPositions(); refreshStatus(); }, 15000);
  setInterval(() => renderChart(true), 20000);
  // Only ages the "il y a N s" labels and the staleness badge: the prices
  // themselves arrive on their own ticks.
  setInterval(renderFeed, 5000);
}

document.addEventListener('DOMContentLoaded', boot);
