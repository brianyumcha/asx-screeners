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

    active_href = 'pullback.html' if 'Pullback' in title else 'pre-breakout.html'
    html = HTML_TEMPLATE
    html = html.replace('##NAV##', render_nav(active_href))
    html = html.replace('##TITLE##', title)
    html = html.replace('##SUBTITLE##', subtitle)
    html = html.replace('##SESSION_LINE##', session_line)
    html = html.replace('##FOOTER_NOTE##', footer_note)
    html = html.replace('##SECTORS_JSON##', json.dumps(sectors))
    html = html.replace('##DATA_JSON##', json.dumps(all_cards))

    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html)
    return out_path


NAV_SCREENER_LINKS = [
    ('higher-high.html', 'Higher-High'),
    ('pre-breakout.html', 'Pre-Breakout (OBV)'),
    ('pullback.html', 'Pullback (Zag Zone)'),
]
NAV_INDEX_LINKS = [
    ('energy-index.html', 'Energy'),
    ('healthcare-index.html', 'Healthcare'),
    ('materials-index.html', 'Materials'),
    ('tech-index.html', 'Tech'),
]


def render_nav(active_href):
    def group(label, links):
        items = "\n".join(
            '<a href="{0}"{1}>{2}</a>'.format(href, ' class="active"' if href == active_href else '', text)
            for href, text in links
        )
        return (
            '<div class="sitenav-drop"><button class="sitenav-toggle" type="button">{0} '
            '<span class="sitenav-caret">&#9662;</span></button>'
            '<div class="sitenav-menu">\n{1}\n</div></div>'
        ).format(label, items)
    return (
        '<nav class="sitenav">\n'
        '<a class="sitenav-brand" href="index.html">Brian Yum Cha</a>\n'
        '<span class="sitenav-divider"></span>\n'
        '<div class="sitenav-links">\n'
        + group('Screeners', NAV_SCREENER_LINKS) + "\n"
        + group('ASX Sector Indexes', NAV_INDEX_LINKS) + "\n"
        + '<a class="sitenav-toggle" href="insider-index.html">Insider Buying</a>\n'
        + '</div>\n</nav>\n'
        '<script>\n'
        "document.querySelectorAll('.sitenav-drop').forEach(function(drop){\n"
        "  var toggle = drop.querySelector('.sitenav-toggle');\n"
        "  toggle.addEventListener('click', function(e){\n"
        "    e.stopPropagation();\n"
        "    var willOpen = !drop.classList.contains('open');\n"
        "    document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });\n"
        "    if (willOpen) drop.classList.add('open');\n"
        "  });\n"
        "});\n"
        "document.addEventListener('click', function(){\n"
        "  document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });\n"
        "});\n"
        "document.addEventListener('keydown', function(e){\n"
        "  if (e.key === 'Escape') document.querySelectorAll('.sitenav-drop.open').forEach(function(d){ d.classList.remove('open'); });\n"
        "});\n"
        '</script>\n'
    )


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>##TITLE##</title>
<link rel="icon" type="image/png" href="data:image/png;base64,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">
<script>
// Set before first paint so there's no flash of the wrong theme. Defaults
// to dark (this site's original look) unless the viewer explicitly chose
// light on a previous visit.
try {
  if (localStorage.getItem('theme') === 'light') document.documentElement.setAttribute('data-theme', 'light');
} catch (e) {}
</script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600;700&display=swap');
:root {
  --bg:#121316; --surface:#1c1f24; --border:#454b56; --card:#262b32;
  --accent:#e8b355; --accent-ink:#1c1204; --accent2:#8fc4db; --accent2-ink:#0a1f26;
  --good:#a8d491; --bad:#f0998a; --warn:#eec27a;
  --text:#f5f2e8; --muted:#c9c2b0;
}
[data-theme="light"] {
  --bg:#faf8f2; --surface:#f0ead8; --border:#a89b7a; --card:#e6ddc4;
  --accent:#7a4a0f; --accent-ink:#fdf8ef; --accent2:#163540; --accent2-ink:#eaf5f7;
  --good:#33481f; --bad:#6b241c; --warn:#6b4a0f;
  --text:#141209; --muted:#3d3829;
}
.themebtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  width:2.3rem;height:2.3rem;border-radius:50%;cursor:pointer;font-size:1.05rem;
  display:flex;align-items:center;justify-content:center;flex:none}
.themebtn:hover{border-color:var(--accent2)}
*{margin:0;padding:0;box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,sans-serif;
  font-variant-numeric:tabular-nums;margin:0;}
