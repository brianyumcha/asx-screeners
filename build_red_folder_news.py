"""Rebuilds red-folder-news.html: fetches the upcoming ASX/forex economic
calendar from the same public JSON feed ForexFactory's own "get calendar
events" widget uses (FairEconomy, a ForexFactory data partner - this is
the standard source retail EA developers pull this exact feed from), and
renders a static page listing every upcoming HIGH-impact ("red folder")
event.

Only a "this week" feed is available - confirmed 2026-09-18 that even
ForexFactory's own site only exports this same weekly feed (its "Weekly
Export" JSON/CSV/ICS/XML links all point at this exact URL); a real
month-ahead view exists on forexfactory.com itself, but only as an HTML
table behind Cloudflare, not as any kind of export - scraping that would
need a full headless browser in the GitHub Actions job instead of a
plain HTTP fetch, and could silently break if Cloudflare ever blocks the
runner. Not worth that fragility for this.

Shows the FULL Sunday-Saturday calendar week the feed currently covers,
not just what's left of it - filtering to "upcoming only" meant the page
showed almost nothing by Friday/Saturday, since the feed is anchored to
the calendar week rather than rolling from today. Past events in the
week are still shown (dimmed) for context, even though this feed has no
"actual" outcome field to report - only forecast/previous either way.

Times are rendered in Sydney local time, computed server-side at build
time (not left to the viewer's browser), matching the rest of the site's
"Rebuilt daily... Sydney time" convention - this avoids showing the
wrong time to a viewer whose browser is in a different timezone.

USD CPI/PPI/FOMC/NFP releases get an extra visual flag (a distinct row
highlight) on top of the High-impact filter, since those specifically
are what the site owner actually watches within the red-folder tier.
"""
import datetime
import json
import time
from zoneinfo import ZoneInfo

import requests
from dashboard_template import render_nav

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TEMPLATE = "red_folder_news_template.html"
OUTPUT = "red-folder-news.html"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# Extra visual flag for the handful of USD releases that move markets more
# than the rest of the High-impact tier - CPI/PPI (inflation), FOMC (rate
# decisions/statements/minutes), and NFP (ForexFactory's own title for this
# is "Non-Farm Employment Change", not "NFP" - matched on both anyway in
# case that ever changes).
BIG_US_KEYWORDS = ["CPI", "PPI", "FOMC", "Federal Funds Rate", "Non-Farm", "Nonfarm", "NFP"]


def is_big_us_event(country, title):
    if country != "USD":
        return False
    return any(kw.lower() in title.lower() for kw in BIG_US_KEYWORDS)


def fetch_calendar():
    headers = {"User-Agent": "Mozilla/5.0"}
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(CALENDAR_URL, headers=headers, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 30))
                print(f"  ⚠ Rate limited, waiting {wait}s (attempt {attempt+1}/4)...")
                time.sleep(min(wait, 60))
                continue
            last_err = f"HTTP {r.status_code}"
        except Exception as e:
            last_err = str(e)
        time.sleep(5)
    raise RuntimeError(f"Could not fetch calendar feed: {last_err}")


def esc_js(s):
    if not s:
        return ""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def main():
    raw = fetch_calendar()
    print(f"Fetched {len(raw)} total calendar events.")

    now_syd = datetime.datetime.now(SYDNEY_TZ)
    today_syd = now_syd.date()
    tomorrow_syd = today_syd + datetime.timedelta(days=1)

    rows = []
    for item in raw:
        if item.get("impact") != "High":
            continue
        try:
            dt = datetime.datetime.fromisoformat(item["date"])
        except Exception:
            continue
        dt_syd = dt.astimezone(SYDNEY_TZ)
        is_past = dt_syd < now_syd

        d = dt_syd.date()
        if d == today_syd:
            date_label = "Today"
        elif d == tomorrow_syd:
            date_label = "Tomorrow"
        else:
            date_label = dt_syd.strftime("%a %-d %b")
        time_label = dt_syd.strftime("%-I:%M%p").lower()
        country = item.get("country") or "—"
        title = item.get("title") or ""

        rows.append((
            int(dt_syd.timestamp()),
            date_label,
            time_label,
            country,
            title,
            item.get("forecast") or "",
            item.get("previous") or "",
            is_big_us_event(country, title),
            is_past,
        ))

    rows.sort(key=lambda r: r[0])
    upcoming_count = sum(1 for r in rows if not r[8])
    print(f"{len(rows)} High-impact ('red folder') event(s) this week ({upcoming_count} still upcoming).")

    data_lines = []
    for ts, date_label, time_label, country, title, forecast, previous, is_key, is_past in rows:
        data_lines.append(
            f'[{ts},"{esc_js(date_label)}","{esc_js(time_label)}","{esc_js(country)}",'
            f'"{esc_js(title)}","{esc_js(forecast)}","{esc_js(previous)}",'
            f'{"true" if is_key else "false"},{"true" if is_past else "false"}]'
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    build_stamp = now_syd.strftime("%-d %b %Y, %-I:%M%p")
    html = html.replace("<<<BUILD_DATE>>>", build_stamp)
    html = html.replace("<<<NAV_HTML>>>", render_nav(OUTPUT))

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
