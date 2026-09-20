"""CI guard against nav drift: every deployed page's <nav> must contain
exactly the canonical link set dashboard_template.py defines (the single
source of truth after the 2026-09-20 nav refactor - see render_nav()'s
docstring/history). Run against the assembled _site/ directory, right
after "Build Pages site" and before upload/deploy, in all 4 workflows -
a page with a stale or hand-copied nav fails the build loudly instead of
sitting broken on the live site for weeks before someone notices.

Before that refactor, nav markup was hand-copied into 19 separate
places (17 *_template.html files, HH SCREENER.py's own embedded
template, and dashboard_template.py's own render_nav) and drifted
independently in at least 4 of them - this script exists so that class
of bug can't ship silently again.

A page with no <nav class="sitenav"> block at all (timing-the-carts.html,
the easter egg page - deliberately nav-less) is skipped, not failed.

Usage:
    python3 check_nav.py                  # checks every *.html in _site/
    python3 check_nav.py _site/foo.html   # check specific file(s)
"""
import glob
import re
import sys

from dashboard_template import NAV_SCREENER_LINKS, NAV_INDEX_LINKS, NAV_TOOLS_LINKS

CANONICAL_HREFS = {
    'index.html', 'timing-the-carts.html',
    *(href for href, _ in NAV_SCREENER_LINKS),
    *(href for href, _ in NAV_INDEX_LINKS),
    *(href for href, _ in NAV_TOOLS_LINKS),
}

NAV_RE = re.compile(r'<nav class="sitenav">(.*?)</nav>', re.S)
HREF_RE = re.compile(r'href="([^"]+)"')


def check_file(path):
    with open(path, encoding='utf-8') as f:
        content = f.read()

    if '<<<NAV_HTML>>>' in content:
        return f"{path}: <<<NAV_HTML>>> placeholder never got substituted"

    m = NAV_RE.search(content)
    if not m:
        return None  # no site nav on this page - intentional (e.g. the easter egg page)

    found = set(HREF_RE.findall(m.group(1)))
    missing = CANONICAL_HREFS - found
    extra = found - CANONICAL_HREFS
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {sorted(missing)}")
        if extra:
            parts.append(f"unexpected extra {sorted(extra)}")
        return f"{path}: nav out of sync - {'; '.join(parts)}"
    return None


def main():
    files = sys.argv[1:] or sorted(glob.glob("_site/*.html"))
    if not files:
        print("No HTML files found to check.")
        sys.exit(1)

    failures = [msg for path in files if (msg := check_file(path))]

    print(f"Checked {len(files)} file(s).")
    if failures:
        print(f"\n❌ {len(failures)} nav inconsistency/inconsistencies found:")
        for f in failures:
            print(f"   {f}")
        sys.exit(1)
    print("✅ All navs consistent with dashboard_template.py's canonical link set.")


if __name__ == '__main__':
    main()
