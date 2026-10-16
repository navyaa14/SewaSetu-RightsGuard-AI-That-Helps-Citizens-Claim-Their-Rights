# -*- coding: utf-8 -*-
"""Apply migrations/*.sql (idempotent) and create officer accounts from OFFICER_ACCOUNTS.

OFFICER_ACCOUNTS = "username|Full Name|Block|password;username2|..."  (Block may be "*" for all)
Run at container start, before the web server.
"""
import os
import sys
import glob
import time

import psycopg2
from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.environ.setdefault("SEWASETU_DEV", os.environ.get("SEWASETU_DEV", "0"))


def connect():
    import configparser
    c = configparser.ConfigParser()
    c.read(os.path.join(BASE, "config", "app.ini"))
    g = lambda k, e: os.environ.get(e) or c.get("database", k)
    for _ in range(450):  # first boot loads a 35 MB seed; on a small VM that takes minutes
        try:
            return psycopg2.connect(host=g("host", "DB_HOST"), port=g("port", "DB_PORT"),
                                    dbname=g("name", "DB_NAME"), user=g("user", "DB_USER"),
                                    password=g("password", "DB_PASSWORD"))
        except psycopg2.OperationalError:
            time.sleep(2)
    raise SystemExit("database not reachable")


def main():
    conn = connect()
    cur = conn.cursor()
    # wait until the seed has finished loading (first boot of postgres)
    for _ in range(300):
        cur.execute("SELECT to_regclass('public.oauth_program_tokens') IS NOT NULL")
        if cur.fetchone()[0]:
            cur.execute("SELECT count(*) FROM pg_stat_activity WHERE query ILIKE 'COPY %%' AND pid <> pg_backend_pid()")
            if cur.fetchone()[0] == 0:
                break
        conn.rollback(); time.sleep(2)
    cur.execute("SELECT pg_advisory_lock(424242)")
    for path in sorted(glob.glob(os.path.join(BASE, "migrations", "*.sql"))):
        with open(path) as fh:
            cur.execute(fh.read())
        print("applied", os.path.basename(path))
    for spec in filter(None, (os.environ.get("OFFICER_ACCOUNTS") or "").split(";")):
        try:
            username, name, block, password = [x.strip() for x in spec.split("|", 3)]
        except ValueError:
            print("skipping malformed officer spec"); continue
        cur.execute("INSERT INTO officers (username, full_name, block, password_hash) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (username) DO UPDATE SET full_name = EXCLUDED.full_name, "
                    "block = EXCLUDED.block, password_hash = EXCLUDED.password_hash, active = TRUE",
                    (username, name, None if block in ("*", "") else block,
                     generate_password_hash(password)))
        print("officer", username, block)
    conn.commit()
    cur.execute("SELECT pg_advisory_unlock(424242)")
    cur.close(); conn.close()


if __name__ == "__main__":
    main()
