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
    REPORT_TZ,
)

CHECK_INTERVAL_SECONDS = int(os.getenv("REPORT_SCHEDULER_INTERVAL_S", "30"))
REPORT_DEVICE = os.getenv("REPORT_DEVICE", "pi4").strip() or "pi4"
REPORT_FIELD = os.getenv("REPORT_FIELD", "power").strip() or "power"


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
            frequency = str(schedule.get("frequency") or "").lower()
            subject = f"GEMP Report ({frequency.capitalize()}) - {schedule_key}"
            body = (
                "Attached is the scheduled GEMP report in PDF format.\n\n"
                f"Schedule: {frequency}\n"
                f"Schedule key: {schedule_key}\n"
                f"Generated at: {now_local.isoformat()}\n"
            )

            send_email_with_attachment(recipients, subject, body, out_path)

            log_report_run(
                conn,
                report_type="gemp_pdf_email",
                schedule_key=schedule_key,
                scheduled_for=now_local.isoformat(),
                status="sent",
                message=f"Sent to {', '.join(recipients)}",
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