body::before{content:'';position:fixed;inset:0;
  background-image:linear-gradient(rgba(232,179,85,.03) 1px,transparent 1px),
  linear-gradient(90deg,rgba(232,179,85,.03) 1px,transparent 1px);
  background-size:40px 40px;pointer-events:none;z-index:0}
.wrap{max-width:1560px;margin:0 auto;position:relative;z-index:1;padding:0 1.6rem 1.6rem}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:.3rem;flex-wrap:wrap;gap:1rem}
h1{font-family:"Fraunces",Georgia,serif;font-size:2.2rem;font-weight:600;letter-spacing:-.01em;
  text-wrap:balance;color:var(--text)}
@media (max-width:640px){h1{font-size:1.8rem}}
.subtitle{font-size:.92rem;color:var(--muted);margin-top:.3rem;max-width:44rem}
.session{font-size:.68rem;color:var(--muted);margin:.6rem 0 1.1rem;letter-spacing:.03em}
.copybtn{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.72rem;padding:.5rem .9rem;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;gap:.4rem;white-space:nowrap;height:fit-content}
.copybtn:hover{border-color:var(--accent)}
.copybtn.copied{border-color:var(--accent);color:var(--accent)}
.topbar-right{display:flex;gap:.6rem;align-items:center;flex-wrap:wrap;background:var(--card);border:1px solid var(--border);border-radius:12px;padding:.5rem .6rem}
.controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;margin-bottom:.7rem}
.pillgroup{display:flex;gap:.3rem;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.2rem}
.pill{background:transparent;border:none;color:var(--muted);font-size:.72rem;
  padding:.4rem .8rem;border-radius:6px;cursor:pointer;white-space:nowrap}
.pill:hover{color:var(--text)}
.pill.active{background:var(--accent2);color:var(--accent2-ink);font-weight:600}
input[type=text]{background:var(--surface);border:1px solid var(--border);color:var(--text);
  font-size:.75rem;padding:.5rem .8rem;border-radius:8px;outline:none;width:170px}
input[type=text]:focus{border-color:rgba(232,179,85,.4)}
.checkline{display:flex;align-items:center;gap:.4rem;font-size:.72rem;color:var(--muted);cursor:pointer}
.checkline input{cursor:pointer}

.sectorrow{display:flex;flex-wrap:wrap;gap:.4rem;margin-bottom:1.1rem}
.sectorpill{background:var(--surface);border:1px solid var(--border);color:var(--muted);
  font-size:.68rem;padding:.35rem .7rem;border-radius:20px;cursor:pointer}
.sectorpill:hover{color:var(--text)}
.sectorpill.active{background:rgba(232,179,85,.12);border-color:var(--accent);color:var(--accent)}

.countline{font-size:.7rem;color:var(--muted);margin-bottom:.8rem}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:1rem}
.sectionhead{grid-column:1/-1;font-family:"IBM Plex Mono",monospace;font-size:.85rem;font-weight:700;
  color:var(--text);margin:1.4rem 0 .2rem;padding-bottom:.4rem;border-bottom:1px solid var(--border)}
.sectionhead:first-child{margin-top:0}

.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:.9rem;
  display:flex;flex-direction:column;gap:.5rem}
.card.seen{opacity:.55}
.cardhead{display:flex;justify-content:space-between;align-items:flex-start}
.cardhead-left{display:flex;align-items:baseline;gap:.4rem}
.ticker{font-family:"IBM Plex Mono",monospace;font-weight:700;font-size:1.02rem;color:var(--text)}
.ticker a{color:inherit;text-decoration:none}
.ticker a:hover{color:var(--accent2)}
.chg{font-size:.72rem;font-weight:600}
.up{color:var(--good)} .dn{color:var(--bad)} .neutral{color:var(--muted)}
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
table.datatable tbody tr{border-bottom:1px solid var(--border);transition:background .1s}
table.datatable tbody tr:hover{background:rgba(232,179,85,.05)}
table.datatable tbody tr.seen{opacity:.5}
table.datatable td{padding:.65rem .8rem;vertical-align:middle;white-space:nowrap}
table.datatable td.ticker-cell{font-family:"IBM Plex Mono",monospace;font-weight:700}
table.datatable td.ticker-cell a{color:var(--accent2);text-decoration:none}
table.datatable td.sector-cell{color:var(--muted);font-size:.74rem}
.notice{background:rgba(232,179,85,.08);border:1px solid rgba(232,179,85,.25);border-radius:4px;
  padding:.7rem .9rem;font-size:.68rem;color:var(--warn);margin-bottom:1rem;line-height:1.6}
