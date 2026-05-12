import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

DB_PATH = os.getenv("DB_PATH", "/var/data/app.db")
DEVICE = os.getenv("DEVICE", "pi4")
TARGET_YEAR = int(os.getenv("TARGET_YEAR", "2025"))
TARGET_MONTH = int(os.getenv("TARGET_MONTH", "4"))
TZ = ZoneInfo(os.getenv("REPORT_TZ", "Asia/Manila"))
MAX_GAP_HOURS = 1.0


def parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def main():
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """
        SELECT time, value AS power
        FROM realtime_points
        WHERE device=?
          AND field='power'
          AND time LIKE ?
        ORDER BY time ASC
        """,
        (DEVICE, f"{TARGET_YEAR}-{TARGET_MONTH:02d}-%"),
    ).fetchall()

    parsed = [(parse_time(ts), float(power)) for ts, power in rows]
    daily = {}

    for (t1, p1), (t2, _) in zip(parsed, parsed[1:]):
        gap_hours = (t2 - t1).total_seconds() / 3600.0
        if gap_hours <= 0 or gap_hours > MAX_GAP_HOURS:
            continue

        cursor = t1
        while cursor < t2:
            next_midnight = datetime(cursor.year, cursor.month, cursor.day, tzinfo=cursor.tzinfo) + timedelta(days=1)
            seg_end = min(next_midnight, t2)
            if cursor.year == TARGET_YEAR and cursor.month == TARGET_MONTH:
                daily[cursor.day] = daily.get(cursor.day, 0.0) + p1 * ((seg_end - cursor).total_seconds() / 3600.0) / 1000.0
            cursor = seg_end

    latest_day = max(daily.keys()) if daily else 0
    last_day_to_show = max(latest_day, 24)

    print(f"History-table kWh verification for {TARGET_YEAR}-{TARGET_MONTH:02d}, device={DEVICE}")
    print("Daily values:")
    for day in range(1, last_day_to_show + 1):
        print(f"Apr {day:02d}: {daily.get(day, 0.0):.3f}")

    def period_summary(start_day: int, end_day: int):
        values = [(day, daily.get(day, 0.0)) for day in range(start_day, end_day + 1)]
        total = sum(v for _, v in values)
        avg = total / len(values) if values else 0.0
        latest = values[-1][1] if values else 0.0
        previous = values[-2][1] if len(values) >= 2 else 0.0
        peak_day, peak_kwh = max(values, key=lambda item: item[1]) if values else (0, 0.0)
        print(f"\nApr {start_day}-{end_day} summary boxes:")
        print(f"Latest day kWh: {latest:.2f}")
        print(f"Previous day kWh: {previous:.2f}")
        print(f"Average/day: {avg:.3f}")
        print(f"Visible {end_day - start_day + 1}d kWh: {total:.3f}")
        print(f"Peak day: Apr {peak_day}")
        print(f"Peak kWh: {peak_kwh:.3f}")

    period_summary(1, 14)
    period_summary(15, 24)
    conn.close()


if __name__ == "__main__":
    main()
