"""
send_test_email.py — send ONE test email using the .env SMTP settings, then exit.

Isolates the SMTP path from the monitor so you can debug delivery quickly.
Prints the SMTP conversation (debug level 1) so failures are easy to read.

    python3 send_test_email.py
"""

import smtplib
import sys
from email.message import EmailMessage

from config import Config


def main() -> None:
    cfg = Config.load()
    if not cfg.smtp_host:
        print("SMTP_HOST is not set in .env — nothing to send. "
              "Set SMTP_* (and EMAIL_TO) first.")
        sys.exit(1)

    msg = EmailMessage()
    msg["From"] = cfg.email_from
    msg["To"] = cfg.email_to
    msg["Subject"] = "[IDMC] SMTP test"
    msg.set_content(
        "If you can read this, the IDMC monitor's email path is working.\n"
        f"Sent via {cfg.smtp_host}:{cfg.smtp_port} "
        f"(starttls={cfg.smtp_starttls}, auth={'yes' if cfg.smtp_user else 'no'})."
    )

    # Stage-by-stage prints (NOT smtplib debug) so we never echo the
    # base64 AUTH line, which contains your username + password.
    try:
        print(f"Connecting to {cfg.smtp_host}:{cfg.smtp_port} ...")
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=15) as s:
            s.ehlo()
            if cfg.smtp_starttls:
                print("Starting TLS ...")
                s.starttls()
                s.ehlo()
            if cfg.smtp_user:
                print(f"Authenticating as {cfg.smtp_user} ...")
                s.login(cfg.smtp_user, cfg.smtp_password)
            print("Sending message ...")
            s.send_message(msg)
        print(f"OK — sent to {cfg.email_to} via {cfg.smtp_host}:{cfg.smtp_port}")
    except Exception as e:
        print(f"FAILED: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
