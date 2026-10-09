#!/usr/bin/env python3
"""
Rebuilds mtm-call-archive.html - an unlisted, searchable single-page
archive of every MtM Academy call summary .txt file, so browsing them
doesn't mean opening the tracker spreadsheet. Reads straight from the
same folder the daily run-mtm automation writes to; re-run any time new
summaries land (the wrapper script calls this automatically after every
run-mtm execution - see Instructions/run_mtm_wrapper.sh).

Each summary file follows the fixed format mtm_call_summary_instructions.md
defines:
    [Full Session Name]
    Hosted by [Host Name] | [Day DD Month YYYY]
    Call URL: [url]

    Bullish Setups: TICKER, TICKER (or "None")
    Bearish Setups: ...
    Neutral / Watching: ...

    TICKER — Bias — Timestamp
    - bullet
    - bullet

    ... (repeating ticker sections) ...

    Session Themes:
    - theme

    Important Note: ...

This script parses that structure into real HTML (headings, bias badges,
bullet lists) rather than dumping raw text, and builds a date-sorted
index table at the top linking to each call's anchor.
"""
import os
import re
from datetime import datetime

SRC_DIR = "/Users/briantam/Library/CloudStorage/GoogleDrive-briantam86@gmail.com/My Drive/MtM Academy - Call Summaries"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mtm-call-archive.html")

FILENAME_RE = re.compile(r"^(\d{8})_(.+)_([^_]+)\.txt$")
HEADING_RE = re.compile(
    r"^(?P<ticker>.+?) — (?P<bias>Bullish \(Unchanged\)|Bearish \(Unchanged\)|Bullish|Bearish|Neutral|Informational) — (?P<ts>.+?)\s*$"
)

# Same convention as append_tracker.py's BIAS_COLORS, reused here so the
# archive's badge colors match what the tracker spreadsheet already shows.
BIAS_COLORS = {
    "bullish": "#137333",
    "bearish": "#C5221F",
    "neutral": "#5F6368",
    "bullish (unchanged)": "#6AA84F",
    "bearish (unchanged)": "#B85450",
    "informational": "#4472C4",
}


def esc(s):
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def parse_file(path, fname):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    lines = text.splitlines()

    call_name = lines[0].strip() if lines else fname
    host_date_line = lines[1].strip() if len(lines) > 1 else ""
    call_url = ""
    for l in lines[:6]:
        if l.startswith("Call URL:"):
            call_url = l.split("Call URL:", 1)[1].strip()
            break

    setups = {"Bullish Setups": "", "Bearish Setups": "", "Neutral / Watching": ""}
    for l in lines[:12]:
        for key in setups:
            if l.startswith(key + ":"):
                setups[key] = l.split(":", 1)[1].strip()

    sections = []  # (ticker, bias, ts, [bullets])
    themes = []
    cur = None
    in_themes = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Session Themes:":
            if cur:
                sections.append(cur)
                cur = None
            in_themes = True
            continue
        if stripped.startswith("Important Note:"):
            break
        if in_themes:
            if stripped.startswith("- "):
                themes.append(stripped[2:].strip())
            continue
        m = HEADING_RE.match(stripped)
        if m:
            if cur:
                sections.append(cur)
            cur = [m.group("ticker").strip(), m.group("bias").strip(), m.group("ts").strip(), []]
        elif cur is not None and stripped.startswith("- "):
            cur[3].append(stripped[2:].strip())
    if cur:
        sections.append(cur)

    fm = FILENAME_RE.match(fname)
    if fm:
        date_prefix, _, host_from_name = fm.groups()
        iso_date = f"{date_prefix[:4]}-{date_prefix[4:6]}-{date_prefix[6:]}"
        host = host_from_name
    else:
        iso_date = "0000-00-00"
        host = "Unknown"

    combined_lower = (call_name + " " + host_date_line).lower()
    is_pre_recorded = "recorded" in combined_lower

    return {
        "fname": fname,
        "call_name": call_name,
        "host_date_line": host_date_line,
        "call_url": call_url,
        "setups": setups,
        "sections": sections,
        "themes": themes,
        "iso_date": iso_date,
        "host": host,
        "is_pre_recorded": is_pre_recorded,
    }


def badge_html(bias):
    key = (bias or "").strip().lower()
    color = BIAS_COLORS.get(key)
    if not color:
        return f'<span class="biasbadge">{esc(bias)}</span>'
    return f'<span class="biasbadge" style="background:{color}">{esc(bias)}</span>'


