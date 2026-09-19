"""Rebuilds earnings-calendar.html: the ASX reporting season calendar,
scraped from CommSec's own public "Reporting Season" page.

Why CommSec and not yfinance (which the rest of this site already uses
for prices): yfinance's per-ticker `.calendar` field is unreliable for
ASX - checked 2026-09-19 against 20 real tickers (BHP, CBA, CSL, WES,
RIO, WOW, TLS, FMG, MIN, PLS, NAB, ANZ, WBC, QAN, JBH, COL, STO, ORG and
more): most just show the LAST reported date, stuck there for weeks
after it passed, because Yahoo doesn't bother rolling it forward until
sometime before the next report. Market Index's own /earnings-calendar
URL 404s. CommSec's page, by contrast, is plain server-rendered HTML
(no Cloudflare block on a normal `requests.get` + User-Agent - confirmed
2026-09-19), and lists 200+ companies with real date/ticker/sector/
DPS/NPAT columns, populated progressively as results land (blank DPS/
NPAT = genuinely not reported yet, not a data gap).

ASX doesn't have a continuous earnings-calendar culture like the US -
there's no single date every company must pre-announce months ahead.
CommSec's page instead covers ONE reporting season at a time: the main
"WEEK 1..4" block (the ~5-week reporting season itself, Feb or Aug) plus
a trailing "Beyond <Month>" section for staggered/late reporters (e.g.
Nov 2026 dates for NUF, ORI, ALL, TNE - confirmed against yfinance's own
lone correctly-updated NUF date, which matched exactly). Outside those
windows the page is retrospective - there's nothing forward-looking to
show until CommSec publishes the next season's calendar (per the site
owner 2026-09-19: show the last completed season, clearly marked as
past, with a note on when the next season is expected, rather than an
empty page for months at a time).

No year is given in each row's date text ("Mon 27 Jul") - inferred from
the season's own month/year (parsed from the page's own "August 2026
Reporting Season Calendar" intro line), wrapping to next year for any
row whose month number is earlier than the season's own month (handles
a "Beyond <season month>" section that runs into the next calendar year,
e.g. a Beyond-February section trailing into December is fine as-is,
but a Beyond-August section trailing into January would need this).
"""
import datetime
import re
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

CALENDAR_URL = "https://www.commsec.com.au/market-news/reporting-season.html"
TEMPLATE = "earnings_calendar_template.html"
OUTPUT = "earnings-calendar.html"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

MIN_ROWS = 50  # circuit breaker - abort rather than publish a near-empty page if CommSec's layout changes

MONTH_NUM = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], start=1
)}
NEXT_SEASON = {"Aug": ("February", 1), "Feb": ("August", 0)}  # (next season name, years to add)


def fetch_html():
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(CALENDAR_URL, headers=headers, timeout=20)
    r.raise_for_status()
    return r.text


def esc_js(s):
    if not s:
        return ""
    return str(s).replace("\\", "\\\\").replace('"', '\\"')


