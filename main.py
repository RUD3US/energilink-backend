import os
import sqlite3
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timedelta, timezone

import jwt
from passlib.context import CryptContext
from fastapi import FastAPI, Depends, Header, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from gemp_docx import build_gemp_docx, build_gemp_pdf
from email_sender import send_plain_email, send_email_with_attachment
from gemp_reporting import (
    ensure_report_tables,
    list_report_recipients,
    add_report_recipient,
    delete_report_recipient,
    get_report_schedule,
    upsert_report_schedule,
    get_active_recipient_emails,
    compute_gemp_dynamic,
    build_gemp_report_payload,
    get_gemp_report_config,
    save_gemp_report_config,
)

DB_PATH = os.getenv("DB_PATH", "/var/data/app.db")
JWT_SECRET = os.getenv("JWT_SECRET", "").strip()
JWT_ALG = "HS256"
JWT_EXPIRE_DAYS = int(os.getenv("JWT_EXPIRE_DAYS", "7"))

CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*").strip()

NOTE_METRIC_CANONICAL = "real_power"
NOTE_METRIC_LEGACY = "power"
ALLOWED_NOTE_METRICS = {NOTE_METRIC_CANONICAL, NOTE_METRIC_LEGACY}
ALLOWED_NOTE_FIELDS = {"power", "power_realtime"}

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

app = FastAPI(title="Power Backend", version="1.0.0")


if CORS_ORIGINS == "*" or CORS_ORIGINS == "":
    allow_origins = ["*"]
    allow_credentials = False