def render_call(call, anchor):
    setups_html = ""
    for label, val in call["setups"].items():
        if not val:
            continue
        setups_html += f'<div class="setupline"><span class="setuplabel">{esc(label)}:</span> {esc(val)}</div>'

    sections_html = ""
    for ticker, bias, ts, bullets in call["sections"]:
        bullets_html = "".join(f"<li>{esc(b)}</li>" for b in bullets)
        sections_html += f"""
        <div class="tickersec">
          <div class="tickerhead"><span class="tk">{esc(ticker)}</span> {badge_html(bias)} <span class="ts mono">{esc(ts)}</span></div>
          <ul>{bullets_html}</ul>
        </div>"""

    themes_html = ""
    if call["themes"]:
        themes_html = "<div class=\"themes\"><h4>Session Themes</h4><ul>" + "".join(
            f"<li>{esc(t)}</li>" for t in call["themes"]
        ) + "</ul></div>"

    url_html = (
        f'<a href="{esc(call["call_url"])}" target="_blank" rel="noopener noreferrer">{esc(call["call_url"])} ↗</a>'
        if call["call_url"] else ""
    )

    return f"""
  <article class="call" id="{anchor}" data-search="{esc((call['call_name'] + ' ' + call['host'] + ' ' + call['iso_date']).lower())}">
    <h2>{esc(call['call_name'])}</h2>
    <div class="callmeta">{esc(call['host_date_line'])}</div>
    <div class="callurl mono">{url_html}</div>
    <div class="setups">{setups_html}</div>
    {sections_html}
    {themes_html}
  </article>"""


def main():
    files = [f for f in os.listdir(SRC_DIR) if FILENAME_RE.match(f)]
    calls = []
    for fname in files:
        path = os.path.join(SRC_DIR, fname)
        try:
            calls.append(parse_file(path, fname))
        except Exception as e:
            print(f"  skipped {fname}: {e}")

    calls.sort(key=lambda c: (c["iso_date"], c["fname"]), reverse=True)

    index_rows = ""
    calls_html = ""
    for i, call in enumerate(calls):
        anchor = f"call-{i}"
        fmt = "Pre-Recorded" if call["is_pre_recorded"] else "Live"
        index_rows += f"""<tr class="idxrow" data-search="{esc((call['call_name'] + ' ' + call['host'] + ' ' + call['iso_date']).lower())}">
          <td class="mono">{esc(call['iso_date'])}</td>
          <td>{esc(call['host'])}</td>
          <td><a href="#{anchor}">{esc(call['call_name'])}</a></td>
          <td class="mono fmt">{fmt}</td>
        </tr>"""
        calls_html += render_call(call, anchor)

    today_str = datetime.now().strftime("%-d %b %Y")
    html = HTML_TEMPLATE
    html = html.replace("##COUNT##", str(len(calls)))
    html = html.replace("##REBUILT_DATE##", today_str)
    html = html.replace("##INDEX_ROWS##", index_rows)
    html = html.replace("##CALLS_HTML##", calls_html)

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Wrote {len(calls)} calls to {OUT_PATH}")