def main():
    html = fetch_html()
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    m = re.search(r"([A-Za-z]+)\s+(\d{4})\s+Reporting Season Calendar", text)
    if not m:
        raise RuntimeError(
            "Could not find the season month/year on CommSec's page (looked for "
            "'<Month> <Year> Reporting Season Calendar') - page layout may have changed."
        )
    season_month_name, season_year = m.group(1), int(m.group(2))
    season_month_abbr = season_month_name[:3]
    season_month_num = MONTH_NUM.get(season_month_abbr)
    if not season_month_num:
        raise RuntimeError(f"Unrecognised season month '{season_month_name}' parsed from CommSec's page.")

    # Year isn't in each row's date text ("Mon 27 Jul") - inferred by walking
    # rows in DOCUMENT order (already chronological: WEEK 1 -> WEEK 2 -> ...
    # -> "Beyond <month>") and bumping the year only when the month number
    # actually decreases from the previous row (a real Dec->Jan wrap), not
    # by comparing against the season's own headline month - the main
    # season block routinely starts a month BEFORE its headline month
    # (WEEK 1 of an "August" season starts in late July), which is not a
    # year wrap.
    year = season_year
    last_mon_num = None
    rows = []
    for table in soup.find_all("table"):
        heading = table.find_previous(["h1", "h2", "h3", "h4", "strong", "b"])
        week_label = heading.get_text(strip=True) if heading else ""
        for tr in table.find_all("tr")[1:]:  # skip header row
            cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
            if len(cells) < 7:
                continue
            date_str, ticker, company, period, sector, dps, npat = cells[:7]
            dm = re.search(r"(\d{1,2})\s+([A-Za-z]{3})", date_str)
            if not dm or not ticker:
                continue
            day, mon_abbr = int(dm.group(1)), dm.group(2)
            mon_num = MONTH_NUM.get(mon_abbr)
            if not mon_num:
                continue
            if last_mon_num is not None and mon_num < last_mon_num:
                year += 1
            last_mon_num = mon_num
            try:
                d = datetime.date(year, mon_num, day)
            except ValueError:
                continue
            rows.append({
                "date": d, "week_label": week_label, "ticker": ticker, "company": company,
                "period": period, "sector": sector, "dps": dps, "npat": npat,
            })

    print(f"Parsed {len(rows)} row(s) across {len(soup.find_all('table'))} table(s).")
    if len(rows) < MIN_ROWS:
        raise RuntimeError(
            f"Only parsed {len(rows)} rows (expected {MIN_ROWS}+) - CommSec's page layout "
            f"likely changed. Aborting without writing a report, so the last good one stays live."
        )

    rows.sort(key=lambda r: (r["date"], r["ticker"]))

    today = datetime.datetime.now(SYDNEY_TZ).date()
    for r in rows:
        reported = bool(r["dps"] or r["npat"])
        if reported:
            r["status"] = "reported"
        elif r["date"] < today:
            r["status"] = "awaiting"  # date passed but CommSec hasn't updated the result yet
        else:
            r["status"] = "upcoming"

    upcoming_count = sum(1 for r in rows if r["status"] != "reported")
    season_active = upcoming_count > 0
    next_season_note = ""
    if not season_active:
        next_name, year_add = NEXT_SEASON.get(season_month_abbr, (None, 0))
        if next_name:
            next_season_note = f"{next_name} {season_year + year_add}"

    print(f"{season_month_name} {season_year} season: {len(rows)} companies, {upcoming_count} not yet reported.")

    data_lines = []
    for r in rows:
        ts = int(datetime.datetime.combine(r["date"], datetime.time(9, 0), SYDNEY_TZ).timestamp())
        data_lines.append(
            f'[{ts},"{esc_js(r["date"].strftime("%a %-d %b"))}","{esc_js(r["week_label"])}",'
            f'"{esc_js(r["ticker"])}","{esc_js(r["company"])}","{esc_js(r["period"])}",'
            f'"{esc_js(r["sector"])}","{esc_js(r["dps"])}","{esc_js(r["npat"])}","{r["status"]}"]'
        )
    data_block = ",\n".join(data_lines)

    with open(TEMPLATE) as f:
        template = f.read()
    for placeholder in ("<<<FULL_DATA>>>", "<<<BUILD_DATE>>>", "<<<SEASON_LABEL>>>", "<<<SEASON_ACTIVE>>>", "<<<NEXT_SEASON_NOTE>>>"):
        assert placeholder in template, f"template placeholder missing: {placeholder}"

    now_syd = datetime.datetime.now(SYDNEY_TZ)
    html_out = template.replace("<<<FULL_DATA>>>", data_block)
    html_out = html_out.replace("<<<BUILD_DATE>>>", now_syd.strftime("%-d %b %Y, %-I:%M%p"))
    html_out = html_out.replace("<<<SEASON_LABEL>>>", f"{season_month_name} {season_year}")
    html_out = html_out.replace("<<<SEASON_ACTIVE>>>", "true" if season_active else "false")
    html_out = html_out.replace("<<<NEXT_SEASON_NOTE>>>", esc_js(next_season_note))

    with open(OUTPUT, "w") as f:
        f.write(html_out)
    print(f"Wrote {OUTPUT} ({len(html_out)} bytes).")


if __name__ == "__main__":
    main()
