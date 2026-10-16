# -*- coding: utf-8 -*-
"""
Nightly deemed-approval job.

Under the Purvanchal Right to Public Services Act, an application not processed
within the statutory SLA stands approved. This job marks such applications
DEEMED_APPROVED and notifies the applicant by SMS.

Scheduled via cron, 02:00 daily. See deploy/crontab.
"""

import os
import sys
from zoneinfo import ZoneInfo
import configparser
from datetime import datetime, timedelta

import psycopg2
import requests

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

config = configparser.ConfigParser()
CONFIG_PATH = os.path.join(BASE_DIR, "config", "app.ini")
with open(CONFIG_PATH) as fh:
    config.read_file(fh)

SLA_DAYS = config.getint("pension", "sla_days")
SMS_GATEWAY_URL = os.environ.get("SMS_GATEWAY_URL") or config.get("app", "sms_gateway_url")
IST = ZoneInfo("Asia/Kolkata")


def now_ist():
    # The portal stores IST wall-clock times; the container clock may be UTC.
    return datetime.now(IST).replace(tzinfo=None)


def db(key, env):
    return os.environ.get(env) or config.get("database", key)


def main():
    conn = psycopg2.connect(host=db("host", "DB_HOST"), port=db("port", "DB_PORT"),
                            dbname=db("name", "DB_NAME"), user=db("user", "DB_USER"),
                            password=db("password", "DB_PASSWORD"))
    cur = conn.cursor()
    now = now_ist()
    cutoff = now - timedelta(days=SLA_DAYS)
    cur.execute(
        """UPDATE applications
           SET status = 'DEEMED_APPROVED', decided_at = %s, decided_by = 'RTPS-AUTO'
           WHERE status = 'PENDING' AND submitted_at < %s
           RETURNING id, application_no, mobile""",
        (now, cutoff))
    rows = cur.fetchall()
    # audit exactly the rows this run changed (the old "decided in the last minute" query
    # double-logged when the job ran twice within a minute, e.g. the startup retry loop)
    for app_id, _, _ in rows:
        cur.execute("INSERT INTO audit_log (application_id, action, actor, note, at) "
                    "VALUES (%s, 'DEEMED_APPROVED', 'system:rtps-cron', "
                    "'not processed within SLA', %s)", (app_id, now))
    conn.commit()
    for _, app_no, mobile in rows:
        try:
            requests.post(SMS_GATEWAY_URL + "/api/send", json={
                "to": mobile,
                "text": "Sewa Setu: your pension application %s stands approved "
                        "under the RTPS Act." % app_no}, timeout=5)
        except Exception:
            pass
    print("%s deemed approval: %d applications approved"
          % (now_ist().strftime("%Y-%m-%d %H:%M:%S"), len(rows)))
    cur.close()
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
