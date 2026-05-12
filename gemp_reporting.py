import os
import json
import sqlite3
import calendar
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

DB_PATH = os.getenv("DB_PATH", "/home/ee/power-backend/app.db")
REPORT_TZ = os.getenv("REPORT_TZ", "Asia/Manila")
ARCHIVE_INTERVAL_HOURS = float(os.getenv("ARCHIVE_INTERVAL_HOURS", "0.25"))
MAX_INTERVAL_GAP_HOURS = float(os.getenv("ARCHIVE_MAX_GAP_HOURS", "6"))

MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_to_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(REPORT_TZ))


def configure_sqlite(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    configure_sqlite(conn)
    return conn


def ensure_report_tables(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_schedule (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            frequency TEXT NOT NULL DEFAULT 'weekly',
            send_time TEXT NOT NULL DEFAULT '08:00',
            day_of_week INTEGER,
            day_of_month INTEGER,
            enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS report_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_type TEXT NOT NULL,
            schedule_key TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            sent_at TEXT,
            status TEXT NOT NULL,
            message TEXT
        );
        """
    )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_report_runs_type_key
        ON report_runs(report_type, schedule_key)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS gemp_report_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload_json TEXT NOT NULL DEFAULT '{}',
            updated_at TEXT NOT NULL
        );
        """
    )

    row = conn.execute("SELECT 1 FROM report_schedule WHERE id=1").fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO report_schedule (
                id, frequency, send_time, day_of_week, day_of_month, enabled, updated_at
            ) VALUES (1, 'weekly', '08:00', 0, 1, 0, ?)
            """,
            (now_iso(),),
        )

    row = conn.execute("SELECT 1 FROM gemp_report_config WHERE id=1").fetchone()
    if not row:
        conn.execute(
            """
            INSERT INTO gemp_report_config (id, payload_json, updated_at)
            VALUES (1, ?, ?)
            """,
            (json.dumps({}), now_iso()),
        )

    conn.commit()


def list_report_recipients(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, email, is_active, created_at FROM report_recipients ORDER BY email ASC"
    ).fetchall()
    return [dict(r) for r in rows]


def add_report_recipient(conn: sqlite3.Connection, email: str) -> Dict[str, Any]:
    email = email.strip().lower()
    if not email:
        raise ValueError("Email is required")

    conn.execute(
        """
        INSERT INTO report_recipients (email, is_active, created_at)
        VALUES (?, 1, ?)
        ON CONFLICT(email) DO UPDATE SET is_active=1
        """,
        (email, now_iso()),
    )
    conn.commit()

    row = conn.execute(
        "SELECT id, email, is_active, created_at FROM report_recipients WHERE email=?",
        (email,),
    ).fetchone()
    return dict(row)


def delete_report_recipient(conn: sqlite3.Connection, recipient_id: int):
    conn.execute("DELETE FROM report_recipients WHERE id=?", (recipient_id,))
    conn.commit()


def get_active_recipient_emails(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT email FROM report_recipients WHERE is_active=1 ORDER BY email ASC"
    ).fetchall()
    return [str(r["email"]) for r in rows]


def get_report_schedule(conn: sqlite3.Connection) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM report_schedule WHERE id=1").fetchone()
    if not row:
        ensure_report_tables(conn)
        row = conn.execute("SELECT * FROM report_schedule WHERE id=1").fetchone()
    return dict(row)


def upsert_report_schedule(
    conn: sqlite3.Connection,
    *,
    frequency: str,
    send_time: str,
    day_of_week: Optional[int],
    day_of_month: Optional[int],
    enabled: int,
) -> Dict[str, Any]:
    frequency = frequency.strip().lower()
    if frequency not in {"weekly", "monthly"}:
        raise ValueError("frequency must be weekly or monthly")

    if not send_time or ":" not in send_time:
        raise ValueError("send_time must be HH:MM")

    hh, mm = send_time.split(":", 1)
    hour = int(hh)
    minute = int(mm)
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("send_time must be valid HH:MM")

    if frequency == "weekly":
        if day_of_week is None:
            day_of_week = 0
        if day_of_week < 0 or day_of_week > 6:
            raise ValueError("day_of_week must be 0..6")
        day_of_month = None

    if frequency == "monthly":
        if day_of_month is None:
            day_of_month = 1
        if day_of_month < 1 or day_of_month > 28:
            raise ValueError("day_of_month must be 1..28")
        day_of_week = None

    # Use UPSERT instead of UPDATE-only so Save Schedule still works even if
    # the existing id=1 row was missing in an older deployed database.
    conn.execute(
        """
        INSERT INTO report_schedule (
            id, frequency, send_time, day_of_week, day_of_month, enabled, updated_at
        )
        VALUES (1, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            frequency=excluded.frequency,
            send_time=excluded.send_time,
            day_of_week=excluded.day_of_week,
            day_of_month=excluded.day_of_month,
            enabled=excluded.enabled,
            updated_at=excluded.updated_at
        """,
        (
            frequency,
            send_time,
            day_of_week,
            day_of_month,
            1 if enabled else 0,
            now_iso(),
        ),
    )
    conn.commit()

    return get_report_schedule(conn)


def normalize_gemp_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    row_map = {}
    for row in rows or []:
        month = str(row.get("month", "")).strip()
        if month:
            row_map[month] = row

    normalized = []
    for month in MONTHS:
        src = row_map.get(month, {})
        normalized.append(
            {
                "month": month,
                "baseline2025": str(src.get("baseline2025", "") or "").strip(),
                "buildingDesc": str(src.get("buildingDesc", "") or "").strip(),
                "grossArea": str(src.get("grossArea", "") or "").strip(),
                "airconArea": str(src.get("airconArea", "") or "").strip(),
                "occupants": str(src.get("occupants", "") or "").strip(),
                "kwh": str(src.get("kwh", "") or "").strip(),
            }
        )
    return normalized


def empty_gemp_payload() -> Dict[str, Any]:
    return {
        "header": {
            "year": "",
            "agency": "",
            "tel": "",
            "address": "",
            "fax": "",
            "region": "",
            "preparedBy": "",
            "preparedByDesignation": "",
            "notedBy": "",
            "notedByDesignation": "",
        },
        "rows": normalize_gemp_rows([]),
        "stats": {
            "avgBaseline": "",
            "avgGrossArea": "",
            "avgAirconArea": "",
            "avgOccupants": "",
            "avgKwh": "",
        },
    }


def get_gemp_report_config(conn: sqlite3.Connection) -> Dict[str, Any]:
    ensure_report_tables(conn)

    row = conn.execute(
        "SELECT payload_json, updated_at FROM gemp_report_config WHERE id=1"
    ).fetchone()

    if not row:
        payload = empty_gemp_payload()
        return {"payload": payload, "updated_at": now_iso()}

    try:
        payload = json.loads(row["payload_json"] or "{}")
    except Exception:
        payload = {}

    header = payload.get("header", {}) or {}
    rows = normalize_gemp_rows(payload.get("rows", []) or [])
    stats = payload.get("stats", {}) or {}

    normalized_payload = {
        "header": {
            "year": str(header.get("year", "") or "").strip(),
            "agency": str(header.get("agency", "") or "").strip(),
            "tel": str(header.get("tel", "") or "").strip(),
            "address": str(header.get("address", "") or "").strip(),
            "fax": str(header.get("fax", "") or "").strip(),
            "region": str(header.get("region", "") or "").strip(),
            "preparedBy": str(header.get("preparedBy", "") or "").strip(),
            "preparedByDesignation": str(header.get("preparedByDesignation", "") or "").strip(),
            "notedBy": str(header.get("notedBy", "") or "").strip(),
            "notedByDesignation": str(header.get("notedByDesignation", "") or "").strip(),
        },
        "rows": rows,
        "stats": {
            "avgBaseline": str(stats.get("avgBaseline", "") or "").strip(),
            "avgGrossArea": str(stats.get("avgGrossArea", "") or "").strip(),
            "avgAirconArea": str(stats.get("avgAirconArea", "") or "").strip(),
            "avgOccupants": str(stats.get("avgOccupants", "") or "").strip(),
            "avgKwh": str(stats.get("avgKwh", "") or "").strip(),
        },
    }

    return {
        "payload": normalized_payload,
        "updated_at": row["updated_at"],
    }


def save_gemp_report_config(conn: sqlite3.Connection, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_report_tables(conn)

    header = payload.get("header", {}) or {}
    rows = normalize_gemp_rows(payload.get("rows", []) or [])
    stats = payload.get("stats", {}) or {}

    normalized_payload = {
        "header": {
            "year": str(header.get("year", "") or "").strip(),
            "agency": str(header.get("agency", "") or "").strip(),
            "tel": str(header.get("tel", "") or "").strip(),
            "address": str(header.get("address", "") or "").strip(),
            "fax": str(header.get("fax", "") or "").strip(),
            "region": str(header.get("region", "") or "").strip(),
            "preparedBy": str(header.get("preparedBy", "") or "").strip(),
            "preparedByDesignation": str(header.get("preparedByDesignation", "") or "").strip(),
            "notedBy": str(header.get("notedBy", "") or "").strip(),
            "notedByDesignation": str(header.get("notedByDesignation", "") or "").strip(),
        },
        "rows": rows,
        "stats": {
            "avgBaseline": str(stats.get("avgBaseline", "") or "").strip(),
            "avgGrossArea": str(stats.get("avgGrossArea", "") or "").strip(),
            "avgAirconArea": str(stats.get("avgAirconArea", "") or "").strip(),
            "avgOccupants": str(stats.get("avgOccupants", "") or "").strip(),
            "avgKwh": str(stats.get("avgKwh", "") or "").strip(),
        },
    }

    conn.execute(
        """
        UPDATE gemp_report_config
        SET payload_json=?, updated_at=?
        WHERE id=1
        """,
        (json.dumps(normalized_payload), now_iso()),
    )
    conn.commit()

    return get_gemp_report_config(conn)


def _parse_float(v: Any) -> Optional[float]:
    try:
        s = str(v).strip()
        if not s:
            return None
        return float(s)
    except Exception:
        return None


def _avg_str(values: List[Optional[float]]) -> str:
    valid = [v for v in values if v is not None]
    if not valid:
        return ""
    return f"{sum(valid) / len(valid):.2f}"


def compute_stats_from_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    monthly_rows = [r for r in rows if str(r.get("month", "")).strip() != "Average"]

    return {
        "avgBaseline": _avg_str([_parse_float(r.get("baseline2025")) for r in monthly_rows]),
        "avgGrossArea": _avg_str([_parse_float(r.get("grossArea")) for r in monthly_rows]),
        "avgAirconArea": _avg_str([_parse_float(r.get("airconArea")) for r in monthly_rows]),
        "avgOccupants": _avg_str([_parse_float(r.get("occupants")) for r in monthly_rows]),
        "avgKwh": _avg_str([_parse_float(r.get("kwh")) for r in monthly_rows]),
    }


def load_recent_history_points(
    conn: sqlite3.Connection,
    device: str,
    limit: int = 10000,
) -> List[Tuple[datetime, float, float, float]]:
    rows = conn.execute(
        """
        WITH history_base AS (
            SELECT
                time,
                MAX(CASE WHEN field='rms_voltage' THEN value END) AS rms_voltage,
                MAX(CASE WHEN field='rms_current' THEN value END) AS rms_current,
                MAX(CASE WHEN field='power' THEN value END) AS power
            FROM realtime_points
            WHERE device=?
              AND field IN ('rms_voltage', 'rms_current', 'power')
            GROUP BY time
        )
        SELECT time, rms_voltage, rms_current, power
        FROM history_base
        WHERE rms_voltage IS NOT NULL
          AND rms_current IS NOT NULL
          AND power IS NOT NULL
        ORDER BY time DESC
        LIMIT ?
        """,
        (device, limit),
    ).fetchall()

    points: List[Tuple[datetime, float, float, float]] = []
    for r in rows:
        try:
            dt = parse_iso_to_dt(r["time"])
            voltage = float(r["rms_voltage"])
            current = float(r["rms_current"])
            power = float(r["power"])
            points.append((dt, voltage, current, power))
        except Exception:
            continue

    points.sort(key=lambda x: x[0])
    return points


def infer_archive_interval_hours(points: List[Tuple[datetime, float, float, float]]) -> float:
    gaps: List[float] = []

    for (current_dt, _, _, _), (next_dt, _, _, _) in zip(points, points[1:]):
        gap_hours = (next_dt - current_dt).total_seconds() / 3600.0
        if 0 < gap_hours <= MAX_INTERVAL_GAP_HOURS:
            gaps.append(gap_hours)

    if not gaps:
        return ARCHIVE_INTERVAL_HOURS

    gaps.sort()
    mid = len(gaps) // 2
    if len(gaps) % 2 == 1:
        return gaps[mid]
    return (gaps[mid - 1] + gaps[mid]) / 2.0


def compute_kwh_from_points(points: List[Tuple[datetime, float, float, float]]) -> Tuple[float, float]:
    total_kwh = 0.0
    represented_hours = 0.0

    for current_dt, voltage, current, power_w in points:
        if power_w < 0:
            continue

        # Ignore invalid rows where both V and A are zero
        if voltage == 0 and current == 0:
            continue

        total_kwh += power_w * ARCHIVE_INTERVAL_HOURS / 1000.0
        represented_hours += ARCHIVE_INTERVAL_HOURS

    return total_kwh, represented_hours


def compute_gemp_dynamic(
    conn: sqlite3.Connection,
    device: str = "pi4",
    field: str = "power",
) -> Dict[str, Any]:
    tz = ZoneInfo(REPORT_TZ)
    now_local = datetime.now(tz)
    last_30_start = now_local - timedelta(days=30)
    month_start = datetime(now_local.year, now_local.month, 1, tzinfo=tz)
    current_month_days = calendar.monthrange(now_local.year, now_local.month)[1]
    current_month_label = calendar.month_name[now_local.month]

    # Use full history-style rows, not raw power-only rows
    points = load_recent_history_points(conn, device=device, limit=10000)
    inferred_interval_hours = infer_archive_interval_hours(points)

    last_30_points = [row for row in points if row[0] >= last_30_start]
    current_month_points = [row for row in points if row[0] >= month_start]

    last_30_days_kwh, _last_30_hours = compute_kwh_from_points(last_30_points)
    current_month_kwh, hours_elapsed_current_month = compute_kwh_from_points(current_month_points)

    avg_daily_kwh_30d = last_30_days_kwh / 30.0 if last_30_days_kwh > 0 else 0.0
    avg_kwh_per_hour_current_month = (
        current_month_kwh / hours_elapsed_current_month
        if hours_elapsed_current_month > 0
        else 0.0
    )
    projected_month_kwh = avg_daily_kwh_30d * current_month_days

    return {
        "device": device,
        "field": field,
        "archive_interval_hours": round(inferred_interval_hours, 6),
        "current_month_label": current_month_label,
        "current_month_days": current_month_days,
        "points_used_last_30_days": len(last_30_points),
        "points_used_current_month": len(current_month_points),
        "hours_elapsed_current_month": round(hours_elapsed_current_month, 4),
        "last_30_days_kwh": round(last_30_days_kwh, 4),
        "avg_daily_kwh_30d": round(avg_daily_kwh_30d, 4),
        "current_month_kwh": round(current_month_kwh, 4),
        "avg_kwh_per_hour_current_month": round(avg_kwh_per_hour_current_month, 6),
        "projected_month_kwh": round(projected_month_kwh, 4),
        "updated_at": now_local.isoformat(),
    }


def build_gemp_report_payload(
    conn: sqlite3.Connection,
    device: str = "pi4",
    field: str = "power",
) -> Dict[str, Any]:
    dynamic = compute_gemp_dynamic(conn, device=device, field=field)
    saved = get_gemp_report_config(conn)["payload"]

    current_year = str(datetime.now(ZoneInfo(REPORT_TZ)).year)
    current_month = dynamic["current_month_label"]

    saved_header = saved.get("header", {}) or {}
    saved_rows = normalize_gemp_rows(saved.get("rows", []) or [])
    row_map = {str(r.get("month", "")).strip(): r for r in saved_rows}

    rows = []
    for month in MONTHS:
        src = row_map.get(month, {})
        rows.append(
            {
                "month": month,
                "baseline2025": str(src.get("baseline2025", "") or "").strip(),
                "buildingDesc": str(src.get("buildingDesc", "") or "").strip(),
                "grossArea": str(src.get("grossArea", "") or "").strip(),
                "airconArea": str(src.get("airconArea", "") or "").strip(),
                "occupants": str(src.get("occupants", "") or "").strip(),
                "kwh": f"{dynamic['current_month_kwh']:.2f}" if month == current_month else str(src.get("kwh", "") or "").strip(),
            }
        )

    stats = compute_stats_from_rows(rows)

    return {
        "header": {
            "year": str(saved_header.get("year") or current_year),
            "agency": str(saved_header.get("agency") or os.getenv("GEMP_AGENCY", "GEMP Agency Name")),
            "tel": str(saved_header.get("tel") or os.getenv("GEMP_TEL", "")),
            "address": str(saved_header.get("address") or os.getenv("GEMP_ADDRESS", "")),
            "fax": str(saved_header.get("fax") or os.getenv("GEMP_FAX", "")),
            "region": str(saved_header.get("region") or os.getenv("GEMP_REGION", "")),
            "preparedBy": str(saved_header.get("preparedBy") or ""),
            "preparedByDesignation": str(saved_header.get("preparedByDesignation") or ""),
            "notedBy": str(saved_header.get("notedBy") or ""),
            "notedByDesignation": str(saved_header.get("notedByDesignation") or ""),
        },
        "rows": rows,
        "stats": stats,
    }


def schedule_key_for_now(schedule: Dict[str, Any], now_local: datetime) -> Optional[str]:
    if not schedule.get("enabled"):
        return None

    send_time = str(schedule.get("send_time") or "08:00")
    hh, mm = send_time.split(":", 1)
    if now_local.hour != int(hh) or now_local.minute != int(mm):
        return None

    frequency = str(schedule.get("frequency") or "").lower()

    if frequency == "weekly":
        target_dow = int(schedule.get("day_of_week") if schedule.get("day_of_week") is not None else 0)
        if now_local.weekday() != target_dow:
            return None
        iso = now_local.isocalendar()
        return f"weekly:{iso.year}-W{iso.week:02d}"

    if frequency == "monthly":
        target_dom = int(schedule.get("day_of_month") if schedule.get("day_of_month") is not None else 1)
        effective_dom = min(target_dom, calendar.monthrange(now_local.year, now_local.month)[1])
        if now_local.day != effective_dom:
            return None
        return f"monthly:{now_local.year}-{now_local.month:02d}"

    return None


def has_report_run(conn: sqlite3.Connection, report_type: str, schedule_key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM report_runs WHERE report_type=? AND schedule_key=? LIMIT 1",
        (report_type, schedule_key),
    ).fetchone()
    return bool(row)


def log_report_run(
    conn: sqlite3.Connection,
    *,
    report_type: str,
    schedule_key: str,
    scheduled_for: str,
    status: str,
    message: str = "",
):
    conn.execute(
        """
        INSERT OR REPLACE INTO report_runs (
            report_type, schedule_key, scheduled_for, sent_at, status, message
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            report_type,
            schedule_key,
            scheduled_for,
            now_iso(),
            status,
            message,
        ),
    )
    conn.commit()
