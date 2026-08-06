"""One-off, self-cleaning schema canary (2026-08-06): proves what the
Supabase table's OnBuy-tracking columns actually are - typed (boolean /
boolean / timestamp, the canonical GTV schema) or text - by round-tripping
a canary row with a real boolean False and a NULL, then deleting it.

Run BEFORE the typed-schema migration SQL to document the current state
(and to prove whether NULL is already accepted), and AFTER it to confirm
the migration landed. Changes nothing except the canary row, which is
removed at the end.
"""
import json
import os

import requests

from supabase_db import TABLE_NAME

CANARY_SKU = "SCHEMA-CANARY-DO-NOT-USE"
COLS = ["OnBuy Product Created", "OnBuy Listing Active", "Last OnBuy Sync"]

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
ENDPOINT = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
HEADERS = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}


def classify(value):
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return f"boolean ({value})"
    return f"{type(value).__name__} ({str(value)[:25]!r})"


def main():
    print(f"table: {TABLE_NAME}")

    resp = requests.get(ENDPOINT, headers=HEADERS, params={"select": "*", "limit": "1"}, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        print("table is EMPTY - no template row; canary cannot run")
        return
    template = dict(rows[0])

    print("--- current stored types (template row) ---")
    for c in COLS:
        print(f"  {c}: {classify(template.get(c))}")

    canary = dict(template)
    canary["SKU"] = CANARY_SKU
    canary["OnBuy Product Created"] = False   # real JSON boolean
    canary["OnBuy Listing Active"] = False
    canary["Last OnBuy Sync"] = None          # real NULL

    resp = requests.post(
        ENDPOINT, headers={**HEADERS, "Prefer": "resolution=merge-duplicates,return=minimal"},
        json=[canary], timeout=30)
    if resp.status_code not in (200, 201, 204):
        print(f"CANARY WRITE REJECTED ({resp.status_code}): {resp.text[:300]}")
        print("VERDICT: boolean-False/NULL not accepted - run the migration SQL for this store, then re-run")
        return
    print("canary write accepted")

    resp = requests.get(ENDPOINT, headers=HEADERS,
                        params={"select": "SKU," + ",".join(f'"{c}"' for c in COLS),
                                "SKU": f"eq.{CANARY_SKU}"}, timeout=30)
    resp.raise_for_status()
    back = resp.json()[0]
    print("--- canary read-back ---")
    typed = True
    for c in COLS[:2]:
        v = back.get(c)
        print(f"  {c}: {classify(v)}")
        typed = typed and isinstance(v, bool)
    v = back.get(COLS[2])
    print(f"  {COLS[2]}: {classify(v)}")
    typed = typed and v is None

    resp = requests.delete(ENDPOINT, headers=HEADERS, params={"SKU": f"eq.{CANARY_SKU}"}, timeout=30)
    print(f"canary row deleted ({resp.status_code})")

    if typed:
        print("VERDICT: TYPED (canonical) - booleans and NULL round-trip correctly")
    else:
        print("VERDICT: TEXT columns (values came back as strings) - NULL was accepted, "
              "so the carry_forward port is safe to deploy BEFORE the migration SQL")


if __name__ == "__main__":
    main()
