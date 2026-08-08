"""One-off, READ-ONLY: dump this semi store's Supabase rows (SKU, status,
price, stock) to a CSV artifact for joining against the shared OnBuy
account's live-listings dump."""
import csv
import os

import requests

from supabase_db import TABLE_NAME

SUPABASE_URL = (os.getenv("SUPABASE_URL") or "").rstrip("/")
KEY = os.getenv("SUPABASE_SERVICE_KEY") or ""
endpoint = f"{SUPABASE_URL}/rest/v1/{TABLE_NAME}"
headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}

with open("store_rows.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(["sku", "status", "sync_status", "selling_price", "stock"])
    offset, page, n = 0, 1000, 0
    while True:
        r = requests.get(endpoint, headers=headers, params={
            "select": 'SKU,Status,"Sync Status","Selling Price (£)",Stock',
            "offset": str(offset), "limit": str(page)}, timeout=30)
        r.raise_for_status()
        rows = r.json()
        for row in rows:
            w.writerow([row.get("SKU"), row.get("Status"), row.get("Sync Status"),
                        row.get("Selling Price (£)"), row.get("Stock")])
            n += 1
        if len(rows) < page:
            break
        offset += page
print(f"store_rows.csv: {n} rows")
