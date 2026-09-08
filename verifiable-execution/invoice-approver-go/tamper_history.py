#!/usr/bin/env python3
"""
tamper_history.py - flip a signed workflow-history row in your own Postgres
store so Catalyst shows the run as tampered.

This edits the Dapr state store DIRECTLY, behind Catalyst's back. That's the
whole point: prove that even a write straight to the database gets caught,
because the row no longer matches the signature Catalyst recorded for it.

It finds the first history row that still says FLAGGED and rewrites it to
CLEARED (same length, so the protobuf stays valid), as if the flagged vendor
had been waved through.

Dry run by default - it shows you the row it would change and stops.
Pass --apply to actually write.

  export SUPABASE_PWD='your-db-password'      # never hard-code this
  uv run --with 'psycopg[binary]' python tamper_history.py            # dry run
  uv run --with 'psycopg[binary]' python tamper_history.py --apply     # write it
"""
import base64
import os
import sys

import psycopg
from psycopg.types.json import Jsonb

# --- your connection (Supabase session pooler - IPv4, works everywhere) ------
# Supabase -> Project Settings -> Database -> Connection string -> "Session pooler".
# Use the pooler host, NOT db.<ref>.supabase.co (that one is IPv6-only and the
# Dapr sidecar can't reach it from Catalyst).
CONN = dict(
    host=os.environ.get("SUPABASE_HOST", "aws-0-<region>.pooler.supabase.com"),
    port=5432,
    dbname="postgres",
    user=os.environ.get("SUPABASE_USER", "postgres.<project-ref>"),
    password=os.environ["SUPABASE_PWD"],
    sslmode="require",
)

OLD, NEW = b"FLAGGED", b"CLEARED"
apply = "--apply" in sys.argv


def main():
    with psycopg.connect(**CONN) as conn, conn.cursor() as cur:
        # Dapr's postgres v2 store keeps one row per key in the `state` table.
        # Workflow history keys look like:  <app>||<instance>||history-00000N
        cur.execute(
            "SELECT key, value FROM state WHERE key LIKE '%%history-%%' ORDER BY key"
        )
        rows = cur.fetchall()
        if not rows:
            sys.exit("no history rows found - check the connection / table name")

        for key, value in rows:
            raw = base64.b64decode(value)  # value is a JSON string of base64 protobuf
            if OLD not in raw:
                continue
            print(f"target row: {key}")
            print(f"  contains {OLD!r} -> rewriting to {NEW!r}")
            if not apply:
                print("\ndry run - nothing written. re-run with --apply to tamper.")
                return
            new_val = base64.b64encode(raw.replace(OLD, NEW)).decode()
            cur.execute("UPDATE state SET value = %s WHERE key = %s", (Jsonb(new_val), key))
            conn.commit()
            print(f"\ntampered {key} in the database.")
            print("now reload the execution in the Catalyst console to see it flagged.")
            return

        print(f"no row still contained {OLD!r} - already tampered, or wrong run.")


if __name__ == "__main__":
    main()
