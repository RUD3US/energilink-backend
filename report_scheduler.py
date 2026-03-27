import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from email_sender import send_email_with_attachment
from gemp_docx import build_gemp_pdf
from gemp_reporting import (
    open_db,
    ensure_report_tables,
    get_report_schedule,
    get_active_recipient_emails,
    schedule_key_for_now,
    has_report_run,
    log_report_run,
    build_gemp_report_payload,
    compute_gemp_dynamic,
    REPORT_TZ,
)

CHECK_INTERVAL_SECONDS = int(os.getenv("REPORT_SCHEDULER_INTERVAL_S", "30"))
REPORT_DEVICE = os.getenv("REPORT_DEVICE", "pi4").strip() or "pi4"
REPORT_FIELD = os.getenv("REPORT_FIELD", "power").strip() or "power"


def safe_str(value, fallback=""):
    if value is None:
        return fallback
    value = str(value).strip()
    return value if value else fallback


def build_gemp_email_content(conn, payload, device=REPORT_DEVICE, field=REPORT_FIELD):
    header = payload.get("header", {}) or {}

    agency = safe_str(header.get("agency"), "Agency")
    year = safe_str(header.get("year"), str(datetime.now().year))
    prepared_by = safe_str(header.get("preparedBy"), "Your Full Name")
    tel = safe_str(header.get("tel"), "")
    smtp_from = safe_str(os.getenv("SMTP_FROM", ""), "")

    dynamic = compute_gemp_dynamic(conn, device=device, field=field)
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


def run_once():
    conn = open_db()
    try:
        ensure_report_tables(conn)

        schedule = get_report_schedule(conn)
        now_local = datetime.now(ZoneInfo(REPORT_TZ))
        schedule_key = schedule_key_for_now(schedule, now_local)

        if not schedule_key:
            return

        if has_report_run(conn, "gemp_pdf_email", schedule_key):
            return

        recipients = get_active_recipient_emails(conn)
        if not recipients:
            log_report_run(
                conn,
                report_type="gemp_pdf_email",
                schedule_key=schedule_key,
                scheduled_for=now_local.isoformat(),
                status="failed",
                message="No active recipients",
            )
            return

        payload = build_gemp_report_payload(conn, device=REPORT_DEVICE, field=REPORT_FIELD)
        out_path = build_gemp_pdf(payload)

        try:
            subject, body = build_gemp_email_content(
                conn,
                payload,
                device=REPORT_DEVICE,
                field=REPORT_FIELD,
            )

            send_email_with_attachment(recipients, subject, body, out_path)

            log_report_run(
                conn,
                report_type="gemp_pdf_email",
                schedule_key=schedule_key,
                scheduled_for=now_local.isoformat(),
                status="sent",
                message=f"Sent to {', '.join(recipients)} | Subject: {subject}",
            )
            print(f"[OK] Sent scheduled GEMP PDF report: {schedule_key}")
        finally:
            try:
                os.remove(out_path)
            except Exception:
                pass

    except Exception as e:
        print(f"[ERROR] Scheduler run failed: {e}")
    finally:
        conn.close()


def main():
    print("GEMP scheduler started")
    print(f"Timezone: {REPORT_TZ}")
    print(f"Check interval: {CHECK_INTERVAL_SECONDS}s")
    print(f"Report device: {REPORT_DEVICE}")
    print(f"Report field: {REPORT_FIELD}")

    while True:
        run_once()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
