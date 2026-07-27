"""One-click check that alert emails actually deliver.

Run from the Actions tab ("Test Alert Email") after setting up or changing
the email secrets/variables - it sends one test message through the exact
same code path the sync uses, and the job goes RED if the send failed
(the sync itself never fails a run over email, so its runs stay green
even when email is broken - this workflow exists to catch that).
"""
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

import notify

messages = []


class _Grab(logging.Handler):
    def emit(self, record):
        messages.append(record.getMessage())


logging.getLogger("onbuy_sync").addHandler(_Grab())

notify.send_alert_email(
    "Test email - alert pipeline check",
    "If you received this, alert emails for this store are working.",
)

if any(m.startswith("Alert email sent") for m in messages):
    print("EMAIL TEST PASSED - check the inbox to confirm delivery")
    sys.exit(0)
print("EMAIL TEST FAILED - see the error above (Variables tab: SMTP_HOST/ALERT_EMAIL_FROM; Secrets: SMTP_USER/SMTP_APP_PASSWORD/ALERT_EMAIL_TO)")
sys.exit(1)