else:
    allow_origins = [o.strip() for o in CORS_ORIGINS.split(",") if o.strip()]
    allow_credentials = True

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso_to_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def cleaned_str(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def require_note_text(text: Optional[str]) -> str:
    cleaned = cleaned_str(text)
    if not cleaned:
        raise HTTPException(status_code=400, detail="Note text is required")
    return cleaned


def configure_sqlite(conn: sqlite3.Connection):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA wal_autocheckpoint = 1000")
    conn.execute("PRAGMA temp_store = MEMORY")


def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    configure_sqlite(conn)
    try:
        yield conn
    finally:
        conn.close()


def ensure_table(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            metric TEXT NOT NULL,
            time TEXT NOT NULL,
            text TEXT NOT NULL,
            author_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS realtime_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL,
            field TEXT NOT NULL,
            time TEXT NOT NULL,
            value REAL NOT NULL
        );
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
        );
        """
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_notes_device_metric_time ON notes(device, metric, time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_realtime_device_field_time ON realtime_points(device, field, time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_monthly_billing_year_month ON monthly_billing_rates(year, month)"
    )

    conn.commit()


def add_missing_columns(conn: sqlite3.Connection):
    rows = conn.execute("PRAGMA table_info(notes)").fetchall()
    cols = set()

    for r in rows:
        try:
            cols.add(r["name"])
        except Exception:
            cols.add(r[1])

    def addcol(sql: str):
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass

    if "anchor_time" not in cols:
        addcol("ALTER TABLE notes ADD COLUMN anchor_time TEXT")
    if "anchor_value" not in cols:
        addcol("ALTER TABLE notes ADD COLUMN anchor_value REAL")
    if "anchor_field" not in cols:
        addcol("ALTER TABLE notes ADD COLUMN anchor_field TEXT")
    if "verified" not in cols:
        addcol("ALTER TABLE notes ADD COLUMN verified INTEGER DEFAULT 0")


def add_missing_billing_columns(conn: sqlite3.Connection):
    rows = conn.execute("PRAGMA table_info(monthly_billing_rates)").fetchall()
    cols = set()

    for r in rows:
        try:
            cols.add(r["name"])
        except Exception:
            cols.add(r[1])

    def addcol(sql: str):
        try:
            conn.execute(sql)
            conn.commit()
        except Exception:
            pass

    if "kwh" not in cols:
        addcol("ALTER TABLE monthly_billing_rates ADD COLUMN kwh REAL NOT NULL DEFAULT 0")
    if "bill_php" not in cols:
        addcol("ALTER TABLE monthly_billing_rates ADD COLUMN bill_php REAL NOT NULL DEFAULT 0")
    if "is_finalized" not in cols:
        addcol("ALTER TABLE monthly_billing_rates ADD COLUMN is_finalized INTEGER NOT NULL DEFAULT 0")


def ensure_realtime_unique_index(conn: sqlite3.Connection):
    rows = conn.execute(
        """
        SELECT device, field, time, MAX(id) AS keep_id
        FROM realtime_points
        GROUP BY device, field, time
        """
    ).fetchall()

    keep_ids = {int(r["keep_id"]) for r in rows} if rows else set()

    if keep_ids:
        placeholders = ",".join("?" for _ in keep_ids)
        conn.execute(
            f"DELETE FROM realtime_points WHERE id NOT IN ({placeholders})",
            tuple(keep_ids),
        )
        conn.commit()

    try:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_realtime_device_field_time ON realtime_points(device, field, time)"
        )
        conn.commit()
    except Exception:
        pass


def normalize_note_target(metric: Optional[str], anchor_field: Optional[str]) -> Tuple[str, str]:
    metric_in = cleaned_str(metric) or NOTE_METRIC_CANONICAL
    if metric_in not in ALLOWED_NOTE_METRICS:
        raise HTTPException(
            status_code=400,
            detail="Notes are only allowed on the two power graphs",
        )

    anchor_field_in = cleaned_str(anchor_field) or "power"
    if anchor_field_in not in ALLOWED_NOTE_FIELDS:
        raise HTTPException(
            status_code=400,
            detail="anchor_field must be 'power' or 'power_realtime'",
        )

    return NOTE_METRIC_CANONICAL, anchor_field_in


def note_anchor_tolerance_s(field: str) -> float:
    if field == "power_realtime":
        return 120.0
    if field == "power":
        return 1900.0
    return 120.0


def find_nearest_realtime(
    db: sqlite3.Connection,
    device: str,
    field: str,
    anchor_time_iso: str,
    tolerance_s: Optional[float] = None,
    max_points: int = 2000,
) -> Tuple[Optional[float], int]:
    try:
        target = parse_iso_to_ts(anchor_time_iso)
    except Exception:
        return None, 0

    if tolerance_s is None:
        tolerance_s = note_anchor_tolerance_s(field)

    rows = db.execute(
        "SELECT time, value FROM realtime_points WHERE device=? AND field=? ORDER BY id DESC LIMIT ?",
        (device, field, max_points),
    ).fetchall()

    if not rows:
        return None, 0

    best_val = None
    best_dt = float("inf")

    for r in rows:
        try:
            ts = parse_iso_to_ts(r["time"])
        except Exception:
            continue

        dt = abs(ts - target)
        if dt < best_dt:
            best_dt = dt
            best_val = float(r["value"])

    if best_val is None or best_dt > tolerance_s:
        return None, 0

    return best_val, 1


def hash_password(pw: str) -> str:
    return pwd_context.hash(pw)


def verify_password(pw: str, pw_hash: str) -> bool:
    return pwd_context.verify(pw, pw_hash)


def create_token(user_id: int) -> str:
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET is missing in environment.")

    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=JWT_EXPIRE_DAYS)
    payload = {
        "sub": str(user_id),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def get_bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None

    parts = authorization.split(" ", 1)
    if len(parts) != 2:
        return None
    if parts[0].lower() != "bearer":
        return None

    return parts[1].strip()


def get_current_user(
    db: sqlite3.Connection = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    tok = get_bearer_token(authorization)
    if not tok:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    try:
        payload = jwt.decode(tok, JWT_SECRET, algorithms=[JWT_ALG])
        user_id = int(payload.get("sub"))
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    row = db.execute("SELECT id, email FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    return {"id": int(row["id"]), "email": row["email"]}


def normalize_gemp_payload(raw: Dict[str, Any]) -> Dict[str, Any]:
    header = raw.get("header", {}) or {}
    rows = raw.get("rows", []) or []
    stats = raw.get("stats", {}) or {}

    normalized_rows = []
    for row in rows:
        normalized_rows.append(
            {
                "month": row.get("month", "") or "",
                "baseline2025": row.get("baseline2025", "") or "",
                "buildingDesc": row.get("buildingDesc", "") or "",
                "grossArea": row.get("grossArea", "") or "",
                "airconArea": row.get("airconArea", "") or "",
                "occupants": row.get("occupants", "") or "",
                "kwh": row.get("kwh", "") or "",
            }
        )

    return {
        "header": {
            "year": header.get("year", "") or "",
            "agency": header.get("agency", "") or "",
            "tel": header.get("tel", "") or "",
            "address": header.get("address", "") or "",
            "fax": header.get("fax", "") or "",
            "region": header.get("region", "") or "",
            "defaultBuildingDesc": header.get("defaultBuildingDesc", "") or "",
            "defaultGrossArea": header.get("defaultGrossArea", "") or "",
            "defaultAirconArea": header.get("defaultAirconArea", "") or "",
            "defaultOccupants": header.get("defaultOccupants", "") or "",
            "preparedBy": header.get("preparedBy", "") or "",
            "preparedByDesignation": header.get("preparedByDesignation", "") or "",
            "notedBy": header.get("notedBy", "") or "",
            "notedByDesignation": header.get("notedByDesignation", "") or "",
        },
        "rows": normalized_rows,
        "stats": {
            "avgBaseline": stats.get("avgBaseline", "") or "",
            "avgGrossArea": stats.get("avgGrossArea", "") or "",
            "avgAirconArea": stats.get("avgAirconArea", "") or "",
            "avgOccupants": stats.get("avgOccupants", "") or "",
            "avgKwh": stats.get("avgKwh", "") or "",
        },
    }


def safe_str(value: Optional[str], fallback: str = "") -> str:
    if value is None:
        return fallback
    value = str(value).strip()
    return value if value else fallback


def build_gemp_email_content(
    report_payload: Dict[str, Any],
    device: str,
    field: str,
    db: sqlite3.Connection,
) -> Tuple[str, str]:
    header = report_payload.get("header", {}) or {}

    agency = safe_str(header.get("agency"), "Agency")
    year = safe_str(header.get("year"), str(datetime.now().year))
    prepared_by = safe_str(header.get("preparedBy"), "Your Full Name")
    tel = safe_str(header.get("tel"), "")
    smtp_from = safe_str(os.getenv("SMTP_FROM", ""), "")

    dynamic = compute_gemp_dynamic(db, device=device, field=field)
    month_label = safe_str(dynamic.get("current_month_label"), datetime.now().strftime("%B"))

    month_year_subject = f"{month_label} {year}".strip()
    month_year_body = f"{month_label}, {year}".strip(", ")

    contact_parts = [x for x in [tel, smtp_from] if x]
    contact_line = " / ".join(contact_parts) if contact_parts else "Contact Number/Email Address"

    subject = f"{agency} - MECR Submission - {month_year_subject}"

    body = (
        "Dear DOE-EUMB Secretariat,\n"
        "Good day.\n\n"
        "In compliance with the Government Energy Management Program (GEMP) under Republic Act No. 11285, "
        f"we are officially submitting the following energy consumption report for the month of {month_year_body}:\n\n"
        "Monthly Electricity Consumption Report (MECR) – Annex A\n\n"
        "We hope you find everything in order. Should you have any questions or require further clarification, "
        "please feel free to reach out.\n"
        "Thank you.\n\n"
        "Best regards,\n"
        f"{prepared_by}\n"
        "Designated Energy Efficiency and Conservation Officer (EECO)\n"
        f"{agency}\n"
        f"{contact_line}"
    )

    return subject, body


def month_label_from_number(month: int) -> str:
    months = [
        "",
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
    if 1 <= month <= 12:
        return months[month]
    return f"Month {month}"


def get_current_year_monthly_kwh_map(
    db: sqlite3.Connection,
    device: str,
    field: str,
) -> Dict[int, float]:
    payload = build_gemp_report_payload(db, device=device, field=field)
    rows = payload.get("rows", []) or []

    month_name_to_number = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    output: Dict[int, float] = {}

    for row in rows:
        raw_month = str(row.get("month", "") or "").strip()
        month_num = month_name_to_number.get(raw_month.lower())
        if not month_num:
            continue

        raw_kwh = row.get("kwh", "")
        try:
            kwh = float(raw_kwh) if str(raw_kwh).strip() != "" else 0.0
        except Exception:
            kwh = 0.0

        output[month_num] = round(kwh, 2)

    return output


def sync_current_year_billing_records(
    db: sqlite3.Connection,
    device: str,
    field: str,
):
    now = datetime.now()
    current_year = now.year
    current_month = now.month

    live_kwh_by_month = get_current_year_monthly_kwh_map(db, device=device, field=field)

    existing_rows = db.execute(
        """
        SELECT year, month, cost_per_kwh, kwh, bill_php, is_finalized, updated_at
        FROM monthly_billing_rates
        WHERE year=?
        """,
        (current_year,),
    ).fetchall()

    existing_by_month = {int(r["month"]): r for r in existing_rows}

    dirty = False

    for month_num in range(1, current_month + 1):
        live_kwh = round(live_kwh_by_month.get(month_num, 0.0), 2)
        existing = existing_by_month.get(month_num)

        if existing is None:
            is_finalized = 0 if month_num == current_month else 1
            db.execute(
                """
                INSERT INTO monthly_billing_rates (
                    year, month, cost_per_kwh, kwh, bill_php, is_finalized, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    current_year,
                    month_num,
                    0.0,
                    live_kwh,
                    0.0,
                    is_finalized,
                    now_iso(),
                ),
            )
            dirty = True
            continue

        stored_rate = float(existing["cost_per_kwh"] or 0.0)
        stored_kwh = float(existing["kwh"] or 0.0)
        stored_bill = float(existing["bill_php"] or 0.0)
        stored_finalized = int(existing["is_finalized"] or 0)

        if month_num == current_month:
            new_kwh = live_kwh
            new_finalized = 0
        else:
            new_kwh = stored_kwh if stored_kwh > 0 else live_kwh
            new_finalized = 1

        new_bill = round(new_kwh * stored_rate, 2)

        if (
            round(stored_kwh, 2) != round(new_kwh, 2)
            or round(stored_bill, 2) != round(new_bill, 2)
            or stored_finalized != new_finalized
        ):
            db.execute(
                """
                UPDATE monthly_billing_rates
                SET kwh=?, bill_php=?, is_finalized=?, updated_at=?
                WHERE year=? AND month=?
                """,
                (
                    new_kwh,
                    new_bill,
                    new_finalized,
                    now_iso(),
                    current_year,
                    month_num,
                ),
            )
            dirty = True

    if dirty:
        db.commit()