footer{margin-top:2rem;font-size:.62rem;color:var(--muted);border-top:1px solid var(--border);padding-top:1rem}
.sitenav{position:sticky;top:0;z-index:500;display:flex;align-items:center;gap:1.4rem;
  background:var(--card, var(--surface2, var(--surface)));border-bottom:1px solid var(--border);
  padding:.7rem 1.1rem;margin-bottom:1.5rem;font-family:"IBM Plex Sans",-apple-system,BlinkMacSystemFont,sans-serif;
  box-shadow:0 2px 10px rgba(0,0,0,.12)}
.sitenav-brand{font-family:"IBM Plex Mono",monospace;font-size:.85rem;font-weight:700;
  letter-spacing:.02em;color:var(--accent);text-decoration:none;white-space:nowrap}
.sitenav-brand:hover{opacity:.8}
.sitenav-divider{width:1px;height:1.3rem;background:var(--border);flex:none}
.sitenav-links{display:flex;gap:.2rem}
.sitenav-drop{position:relative}
.sitenav-toggle{background:transparent;border:none;color:var(--text, var(--ink));
  font-family:inherit;font-size:.8rem;padding:.5rem .65rem;border-radius:6px;cursor:pointer;
  display:flex;align-items:center;gap:.3rem}
.sitenav-toggle:hover,.sitenav-drop.open .sitenav-toggle{background:var(--bg);color:var(--accent)}
.sitenav-caret{font-size:.85rem;opacity:1;color:var(--accent);display:inline-block;
  transition:transform .15s ease}
.sitenav-drop:hover .sitenav-caret,.sitenav-drop.open .sitenav-caret{transform:rotate(180deg)}
.sitenav-menu{position:absolute;top:100%;left:0;margin-top:.3rem;min-width:190px;
  background:var(--surface);border:1px solid var(--border);border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.25);padding:.3rem;
  opacity:0;visibility:hidden;transform:translateY(-4px);
  transition:opacity .12s ease,transform .12s ease,visibility .12s}
.sitenav-drop:hover .sitenav-menu,.sitenav-drop.open .sitenav-menu{
  opacity:1;visibility:visible;transform:translateY(0)}
.sitenav-menu a{display:block;padding:.5rem .6rem;border-radius:6px;font-size:.78rem;
  color:var(--text, var(--ink));text-decoration:none;white-space:nowrap}
.sitenav-menu a:hover{background:var(--bg);color:var(--accent)}
.sitenav-menu a.active{color:var(--accent);font-weight:600}
.sitenav-toggle.active{background:var(--bg);color:var(--accent)}
@media (max-width:640px){
  .sitenav{padding:.5rem .7rem;gap:.7rem}
  .sitenav-brand{font-size:.7rem}
  .sitenav-toggle{font-size:.74rem;padding:.45rem .5rem}
  .sitenav-menu{min-width:170px}
}
</style>
</head>
<body>
##NAV##

<div class="wrap">
  <div class="topbar">
    <div>
      <h1>##TITLE##</h1>
      <div class="subtitle">##SUBTITLE##</div>
    </div>
    <div class="topbar-right">
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
  line(sma50, isLightTheme() ? 'rgba(61,56,41,0.55)' : 'rgba(201,194,176,0.55)', true);
  line(sma20, isLightTheme() ? 'rgba(22,53,64,0.85)' : 'rgba(143,196,219,0.85)', false);

  // Deliberately more saturated than the --good/--bad text tokens (those
  // are tuned to sit quietly inline in prose; a candlestick needs to read
  // as up/down at a glance, so it gets its own punchier pair here).
  const CANDLE_UP   = isLightTheme() ? '#15803d' : '#4ade80';
  const CANDLE_DOWN = isLightTheme() ? '#b91c1c' : '#f87171';

  for (let i = 0; i < n2; i++) {
    const up = closes[i] >= opens[i];
    ctx.strokeStyle = ctx.fillStyle = up ? CANDLE_UP : CANDLE_DOWN;
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
  ctx.font = '600 10px "IBM Plex Mono", monospace';
  const label = lastPrice.toFixed(lastPrice < 1 ? 3 : 2);
  const tw = ctx.measureText(label).width;
  const bx = W - tw - 10, by = y(lastPrice);
  ctx.fillStyle = up ? CANDLE_UP : CANDLE_DOWN;
  ctx.fillRect(bx - 4, by - 8, tw + 8, 16);
  ctx.fillStyle = isLightTheme() ? '#fdf8ef' : '#1c1204';
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