HTML_TEMPLATE = r"""<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="robots" content="noindex, nofollow">
<title>MtM Call Summary Archive</title>
<style>
:root{
  --bg:#faf8f2; --surface:#f0ead8; --surface2:#e6ddc4; --border:#a89b7a;
  --ink:#141209; --ink-soft:#3d3829; --ink-faint:#6b6552;
  --accent:#7a4a0f; --accent-ink:#fdf8ef;
  --shadow: 0 1px 2px rgba(20,18,9,.06), 0 4px 16px rgba(20,18,9,.05);
}
@media (prefers-color-scheme: dark){
  :root:not([data-theme="light"]){
    --bg:#121316; --surface:#1c1f24; --surface2:#262b32; --border:#454b56;
    --ink:#f5f2e8; --ink-soft:#c9c2b0; --ink-faint:#8f8874;
    --accent:#e8b355; --accent-ink:#1c1204;
    --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
  }
}
:root[data-theme="dark"]{
  --bg:#121316; --surface:#1c1f24; --surface2:#262b32; --border:#454b56;
  --ink:#f5f2e8; --ink-soft:#c9c2b0; --ink-faint:#8f8874;
  --accent:#e8b355; --accent-ink:#1c1204;
  --shadow: 0 1px 2px rgba(0,0,0,.3), 0 8px 24px rgba(0,0,0,.35);
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"IBM Plex Sans",-apple-system,sans-serif;font-size:16px;line-height:1.6}
.wrap{max-width:1100px;margin:0 auto;padding:2.4rem 1.5rem 5rem}
h1,h2,h3,h4{font-family:"Fraunces",Georgia,serif;font-weight:600;text-wrap:balance;color:var(--ink)}
h1{font-size:2.2rem;letter-spacing:-.01em;margin:0 0 .2rem}
h2{font-size:1.4rem;margin:0 0 .2rem}
h4{font-size:1rem;margin:1rem 0 .4rem;color:var(--ink-faint)}
.mono{font-family:"IBM Plex Mono",monospace;font-variant-numeric:tabular-nums}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;margin-bottom:.4rem}
.themebtn{border:1px solid var(--border);background:var(--surface);color:var(--ink);width:2.3rem;height:2.3rem;border-radius:50%;cursor:pointer;font-size:1.05rem;display:flex;align-items:center;justify-content:center;flex:none}
.subtitle{color:var(--ink-faint);font-size:.95rem;margin:.3rem 0 1.2rem;max-width:40rem}
#searchBox{width:100%;max-width:28rem;padding:.6rem .9rem;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--ink);font-size:.95rem;margin-bottom:1.4rem}
#searchBox:focus{outline:2px solid var(--accent);outline-offset:1px}
.idxwrap{height:50vh;overflow-y:auto;border:1px solid var(--border);border-radius:8px;margin-bottom:3rem}
table.idx{width:100%;border-collapse:collapse;font-size:.85rem;color:var(--ink)}
table.idx th{position:sticky;top:0;text-align:left;padding:.4rem .6rem;font-size:.66rem;color:var(--ink-faint);text-transform:uppercase;letter-spacing:.05em;border-bottom:1px solid var(--border);background:var(--surface)}
table.idx td{padding:.45rem .6rem;border-bottom:1px solid var(--border);vertical-align:top}
table.idx tr:last-child td{border-bottom:none}
table.idx td.fmt{color:var(--ink-faint)}
table.idx a{color:var(--ink);text-decoration:none}
table.idx a:hover{color:var(--accent)}
.call{border-top:1px solid var(--border);padding-top:1.6rem;margin-top:1.8rem;scroll-margin-top:1rem}
.callmeta{color:var(--ink-faint);font-size:.88rem}
.callurl{font-size:.8rem;margin:.3rem 0 1rem}
.callurl a{color:var(--accent)}
.setups{margin-bottom:1rem}
.setupline{font-size:.85rem;color:var(--ink-soft);margin:.2rem 0}
.setuplabel{color:var(--ink-faint);font-weight:600}
.tickersec{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:.8rem 1rem;margin-bottom:.7rem}
.tickerhead{display:flex;align-items:center;gap:.5rem;margin-bottom:.4rem}
.tk{font-family:"IBM Plex Mono",monospace;font-weight:700}
.biasbadge{color:#fff;font-size:.68rem;font-weight:600;padding:.15rem .5rem;border-radius:4px}
.ts{font-size:.72rem;color:var(--ink-faint);margin-left:auto}
.tickersec ul{margin:.3rem 0 0;padding-left:1.2rem;color:var(--ink-soft);font-size:.88rem}
.tickersec li{margin:.25rem 0}
.themes ul{margin:.3rem 0 0;padding-left:1.2rem;color:var(--ink-soft);font-size:.88rem}
footer{margin-top:3rem;padding-top:1.4rem;border-top:1px solid var(--border);font-size:.8rem;color:var(--ink-faint)}
</style>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500;600&display=swap">

<div class="wrap">
  <div class="topbar">
    <div>
      <h1>MtM Call Summary Archive</h1>
      <p class="subtitle">Every call summary from the daily run-mtm automation, ##COUNT## calls total - full text, searchable, no spreadsheet required.</p>
    </div>
    <button class="themebtn" id="themeBtn" title="Toggle light/dark">🌙</button>
  </div>

  <input type="text" id="searchBox" placeholder="Search by ticker, educator, theme, date...">

  <div class="idxwrap">
    <table class="idx" id="idxTable">
      <thead><tr><th>Date</th><th>Educator</th><th>Call</th><th>Format</th></tr></thead>
      <tbody>##INDEX_ROWS##</tbody>
    </table>
  </div>

  <div id="calls">##CALLS_HTML##</div>

  <footer>Rebuilt ##REBUILT_DATE## by build_mtm_archive.py, run automatically after every run-mtm execution.</footer>
</div>

<script>
const themeBtn = document.getElementById('themeBtn');
function applyTheme(t){
  if(t){ document.documentElement.setAttribute('data-theme', t); } else { document.documentElement.removeAttribute('data-theme'); }
  themeBtn.textContent = (t==='dark') ? '☀️' : '🌙';
}
try{ const saved = localStorage.getItem('mtmarchive-theme'); if(saved) applyTheme(saved); }catch(e){}
themeBtn.addEventListener('click', ()=>{
  const current = document.documentElement.getAttribute('data-theme');
  const next = current==='dark' ? 'light' : 'dark';
  applyTheme(next);
  try{ localStorage.setItem('mtmarchive-theme', next); }catch(e){}
});

const searchBox = document.getElementById('searchBox');
const idxRows = [...document.querySelectorAll('.idxrow')];
const callEls = [...document.querySelectorAll('.call')];
searchBox.addEventListener('input', () => {
  const q = searchBox.value.trim().toLowerCase();
  idxRows.forEach(r => { r.style.display = (!q || r.dataset.search.includes(q)) ? '' : 'none'; });
  callEls.forEach(c => {
    const matches = !q || c.dataset.search.includes(q) || c.textContent.toLowerCase().includes(q);
    c.style.display = matches ? '' : 'none';
  });
});
</script>
"""


if __name__ == "__main__":
    main()