def get_monthly_billing_rates(
    db: sqlite3.Connection,
    year: int,
    device: str = "pi4",
    field: str = "power",
) -> List[Dict[str, Any]]:
    if year == datetime.now().year:
        sync_current_year_billing_records(db, device=device, field=field)

    rows = db.execute(
        """
        SELECT year, month, cost_per_kwh, updated_at
        FROM monthly_billing_rates
        WHERE year=? AND cost_per_kwh > 0
        ORDER BY month ASC
        """,
        (year,),
    ).fetchall()

    return [
        {
            "year": int(r["year"]),
            "month": int(r["month"]),
            "cost_per_kwh": float(r["cost_per_kwh"]),
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def save_monthly_billing_rate(
    db: sqlite3.Connection,
    year: int,
    month: int,
    cost_per_kwh: float,
    device: str = "pi4",
    field: str = "power",
) -> Dict[str, Any]:
    if year < 2000 or year > 3000:
        raise HTTPException(status_code=400, detail="Invalid year")
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid month")
    if cost_per_kwh < 0:
        raise HTTPException(status_code=400, detail="cost_per_kwh must be >= 0")

    now = datetime.now()
    current_year = now.year
    current_month = now.month

    if year == current_year:
        sync_current_year_billing_records(db, device=device, field=field)

    row = db.execute(
        """
        SELECT year, month, cost_per_kwh, kwh, bill_php, is_finalized, updated_at
        FROM monthly_billing_rates
        WHERE year=? AND month=?
        """,
        (year, month),
    ).fetchone()

    kwh = 0.0
    is_finalized = 1

    if row is not None:
        kwh = float(row["kwh"] or 0.0)
        is_finalized = int(row["is_finalized"] or 0)
    else:
        if year == current_year:
            live_map = get_current_year_monthly_kwh_map(db, device=device, field=field)
            kwh = round(live_map.get(month, 0.0), 2)
            is_finalized = 0 if month == current_month else 1
        else:
            kwh = 0.0
            is_finalized = 1

    if year == current_year and month == current_month:
        live_map = get_current_year_monthly_kwh_map(db, device=device, field=field)
        kwh = round(live_map.get(month, 0.0), 2)
        is_finalized = 0

    bill_php = round(kwh * float(cost_per_kwh), 2)

    db.execute(
        """
        INSERT INTO monthly_billing_rates (
            year, month, cost_per_kwh, kwh, bill_php, is_finalized, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(year, month)
        DO UPDATE SET
            cost_per_kwh=excluded.cost_per_kwh,
            kwh=excluded.kwh,
            bill_php=excluded.bill_php,
            is_finalized=excluded.is_finalized,
            updated_at=excluded.updated_at
        """,
        (
            year,
            month,
            float(cost_per_kwh),
            kwh,
            bill_php,
            is_finalized,
            now_iso(),
        ),
    )
    db.commit()

    saved = db.execute(
        """
        SELECT year, month, cost_per_kwh, updated_at
        FROM monthly_billing_rates
        WHERE year=? AND month=?
        """,
        (year, month),
    ).fetchone()

    return {
        "year": int(saved["year"]),
        "month": int(saved["month"]),
        "cost_per_kwh": float(saved["cost_per_kwh"]),
        "updated_at": saved["updated_at"],
    }


def compute_monthly_billing_rows(
    db: sqlite3.Connection,
    year: int,
    device: str,
    field: str,
) -> List[Dict[str, Any]]:
    if year == datetime.now().year:
        sync_current_year_billing_records(db, device=device, field=field)

    rows = db.execute(
        """
        SELECT year, month, cost_per_kwh, kwh, bill_php, is_finalized, updated_at
        FROM monthly_billing_rates
        WHERE year=?
        ORDER BY month ASC
        """,
        (year,),
    ).fetchall()

    rows_by_month = {int(r["month"]): r for r in rows}

    output = []

    for month_num in range(1, 13):
        row = rows_by_month.get(month_num)

        if row is None:
            output.append(
                {
                    "year": year,
                    "month": month_num,
                    "month_label": month_label_from_number(month_num),
                    "kwh": 0.0,
                    "cost_per_kwh": None,
                    "bill_php": None,
                    "updated_at": None,
                }
            )
            continue

        stored_rate = float(row["cost_per_kwh"] or 0.0)
        stored_kwh = float(row["kwh"] or 0.0)
        stored_bill = float(row["bill_php"] or 0.0)

        output.append(
            {
                "year": year,
                "month": month_num,
                "month_label": month_label_from_number(month_num),
                "kwh": round(stored_kwh, 2),
                "cost_per_kwh": round(stored_rate, 4) if stored_rate > 0 else None,
                "bill_php": round(stored_bill, 2) if stored_rate > 0 else None,
                "updated_at": row["updated_at"],
            }
        )

    return output

def validate_metrics_payload(payload: "MetricsIn") -> None:
    if payload.rms_voltage < 0:
        raise HTTPException(status_code=400, detail="rms_voltage must be >= 0")

    if payload.rms_current < 0:
        raise HTTPException(status_code=400, detail="rms_current must be >= 0")

    if payload.power is not None and payload.power < 0:
        raise HTTPException(status_code=400, detail="power must be >= 0")

    if payload.apparent_power is not None and payload.apparent_power < 0:
        raise HTTPException(status_code=400, detail="apparent_power must be >= 0")

    if payload.power_factor is not None:
        if payload.power_factor < 0 or payload.power_factor > 1:
            raise HTTPException(status_code=400, detail="power_factor must be between 0 and 1")

    if payload.power_factor is not None and payload.power is not None:
        if payload.power_factor <= 0.001 and payload.power > 0:
            raise HTTPException(
                status_code=400,
                detail="Rejected invalid reading: near-zero power factor while power is positive",
            )

    if payload.rms_voltage == 0 and payload.rms_current == 0:
        raise HTTPException(
            status_code=400,
            detail="Rejected invalid reading: voltage and current are both zero",
        )
        
@app.on_event("startup")
def startup():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    try:
        configure_sqlite(conn)
        ensure_table(conn)
        add_missing_columns(conn)
        add_missing_billing_columns(conn)
        ensure_report_tables(conn)
        ensure_realtime_unique_index(conn)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.commit()
    finally:
        conn.close()


class SendPlainEmailIn(BaseModel):
    recipients: List[str]
    subject: str = "EnergiLink SMTP test"
    body: str = "If you received this email, SMTP sending is working."


class AuthIn(BaseModel):
    email: str
    password: str


class AuthOut(BaseModel):
    token: str


class MeOut(BaseModel):
    id: int
    email: str


class RealtimeIn(BaseModel):
    device: str = "pi4"
    field: str = "rms_voltage"
    time: Optional[str] = None
    value: float


class RealtimeOut(BaseModel):
    time: str
    value: float


class HistoryRowOut(BaseModel):
    time: str
    rms_voltage: Optional[float] = None
    rms_current: Optional[float] = None
    power: Optional[float] = None
    power_factor: Optional[float] = None
    note: Optional[str] = None


class NoteCreateIn(BaseModel):
    device: str = "pi4"
    metric: str = NOTE_METRIC_CANONICAL
    text: str
    time: Optional[str] = None
    anchor_time: Optional[str] = None
    anchor_value: Optional[float] = None
    anchor_field: str = "power"


class NoteUpdateIn(BaseModel):
    device: Optional[str] = None
    metric: Optional[str] = None
    text: Optional[str] = None
    time: Optional[str] = None
    anchor_time: Optional[str] = None
    anchor_value: Optional[float] = None
    anchor_field: Optional[str] = None


class NoteOut(BaseModel):
    id: int
    device: str
    metric: str
    time: str
    text: str
    author_id: int
    created_at: str
    updated_at: str
    anchor_time: Optional[str] = None
    anchor_value: Optional[float] = None
    anchor_field: Optional[str] = None
    verified: int = 0


class MetricsIn(BaseModel):
    device: str = "pi4"
    time: Optional[str] = None
    rms_voltage: float
    rms_current: float
    power: Optional[float] = None
    apparent_power: Optional[float] = None
    power_factor: Optional[float] = None


class GempHeaderIn(BaseModel):
    year: Optional[str] = None
    agency: Optional[str] = None
    tel: Optional[str] = None
    address: Optional[str] = None
    fax: Optional[str] = None
    region: Optional[str] = None
    defaultBuildingDesc: Optional[str] = None
    defaultGrossArea: Optional[str] = None
    defaultAirconArea: Optional[str] = None
    defaultOccupants: Optional[str] = None
    preparedBy: Optional[str] = None
    preparedByDesignation: Optional[str] = None
    notedBy: Optional[str] = None
    notedByDesignation: Optional[str] = None


class GempRowIn(BaseModel):
    month: str
    baseline2025: Optional[str] = None
    buildingDesc: Optional[str] = None
    grossArea: Optional[str] = None
    airconArea: Optional[str] = None
    occupants: Optional[str] = None
    kwh: Optional[str] = None


class GempStatsIn(BaseModel):
    avgBaseline: Optional[str] = None
    avgGrossArea: Optional[str] = None
    avgAirconArea: Optional[str] = None
    avgOccupants: Optional[str] = None
    avgKwh: Optional[str] = None


class GempReportIn(BaseModel):
    header: GempHeaderIn
    rows: List[GempRowIn]
    stats: GempStatsIn


class GempDynamicOut(BaseModel):
    device: str
    field: str
    archive_interval_hours: float
    current_month_label: str
    current_month_days: int
    points_used_last_30_days: int
    points_used_current_month: int
    hours_elapsed_current_month: float
    last_30_days_kwh: float
    avg_daily_kwh_30d: float
    current_month_kwh: float
    avg_kwh_per_hour_current_month: float
    projected_month_kwh: float
    updated_at: str


class ReportRecipientIn(BaseModel):
    email: str


class ReportRecipientOut(BaseModel):
    id: int
    email: str
    is_active: int
    created_at: str


class ReportScheduleIn(BaseModel):
    frequency: str
    send_time: str
    day_of_week: Optional[int] = None
    day_of_month: Optional[int] = None
    enabled: int = 0


class ReportScheduleOut(BaseModel):
    id: int
    frequency: str
    send_time: str
    day_of_week: Optional[int] = None
    day_of_month: Optional[int] = None
    enabled: int
    updated_at: str


class SendTestReportIn(BaseModel):
    recipients: Optional[List[str]] = None


class MonthlyBillingRateIn(BaseModel):
    year: int
    month: int
    cost_per_kwh: float


class MonthlyBillingRateOut(BaseModel):
    year: int
    month: int
    cost_per_kwh: float
    updated_at: str


class MonthlyBillingRowOut(BaseModel):
    year: int
    month: int
    month_label: str
    kwh: float
    cost_per_kwh: Optional[float] = None
    bill_php: Optional[float] = None
    updated_at: Optional[str] = None


class PartialResetIn(BaseModel):
    device: Optional[str] = None


@app.get("/health")
def health(db: sqlite3.Connection = Depends(get_db)):
    try:
        db.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "ok": True,
        "db_ok": db_ok,
        "cors_origins": allow_origins,
        "bucket": os.getenv("INFLUX_BUCKET", ""),
        "org": os.getenv("INFLUX_ORG", ""),
    }


@app.get("/reports/email/debug-config")
def email_debug_config():
    return {
        "SMTP_HOST": bool(os.getenv("SMTP_HOST", "").strip()),
        "SMTP_PORT": os.getenv("SMTP_PORT", ""),
        "SMTP_USERNAME": bool(os.getenv("SMTP_USERNAME", "").strip()),
        "SMTP_PASSWORD": bool(os.getenv("SMTP_PASSWORD", "").strip()),
        "SMTP_FROM": os.getenv("SMTP_FROM", ""),
        "SMTP_STARTTLS": os.getenv("SMTP_STARTTLS", ""),
        "SMTP_SSL": os.getenv("SMTP_SSL", ""),
    }


@app.post("/ingest/metrics")
def ingest_metrics(payload: MetricsIn, db: sqlite3.Connection = Depends(get_db)):
    validate_metrics_payload(payload)
    t = payload.time.strip() if payload.time else now_iso()

    rows = [
        (payload.device, "rms_voltage", t, float(payload.rms_voltage)),
        (payload.device, "rms_current", t, float(payload.rms_current)),
    ]

    if payload.power is not None:
        rows.append((payload.device, "power", t, float(payload.power)))

    if payload.apparent_power is not None:
        rows.append((payload.device, "apparent_power", t, float(payload.apparent_power)))

    if payload.power_factor is not None:
        rows.append((payload.device, "power_factor", t, float(payload.power_factor)))

    db.executemany(
        "INSERT OR REPLACE INTO realtime_points (device, field, time, value) VALUES (?, ?, ?, ?)",
        rows,
    )
    db.commit()
    return {"ok": True}


@app.post("/auth/signup", response_model=AuthOut)
def signup(payload: AuthIn, db: sqlite3.Connection = Depends(get_db)):
    email = payload.email.strip().lower()
    pw = payload.password

    if not email or not pw:
        raise HTTPException(status_code=400, detail="Email and password required")

    existing = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    pw_hash = hash_password(pw)
    created = now_iso()

    cur = db.execute(
        "INSERT INTO users (email, password_hash, created_at) VALUES (?, ?, ?)",
        (email, pw_hash, created),
    )
    db.commit()

    user_id = cur.lastrowid
    token = create_token(int(user_id))
    return {"token": token}


@app.post("/auth/login", response_model=AuthOut)
def login(payload: AuthIn, db: sqlite3.Connection = Depends(get_db)):
    email = payload.email.strip().lower()
    pw = payload.password

    row = db.execute("SELECT id, password_hash FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not verify_password(pw, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    token = create_token(int(row["id"]))
    return {"token": token}


@app.get("/auth/me", response_model=MeOut)
def me(user=Depends(get_current_user)):
    return user


@app.post("/ingest/vrms")
def ingest_vrms(payload: RealtimeIn, db: sqlite3.Connection = Depends(get_db)):
    ts = payload.time.strip() if payload.time else now_iso()
    db.execute(
        "INSERT OR REPLACE INTO realtime_points (device, field, time, value) VALUES (?, ?, ?, ?)",
        (payload.device, payload.field, ts, float(payload.value)),
    )
    db.commit()
    return {"ok": True}


@app.get("/public/realtime", response_model=List[RealtimeOut])
def public_realtime(
    device: str = "pi4",
    field: str = "rms_voltage",
    limit: int = 300,
    db: sqlite3.Connection = Depends(get_db),
):
    limit = max(1, min(limit, 5000))

    actual_field = field
    scale = 1.0

    if field == "power_kw":
        if db.execute(
            "SELECT 1 FROM realtime_points WHERE device=? AND field='power' LIMIT 1",
            (device,),
        ).fetchone():
            actual_field = "power"
        elif db.execute(
            "SELECT 1 FROM realtime_points WHERE device=? AND field='apparent_power' LIMIT 1",
            (device,),
        ).fetchone():
            actual_field = "apparent_power"
        scale = 1.0 / 1000.0

    rows = db.execute(
        "SELECT time, value FROM realtime_points WHERE device=? AND field=? ORDER BY id DESC LIMIT ?",
        (device, actual_field, limit),
    ).fetchall()

    return [{"time": r["time"], "value": float(r["value"]) * scale} for r in rows][::-1]


@app.get("/public/history", response_model=List[HistoryRowOut])
def public_history(
    device: str = "pi4",
    limit: int = 200,
    db: sqlite3.Connection = Depends(get_db),
):
    limit = max(1, min(limit, 1000))

    rows = db.execute(
        """
        WITH history_base AS (
            SELECT
                device,
                time,
                MAX(CASE WHEN field='rms_voltage' THEN value END) AS rms_voltage,
                MAX(CASE WHEN field='rms_current' THEN value END) AS rms_current,
                MAX(CASE WHEN field='power' THEN value END) AS power,
                MAX(CASE WHEN field='power_factor' THEN value END) AS power_factor
            FROM realtime_points
            WHERE device=?
              AND field IN ('rms_voltage', 'rms_current', 'power', 'power_factor')
            GROUP BY device, time
        ),
        note_base AS (
            SELECT
                device,
                COALESCE(anchor_time, time) AS note_time,
                GROUP_CONCAT(text, ' | ') AS note
            FROM notes
            WHERE device=?
            GROUP BY device, COALESCE(anchor_time, time)
        )
        SELECT
            h.time,
            h.rms_voltage,
            h.rms_current,
            h.power,
            h.power_factor,
            n.note
        FROM history_base h
        LEFT JOIN note_base n
            ON h.device = n.device
           AND h.time = n.note_time
        WHERE h.rms_voltage IS NOT NULL
          AND h.rms_current IS NOT NULL
          AND h.power IS NOT NULL
          AND h.power_factor IS NOT NULL
          AND NOT (h.rms_voltage = 0 AND h.rms_current = 0)
        ORDER BY h.time DESC
        LIMIT ?
        """,
        (device, device, limit),
    ).fetchall()

    return [
        {
            "time": r["time"],
            "rms_voltage": float(r["rms_voltage"]) if r["rms_voltage"] is not None else None,
            "rms_current": float(r["rms_current"]) if r["rms_current"] is not None else None,
            "power": float(r["power"]) if r["power"] is not None else None,
            "power_factor": float(r["power_factor"]) if r["power_factor"] is not None else None,
            "note": r["note"],
        }
        for r in rows
    ]


@app.get("/public/notes", response_model=List[NoteOut])
def public_notes(
    device: str = "pi4",
    metric: str = NOTE_METRIC_CANONICAL,
    limit: int = 200,
    db: sqlite3.Connection = Depends(get_db),
):
    limit = max(1, min(limit, 2000))
    metric_in = cleaned_str(metric) or NOTE_METRIC_CANONICAL

    if metric_in in ALLOWED_NOTE_METRICS:
        rows = db.execute(
            """
            SELECT * FROM notes
            WHERE device=? AND metric IN (?, ?)
            ORDER BY time DESC
            LIMIT ?
            """,
            (device, NOTE_METRIC_CANONICAL, NOTE_METRIC_LEGACY, limit),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM notes WHERE device=? AND metric=? ORDER BY time DESC LIMIT ?",
            (device, metric_in, limit),
        ).fetchall()

    return [dict(r) for r in rows]


@app.post("/notes", response_model=NoteOut)
def create_note(
    payload: NoteCreateIn,
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(get_current_user),
):
    device = cleaned_str(payload.device) or "pi4"
    metric, anchor_field = normalize_note_target(payload.metric, payload.anchor_field)
    text = require_note_text(payload.text)
    anchor_time = cleaned_str(payload.anchor_time) or cleaned_str(payload.time) or now_iso()
    created = now_iso()

    if payload.anchor_value is not None:
        anchor_value = float(payload.anchor_value)
        verified = 1
    else:
        anchor_value, verified = find_nearest_realtime(
            db,
            device,
            anchor_field,
            anchor_time,
        )

    cur = db.execute(
        """INSERT INTO notes (
               device, metric, time, text, author_id, created_at, updated_at,
               anchor_time, anchor_value, anchor_field, verified
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            device,
            metric,
            anchor_time,
            text,
            int(user["id"]),
            created,
            created,
            anchor_time,
            anchor_value,
            anchor_field,
            int(verified),
        ),
    )
    db.commit()

    row = db.execute("SELECT * FROM notes WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


@app.put("/notes/{note_id}", response_model=NoteOut)
def update_note(
    note_id: int,
    payload: NoteUpdateIn,
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(get_current_user),
):
    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")

    if int(row["author_id"]) != int(user["id"]):
        raise HTTPException(status_code=403, detail="Not allowed to edit this note")

    device_in = cleaned_str(payload.device)
    metric_in = cleaned_str(payload.metric)
    anchor_field_in = cleaned_str(payload.anchor_field)
    time_in = cleaned_str(payload.time)
    anchor_time_in = cleaned_str(payload.anchor_time)

    device = device_in or row["device"]
    metric, anchor_field = normalize_note_target(
        metric_in or row["metric"],
        anchor_field_in or row["anchor_field"] or "power",
    )

    anchor_time_from_payload = anchor_time_in or time_in
    anchor_time = anchor_time_from_payload or row["anchor_time"] or row["time"]

    if payload.text is not None:
        text = require_note_text(payload.text)
    else:
        text = row["text"]

    updated = now_iso()
    anchor_value = row["anchor_value"]
    verified = int((row["verified"] or 0))

    anchor_changed = (
        anchor_time_from_payload is not None
        or anchor_field_in is not None
        or payload.anchor_value is not None
    )

    if anchor_changed:
        if payload.anchor_value is not None:
            anchor_value = float(payload.anchor_value)
            verified = 1
        else:
            anchor_value, verified = find_nearest_realtime(
                db,
                device,
                anchor_field,
                anchor_time,
            )

    db.execute(
        """UPDATE notes
           SET device=?, metric=?, time=?, text=?, updated_at=?,
               anchor_time=?, anchor_value=?, anchor_field=?, verified=?
           WHERE id=?""",
        (
            device,
            metric,
            anchor_time,
            text,
            updated,
            anchor_time,
            anchor_value,
            anchor_field,
            int(verified),
            note_id,
        ),
    )
    db.commit()

    row2 = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return dict(row2)


@app.delete("/notes/{note_id}")
def delete_note(
    note_id: int,
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(get_current_user),
):
    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Note not found")

    if int(row["author_id"]) != int(user["id"]):
        raise HTTPException(status_code=403, detail="Not allowed to delete this note")

    db.execute("DELETE FROM notes WHERE id=?", (note_id,))
    db.commit()
    return {"ok": True}


@app.get("/reports/gemp/dynamic", response_model=GempDynamicOut)
def get_gemp_dynamic(
    device: str = "pi4",
    field: str = "power",
    db: sqlite3.Connection = Depends(get_db),
):
    return compute_gemp_dynamic(db, device=device, field=field)


@app.get("/public/summary/current-month-kwh")
def current_month_kwh_summary(
    device: str = "pi4",
    field: str = "power",
    db: sqlite3.Connection = Depends(get_db),
):
    data = compute_gemp_dynamic(db, device=device, field=field)
    return {
        "device": data["device"],
        "field": data["field"],
        "current_month_label": data["current_month_label"],
        "current_month_kwh": data["current_month_kwh"],
        "updated_at": data["updated_at"],
    }


@app.get("/billing/monthly-rates", response_model=List[MonthlyBillingRateOut])
def read_monthly_billing_rates(
    year: int,
    device: str = "pi4",
    field: str = "power",
    db: sqlite3.Connection = Depends(get_db),
):
    return get_monthly_billing_rates(db, year, device=device, field=field)


@app.put("/billing/monthly-rates", response_model=MonthlyBillingRateOut)
def update_monthly_billing_rate(
    payload: MonthlyBillingRateIn,
    device: str = "pi4",
    field: str = "power",
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(get_current_user),
):
    return save_monthly_billing_rate(
        db,
        year=payload.year,
        month=payload.month,
        cost_per_kwh=payload.cost_per_kwh,
        device=device,
        field=field,
    )


@app.get("/billing/monthly-summary", response_model=List[MonthlyBillingRowOut])
def read_monthly_billing_summary(
    year: int,
    device: str = "pi4",
    field: str = "power",
    db: sqlite3.Connection = Depends(get_db),
):
    return compute_monthly_billing_rows(
        db,
        year=year,
        device=device,
        field=field,
    )


@app.post("/admin/reset/readings")
def partial_reset_readings(
    payload: Optional[PartialResetIn] = None,
    db: sqlite3.Connection = Depends(get_db),
    user=Depends(get_current_user),
):
    device = payload.device.strip() if payload and payload.device else None

    if device:
        cur1 = db.execute("DELETE FROM realtime_points WHERE device=?", (device,))
        deleted_realtime = cur1.rowcount

        cur2 = db.execute("DELETE FROM notes WHERE device=?", (device,))
        deleted_notes = cur2.rowcount
    else:
        cur1 = db.execute("DELETE FROM realtime_points")
        deleted_realtime = cur1.rowcount

        cur2 = db.execute("DELETE FROM notes")
        deleted_notes = cur2.rowcount

    db.commit()

    return {
        "ok": True,
        "device": device,
        "deleted_realtime_points": deleted_realtime,
        "deleted_notes": deleted_notes,
    }


@app.post("/reports/gemp/docx")
def export_gemp_docx(payload: GempReportIn, background_tasks: BackgroundTasks):
    raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    data = normalize_gemp_payload(raw)

    out_path = build_gemp_docx(data)

    filename_year = data["header"].get("year") or "report"
    filename = f"gemp_report_{filename_year}.docx"

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except Exception:
            pass

    background_tasks.add_task(cleanup_file, out_path)

    return FileResponse(
        path=out_path,
        filename=filename,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.post("/reports/gemp/pdf")
def export_gemp_pdf(payload: GempReportIn, background_tasks: BackgroundTasks):
    raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    data = normalize_gemp_payload(raw)

    out_path = build_gemp_pdf(data)

    filename_year = data["header"].get("year") or "report"
    filename = f"gemp_report_{filename_year}.pdf"

    def cleanup_file(path: str):
        try:
            os.remove(path)
        except Exception:
            pass

    background_tasks.add_task(cleanup_file, out_path)

    return FileResponse(
        path=out_path,
        filename=filename,
        media_type="application/pdf",
    )


@app.get("/reports/gemp/config")
def read_gemp_config(db: sqlite3.Connection = Depends(get_db)):
    return get_gemp_report_config(db)


@app.put("/reports/gemp/config")
def update_gemp_config(
    payload: GempReportIn,
    db: sqlite3.Connection = Depends(get_db),
):
    raw = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    data = normalize_gemp_payload(raw)
    return save_gemp_report_config(db, data)


@app.get("/reports/settings/recipients", response_model=List[ReportRecipientOut])
def get_report_recipients(db: sqlite3.Connection = Depends(get_db)):
    return list_report_recipients(db)


@app.post("/reports/settings/recipients", response_model=ReportRecipientOut)
def create_report_recipient(
    payload: ReportRecipientIn,
    db: sqlite3.Connection = Depends(get_db),
):
    try:
        return add_report_recipient(db, payload.email)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/reports/settings/recipients/{recipient_id}")
def remove_report_recipient(
    recipient_id: int,
    db: sqlite3.Connection = Depends(get_db),
):
    delete_report_recipient(db, recipient_id)
    return {"ok": True}


@app.get("/reports/settings/schedule", response_model=ReportScheduleOut)
def read_report_schedule(db: sqlite3.Connection = Depends(get_db)):
    return get_report_schedule(db)


@app.put("/reports/settings/schedule", response_model=ReportScheduleOut)
def save_report_schedule(
    payload: ReportScheduleIn,
    db: sqlite3.Connection = Depends(get_db),
):
    try:
        return upsert_report_schedule(
            db,
            frequency=payload.frequency,
            send_time=payload.send_time,
            day_of_week=payload.day_of_week,
            day_of_month=payload.day_of_month,
            enabled=payload.enabled,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/reports/email/send-plain-test")
def send_plain_test_email(payload: SendPlainEmailIn):
    recipients = [r.strip().lower() for r in payload.recipients if r.strip()]
    if not recipients:
        raise HTTPException(status_code=400, detail="No recipients provided")

    try:
        result = send_plain_email(
            recipients=recipients,
            subject=payload.subject,
            body=payload.body,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMTP send failed: {e}")

    return {"ok": True, **result}


@app.post("/reports/gemp/send-test")
def send_test_gemp_report(
    payload: Optional[SendTestReportIn] = None,
    db: sqlite3.Connection = Depends(get_db),
):
    recipients = []
    if payload and payload.recipients:
        recipients = [r.strip().lower() for r in payload.recipients if r.strip()]

    if not recipients:
        recipients = get_active_recipient_emails(db)

    if not recipients:
        raise HTTPException(status_code=400, detail="No active recipients configured")

    device = "pi4"
    field = "power"

    try:
        report_payload = build_gemp_report_payload(db, device=device, field=field)
        out_path = build_gemp_pdf(report_payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report build failed: {e}")

    try:
        subject, body = build_gemp_email_content(
            report_payload=report_payload,
            device=device,
            field=field,
            db=db,
        )
        result = send_email_with_attachment(recipients, subject, body, out_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMTP send failed: {e}")
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass

    return {
        "ok": True,
        "sent_to": recipients,
        "subject": subject,
        **result,
    }
