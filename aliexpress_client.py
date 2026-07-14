"""AliExpress Dropshipping (DS) API client for the YRA store.

Fetches a product by the ID in its URL and normalizes it to the exact same
shape as the eBay fetcher, so everything downstream (pricing, change
alerts, Sheet writes, Supabase, CSV export) works identically for both
suppliers.

Setup (three GitHub secrets):
  ALI_APP_KEY / ALI_APP_SECRET - from the app console on open.aliexpress.com
  ALI_ACCESS_TOKEN             - from the one-time authorization step; run
                                 get_aliexpress_token.py once to obtain it.

Store policy (same as eBay): products with multiple options (variant SKUs)
are NOT imported - they come back flagged is_variant=True and the row tells
an employee to replace the link with a single-option product.
"""
import hashlib
import hmac
import logging
import os
import re
import time

import requests

from retry_utils import AuthError, PermanentError, RateLimitError, TransientError, with_retry
from sanitize import sanitize_description, validate_images
from variant_match import match_variant_choice, options_text

logger = logging.getLogger("onbuy_sync")

GATEWAY = "https://api-sg.aliexpress.com/sync"

ALI_APP_KEY = os.getenv("ALI_APP_KEY")
ALI_APP_SECRET = os.getenv("ALI_APP_SECRET")
ALI_ACCESS_TOKEN = os.getenv("ALI_ACCESS_TOKEN")


def ali_ready():
    return bool(ALI_APP_KEY and ALI_APP_SECRET and ALI_ACCESS_TOKEN)


def sign_params(params, secret, api_path=""):
    """TOP-protocol HMAC-SHA256 signature: concatenate the (optional) API
    path and every key+value pair sorted by key, digest with the app secret,
    uppercase hex."""
    base = api_path + "".join(f"{k}{params[k]}" for k in sorted(params))
    return hmac.new(secret.encode("utf-8"), base.encode("utf-8"), hashlib.sha256).hexdigest().upper()


def _classify_error(body):
    """AliExpress errors arrive as HTTP 200 with an error_response body -
    map them onto the shared retry exception hierarchy."""
    err = body.get("error_response") or {}
    code = str(err.get("code", ""))
    text = f"aliexpress error {code}: {err.get('msg', '')} {err.get('sub_msg', '')}".strip()
    lowered = text.lower()
    if "session" in lowered or "token" in lowered or code in ("25", "26", "27"):
        raise AuthError(text + " - re-run the AliExpress authorization step and update ALI_ACCESS_TOKEN")
    if "limit" in lowered or "flow" in lowered or code == "7":
        raise RateLimitError()
    if code in ("15", "9"):  # remote service error / system busy
        raise TransientError(text)
    raise PermanentError(text)


