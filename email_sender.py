import os
import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from typing import List, Dict, Any


def _smtp_config() -> Dict[str, Any]:
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "").strip()
    smtp_from = os.getenv("SMTP_FROM", smtp_username).strip()

    use_starttls = os.getenv("SMTP_STARTTLS", "1").strip() == "1"
    use_ssl = os.getenv("SMTP_SSL", "0").strip() == "1"

    if not smtp_host:
        raise RuntimeError("SMTP_HOST is missing")
    if not smtp_username:
        raise RuntimeError("SMTP_USERNAME is missing")
    if not smtp_password:
        raise RuntimeError("SMTP_PASSWORD is missing")
    if not smtp_from:
        raise RuntimeError("SMTP_FROM is missing")

    return {
        "host": smtp_host,
        "port": smtp_port,
        "username": smtp_username,
        "password": smtp_password,
        "from": smtp_from,
        "starttls": use_starttls,
        "ssl": use_ssl,
    }


def _send_message(msg: EmailMessage, cfg: Dict[str, Any]) -> None:
    try:
        if cfg["ssl"]:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=30, context=context) as server:
                server.ehlo()
                server.login(cfg["username"], cfg["password"])
                server.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as server:
                server.ehlo()
                if cfg["starttls"]:
                    context = ssl.create_default_context()
                    server.starttls(context=context)
                    server.ehlo()
                server.login(cfg["username"], cfg["password"])
                server.send_message(msg)

    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(f"SMTP authentication failed: {e}") from e
    except smtplib.SMTPConnectError as e:
        raise RuntimeError(f"SMTP connect failed: {e}") from e
    except smtplib.SMTPServerDisconnected as e:
        raise RuntimeError(f"SMTP server disconnected: {e}") from e
    except smtplib.SMTPException as e:
        raise RuntimeError(f"SMTP error: {e}") from e
    except Exception as e:
        raise RuntimeError(f"Unexpected email send failure: {e}") from e


def send_plain_email(
    recipients: List[str],
    subject: str,
    body: str,
) -> Dict[str, Any]:
    cfg = _smtp_config()

    if not recipients:
        raise RuntimeError("No recipients provided")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(recipients)
    msg["Reply-To"] = cfg["from"]
    msg.set_content(body)

    _send_message(msg, cfg)

    return {
        "from": cfg["from"],
        "to": recipients,
        "subject": subject,
    }


def send_email_with_attachment(
    recipients: List[str],
    subject: str,
    body: str,
    attachment_path: str,
) -> Dict[str, Any]:
    cfg = _smtp_config()

    if not recipients:
        raise RuntimeError("No recipients provided")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(recipients)
    msg["Reply-To"] = cfg["from"]
    msg.set_content(body)

    ctype, encoding = mimetypes.guess_type(attachment_path)
    if ctype is None or encoding is not None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)

    with open(attachment_path, "rb") as f:
        msg.add_attachment(
            f.read(),
            maintype=maintype,
            subtype=subtype,
            filename=os.path.basename(attachment_path),
        )

    _send_message(msg, cfg)

    return {
        "from": cfg["from"],
        "to": recipients,
        "subject": subject,
        "attachment": os.path.basename(attachment_path),
    }
