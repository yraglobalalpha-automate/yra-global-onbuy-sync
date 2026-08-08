"""One-off, READ-ONLY: forensic audit of the live OnBuy listings for the
Aug-2026 suspended/no-price-no-stock incident. For every listing: price,
stock, created_at, updated_at. Outputs:
- empty-vs-healthy split by listing AGE (created before/after Aug 1)
- for OLD listings now empty (previously live, since wiped): an updated_at
  histogram - the wipe moment clusters at OnBuy's event time
- current state of the three listings attached manually on 2026-08-05
Changes nothing."""
import os
from collections import Counter

import requests as _requests  # noqa: F401

from onbuy_client import BASE_URL, OnBuyClient
from retry_utils import with_retry

ATTACHED_0805 = {"902632102049", "635271365895", "318153081532"}


def main():
    onbuy = OnBuyClient()
    if not onbuy.authenticate():
        raise SystemExit("OnBuy auth failed")

    listings = []
    offset, limit = 0, 100
    while True:
        def _page(off=offset):
            r = onbuy._send("GET", f"{BASE_URL}/listings", what="listings page",
                            params={"site_id": onbuy.site_id, "limit": limit, "offset": off},
                            timeout=60)
            r.raise_for_status()
            return r
        body = with_retry(_page, what=f"listings page offset {offset}", max_attempts=3).json()
        items = body.get("results") if isinstance(body, dict) else body
        if not isinstance(items, list):
            break
        listings.extend(i or {} for i in items)
        if len(items) < limit:
            break
        offset += limit
    print(f"live listings: {len(listings)}")

    def empty_both(it):
        return (it.get("price") in (None, "", "0.00", "0", 0, 0.0)
                and it.get("stock") in (None, "", 0, "0"))

    old_empty, new_empty, old_ok, new_ok = [], [], 0, 0
    for it in listings:
        created = str(it.get("created_at") or "")[:10]
        is_old = created < "2026-08-01"
        if empty_both(it):
            (old_empty if is_old else new_empty).append(it)
        else:
            if is_old:
                old_ok += 1
            else:
                new_ok += 1

    print(f"created BEFORE Aug 1: {old_ok} healthy, {len(old_empty)} EMPTY (previously live, now wiped)")
    print(f"created Aug 1 or after: {new_ok} healthy, {len(new_empty)} EMPTY (born empty)")

    print("--- WIPED old listings: updated_at histogram (day hour) ---")
    hist = Counter(str(it.get("updated_at") or "?")[:13] for it in old_empty)
    for k in sorted(hist):
        print(f"  {k}h: {hist[k]}")

    print("--- sample WIPED old listings (sku / created / updated) ---")
    for it in old_empty[:8]:
        print(f"  {it.get('sku')} / created {it.get('created_at')} / updated {it.get('updated_at')}")

    print("--- born-empty new listings: created_at by day ---")
    hist = Counter(str(it.get("created_at") or "?")[:10] for it in new_empty)
    for k in sorted(hist):
        print(f"  {k}: {hist[k]}")

    print("--- the three listings attached manually on 2026-08-05 ---")
    for it in listings:
        if str(it.get("sku")) in ATTACHED_0805:
            print(f"  {it.get('sku')}: price={it.get('price')!r} stock={it.get('stock')!r} "
                  f"updated={it.get('updated_at')}")


if __name__ == "__main__":
    main()
