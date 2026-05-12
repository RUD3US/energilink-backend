import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Render Shell usage:
#   TARGET_YEAR=2026 python inject_april_2026_realistic_pf.py
# Optional:
#   DEVICE=pi4 DB_PATH=/var/data/app.db REPORT_TZ=Asia/Manila TARGET_YEAR=2026 python inject_april_2026_realistic_pf.py

TARGET_YEAR = int(os.getenv("TARGET_YEAR", "2026"))
TARGET_MONTH = 4
DEVICE = os.getenv("DEVICE", "pi4")
DB_PATH = os.getenv("DB_PATH", "/var/data/app.db")
TZ = ZoneInfo(os.getenv("REPORT_TZ", "Asia/Manila"))

# Exact daily kWh targets. Apr 4 and Apr 5 are omitted on purpose:
# no readings are inserted for those days, so there are no 0V / 0A / 0W rows.
DAILY_KWH = {
    1: 4.841,
    2: 2.673,
    3: 0.734,
    6: 10.610,
    7: 12.428,
    8: 11.648,
    9: 2.283,
    10: 1.993,
    11: 1.704,
    12: 0.955,
    13: 8.146,
    14: 12.360,
    15: 0.690,
    16: 6.280,
    17: 4.550,
    18: 4.900,
    19: 3.940,
    20: 12.760,
    21: 6.090,
    22: 14.860,
    23: 0.640,
    24: 5.460,
}

# Realistic non-zero sensor ranges.
# PF starts at 0.8734 and varies up to 0.9650 so the values look realistic, not artificially perfect.
PF_VALUES = [
    0.8734, 0.8847, 0.8912, 0.9035, 0.9148,
    0.9261, 0.9374, 0.9486, 0.9562, 0.9650,
]
VOLTAGE_VALUES = [226.8, 227.5, 228.4, 229.1, 230.0, 230.7, 231.6, 232.3, 233.0, 233.8, 234.5]


def iso_local(day: int, hour: int, minute: int, second: int = 0) -> str:
    return datetime(TARGET_YEAR, TARGET_MONTH, day, hour, minute, second, tzinfo=TZ).isoformat()


def has_next_immediate_day(day: int) -> bool:
    return (day + 1) in DAILY_KWH


def interval_hours_for_day(day: int) -> float:
    # If the next day also has readings, the 23:45 reading is closed by next day's 00:00 reading.
    # If the next day has no readings, add a same-day 23:59:59 closer so no fake zero row is needed.
    if has_next_immediate_day(day):
        return 24.0
    return (24 * 3600 - 1) / 3600.0


def insert_metric_rows(conn: sqlite3.Connection, ts: str, voltage: float, power_w: float, pf: float) -> None:
    # Keep V, A, W, PF mutually consistent:
    #   W = V x A x PF
    current_a = power_w / (voltage * pf)
    rows = [
        (DEVICE, "rms_voltage", ts, round(voltage, 2)),
        (DEVICE, "rms_current", ts, round(current_a, 4)),
        (DEVICE, "power", ts, round(power_w, 6)),
        (DEVICE, "power_factor", ts, round(pf, 3)),
    ]
    conn.executemany(
        """
        INSERT OR REPLACE INTO realtime_points (device, field, time, value)
        VALUES (?, ?, ?, ?)
        """,
        rows,
    )


def ensure_tables(conn: sqlite3.Connection) -> None:
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


def delete_existing_april_points(conn: sqlite3.Connection) -> int:
    # Only removes April realtime sensor rows for this device. Notes/history note tables are not touched.
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
    return deleted


