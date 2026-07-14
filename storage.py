"""Uploads feed.xml to Supabase Storage instead of committing it to git.

The old workflow committed a new feed.xml on every 3-hour run, forever - 397 of
this repo's ~550 commits were nothing but that file, growing the repo without
bound. Supabase Storage overwrites the same object in place instead.
"""
import logging
import os

import requests

logger = logging.getLogger("onbuy_sync")


def upload_feed(local_path="feed.xml", remote_path="feed.xml"):
    """Returns the public URL on success, or None if not configured / failed.
    Never raises - a failed feed upload should not fail the whole run, since
    the Sheet + OnBuy API updates (if enabled) already happened."""
    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_KEY")
    # No default bucket on this store: nothing consumes the hosted feed
    # (OnBuy is updated manually via the CSV export), so uploading is
    # opt-in via the SUPABASE_FEED_BUCKET Variable.
    bucket = os.getenv("SUPABASE_FEED_BUCKET")
    if not bucket:
        logger.info("SUPABASE_FEED_BUCKET not set - feed.xml not uploaded (set the Variable and create the bucket to host it)")
        return None

    if not supabase_url or not service_key:
        logger.warning("SUPABASE_URL/SUPABASE_SERVICE_KEY not set - feed.xml stays local only this run")
        return None

    try:
        with open(local_path, "rb") as f:
            data = f.read()
    except OSError as exc:
        logger.error("Could not read %s for upload: %s", local_path, exc)
        return None

    endpoint = f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}/{remote_path}"
    headers = {
        "Authorization": f"Bearer {service_key}",
        "apikey": service_key,
        "Content-Type": "application/xml",
        "x-upsert": "true",  # overwrite in place instead of erroring on existing object
    }

    try:
        resp = requests.post(endpoint, headers=headers, data=data, timeout=30)
    except requests.exceptions.RequestException as exc:
        logger.error("Feed upload failed: %s", exc)
        return None

    if resp.status_code not in (200, 201):
        logger.error("Feed upload failed (%s): %s", resp.status_code, resp.text[:300])
        return None

    public_url = f"{supabase_url.rstrip('/')}/storage/v1/object/public/{bucket}/{remote_path}"
    logger.info("Feed uploaded: %s", public_url)
    return public_url
