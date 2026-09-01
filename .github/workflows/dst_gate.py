"""
Decides whether the current scheduled workflow trigger should actually run,
and whether it's a "full run" (OBV + Pullback + HH) or "HH only".

GitHub Actions cron triggers are fixed UTC times — there's no native way to
say "10:30am Sydney time, DST-aware". Workaround: scan.yml registers BOTH an
AEST-times and an AEDT-times cron entry for every slot (see comments there).
At runtime, this script checks the real current Australia/Sydney UTC offset
and only lets the entry matching the *actual* current season proceed; the
other season's entry fires too (GitHub doesn't know about DST either) but
gets skipped here as a no-op.

One slot (3:30pm AEST / 4:30pm AEDT) happens to land on the same UTC cron
string in both seasons, so it's marked "always valid" below and never skipped.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

# cron string -> (season it belongs to, is it the full-run slot)
# season: "AEST", "AEDT", or None (valid in both seasons, no gating needed)
SLOTS = {
    "30 0 * * 1-5":  ("AEST", False),  # 10:30am AEST
    "0 3 * * 1-5":   ("AEST", False),  # 1:00pm AEST
    "30 6 * * 1-5":  ("AEST", False),  # 4:30pm AEST
    "0 7 * * 1-5":   ("AEST", True),   # 5:00pm AEST — full run
    "30 23 * * 0-4": ("AEDT", False),  # 10:30am AEDT (crosses into previous UTC day)
    "0 2 * * 1-5":   ("AEDT", False),  # 1:00pm AEDT
    "30 4 * * 1-5":  ("AEDT", False),  # 3:30pm AEDT
    "0 6 * * 1-5":   ("AEDT", True),   # 5:00pm AEDT — full run
    "30 5 * * 1-5":  (None, False),    # 3:30pm AEST == 4:30pm AEDT (same UTC instant)
}

event_name = os.environ["GITHUB_EVENT_NAME"]
schedule = os.environ.get("EVENT_SCHEDULE", "")
is_dst = datetime.now(ZoneInfo("Australia/Sydney")).dst().total_seconds() > 0
current_season = "AEDT" if is_dst else "AEST"

if event_name == "workflow_dispatch":
    should_run, full_run = True, True
else:
    season, full = SLOTS.get(schedule, (None, False))
    should_run = season is None or season == current_season
    full_run = should_run and full

print(f"event={event_name} schedule={schedule!r} current_season={current_season} "
      f"should_run={should_run} full_run={full_run}")

with open(os.environ["GITHUB_OUTPUT"], "a") as f:
    f.write(f"should_run={'true' if should_run else 'false'}\n")
    f.write(f"full_run={'true' if full_run else 'false'}\n")
