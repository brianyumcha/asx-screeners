"""
Shared card-grid HTML dashboard renderer, used by both OBV SCREENER.py and
PULLBACK SCREENER.py so the two reports look and behave consistently
(styled after traders-hub.luk.com.au/pre-breakout-screener - card grid with
a mini candlestick+SMA chart per stock, sector/size/timeframe filters,
ranked/sector view toggle, cooldown history reveal).

This is a STATIC file only - there's no backend, no live "run scan now", no
accounts. Every value baked into the page comes from the scan that produced
it. Re-running the screener script overwrites the file with a fresh scan.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

SYDNEY_TZ = ZoneInfo("Australia/Sydney")


def size_bucket(market_cap):
    if market_cap >= 2_000_000_000:
        return 'L'
    if market_cap >= 300_000_000:
        return 'M'
    return 'S'


def render_dashboard_html(
    cards,               # list of dicts, see card schema below
    excluded_cards,       # cooldown-hidden cards, same schema, shown via "already seen" toggle
    total_scanned,
    title,
    subtitle,
    footer_note,
    out_path,
):
    """
    Card schema (each item in `cards` / `excluded_cards`):
      {
        'ticker': str, 'sector': str, 'market_cap': int,
        'price': float, 'change_1d': float, 'score': int (0-100),
        'stats': [ {'label': str, 'value': str}, ... up to 3 ],
        'dates': [...], 'opens': [...], 'highs': [...], 'lows': [...],
        'closes': [...], 'volumes': [...],   # trailing ~260 daily bars
      }
    """
    for c in cards:
        c['size'] = size_bucket(c['market_cap'])
        c['already_seen'] = False
    for c in excluded_cards:
        c['size'] = size_bucket(c['market_cap'])
        c['already_seen'] = True

    all_cards = cards + excluded_cards
    sectors = sorted({c['sector'] for c in all_cards})

    now = datetime.now(SYDNEY_TZ)
    session_line = (
        f"Session {now.strftime('%Y-%m-%d')} · {total_scanned} scanned · "
        f"{len(cards)} new · {len(excluded_cards)} already seen · "
        f"last run {now.strftime('%Y-%m-%d %H:%M')} Sydney time"
    )

    html = HTML_TEMPLATE
    html = html.replace('##TITLE##', title)
    html = html.replace('##SUBTITLE##', subtitle)
    html = html.replace('##SESSION_LINE##', session_line)
    html = html.replace('##FOOTER_NOTE##', footer_note)
    html = html.replace('##SECTORS_JSON##', json.dumps(sectors))
    html = html.replace('##DATA_JSON##', json.dumps(all_cards))

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return out_path


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>##TITLE##</title>
<script>
// Set before first paint so there's no flash of the wrong theme. Defaults
// to dark (this site's original look) unless the viewer explicitly chose
// light on a previous visit.
try {
  if (localStorage.getItem('theme') === 'light') document.documentElement.setAttribute('data-theme', 'light');
} catch (e) {}
</script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Syne:wght@700;800&display=swap');
:root {
  --bg:#0a0c0f; --surface:#111418; --border:#1e2530;
  --accent:#00e5a0; --accent2:#00aaff; --warn:#ffb800; --danger:#ff4455;
  --text:#e8edf2; --muted:#5a6478; --card:#141820;
}
[data-theme="light"] {
  --bg:#f4f6f9; --surface:#ffffff; --border:#dde3ea;
  --accent:#00a37b; --accent2:#0077b3; --warn:#a66a00; --danger:#d6293a;
  --text:#1a2029; --muted:#65707f; --card:#ffffff;
}
.themebtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.9rem;padding:.5rem .65rem;border-radius:6px;cursor:pointer;line-height:1;height:fit-content}
.themebtn:hover{border-color:var(--accent2)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;
  font-variant-numeric:tabular-nums;padding:1.6rem;}
body::before{content:'';position:fixed;inset:0;
  background-image:linear-gradient(rgba(0,229,160,.03) 1px,transparent 1px),
  linear-gradient(90deg,rgba(0,229,160,.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0}
.wrap{max-width:1560px;margin:0 auto;position:relative;z-index:1}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.3rem;flex-wrap:wrap;gap:1rem}
h1{font-family:'Syne',sans-serif;font-size:1.9rem;font-weight:800;letter-spacing:-.03em;
  background:linear-gradient(135deg,var(--accent),var(--accent2));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.subtitle{font-size:.78rem;color:var(--muted);margin-top:.3rem}
.session{font-size:.68rem;color:var(--muted);margin:.6rem 0 1.1rem;letter-spacing:.03em}
.copybtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.72rem;padding:.5rem .9rem;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;gap:.4rem;white-space:nowrap;height:fit-content}
.copybtn:hover{border-color:var(--accent)}
.copybtn.copied{border-color:var(--accent);color:var(--accent)}
.topbar-right{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap}
.reportnav{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-family:'Inter',sans-serif;font-size:.72rem;padding:.5rem .8rem;border-radius:6px;
  cursor:pointer;outline:none;height:fit-content}
.reportnav:hover{border-color:var(--accent2)}

.controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-bottom:.7rem}
.pillgroup{display:flex;gap:.3rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.2rem}
.pill{background:transparent;border:none;color:var(--muted);font-size:.72rem;
  padding:.4rem .8rem;border-radius:6px;cursor:pointer;white-space:nowrap}
.pill:hover{color:var(--text)}
.pill.active{background:var(--accent2);color:#04121a;font-weight:600}
input[type=text]{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.75rem;padding:.5rem .8rem;border-radius:8px;outline:none;width:170px}
input[type=text]:focus{border-color:rgba(0,229,160,.4)}
.checkline{display:flex;align-items:center;gap:.4rem;font-size:.72rem;color:var(--muted);cursor:pointer}
.checkline input{cursor:pointer}

.sectorrow{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.1rem}
.sectorpill{background:var(--surface);border:1px solid var(--border);color:var(--muted);
  font-size:.68rem;padding:.35rem .7rem;border-radius:20px;cursor:pointer}
.sectorpill:hover{color:var(--text)}
.sectorpill.active{background:rgba(0,229,160,.12);border-color:var(--accent);color:var(--accent)}

.countline{font-size:.7rem;color:var(--muted);margin-bottom:.8rem}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}
.sectionhead{grid-column:1/-1;font-family:'Syne',sans-serif;font-size:.85rem;font-weight:700;
  color:var(--text);margin:1.4rem 0 .2rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.sectionhead:first-child{margin-top:0}

.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.9rem;
  display:flex;flex-direction:column;gap:.5rem}
.card.seen{opacity:.55}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start}
.cardhead-left{display:flex;align-items:baseline;gap:.4rem}
.ticker{font-family:'Syne',sans-serif;font-weight:700;font-size:1.02rem;color:var(--text)}
.ticker a{color:inherit;text-decoration:none}
.ticker a:hover{color:var(--accent2)}
.chg{font-size:.72rem;font-weight:600}
.up{color:var(--accent)} .dn{color:var(--danger)} .neutral{color:var(--muted)}
.scorebar{display:flex;align-items:center;gap:.4rem}
.scorebar .bg{width:42px;height:4px;background:var(--border);border-radius:2px;overflow:hidden}
.scorebar .fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--accent2),var(--accent))}
.scorebar .num{font-size:.68rem;color:var(--muted)}

.chartwrap{position:relative;width:100%;height:150px}
canvas{width:100%;height:100%;display:block}

.statrow{display:flex;justify-content:space-between;font-size:.68rem;color:var(--muted);
  border-top:1px solid var(--border);padding-top:.5rem}
.statrow .v{color:var(--text)}
.sectortag{font-size:.64rem;color:var(--muted);text-transform:none}

.empty{grid-column:1/-1;text-align:center;color:var(--muted);padding:3rem 0;font-size:.85rem}
.empty.table-empty{text-align:center;color:var(--muted);padding:3rem 0;font-size:.85rem}

table.datatable{width:100%;border-collapse:collapse;font-size:.8rem}
table.datatable thead tr{border-bottom:1px solid var(--border)}
table.datatable th{text-align:left;padding:.6rem .8rem;font-size:.66rem;color:var(--muted);text-transform:uppercase;
  letter-spacing:.06em;cursor:pointer;white-space:nowrap;user-select:none;font-weight:600}
table.datatable th:hover{color:var(--text)}
table.datatable th.sorted{color:var(--accent)}
table.datatable tbody tr{border-bottom:1px solid rgba(30,37,48,.6);transition:background .1s}
table.datatable tbody tr:hover{background:rgba(0,229,160,.03)}
table.datatable tbody tr.seen{opacity:.5}
table.datatable td{padding:.65rem .8rem;vertical-align:middle;white-space:nowrap}
table.datatable td.ticker-cell{font-family:'Syne',sans-serif;font-weight:700}
table.datatable td.ticker-cell a{color:var(--accent2);text-decoration:none}
table.datatable td.sector-cell{color:var(--muted);font-size:.74rem}
.notice{background:rgba(255,184,0,.05);border:1px solid rgba(255,184,0,.15);border-radius:4px;
  padding:.7rem .9rem;font-size:.68rem;color:var(--warn);margin-bottom:1rem;line-height:1.6}
footer{margin-top:2rem;font-size:.62rem;color:var(--muted);border-top:1px solid var(--border);padding-top:1rem}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div>
      <h1>##TITLE##</h1>
      <div class="subtitle">##SUBTITLE##</div>
    </div>
    <div class="topbar-right">
      <select class="reportnav" id="reportNav" onchange="if(this.value) location.href=this.value">
        <option value="pre-breakout.html">📈 Pre-Breakout (OBV)</option>
        <option value="pullback.html">↩️ Pullback (Zag Zone)</option>
        <option value="higher-high.html">⬆️ Higher-High</option>
      </select>
      <button class="copybtn" id="copyBtn">📋 Copy TradingView list</button>
      <button class="themebtn" id="themeBtn" title="Toggle light/dark">🌙</button>
    </div>
  </div>
  <div class="session">##SESSION_LINE##</div>

  <div class="notice">⚠ Static report from a single scan run — not live. Re-run the script for fresh data. Nothing here is a trade recommendation.</div>

  <div class="controls">
    <div class="pillgroup" id="modeToggle">
      <button class="pill active" data-mode="chart">📊 Charts</button>
      <button class="pill" data-mode="table">☰ Table</button>
    </div>
    <div class="pillgroup" id="viewToggle">
      <button class="pill active" data-view="ranked">Ranked</button>
      <button class="pill" data-view="sector">Sector</button>
    </div>
    <div class="pillgroup" id="sizeToggle">
      <button class="pill active" data-size="S">S</button>
      <button class="pill active" data-size="M">M</button>
      <button class="pill active" data-size="L">L</button>
    </div>
    <div class="pillgroup" id="tfToggle">
      <button class="pill" data-tf="63">3M</button>
      <button class="pill active" data-tf="126">6M</button>
      <button class="pill" data-tf="252">12M</button>
    </div>
    <input type="text" id="search" placeholder="Search ticker...">
    <label class="checkline"><input type="checkbox" id="showSeen"> Show already seen</label>
  </div>

  <div class="sectorrow" id="sectorRow"></div>
  <div class="countline" id="countLine"></div>
  <div class="grid" id="grid"></div>
  <div id="tableWrap" style="display:none;overflow-x:auto;"></div>

  <footer>##FOOTER_NOTE##</footer>
</div>

<script>
document.getElementById('reportNav').value = location.pathname.split('/').pop() || 'pre-breakout.html';

const themeBtn = document.getElementById('themeBtn');
function isLightTheme() { return document.documentElement.getAttribute('data-theme') === 'light'; }
function syncThemeBtn() { themeBtn.textContent = isLightTheme() ? '☀️' : '🌙'; }
syncThemeBtn();
themeBtn.addEventListener('click', () => {
  const next = isLightTheme() ? null : 'light';
  if (next) document.documentElement.setAttribute('data-theme', next);
  else document.documentElement.removeAttribute('data-theme');
  try { localStorage.setItem('theme', next || 'dark'); } catch (e) {}
  syncThemeBtn();
  render();  // re-run chart-mode canvas drawing with theme-correct colors
});

const DATA = ##DATA_JSON##;
const SECTORS = ##SECTORS_JSON##;

let state = {
  mode: 'chart',   // 'chart' or 'table'
  view: 'ranked',
  sizes: new Set(['S','M','L']),
  tf: 126,
  search: '',
  showSeen: false,
  sector: null,
  sortKey: 'score',
  sortDir: -1,
};

const sectorRow = document.getElementById('sectorRow');
sectorRow.innerHTML = '<button class="sectorpill active" data-sector="">All sectors</button>' +
  SECTORS.map(s => `<button class="sectorpill" data-sector="${esc(s)}">${esc(s)}</button>`).join('');
sectorRow.querySelectorAll('.sectorpill').forEach(el => el.addEventListener('click', () => {
  state.sector = el.dataset.sector || null;
  sectorRow.querySelectorAll('.sectorpill').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
  render();
}));

document.getElementById('modeToggle').addEventListener('click', e => {
  if (!e.target.dataset.mode) return;
  state.mode = e.target.dataset.mode;
  document.querySelectorAll('#modeToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  document.getElementById('viewToggle').style.display = state.mode === 'chart' ? '' : 'none';
  document.getElementById('tfToggle').style.display = state.mode === 'chart' ? '' : 'none';
  render();
});
document.getElementById('viewToggle').addEventListener('click', e => {
  if (!e.target.dataset.view) return;
  state.view = e.target.dataset.view;
  document.querySelectorAll('#viewToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('sizeToggle').addEventListener('click', e => {
  const sz = e.target.dataset.size;
  if (!sz) return;
  if (state.sizes.has(sz)) state.sizes.delete(sz); else state.sizes.add(sz);
  e.target.classList.toggle('active');
  render();
});
document.getElementById('tfToggle').addEventListener('click', e => {
  if (!e.target.dataset.tf) return;
  state.tf = parseInt(e.target.dataset.tf, 10);
  document.querySelectorAll('#tfToggle .pill').forEach(x => x.classList.remove('active'));
  e.target.classList.add('active');
  render();
});
document.getElementById('search').addEventListener('input', e => {
  state.search = e.target.value.toUpperCase();
  render();
});
document.getElementById('showSeen').addEventListener('change', e => {
  state.showSeen = e.target.checked;
  render();
});
document.getElementById('copyBtn').addEventListener('click', () => {
  const visible = filteredCards();
  const text = visible.map(c => `ASX:${c.ticker}`).join(',');
  navigator.clipboard.writeText(text).then(() => {
    const btn = document.getElementById('copyBtn');
    btn.classList.add('copied');
    btn.textContent = `✓ Copied ${visible.length} tickers`;
    setTimeout(() => { btn.classList.remove('copied'); btn.textContent = '📋 Copy TradingView list'; }, 1800);
  });
});

function esc(s){ return String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function filteredCards() {
  return DATA.filter(c =>
    (state.showSeen || !c.already_seen) &&
    state.sizes.has(c.size) &&
    (!state.sector || c.sector === state.sector) &&
    (!state.search || c.ticker.includes(state.search))
  );
}

function sma(closes, period) {
  const out = new Array(closes.length).fill(null);
  let sum = 0;
  for (let i = 0; i < closes.length; i++) {
    sum += closes[i];
    if (i >= period) sum -= closes[i - period];
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

function drawChart(canvas, card, tf) {
  const n = card.closes.length;
  const start = Math.max(0, n - tf);
  const closes = card.closes.slice(start), opens = card.opens.slice(start),
        highs = card.highs.slice(start), lows = card.lows.slice(start);
  if (closes.length < 2) return;

  const sma20 = sma(card.closes, 20).slice(start);
  const sma50 = sma(card.closes, 50).slice(start);

  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = rect.width * dpr;
  canvas.height = rect.height * dpr;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  const W = rect.width, H = rect.height;
  ctx.clearRect(0, 0, W, H);

  const allVals = [...highs, ...lows, ...sma20.filter(v=>v!=null), ...sma50.filter(v=>v!=null)];
  const lo = Math.min(...allVals), hi = Math.max(...allVals);
  const pad = (hi - lo) * 0.08 || 1;
  const yMin = lo - pad, yMax = hi + pad;
  const y = v => H - ((v - yMin) / (yMax - yMin)) * H;
  const n2 = closes.length;
  const cw = W / n2;
  const x = i => i * cw + cw / 2;

  function line(series, color, dashed) {
    ctx.beginPath();
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.3;
    ctx.setLineDash(dashed ? [3, 3] : []);
    let started = false;
    for (let i = 0; i < series.length; i++) {
      if (series[i] == null) continue;
      const px = x(i), py = y(series[i]);
      if (!started) { ctx.moveTo(px, py); started = true; } else { ctx.lineTo(px, py); }
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }
  line(sma50, isLightTheme() ? 'rgba(70,80,95,0.55)' : 'rgba(180,190,200,0.55)', true);
  line(sma20, 'rgba(0,170,255,0.85)', false);

  for (let i = 0; i < n2; i++) {
    const up = closes[i] >= opens[i];
    ctx.strokeStyle = ctx.fillStyle = up ? '#00e5a0' : '#ff4455';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(i), y(highs[i]));
    ctx.lineTo(x(i), y(lows[i]));
    ctx.stroke();
    const bodyTop = y(Math.max(opens[i], closes[i]));
    const bodyBot = y(Math.min(opens[i], closes[i]));
    const bw = Math.max(1, cw * 0.6);
    ctx.fillRect(x(i) - bw / 2, bodyTop, bw, Math.max(1, bodyBot - bodyTop));
  }

  // floating last-price badge
  const lastPrice = closes[closes.length - 1];
  const up = card.change_1d >= 0;
  ctx.font = '600 10px Inter, sans-serif';
  const label = lastPrice.toFixed(lastPrice < 1 ? 3 : 2);
  const tw = ctx.measureText(label).width;
  const bx = W - tw - 10, by = y(lastPrice);
  ctx.fillStyle = up ? 'rgba(0,229,160,0.9)' : 'rgba(255,68,85,0.9)';
  ctx.fillRect(bx - 4, by - 8, tw + 8, 16);
  ctx.fillStyle = '#04121a';
  ctx.fillText(label, bx, by + 3);
}

function cardHtml(c) {
  const chgClass = c.change_1d > 0 ? 'up' : c.change_1d < 0 ? 'dn' : 'neutral';
  const chgSign = c.change_1d > 0 ? '+' : '';
  const statsHtml = c.stats.map(s => `<span>${esc(s.label)} <span class="v">${esc(s.value)}</span></span>`).join('');
  const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${c.ticker}`;
  return `<div class="card${c.already_seen ? ' seen' : ''}" data-ticker="${c.ticker}">
    <div class="cardhead">
      <div class="cardhead-left">
        <span class="ticker"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(c.ticker)} ↗</a></span>
        <span class="chg ${chgClass}">${chgSign}${c.change_1d.toFixed(1)}%</span>
      </div>
      <div class="scorebar"><div class="bg"><div class="fill" style="width:${Math.min(100,c.score)}%"></div></div><span class="num">${c.score}</span></div>
    </div>
    <div class="chartwrap"><canvas></canvas></div>
    <div class="statrow">${statsHtml}</div>
    <div class="sectortag">${esc(c.sector)}</div>
  </div>`;
}

function statNumeric(value) {
  const m = String(value).match(/-?[\d.]+/);
  return m ? parseFloat(m[0]) : NaN;
}

function renderTable(visible) {
  const wrap = document.getElementById('tableWrap');
  if (visible.length === 0) {
    wrap.innerHTML = '<div class="empty table-empty">No stocks match the current filters.</div>';
    return;
  }
  const statLabels = visible[0].stats.map(s => s.label);
  const sorters = {
    ticker: c => c.ticker,
    sector: c => c.sector,
    price: c => c.price,
    change_1d: c => c.change_1d,
    score: c => c.score,
  };
  statLabels.forEach((label, i) => { sorters['stat' + i] = c => statNumeric(c.stats[i].value); });

  const getVal = sorters[state.sortKey] || sorters.score;
  const sorted = [...visible].sort((a, b) => {
    const av = getVal(a), bv = getVal(b);
    if (typeof av === 'string') return state.sortDir * av.localeCompare(bv);
    return state.sortDir * ((av ?? -Infinity) - (bv ?? -Infinity));
  });

  const headers = [
    ['ticker', 'Ticker'], ['sector', 'Sector'], ['price', 'Price'],
    ['change_1d', '1D Chg'], ['score', 'Score'],
    ...statLabels.map((l, i) => ['stat' + i, l]),
  ];
  const thead = headers.map(([key, label]) =>
    `<th class="${state.sortKey === key ? 'sorted' : ''}" data-sortkey="${key}">${esc(label)}${state.sortKey === key ? (state.sortDir === 1 ? ' ↑' : ' ↓') : ''}</th>`
  ).join('');

  const rows = sorted.map(c => {
    const chgClass = c.change_1d > 0 ? 'up' : c.change_1d < 0 ? 'dn' : 'neutral';
    const chgSign = c.change_1d > 0 ? '+' : '';
    const tvUrl = `https://www.tradingview.com/chart/?symbol=ASX:${c.ticker}`;
    const statCells = c.stats.map(s => `<td>${esc(s.value)}</td>`).join('');
    return `<tr class="${c.already_seen ? 'seen' : ''}">
      <td class="ticker-cell"><a href="${tvUrl}" target="_blank" rel="noopener">${esc(c.ticker)}</a></td>
      <td class="sector-cell">${esc(c.sector)}</td>
      <td>$${c.price.toFixed(c.price < 1 ? 3 : 2)}</td>
      <td class="${chgClass}">${chgSign}${c.change_1d.toFixed(1)}%</td>
      <td>${c.score}</td>
      ${statCells}
    </tr>`;
  }).join('');

  wrap.innerHTML = `<table class="datatable"><thead><tr>${thead}</tr></thead><tbody>${rows}</tbody></table>`;
  wrap.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {
    const key = th.dataset.sortkey;
    if (state.sortKey === key) state.sortDir *= -1; else { state.sortKey = key; state.sortDir = -1; }
    render();
  }));
}

function render() {
  const visible = filteredCards();
  document.getElementById('countLine').textContent = `${visible.length} stocks`;
  const grid = document.getElementById('grid');
  const tableWrap = document.getElementById('tableWrap');

  if (state.mode === 'table') {
    grid.style.display = 'none';
    tableWrap.style.display = '';
    renderTable(visible);
    return;
  }
  grid.style.display = '';
  tableWrap.style.display = 'none';

  if (visible.length === 0) {
    grid.innerHTML = '<div class="empty">No stocks match the current filters.</div>';
    return;
  }

  let html = '';
  if (state.view === 'ranked') {
    const sorted = [...visible].sort((a, b) => b.score - a.score);
    html = sorted.map(cardHtml).join('');
  } else {
    const bySector = {};
    visible.forEach(c => { (bySector[c.sector] = bySector[c.sector] || []).push(c); });
    Object.keys(bySector).sort().forEach(sec => {
      const list = bySector[sec].sort((a, b) => b.score - a.score);
      html += `<div class="sectionhead">${esc(sec)} (${list.length})</div>` + list.map(cardHtml).join('');
    });
  }
  grid.innerHTML = html;

  grid.querySelectorAll('.card').forEach(el => {
    const c = DATA.find(d => d.ticker === el.dataset.ticker);
    const canvas = el.querySelector('canvas');
    requestAnimationFrame(() => drawChart(canvas, c, state.tf));
  });
}

render();
</script>
</body>
</html>"""
