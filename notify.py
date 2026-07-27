"""Alert emails (change alerts + run failures).

Works with any SMTP provider. Two ready-made options:
- Resend:  SMTP_HOST=smtp.resend.com, SMTP_USER=resend (the literal word),
  SMTP_APP_PASSWORD=<Resend API key>, ALERT_EMAIL_FROM=<sender address on a
  domain verified in Resend, or onboarding@resend.dev for testing>,
  ALERT_EMAIL_TO=<where alerts go>.
- Gmail:   just SMTP_USER=<gmail address> and SMTP_APP_PASSWORD=<app
  password> (not the normal account password - Gmail blocks those).

Silently no-ops if the secrets aren't configured, so this is safe to deploy
before the email secrets exist.
"""
import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger("onbuy_sync")


def send_alert_email(subject, body):
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or "465")
    user = os.getenv("SMTP_USER")
    app_password = os.getenv("SMTP_APP_PASSWORD")
    # From defaults to the SMTP username (right for Gmail); providers like
    # Resend log in as a fixed username ("resend"), so the sender address
    # must come from ALERT_EMAIL_FROM there.
    from_addr = os.getenv("ALERT_EMAIL_FROM") or user
    to_addr = os.getenv("ALERT_EMAIL_TO", user)

    if not user or not app_password:
        logger.warning("SMTP_USER/SMTP_APP_PASSWORD not set - skipping alert email: %s", subject)
        return

    # Explicit UTF-8: change-alert bodies contain £ signs, and leaving the
    # charset to guesswork is how they end up rendered as "Â£".
    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = f"[YRA Sync] {subject}"
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP_SSL(host, port, timeout=15) as server:
            server.login(user, app_password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        logger.info("Alert email sent: %s", subject)
    except Exception as exc:  # an alert failure must never crash the run itself
        logger.error("Failed to send alert email: %s", exc)