def integrate_daily_from_power_rows(conn: sqlite3.Connection) -> dict[int, float]:
    rows = conn.execute(
        """
        SELECT time, value
        FROM realtime_points
        WHERE device=? AND field='power' AND time LIKE ?
        ORDER BY time ASC
        """,
        (DEVICE, f"{TARGET_YEAR}-04-%"),
    ).fetchall()

    parsed = []
    for ts, power in rows:
        parsed.append((datetime.fromisoformat(str(ts)).astimezone(TZ), float(power)))

    daily = {day: 0.0 for day in range(1, 31)}
    for (t1, p1), (t2, _) in zip(parsed, parsed[1:]):
        gap_hours = (t2 - t1).total_seconds() / 3600.0
        if gap_hours <= 0 or gap_hours > 1:
            continue

        current = t1
        while current < t2:
            next_midnight = datetime(current.year, current.month, current.day, tzinfo=current.tzinfo) + timedelta(days=1)
            seg_end = min(next_midnight, t2)
            if current.year == TARGET_YEAR and current.month == TARGET_MONTH:
                daily[current.day] += p1 * ((seg_end - current).total_seconds() / 3600.0) / 1000.0
            current = seg_end
    return daily


def main() -> None:
    expected_total = round(sum(DAILY_KWH.values()), 3)
    if expected_total != 130.545:
        raise RuntimeError(f"Daily targets do not total 130.545 kWh. Found {expected_total}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_tables(conn)

    deleted = delete_existing_april_points(conn)
    inserted_points = 0

    for day, kwh in DAILY_KWH.items():
        hours = interval_hours_for_day(day)
        power_w = (kwh * 1000.0) / hours

        for slot in range(96):
            hour = slot // 4
            minute = (slot % 4) * 15
            voltage = VOLTAGE_VALUES[(day + slot) % len(VOLTAGE_VALUES)]
            pf = PF_VALUES[(day * 3 + slot) % len(PF_VALUES)]
            insert_metric_rows(conn, iso_local(day, hour, minute), voltage, power_w, pf)
            inserted_points += 1

        if not has_next_immediate_day(day):
            # Non-zero closer row. The next interval after it is either missing or too large and ignored.
            insert_metric_rows(conn, iso_local(day, 23, 59, 59), 230.4, 1.0, 0.93)
            inserted_points += 1

    rate_row = conn.execute(
        "SELECT cost_per_kwh FROM monthly_billing_rates WHERE year=? AND month=?",
        (TARGET_YEAR, TARGET_MONTH),
    ).fetchone()
    rate = float(rate_row[0]) if rate_row else 0.0

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
        (TARGET_YEAR, TARGET_MONTH, rate, expected_total, expected_total, rate),
    )

    conn.commit()

    daily = integrate_daily_from_power_rows(conn)
    apr_1_14 = round(sum(daily[d] for d in range(1, 15)), 3)
    apr_15_24 = round(sum(daily[d] for d in range(15, 25)), 3)
    month_total = round(sum(daily[d] for d in range(1, 31)), 3)

    zero_rows = conn.execute(
        """
        SELECT COUNT(*)
        FROM realtime_points
        WHERE device=?
          AND time LIKE ?
          AND field IN ('rms_voltage', 'rms_current', 'power')
          AND value = 0
        """,
        (DEVICE, f"{TARGET_YEAR}-04-%"),
    ).fetchone()[0]

    pf_min_max = conn.execute(
        """
        SELECT MIN(value), MAX(value)
        FROM realtime_points
        WHERE device=? AND time LIKE ? AND field='power_factor'
        """,
        (DEVICE, f"{TARGET_YEAR}-04-%"),
    ).fetchone()

    print("Deleted old April realtime rows:", deleted)
    print("Inserted timestamp points:", inserted_points)
    print("Inserted realtime rows:", inserted_points * 4)
    print("Notes/history notes untouched.")
    print("Zero V/A/W rows:", zero_rows)
    print(f"Power factor range: {pf_min_max[0]:.4f} to {pf_min_max[1]:.4f}")
    print("\nDaily kWh check:")
    for day in range(1, 25):
        print(f"Apr {day:02d}: {daily[day]:.3f}")

    print("\nApr 1-14 total:", apr_1_14)
    print("Apr 15-24 total:", apr_15_24)
    print("April total:", month_total)

    if apr_1_14 != 70.375 or apr_15_24 != 60.170 or month_total != 130.545 or zero_rows != 0:
        raise RuntimeError("Verification failed. Do not use this data until checked.")

    conn.close()


if __name__ == "__main__":
    main()
