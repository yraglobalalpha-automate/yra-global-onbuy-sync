"""TVC Mall Open API client for the YRA store (third supplier).

Fetches a product by the ItemNo in its URL and normalizes it to the exact
same shape as the eBay/AliExpress fetchers, so pricing, alerts, guards,
the Supplier column and the CSV export all work identically.

Setup (repo secrets - either pair works, email+password preferred):
  TVC_EMAIL / TVC_PASSWORD - the pipeline logs itself in each run via
                             /Authorization/GetAuthorization, so tokens
                             can never silently expire on us.
  TVC_TOKEN                - a static token instead (used only when the
                             email/password pair is absent).

Base host verified live 2026-07-25: openapi.tvc-mall.com (login endpoint
answers the documented {"Success":..,"AuthorizationToken"/"Reason":..}
shape; the product endpoint 401s without auth). Override with the
TVC_BASE_URL Variable if TVC ever move it.

Variants: TVC gives every version its own ItemNo, and Detail_NewVersion
returns exactly the requested version (CurrentItem) - so the standard
paste-the-link-you-are-viewing rule handles variants with no extra
machinery, cleaner than either other supplier.
"""
import logging
import os
import re
import urllib.parse

import requests

from retry_utils import AuthError, PermanentError, RateLimitError, TransientError, with_retry
from sanitize import sanitize_description, validate_images, strip_emojis

logger = logging.getLogger("onbuy_sync")

BASE_URL = (os.getenv("TVC_BASE_URL") or "https://openapi.tvc-mall.com").rstrip("/")
TVC_EMAIL = os.getenv("TVC_EMAIL")
TVC_PASSWORD = os.getenv("TVC_PASSWORD")
TVC_STATIC_TOKEN = os.getenv("TVC_TOKEN")


def tvc_ready():
    return bool((TVC_EMAIL and TVC_PASSWORD) or TVC_STATIC_TOKEN)


def empty_response():
    return {
        "stock": 0, "price": 0, "description": "", "main_image": "",
        "additional_images": [], "title": "", "brand": "", "product_code": "",
        "condition": "", "variant_group": "", "variant_detail": "",
        "shipping_fee": None,
    }


_TOKEN_CACHE = {"token": None}

# TVC's Open API quotes USD regardless of the account's website currency
# (confirmed live 2026-07-25 - switching the site to GBP changed nothing).
# Costs are converted at the daily rate PLUS a pad so a mid-week currency
# swing shows up as slightly-too-high cost (safe) rather than lost margin.
FX_PAD = 1.03
FX_FALLBACK_RATE = 0.85  # deliberately above any recent market rate
_FX_SOURCES = (
    ("open.er-api.com", "https://open.er-api.com/v6/latest/USD"),
    ("frankfurter.dev", "https://api.frankfurter.dev/v1/latest?base=USD&symbols=GBP"),
)
_FX_CACHE = {"rate": None}


def _usd_to_gbp_rate():
    """One rate per run: TVC_USD_TO_GBP Variable if pinned (used as-is),
    else live daily rate * FX_PAD, else the conservative fallback."""
    if _FX_CACHE["rate"]:
        return _FX_CACHE["rate"]
    pinned = os.getenv("TVC_USD_TO_GBP")
    if pinned:
        rate = float(pinned)
        logger.info("USD->GBP rate %.4f pinned via TVC_USD_TO_GBP", rate)
    else:
        rate = None
        for name, url in _FX_SOURCES:
            try:
                got = (requests.get(url, timeout=15).json().get("rates") or {}).get("GBP")
                if got and 0.4 < float(got) < 1.2:  # sanity band against a broken source
                    rate = round(float(got) * FX_PAD, 4)
                    logger.info("USD->GBP rate %.4f (%s daily rate * %d%% cost-safety pad)",
                                rate, name, round((FX_PAD - 1) * 100))
                    break
            except Exception as exc:
                logger.info("FX source %s unusable (%s) - trying next", name, exc)
        if rate is None:
            rate = FX_FALLBACK_RATE
            logger.warning("No FX source reachable - using conservative fallback %.2f "
                           "(prices land slightly high, never low)", rate)
    _FX_CACHE["rate"] = rate
    return rate


