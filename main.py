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

from gemp_docx import build_gemp_docx
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
)

# =========================
# CONFIG
# =========================
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

app = FastAPI(title="Power Backend", version="0.8.0")


# -------------------------
# CORS
# -------------------------
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


# =========================
# HELPERS
# =========================
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
        "CREATE INDEX IF NOT EXISTS idx_notes_device_metric_time ON notes(device, metric, time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_realtime_device_field_time ON realtime_points(device, field, time)"
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


@app.on_event("startup")
def startup():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=5.0)
    try:
        configure_sqlite(conn)
        ensure_table(conn)
        add_missing_columns(conn)
        ensure_report_tables(conn)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        conn.commit()
    finally:
        conn.close()


# =========================
# MODELS
# =========================
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


class GempRowIn(BaseModel):
    month: str
    baseline2016: Optional[str] = None
    buildingDescription: Optional[str] = None
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


# =========================
# ROUTES
# =========================
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

@app.post("/reports/gemp/send-test")
def send_test_gemp_report(
    payload: SendTestReportIn,
    db: sqlite3.Connection = Depends(get_db),
):
    recipients = [r.strip().lower() for r in (payload.recipients or []) if r.strip()]
    if not recipients:
        recipients = get_active_recipient_emails(db)

    if not recipients:
        raise HTTPException(status_code=400, detail="No active recipients configured")

    try:
        report_payload = build_gemp_report_payload(db, device="pi4", field="power")
        out_path = build_gemp_docx(report_payload)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Report build failed: {e}")

    try:
        subject = "GEMP Report Test"
        body = "Attached is the test GEMP report."
        result = send_email_with_attachment(recipients, subject, body, out_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SMTP send failed: {e}")
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass

    return {"ok": True, **result}

@app.post("/ingest/metrics")
def ingest_metrics(payload: MetricsIn, db: sqlite3.Connection = Depends(get_db)):
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
        "INSERT INTO realtime_points (device, field, time, value) VALUES (?, ?, ?, ?)",
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
        "INSERT INTO realtime_points (device, field, time, value) VALUES (?, ?, ?, ?)",
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


@app.post("/reports/gemp/docx")
def export_gemp_docx(payload: GempReportIn, background_tasks: BackgroundTasks):
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict()
    out_path = build_gemp_docx(data)

    filename_year = (data.get("header", {}) or {}).get("year") or "report"
    filename = f"gemp-annex-a-{filename_year}.docx"

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
    payload: SendTestReportIn,
    db: sqlite3.Connection = Depends(get_db),
):
    recipients = [r.strip().lower() for r in (payload.recipients or []) if r.strip()]
    if not recipients:
        recipients = get_active_recipient_emails(db)

    if not recipients:
        raise HTTPException(status_code=400, detail="No active recipients configured")

    report_payload = build_gemp_report_payload(db, device="pi4", field="power")
    out_path = build_gemp_docx(report_payload)

    try:
        subject = "GEMP Report Test"
        body = "Attached is the test GEMP report."
        send_email_with_attachment(recipients, subject, body, out_path)
    finally:
        try:
            os.remove(out_path)
        except Exception:
            pass

    return {"ok": True, "sent_to": recipients}
