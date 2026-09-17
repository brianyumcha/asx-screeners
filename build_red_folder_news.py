"""Rebuilds red-folder-news.html: fetches the upcoming ASX/forex economic
calendar from the same public JSON feed ForexFactory's own "get calendar
events" widget uses (FairEconomy, a ForexFactory data partner - this is
the standard source retail EA developers pull this exact feed from), and
renders a static page listing every upcoming HIGH-impact ("red folder")
event.

Only a "this week" feed is available (no "next week" endpoint exists on
this source as of 2026-09-17), so the visible window is capped at the
end of the current calendar week - it naturally rolls forward as the
feed itself updates for the new week. Only future events (Sydney time)
are shown - anything already released this week is dropped.

Times are rendered in Sydney local time, computed server-side at build
time (not left to the viewer's browser), matching the rest of the site's
"Rebuilt daily... Sydney time" convention - this avoids showing the
wrong time to a viewer whose browser is in a different timezone.
"""
import datetime
import json
import time
from zoneinfo import ZoneInfo

import requests

CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TEMPLATE = "red_folder_news_template.html"
OUTPUT = "red-folder-news.html"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")


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
        if dt_syd < now_syd:
            continue  # only upcoming events

        d = dt_syd.date()
        if d == today_syd:
            date_label = "Today"
        elif d == tomorrow_syd:
            date_label = "Tomorrow"
        else:
            date_label = dt_syd.strftime("%a %-d %b")
        time_label = dt_syd.strftime("%-I:%M%p").lower()

        rows.append((
            int(dt_syd.timestamp()),
            date_label,
            time_label,
            item.get("country") or "—",
            item.get("title") or "",
            item.get("forecast") or "",
            item.get("previous") or "",
        ))

    rows.sort(key=lambda r: r[0])
    print(f"{len(rows)} upcoming High-impact ('red folder') event(s).")

    data_lines = []
    for ts, date_label, time_label, country, title, forecast, previous in rows:
        data_lines.append(
            f'[{ts},"{esc_js(date_label)}","{esc_js(time_label)}","{esc_js(country)}",'
            f'"{esc_js(title)}","{esc_js(forecast)}","{esc_js(previous)}"]'
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE) as f:
        template = f.read()
    assert "<<<FULL_DATA>>>" in template, "template placeholder missing"
    html = template.replace("<<<FULL_DATA>>>", data_block)
    build_stamp = now_syd.strftime("%-d %b %Y, %-I:%M%p")
    html = html.replace("<<<BUILD_DATE>>>", build_stamp)

    with open(OUTPUT, "w") as f:
        f.write(html)
    print(f"Wrote {OUTPUT} ({len(html)} bytes).")


if __name__ == "__main__":
    main()
