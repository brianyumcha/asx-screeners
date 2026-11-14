"""
Populates _site/ for GitHub Pages deployment from the repo's generated
HTML pages. Single source of truth for "what pages does this site have" -
every deploy workflow (scan.yml, deploy_only.yml, materials_index.yml,
insider_index.yml, wave2_scanner.yml, options_screener.yml) calls this
instead of each hand-listing its own `cp` commands into _site/.

That hand-listing is what caused the live site to lose mtm-call-
archive.html, craig-bear-map.html and lower-low.html on 2026-11-09:
3 of those 6 workflows' lists had drifted out of sync and were each
missing those 3 pages, and since every workflow shares the same GitHub
Pages deployment target with full-REPLACE semantics (not a merge), the
next one of those 3 incomplete workflows to run silently deleted those
pages from the live site. Six independently-maintained copies of the
same list is exactly the kind of thing that drifts - this collapses
them into one.

A source file not present when this runs is just skipped (not an
error) - a given workflow may not have generated some of these pages
itself (e.g. deploy_only.yml never runs the screener scans), and relies
on whatever's already checked out, or produced earlier in the SAME run
by another step.

For the gitignored screener pages (pre-breakout/pullback/momentum/
higher-high/lower-low/volume-anomaly), a workflow that doesn't generate
them itself needs a THIRD option: fall back to whatever's currently
live, so deploying doesn't just delete them. That used to be each
workflow's own separate hand-maintained curl block (a "preserve
existing screener pages" step) - the same drift problem this script
already fixed once at the `cp` layer, just one level up: those curl
blocks had themselves drifted and were missing lower-low.html and
volume-anomaly.html entirely (found 2026-11-09, right after fixing the
first drift - materials_index.yml's redeploy still 404'd lower-low.html
even with this script wired in, because nothing had fetched it locally
for this script to find). Pass --preserve-from-live to fold that
fallback in here instead, as a THIRD source candidate behind a page's
local names - one list, one place, instead of yet another copy to drift.
Omit the flag for scan.yml specifically: it generates every screener
page itself, so silently falling back to a stale live copy there would
mask a real generation failure instead of surfacing it.

Usage (from within a workflow, after mkdir -p _site and any build
steps):
    python3 deploy_site.py
    python3 deploy_site.py --preserve-from-live   # workflows that don't
                                                    # generate their own
                                                    # screener pages
"""
import os
import shutil
import sys
import urllib.error
import urllib.request

SITE_BASE_URL = "https://brianyumcha.github.io/asx-screeners/"

SITE_DIR = "_site"

# published path -> source filename(s) to try, in order, taking the
# first that exists. Identity mappings (name unchanged) are listed
# anyway for completeness/auditability - this IS the canonical list of
# every page this site serves, so a new page gets added HERE once, not
# once per workflow.
#
# The 6 screener pages (pre-breakout/pullback/momentum/higher-high/
# lower-low/volume-anomaly) list TWO source candidates: the screener
# script's own native output filename (e.g. "asx_ll_results.html",
# produced by a workflow that actually runs that scan), and the already-
# published name (e.g. "lower-low.html") - deploy_only.yml never runs
# the scans itself, so it curls these down from the LIVE site under
# their published names instead, specifically so this step has
# something to re-publish rather than silently deleting them.
PAGES = {
    "index.html": ["index.html"],
    "materials-index.html": ["materials-index.html"],
    "healthcare-index.html": ["healthcare-index.html"],
    "energy-index.html": ["energy-index.html"],
    "tech-index.html": ["tech-index.html"],
    "real-estate-index.html": ["real-estate-index.html"],
    "financials-index.html": ["financials-index.html"],
    "staples-index.html": ["staples-index.html"],
    "discretionary-index.html": ["discretionary-index.html"],
    "industrials-index.html": ["industrials-index.html"],
    "utilities-index.html": ["utilities-index.html"],
    "comms-index.html": ["comms-index.html"],
    "red-folder-news.html": ["red-folder-news.html"],
    "sector-rotation.html": ["sector-rotation.html"],
    "earnings-calendar.html": ["earnings-calendar.html"],
    "insider-index.html": ["insider-index.html"],
    "options-us-csp.html": ["options-us-csp.html"],
    "options-us-etf.html": ["options-us-etf.html"],
    "conviction-stack.html": ["conviction-stack.html"],
    "wave2-scanner.html": ["wave2-scanner.html"],
    "craig-bullish-map.html": ["craig-bullish-map.html"],
    "craig-bear-map.html": ["craig-bear-map.html"],
    "mtm-call-archive.html": ["mtm-call-archive.html"],
    "timing-the-carts.html": ["timing-the-carts.html"],
    "morning-brief.html": ["morning-brief.html"],
    "morning-brief-manifest.json": ["morning-brief-manifest.json"],
    "morning-brief-icon-192.png": ["morning-brief-icon-192.png"],
    "morning-brief-icon-512.png": ["morning-brief-icon-512.png"],
    "morning-brief-apple-touch-icon.png": ["morning-brief-apple-touch-icon.png"],
    "morning-brief-icon-maskable-192.png": ["morning-brief-icon-maskable-192.png"],
    "morning-brief-icon-maskable-512.png": ["morning-brief-icon-maskable-512.png"],
    "pre-breakout.html": ["asx_breakout_results.html", "pre-breakout.html"],
    "pullback.html": ["asx_pullback_results.html", "pullback.html"],
    "momentum.html": ["asx_momentum_results.html", "momentum.html"],
    "higher-high.html": ["asx_hh_results.html", "higher-high.html"],
    "lower-low.html": ["asx_ll_results.html", "lower-low.html"],
    "volume-anomaly.html": ["asx_volume_anomaly_results.html", "volume-anomaly.html"],
}


def fetch_live(dest):
    """Best-effort fetch of the currently-live copy of `dest`, saved to the
    repo root under that same name - matches the old `curl -sf -o dest url
    || true` preserve-step behaviour: a failure (404, network error) is
    silent, not an error, since a page that's never been live yet (first-
    ever run) is a normal case, not a bug."""
    try:
        with urllib.request.urlopen(SITE_BASE_URL + dest, timeout=15) as resp:
            data = resp.read()
    except (urllib.error.URLError, TimeoutError):
        return False
    with open(dest, "wb") as f:
        f.write(data)
    return True


def main():
    preserve_from_live = "--preserve-from-live" in sys.argv
    os.makedirs(SITE_DIR, exist_ok=True)
    copied, missing, fetched = [], [], []
    for dest, sources in PAGES.items():
        src = next((s for s in sources if os.path.isfile(s)), None)
        if not src and preserve_from_live and fetch_live(dest):
            src = dest
            fetched.append(dest)
        if src:
            shutil.copy(src, os.path.join(SITE_DIR, dest))
            copied.append(dest)
        else:
            missing.append(dest)
    print(f"deploy_site.py: copied {len(copied)}/{len(PAGES)} page(s) into {SITE_DIR}/.")
    if fetched:
        print(f"  Fetched from live (not generated/checked out this run): {', '.join(fetched)}")
    if missing:
        print(f"  Not present this run (fine if this workflow doesn't generate them): {', '.join(missing)}")


if __name__ == "__main__":
    main()