def _get_token(force=False):
    """Login token, cached per run. A static TVC_TOKEN is used only when the
    email/password pair is absent (it cannot be refreshed on 401)."""
    if not (TVC_EMAIL and TVC_PASSWORD):
        if TVC_STATIC_TOKEN:
            return TVC_STATIC_TOKEN
        raise AuthError("tvcmall: no TVC_EMAIL/TVC_PASSWORD (or TVC_TOKEN) secrets set")
    if _TOKEN_CACHE["token"] and not force:
        return _TOKEN_CACHE["token"]
    try:
        resp = requests.get(f"{BASE_URL}/Authorization/GetAuthorization",
                            params={"email": TVC_EMAIL, "password": TVC_PASSWORD}, timeout=20)
    except requests.exceptions.RequestException as exc:
        raise TransientError(f"tvcmall login: {exc}")
    if resp.status_code >= 500:
        raise TransientError(f"tvcmall login {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise PermanentError(f"tvcmall login non-JSON answer: {resp.text[:150]}")
    if not body.get("Success") or not body.get("AuthorizationToken"):
        raise AuthError(f"tvcmall login failed: {body.get('Reason') or resp.text[:120]} "
                        "- check the TVC_EMAIL/TVC_PASSWORD secrets")
    _TOKEN_CACHE["token"] = body["AuthorizationToken"]
    return _TOKEN_CACHE["token"]


def _api_get(path, params):
    return _api_request("GET", path, params=params)


def _api_post(path, json_body):
    return _api_request("POST", path, json_body=json_body)


def _api_request(method, path, params=None, json_body=None):
    """Authenticated call with the documented 'TVC <token>' header; one
    re-login on 401 (token died mid-run), mirroring the OnBuy client."""
    def _do(token):
        return requests.request(method, BASE_URL + path, params=params, json=json_body,
                                headers={"Authorization": f"TVC {token}"}, timeout=25)

    try:
        resp = _do(_get_token())
        if resp.status_code == 401 and TVC_EMAIL and TVC_PASSWORD:
            logger.info("tvcmall 401 - refreshing the login token once")
            resp = _do(_get_token(force=True))
    except requests.exceptions.RequestException as exc:
        raise TransientError(f"tvcmall {path}: {exc}")

    if resp.status_code == 429:
        raise RateLimitError()
    if resp.status_code >= 500:
        raise TransientError(f"tvcmall {path} {resp.status_code}")
    if resp.status_code == 401:
        raise AuthError("tvcmall auth rejected - check the TVC secrets (a static TVC_TOKEN may have expired)")
    if resp.status_code >= 400:
        raise PermanentError(f"tvcmall {path} {resp.status_code}: {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError:
        raise PermanentError(f"tvcmall {path} non-JSON answer: {resp.text[:200]}")


def _get_shipping_gbp(item_no):
    """Cheapest GB shipping cost for one unit, in GBP - or None when TVC
    has no freight option to GB at all (the product cannot be fulfilled).
    Quoted per product because rates vary by weight/size, not flat."""
    body = _api_post("/order/shippingcost",
                     {"skuinfo": f"{item_no}*1", "countrycode": "GB", "shippingmethodcode": ""})
    methods = (body.get("Shippings") or []) if isinstance(body, dict) else []
    if not (isinstance(body, dict) and body.get("Success")) or not methods:
        reason = str(body.get("Reason") or "")[:120] if isinstance(body, dict) else str(body)[:120]
        logger.info("TVCMALL NO GB FREIGHT for %s (%s)", item_no, reason or "empty answer")
        return None
    priced = [m for m in methods if m.get("ShippingCost") is not None]
    if not priced:
        logger.info("TVCMALL NO GB FREIGHT for %s (methods carry no cost)", item_no)
        return None
    best = min(priced, key=lambda m: float(m["ShippingCost"]))
    cost = float(best["ShippingCost"])
    currency = str(body.get("Currency") or "USD").strip().upper()
    if cost > 0 and currency == "USD":
        cost = round(cost * _usd_to_gbp_rate(), 2)
    elif cost > 0 and currency not in ("GBP",):
        raise PermanentError(f"tvcmall {item_no}: shipping quoted in {currency}, not GBP/USD - "
                             "no conversion configured for this currency")
    logger.info("TVC shipping for %s: %s GBP %.2f (%s)", item_no,
                best.get("ShippingMethod") or best.get("ShippingMethodCode") or "?",
                cost, best.get("DeliveryCycle") or "no cycle given")
    return cost


# TVC product URLs carry the ItemNo in a few shapes; the patterns below are
# best-effort until a real link is seen - anything unmatched flags the row
# as an unreadable link (existing machinery), which teaches us the shape.
_ITEM_PATTERNS = (
    re.compile(r"[-/]sku([A-Za-z0-9]{4,})\.html", re.IGNORECASE),
    re.compile(r"[?&](?:sku|itemno|item_no|itemcode)=([A-Za-z0-9-]{4,})", re.IGNORECASE),
    re.compile(r"/details/[^/?#]*-([0-9]{5,}[A-Za-z]?)\.html", re.IGNORECASE),
)


def extract_item_no(url):
    for pattern in _ITEM_PATTERNS:
        match = pattern.search(str(url or ""))
        if match:
            return match.group(1)
    return None


_TVC_IMAGE_HOST_RE = re.compile(r"(?:^|\.)(?:tvc-mall|tvcmall)\.com$", re.IGNORECASE)


def _tvc_own_image(u):
    try:
        host = urllib.parse.urlsplit(u).netloc
    except ValueError:
        return False
    return bool(_TVC_IMAGE_HOST_RE.search(host or ""))


# Live lesson #7: the API returns root-relative image paths
# ("/uploads/details/<SKU>-1.jpg"), not the absolute URLs its docs show.
# img.tvc-mall.com is the host that actually serves them (live-verified
# 2026-07-27: 200 image/jpeg; www. answers HTML, upload-img. 404s).
TVC_IMAGE_BASE = (os.getenv("TVC_IMAGE_BASE_URL") or "https://img.tvc-mall.com").rstrip("/")


def _extract_image_urls(body):
    """Product image URLs in API order, tolerating TVC's inconsistent
    shapes: Images as {'ProductImages':[...]} or a plain list; items as
    dicts keyed Url/url/ImageUrl/image/src or bare strings; root-relative
    paths completed with TVC_IMAGE_BASE."""
    field = body.get("Images")
    if isinstance(field, dict):
        items = (field.get("ProductImages") or field.get("productImages")
                 or field.get("Productimages") or field.get("Images")
                 or field.get("List") or [])
    elif isinstance(field, list):
        items = field
    else:
        items = []
    urls, seen = [], set()
    for it in items:
        if isinstance(it, str):
            u = it.strip()
        elif isinstance(it, dict):
            u = str(it.get("Url") or it.get("url") or it.get("ImageUrl")
                    or it.get("imageUrl") or it.get("Image") or it.get("image")
                    or it.get("src") or "").strip()
        else:
            u = ""
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = TVC_IMAGE_BASE + u
        if u.startswith("http") and u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def get_tvcmall_data(url):
    """Returns (available, data) with the exact same contract as the eBay
    and AliExpress fetchers. Raises Transient/Permanent/AuthError when the
    fetch itself failed - callers must not treat that as 'removed'."""
    item_no = extract_item_no(url)
    if not item_no:
        logger.info("UNREADABLE TVCMALL LINK (no item number): %s", url)
        data = empty_response()
        data["is_unreadable_link"] = True
        return False, data

    # TVC item numbers end in an UPPERCASE letter ("6619000677A") but URLs
    # lowercase their slugs - first live test returned not-found for the
    # lowercased form. Try canonical uppercase first,original form as fallback.
    body = with_retry(lambda: _api_get("/OpenApi/Product/Detail_NewVersion", {"ItemNo": item_no.upper()}),
                      what=f"tvcmall item {item_no}", max_attempts=3)
    if (not isinstance(body, dict) or not (body.get("SKU") or body.get("Sku"))) and item_no != item_no.upper():
        body = with_retry(lambda: _api_get("/OpenApi/Product/Detail_NewVersion", {"ItemNo": item_no}),
                          what=f"tvcmall item {item_no}", max_attempts=3)

    if not isinstance(body, dict) or not (body.get("SKU") or body.get("Sku")):
        message = str(body.get("Message") or "")[:150] if isinstance(body, dict) else str(body)[:150]
        logger.info("TVCMALL ITEM NOT FOUND/UNAVAILABLE (%s): %s", message or "empty answer", item_no)
        return False, empty_response()
    # Every later call must use TVC's own SKU from this response, never the
    # URL's casing - the freight endpoint answers "no freight was found" for
    # a lowercased SKU it does not recognize (live lesson #4, 2026-07-27:
    # 16/16 in-stock items got marked unshippable by exactly this).
    item_no = str(body.get("SKU") or body.get("Sku")).strip() or item_no.upper()

    # Country gate - the AliExpress 482 lesson, checked proactively here.
    countries = body.get("AllowSaleCountrys") or []
    if countries and not any(str(c).strip().upper() in ("*", "ALL", "ANY", "WORLDWIDE",
                                                        "GB", "UK", "UNITED KINGDOM", "GBR")
                             for c in countries):
        logger.info("TVCMALL NOT SELLABLE TO GB: %s (allowed: %s)", item_no, str(countries)[:80])
        return False, empty_response()

    price = float(body.get("DiscountedPrice") or body.get("Price") or 0)
    currency = str(body.get("CurrencyCode") or "").strip().upper()
    if price > 0 and currency == "USD":
        price = round(price * _usd_to_gbp_rate(), 2)
    elif price > 0 and currency and currency not in ("GBP",):
        # Mispricing is worse than a loud failure - only currencies with a
        # tested conversion may pass.
        raise PermanentError(f"tvcmall {item_no}: prices arrive in {currency}, not GBP/USD - "
                             "no conversion configured for this currency")
    if price <= 0:
        logger.info("NO PRICE: tvcmall %s", item_no)
        return False, empty_response()

    # StockStatus/SalesStatus are status flags, not counts (like eBay's
    # IN_STOCK) - zero/falsy means not buyable; in-stock rows get the same
    # default quantity eBay's hidden counts get. First live products will
    # confirm the enum; the diagnostic log keeps the raw values visible.
    stock_status = body.get("StockStatus")
    sales_status = body.get("SalesStatus")
    if not stock_status or (sales_status is not None and not sales_status):
        logger.info("OUT OF STOCK: tvcmall %s (StockStatus=%r SalesStatus=%r ProductStatus=%r)",
                    item_no, stock_status, sales_status, body.get("ProductStatus"))
        return False, empty_response()
    stock = 5

    # Freight is a second call, made only for buyable items. No GB freight
    # at all means the order could never be fulfilled - unavailable, same
    # treatment as the country gate.
    shipping_fee = _get_shipping_gbp(item_no)
    if shipping_fee is None:
        return False, empty_response()

    image_urls = _extract_image_urls(body)
    if not image_urls:
        # Teaches us the real field shape from the log if TVC changes it.
        logger.info("TVCMALL NO IMAGES PARSED for %s - Images field: %.300s",
                    item_no, repr(body.get("Images")))
    # TVC's own image CDN serves an incomplete TLS certificate chain
    # (browsers tolerate it, strict clients don't - live lesson #6: the
    # availability checker rejected every image, leaving the cells empty).
    # Their API's URLs for their own products are taken on trust; only
    # third-party-hosted URLs still get availability-checked.
    third_party = [u for u in image_urls if not _tvc_own_image(u)]
    checked = set(validate_images(third_party, max_images=11)) if third_party else set()
    all_images = [u for u in image_urls if _tvc_own_image(u) or u in checked][:11]
    main_image = all_images[0] if all_images else ""
    additional_images = all_images[1:11]

    # Brand arrives as a whole object ({'code','name','image','url',...}),
    # not a string - live lesson #5 (2026-07-27, rows filled with the raw
    # dict text). Only its display name belongs on the sheet.
    def _brand_name(val):
        if isinstance(val, dict):
            val = val.get("name") or val.get("Name")
        return str(val or "").strip()

    brand = _brand_name(body.get("Brand"))
    if not brand:
        props = body.get("Properties") or {}
        brand = _brand_name(props.get("Brand") or props.get("Brand Name"))

    return True, {
        "stock": stock,
        "price": price,
        "description": sanitize_description(str(body.get("Description") or "")),
        "main_image": main_image,
        "additional_images": additional_images,
        "title": strip_emojis(body.get("Title")),
        "brand": brand,
        "product_code": str(body.get("EanCode") or "").strip(),
        "condition": "New",
        "variant_group": "",
        "variant_detail": "",
        "shipping_fee": shipping_fee,
    }
