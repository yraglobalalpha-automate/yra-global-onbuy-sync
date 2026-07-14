"""Upserts processed product rows into the Supabase Postgres table.

Distinct from storage.py (which uploads feed.xml to Supabase Storage) - this
writes to the actual "OnBuy_Feed_Master" table via PostgREST, using the
upsert-via-POST pattern (Prefer: resolution=merge-duplicates) keyed on SKU,
the table's primary key. This mirrors what gets written to the Google Sheet
so Supabase accumulates a queryable copy of the catalog without yet becoming
the pipeline's source of truth.
"""
import logging
import os

import requests

logger = logging.getLogger("onbuy_sync")

# Case-sensitive - matches the quoted identifier in the table DDL (see the
# CREATE TABLE statement in README.md). Override with SUPABASE_TABLE_NAME if
# the table is named differently.
TABLE_NAME = os.getenv("SUPABASE_TABLE_NAME") or "YRA_Feed_Master"


def upsert_products(rows):
    """rows: list of dicts using the exact column names from the Supabase
    table (including spaces/case/currency symbols, e.g. "Cost Price (£)").
    Returns True on success. Never raises - a failed Supabase export must not
    fail the whole run, since the Sheet + OnBuy updates already happened by
    the time this is called.
    """
    if not rows:
        return True

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY")

    if not supabase_url or not service_key:
        logger.warning("SUPABASE_URL/SUPABASE_SERVICE_KEY not set - skipping database export")
        return False

    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{TABLE_NAME}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates,return=minimal",
    }

    try:
        resp = requests.post(endpoint, headers=headers, json=rows, timeout=30)
    except requests.exceptions.RequestException as exc:
        logger.error("Supabase database export failed: %s", exc)
        return False

    if resp.status_code not in (200, 201, 204):
        logger.error("Supabase database export failed (%s): %s", resp.status_code, resp.text[:500])
        return False

    logger.info("Supabase database export: upserted %d row(s)", len(rows))
    return True


def delete_products(skus):
    """Deletes these SKUs' rows entirely. Only used for the rare case where a
    product must not exist at all (OnBuy rejected the brand as a registered
    trademark another seller owns, and policy is to remove the row, not just
    stop syncing it) - not a general-purpose bulk delete. Never raises -
    returns True/False; a failed delete here doesn't crash the run, since the
    Sheet row removal (the primary signal generate_xml.py's next run keys
    off of) is a separate call.
    """
    if not skus:
        return True

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        logger.warning("SUPABASE_URL/SUPABASE_SERVICE_KEY not set - skipping database delete")
        return False

    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{TABLE_NAME}"
    headers = {
        "apikey": service_key,
        "Authorization": f"Bearer {service_key}",
        "Prefer": "return=minimal",
    }
    params = {"SKU": f"in.({','.join(skus)})"}

    try:
        resp = requests.delete(endpoint, headers=headers, params=params, timeout=30)
    except requests.exceptions.RequestException as exc:
        logger.error("Supabase database delete failed: %s", exc)
        return False

    if resp.status_code not in (200, 204):
        logger.error("Supabase database delete failed (%s): %s", resp.status_code, resp.text[:500])
        return False

    logger.info("Supabase database export: deleted %d row(s) (%s)", len(skus), ", ".join(skus))
    return True


TRACKING_COLUMNS = (
    "OPC",
    "Sync Status",
    "OnBuy Product Created",
    "OnBuy Listing Active",
    "OnBuy Product ID",
    "Last OnBuy Sync",
)


def fetch_existing_fields(skus):
    """Returns {sku: {column: value}} for whichever of these SKUs already
    have a Supabase row, covering OPC and the OnBuy-tracking columns.

    generate_xml.py sends a single upsert per run covering every processed
    row, and that upsert must supply every NOT NULL column every time -
    Postgres validates NOT NULL constraints on the row it would insert
    *before* it even checks whether ON CONFLICT DO UPDATE applies, so a
    partial-column upsert fails outright even when a full row already
    exists for that key (confirmed the hard way: a separate tracking-only
    upsert kept failing with a NOT NULL violation on Title even immediately
    after a successful full-row upsert for the same SKU in the same run).
    This pre-fetch is what lets generate_xml.py carry forward whatever was
    already there for rows not touched by an OnBuy push this run, instead
    of blanking those columns out. Never raises - returns {} on any
    failure, which just means every row falls back to defaults this run
    (safe, if not perfectly up to date).
    """
    if not skus:
        return {}

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        return {}

    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{TABLE_NAME}"
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    params = {"select": "SKU," + ",".join(TRACKING_COLUMNS), "SKU": f"in.({','.join(skus)})"}

    try:
        resp = requests.get(endpoint, headers=headers, params=params, timeout=30)
    except requests.exceptions.RequestException as exc:
        logger.error("Fetching existing Supabase fields failed: %s", exc)
        return {}

    if resp.status_code != 200:
        logger.error("Fetching existing Supabase fields failed (%s): %s", resp.status_code, resp.text[:300])
        return {}

    try:
        return {row["SKU"]: row for row in resp.json()}
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("Fetching existing Supabase fields: unexpected response shape: %s", exc)
        return {}


def fetch_full_rows(skus):
    """Returns {sku: {every column}} for whichever of these SKUs already have
    a Supabase row. For callers (like backfill_onbuy_status.py) that only want
    to change a couple of tracking columns for a SKU - fetch the full row
    first, update just those columns in the returned dict, and upsert that
    complete dict back. Sending a partial dict directly would hit the same
    NOT NULL problem described in fetch_existing_fields(). Never raises -
    returns {} on any failure.
    """
    if not skus:
        return {}

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not supabase_url or not service_key:
        return {}

    endpoint = f"{supabase_url.rstrip('/')}/rest/v1/{TABLE_NAME}"
    headers = {"apikey": service_key, "Authorization": f"Bearer {service_key}"}
    params = {"select": "*", "SKU": f"in.({','.join(skus)})"}

    try:
        resp = requests.get(endpoint, headers=headers, params=params, timeout=30)
    except requests.exceptions.RequestException as exc:
        logger.error("Fetching full Supabase rows failed: %s", exc)
        return {}

    if resp.status_code != 200:
        logger.error("Fetching full Supabase rows failed (%s): %s", resp.status_code, resp.text[:300])
        return {}

    try:
        return {row["SKU"]: row for row in resp.json()}
    except (ValueError, KeyError, TypeError) as exc:
        logger.error("Fetching full Supabase rows: unexpected response shape: %s", exc)
        return {}
