import os
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# Change this in Render Shell if your graph month is different:
#   TARGET_YEAR=2026 python inject_april_targets.py
TARGET_YEAR = int(os.getenv("TARGET_YEAR", "2025"))
TARGET_MONTH = 4
DEVICE = os.getenv("DEVICE", "pi4")
DB_PATH = os.getenv("DB_PATH", "/var/data/app.db")
TZ = ZoneInfo(os.getenv("REPORT_TZ", "Asia/Manila"))

# Exact daily kWh targets from the reference screenshots.
# Apr 4 and Apr 5 are intentionally omitted: no readings, no zero V/A/W rows.
DAILY_KWH = {
    1: 4.841,
    2: 2.673,
    3: 0.734,
    6: 10.61,
    7: 12.428,
    8: 11.648,
    9: 2.283,
    10: 1.993,
    11: 1.704,
    12: 0.955,
    13: 8.146,
    14: 12.36,
    15: 0.69,
    16: 6.28,
    17: 4.55,
    18: 4.90,
    19: 3.94,
    20: 12.76,
    21: 6.09,
    22: 14.86,
    23: 0.64,
    24: 5.46,
}

FIELDS = ("rms_voltage", "rms_current", "power", "power_factor")


def iso_local(day: int, hour: int, minute: int, second: int = 0) -> str:
    dt = datetime(TARGET_YEAR, TARGET_MONTH, day, hour, minute, second, tzinfo=TZ)
    return dt.isoformat()


def has_next_immediate_day(day: int) -> bool:
    return (day + 1) in DAILY_KWH


def day_interval_hours(day: int) -> float:
    # 96 quarter-hour points normally represent 24 hours because the last
    # 23:45 reading is closed by the next day's 00:00 reading.
    # Apr 3 and Apr 24 have no immediate next-day reading, so a same-day
    # 23:59:59 closer is added instead. That gives 23h 59m 59s.
    if has_next_immediate_day(day):
        return 24.0
    return (24 * 3600 - 1) / 3600.0


def insert_metric_rows(conn: sqlite3.Connection, ts: str, voltage: float, power: float, pf: float):
    current = power / (voltage * pf)
    rows = [
        (DEVICE, "rms_voltage", ts, round(voltage, 2)),
        (DEVICE, "rms_current", ts, round(current, 3)),
        (DEVICE, "power", ts, round(power, 6)),
        (DEVICE, "power_factor", ts, round(pf, 3)),
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO realtime_points (device, field, time, value)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")

    # Required tables/indexes, in case shell is run before app startup.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS realtime_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            field TEXT NOT NULL,
            time TEXT NOT NULL,
            value REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_realtime_device_field_time
        ON realtime_points(device, field, time)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS monthly_billing_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            cost_per_kwh REAL NOT NULL DEFAULT 0,
            kwh REAL NOT NULL DEFAULT 0,
            bill_php REAL NOT NULL DEFAULT 0,
            is_finalized INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(year, month)
        )
        """
    )

    # Remove only April target readings for this device. Notes are not touched.
    patterns = [
        f"{TARGET_YEAR}-04-%",      # ISO format
        f"4/%/{TARGET_YEAR}%",     # m/d/yyyy format
        f"04/%/{TARGET_YEAR}%",    # mm/d/yyyy format
    ]
    deleted = 0
    for pat in patterns:
        cur = conn.execute(
            "DELETE FROM realtime_points WHERE device=? AND time LIKE ?",
            (DEVICE, pat),
        )
        deleted += cur.rowcount if cur.rowcount is not None else 0

    inserted_points = 0

    for day, kwh in DAILY_KWH.items():
        hours = day_interval_hours(day)
        power = (kwh * 1000.0) / hours

        for slot in range(96):
            hour = slot // 4
            minute = (slot % 4) * 15
            # Slightly varied but non-zero sensor values.
            voltage = 232.0 + ((day + slot) % 9) * 1.35
            pf = 0.42 + ((day + slot) % 5) * 0.01
            ts = iso_local(day, hour, minute)
            insert_metric_rows(conn, ts, voltage, power, pf)
            inserted_points += 1

        if not has_next_immediate_day(day):
            # Closes the last interval for days followed by a no-reading gap or month end.
            # Use tiny non-zero power so there are no 0V/0A/0W rows.
            ts = iso_local(day, 23, 59, 59)
            insert_metric_rows(conn, ts, 235.0, 1.0, 0.42)
            inserted_points += 1

    total_kwh = round(sum(DAILY_KWH.values()), 3)
    old_rate_row = conn.execute(
        "SELECT cost_per_kwh FROM monthly_billing_rates WHERE year=? AND month=?",
        (TARGET_YEAR, TARGET_MONTH),
    ).fetchone()
    rate = float(old_rate_row[0]) if old_rate_row else 0.0
    conn.execute(
        """
        INSERT INTO monthly_billing_rates (
            year, month, cost_per_kwh, kwh, bill_php, is_finalized, updated_at
        )
        VALUES (?, ?, ?, ?, ROUND(? * ?, 2), 1, datetime('now'))
        ON CONFLICT(year, month)
        DO UPDATE SET
            kwh=excluded.kwh,
            bill_php=excluded.bill_php,
            is_finalized=excluded.is_finalized,
            updated_at=excluded.updated_at
        """,
        (TARGET_YEAR, TARGET_MONTH, rate, total_kwh, total_kwh, rate),
    )

    conn.commit()

    # Verification using the same integration logic used by the frontend graph.
    rows = conn.execute(
        """
        SELECT time, value AS power
        FROM realtime_points
        WHERE device=? AND field='power' AND time LIKE ?
        ORDER BY time ASC
        """,
        (DEVICE, f"{TARGET_YEAR}-04-%"),
    ).fetchall()

    parsed = []
    for ts, power in rows:
        parsed.append((datetime.fromisoformat(str(ts)).astimezone(TZ), float(power)))

    daily = {day: 0.0 for day in range(1, 25)}
    for (t1, p1), (t2, _) in zip(parsed, parsed[1:]):
        gap_hours = (t2 - t1).total_seconds() / 3600.0
        if gap_hours <= 0 or gap_hours > 1:
            continue
        current = t1
        while current < t2:
            next_midnight = datetime(current.year, current.month, current.day, tzinfo=current.tzinfo) + timedelta(days=1)
            seg_end = min(next_midnight, t2)
            if current.month == TARGET_MONTH and 1 <= current.day <= 24:
                daily[current.day] += p1 * ((seg_end - current).total_seconds() / 3600.0) / 1000.0
            current = seg_end

    print("Deleted old April realtime rows:", deleted)
    print("Inserted timestamp points:", inserted_points)
    print("Inserted realtime rows:", inserted_points * 4)
    print("Notes table untouched.")
    print("\nDaily kWh check:")
    for day in range(1, 25):
        print(f"Apr {day:02d}: {daily[day]:.3f}")
    print("\nApr 1-14 total:", round(sum(daily[d] for d in range(1, 15)), 3))
    print("Apr 15-24 total:", round(sum(daily[d] for d in range(15, 25)), 3))
    print("Month total:", total_kwh)
    conn.close()


if __name__ == "__main__":
    main()
