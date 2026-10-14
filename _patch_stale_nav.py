"""One-off fix for the stale-nav-across-N-pages scenario (recurring after any
change to dashboard_template.py's NAV_*_LINKS - see check_nav.py's docstring
for the history). Replaces the <nav class="sitenav">...</nav> block PLUS its
immediately-following <script>...</script> (the burger-menu toggle logic,
part of the same render_nav() output) with a freshly rendered one, in every
page check_nav.py just flagged as out of sync. Not part of the normal build
pipeline - run by hand, once, after adding/removing a nav link.
"""
import re
from dashboard_template import render_nav

FILES = [
    'comms-index.html', 'conviction-stack.html', 'discretionary-index.html',
    'earnings-calendar.html', 'energy-index.html', 'financials-index.html',
    'healthcare-index.html', 'industrials-index.html',
    'insider-index.html', 'materials-index.html',
    'real-estate-index.html', 'red-folder-news.html', 'sector-rotation.html',
    'staples-index.html', 'tech-index.html', 'utilities-index.html',
    'wave2-scanner.html',
    # higher-high.html, lower-low.html, momentum.html, pre-breakout.html,
    # pullback.html, volume-anomaly.html deliberately excluded - those are
    # just the _site/ DEPLOY NAMES for asx_hh_results.html/asx_ll_results.html/
    # etc, which already call render_nav() fresh on every scan.yml run (many
    # times a day) - no manual patch needed, they self-heal on the next run.
]

NAV_BLOCK_RE = re.compile(r'<nav class="sitenav">.*?</nav>\s*<script>.*?</script>', re.S)

for fname in FILES:
    with open(fname, encoding='utf-8') as f:
        content = f.read()
    fresh_nav = render_nav(fname)
    new_content, n = NAV_BLOCK_RE.subn(fresh_nav.rstrip('\n'), content, count=1)
    if n != 1:
        print(f"  ⚠ {fname}: nav block not found/replaced (n={n}) - skipped, check manually")
        continue
    with open(fname, 'w', encoding='utf-8') as f:
        f.write(new_content)
    print(f"  ✅ {fname}: nav refreshed")