def _call(method, **business_params):
    params = {
        "method": method,
        "app_key": ALI_APP_KEY,
        "access_token": ALI_ACCESS_TOKEN,
        "timestamp": str(int(time.time() * 1000)),
        "sign_method": "sha256",
        "format": "json",
    }
    params.update({k: str(v) for k, v in business_params.items()})
    params["sign"] = sign_params(params, ALI_APP_SECRET)
    resp = requests.post(GATEWAY, data=params, timeout=30)
    if resp.status_code == 429:
        raise RateLimitError()
    if resp.status_code >= 500:
        raise TransientError(f"aliexpress gateway {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise PermanentError(f"aliexpress gateway {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    if "error_response" in body:
        _classify_error(body)
    return body


_ITEM_ID_PATTERNS = (
    re.compile(r"/item/(?:[^/]*/)?(\d{6,})(?:\.html)?"),   # .../item/1005006123456789.html
    re.compile(r"[?&]productId=(\d{6,})"),
)

_SKU_ID_PATTERN = re.compile(r"[?&]sku_?[iI]d=(\d{6,})")


def extract_sku_id(url):
    """Some AliExpress links carry the selected option's own id (sku_id) -
    when present it picks the variant directly, no Variant Choice needed."""
    match = _SKU_ID_PATTERN.search(str(url))
    return match.group(1) if match else None


def extract_product_id(url):
    for pattern in _ITEM_ID_PATTERNS:
        match = pattern.search(str(url))
        if match:
            return match.group(1)
    return None


def _as_list(container, *keys):
    """AliExpress wraps lists as {"some_d_t_o": [...]} with inconsistent key
    styles - unwrap whichever variant is present."""
    if isinstance(container, list):
        return container
    if isinstance(container, dict):
        for key in keys:
            value = container.get(key)
            if isinstance(value, list):
                return value
    return []


def _first_number(sku, *keys):
    for key in keys:
        value = sku.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return None


def empty_response():
    return {
        "stock": 0, "price": 0, "description": "", "main_image": "",
        "additional_images": [], "title": "", "brand": "", "product_code": "",
        "condition": "", "variant_group": "", "variant_detail": "",
    }


def _sku_properties(sku):
    """A variant SKU's option list: [(name, value, image), ...] - e.g.
    [("Color", "Army Green", "https://...jpg"), ("Size", "XL", "")]."""
    props = []
    for prop in _as_list(sku.get("ae_sku_property_dtos") or {},
                         "ae_sku_property_d_t_o", "ae_sku_property_dto"):
        name = str(prop.get("sku_property_name") or "").strip()
        value = str(prop.get("property_value_definition_name")
                    or prop.get("sku_property_value") or "").strip()
        if name and value:
            props.append((name, value, str(prop.get("sku_image") or "").strip()))
    return props


# One ds.product.get per product per run: sibling variant rows share the
# same base link, so the second..Nth row reuses the first row's response
# instead of re-calling the API (module state = process state = one run).
_PRODUCT_CACHE = {}


def get_aliexpress_data(url, variant_choice="", variants_enabled=True):
    """Returns (available, data) with the exact same contract as
    generate_xml.get_ebay_data. Raises Transient/Permanent/AuthError when
    the fetch itself failed - callers must not treat that as 'removed'.

    Multi-option listings: the variant is picked by a sku_id in the URL when
    present, else by matching variant_choice (the Sheet's "Variant Choice"
    text) against each option combination. No pick -> is_variant flag with
    the available options listed for the employee. With
    variants_enabled=False (store policy switch, see generate_xml.py's
    VARIANTS_ENABLED), multi-option listings are flagged outright."""
    product_id = extract_product_id(url)
    if not product_id:
        # Not "product removed" - we never reached the API. Flag it for an
        # employee to paste the real product-page link (browse/share/bundle
        # URLs may mention several products, so guessing an id is unsafe).
        logger.info("UNREADABLE ALIEXPRESS LINK (no product id): %s", url)
        data = empty_response()
        data["is_unreadable_link"] = True
        return False, data

    body = _PRODUCT_CACHE.get(product_id)
    if body is None:
        body = with_retry(
            lambda: _call(
                "aliexpress.ds.product.get",
                product_id=product_id,
                ship_to_country="GB",
                target_currency="GBP",
                target_language="en",
            ),
            what=f"aliexpress item {product_id}", max_attempts=3,
        )
        _PRODUCT_CACHE[product_id] = body

    response = body.get("aliexpress_ds_product_get_response") or {}
    result = response.get("result") or {}
    if not result:
        raise PermanentError(
            f"aliexpress item {product_id}: unexpected response shape: {str(body)[:300]}")

    base = result.get("ae_item_base_info_dto") or {}
    status = str(base.get("product_status_type") or "").strip()
    if status and status.lower() != "onselling":
        logger.info("ALIEXPRESS ITEM NOT SELLING (%s): %s", status, product_id)
        return False, empty_response()

    skus = _as_list(result.get("ae_item_sku_info_dtos") or {},
                    "ae_item_sku_info_d_t_o", "ae_item_sku_info_dto", "aeop_ae_product_s_k_u")

    variant_group, variant_detail, sku_image = "", "", ""
    sku = skus[0] if skus else {}
    if len(skus) > 1 and not variants_enabled:
        logger.info("VARIANT LINK (variants on hold): aliexpress %s", product_id)
        data = empty_response()
        data["is_variant"] = True
        data["variant_reason"] = "disabled"
        return False, data
    if len(skus) > 1:
        candidates = [(i, [v for _, v, _ in _sku_properties(s)]) for i, s in enumerate(skus)]
        chosen_idx, reason = None, None
        url_sku_id = extract_sku_id(url)
        if url_sku_id:
            chosen_idx = next((i for i, s in enumerate(skus)
                               if str(s.get("sku_id") or "") == url_sku_id), None)
        if chosen_idx is None and str(variant_choice or "").strip():
            chosen_idx, reason = match_variant_choice(variant_choice, candidates)
        if chosen_idx is None:
            logger.info("VARIANT NEEDS A CHOICE: aliexpress %s", product_id)
            data = empty_response()
            data["is_variant"] = True
            data["variant_options"] = options_text(candidates)
            data["variant_reason"] = reason or ("no_match" if (url_sku_id or str(variant_choice or "").strip()) else "missing")
            return False, data
        sku = skus[chosen_idx]
        variant_group = product_id
        variant_detail = "; ".join(f"{n}={v}" for n, v, _ in _sku_properties(sku))
        sku_image = next((img for _, _, img in _sku_properties(sku) if img), "")

    price = _first_number(sku, "offer_sale_price", "offer_bulk_sale_price", "sku_price") or 0
    stock_value = _first_number(sku, "sku_available_stock", "s_k_u_available_stock", "ipm_sku_stock")
    stock = int(stock_value) if stock_value else 0
    if price <= 0:
        logger.info("NO PRICE: aliexpress %s", product_id)
        return False, empty_response()
    if stock <= 0:
        logger.info("OUT OF STOCK: aliexpress %s", product_id)
        return False, empty_response()

    multimedia = result.get("ae_multimedia_info_dto") or {}
    image_urls = [u.strip() for u in str(multimedia.get("image_urls") or "").split(";") if u.strip()]
    if sku_image:
        # The chosen option's own photo (e.g. the actual colour) leads.
        image_urls = [sku_image] + [u for u in image_urls if u != sku_image]
    all_images = validate_images(image_urls, max_images=11)
    main_image = all_images[0] if all_images else ""
    additional_images = all_images[1:11]

    brand = ""
    for prop in _as_list(result.get("ae_item_properties") or {},
                         "ae_item_property", "aeop_ae_product_property"):
        if str(prop.get("attr_name") or "").strip().lower() in ("brand name", "brand"):
            brand = str(prop.get("attr_value") or "").strip()
            break

    description = sanitize_description(base.get("detail") or base.get("mobile_detail") or "")

    return True, {
        "stock": stock,
        "price": price,
        "description": description,
        "main_image": main_image,
        "additional_images": additional_images,
        "title": str(base.get("subject") or "").strip(),
        "brand": brand,
        "product_code": "",  # AliExpress doesn't reliably expose EANs
        "condition": "New",
        "variant_group": variant_group,
        "variant_detail": variant_detail,
    }
