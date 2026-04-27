import re
import sqlite3
import sys
from datetime import timezone
from zoneinfo import ZoneInfo
from dateutil import parser as dp

DB = "/var/data/app.db"
DEV = "pi4"
ROWS_FILE = "manual_rows.txt"
DRY = "--dry-run" in sys.argv
TZ = ZoneInfo("Asia/Manila")

ROW = re.compile(
    r"^(\d{1,2}/\d{1,2}/\d{4},\s+\d{1,2}:\d{2}:\d{2}\s+[AP]M)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s+"
    r"([-+]?\d+(?:\.\d+)?)\s*$"
)

FIELDS = ["rms_voltage", "rms_current", "power", "power_factor"]

def show(dt):
    h = dt.hour % 12 or 12
    ap = "AM" if dt.hour < 12 else "PM"
    return f"{dt.month}/{dt.day}/{dt.year}, {h}:{dt.minute:02d}:{dt.second:02d} {ap}"

def parse_db_time(raw):
    try:
        return dp.parse(str(raw).replace("Z", "+00:00"))
    except Exception:
        return None

def local_month(raw):
    dt = parse_db_time(raw)
    if not dt:
        return None
    if dt.tzinfo:
        dt = dt.astimezone(TZ)
    return dt.year, dt.month

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
con.execute("PRAGMA busy_timeout=5000")

lookup = {}
for r in con.execute("SELECT DISTINCT time FROM realtime_points WHERE device=?", (DEV,)):
    raw = str(r["time"]).strip()
    if raw:
        lookup[raw] = raw

    dt = parse_db_time(raw)
    if not dt:
        continue

    if dt.tzinfo:
        lookup[show(dt.astimezone(TZ))] = raw
        lookup[show(dt.astimezone(timezone.utc))] = raw
    else:
        lookup[show(dt)] = raw
        lookup[show(dt.replace(tzinfo=timezone.utc).astimezone(TZ))] = raw

manual_db_times = set()
matched = 0
not_found = 0
bad = 0
changed = 0

with open(ROWS_FILE, "r", encoding="utf-8", errors="replace") as f:
    for line_no, line in enumerate(f, 1):
        text = line.strip()

        if not text or text.lower().startswith("time "):
            continue

        m = ROW.match(text)

        if not m:
            print("BAD LINE", line_no, ":", text[:120])
            bad += 1
            continue

        time_text = m.group(1)
        vals = [float(x) for x in m.groups()[1:]]
        db_time = lookup.get(time_text)

        if not db_time:
            print("NOT FOUND:", time_text)
            not_found += 1
            continue

        manual_db_times.add(db_time)
        matched += 1

        for field, val in zip(FIELDS, vals):
            old = con.execute(
                "SELECT id,value FROM realtime_points WHERE device=? AND time=? AND field=? LIMIT 1",
                (DEV, db_time, field),
            ).fetchone()

            if not old:
                continue

            oldv = float(old["value"])

            if abs(oldv - val) > 0.000001:
                con.execute(
                    "UPDATE realtime_points SET value=? WHERE id=?",
                    (val, int(old["id"])),
                )
                changed += 1
                if changed <= 20:
                    print(f"{time_text} | {field}: {oldv} -> {val}")

all_april_times = set()
for r in con.execute(
    "SELECT DISTINCT time FROM realtime_points WHERE device=? AND field IN ('rms_voltage','rms_current','power','power_factor')",
    (DEV,),
):
    raw = str(r["time"]).strip()
    ym = local_month(raw)
    if ym == (2026, 4):
        all_april_times.add(raw)

extra_times = sorted(all_april_times - manual_db_times)

deleted_rows = 0
for t in extra_times:
    cur = con.execute(
        "DELETE FROM realtime_points WHERE device=? AND time=? AND field IN ('rms_voltage','rms_current','power','power_factor')",
        (DEV, t),
    )
    deleted_rows += cur.rowcount

kwh = con.execute(
    """
    SELECT ROUND(SUM(value * 0.25 / 1000.0), 2) AS kwh
    FROM realtime_points
    WHERE device=? AND field='power' AND time LIKE '2026-04-%'
    """,
    (DEV,),
).fetchone()["kwh"] or 0

con.execute(
    """
    UPDATE monthly_billing_rates
    SET
      kwh=?,
      bill_php=ROUND(cost_per_kwh * ?, 2),
      updated_at=datetime('now')
    WHERE year=2026 AND month=4
    """,
    (kwh, kwh),
)

if DRY:
    con.rollback()
else:
    con.commit()

con.close()

print("")
print("Mode:", "DRY RUN - NOT SAVED" if DRY else "LIVE - SAVED")
print("Matched manual rows:", matched)
print("Not found:", not_found)
print("Bad lines:", bad)
print("Changed values:", changed)
print("Extra April timestamps removed:", len(extra_times))
print("Realtime rows deleted:", deleted_rows)
print("April kWh:", kwh)
