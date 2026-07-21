import base64
import csv
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
import json
import requests
from oauth2client.service_account import ServiceAccountCredentials

import notify
import pricing
import storage
import supabase_db
from aliexpress_client import ali_ready, extract_product_id, extract_sku_id, get_aliexpress_data
from variant_match import match_variant_choice, options_text
from onbuy_client import OnBuyClient
from retry_utils import AuthError, PermanentError, RateLimitError, TransientError, raise_for_status, with_retry
from sanitize import sanitize_description, validate_images

# ================= LOGGING =================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("onbuy_sync")

# ================= CONFIG =================
# YRA GLOBAL STORE VARIANT of the OnBuy-eBay pipeline. Key difference from the
# original store: OnBuy is updated MANUALLY via CSV export here - this
# pipeline only fetches from eBay, fills the Sheet, raises change alerts for
# employees to act on, and mirrors everything to Supabase. It never calls
# OnBuy's write API (see ONBUY_API_PUSH_ENABLED below - hard-disabled).
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

# The Google Sheet this store runs on (must be shared, as Editor, with the
# service account email inside GOOGLE_CREDENTIALS).
SHEET_NAME = os.getenv("SHEET_NAME") or "YRA_Feed_Master"

# ================= SETTINGS =================

# TRUE = FETCH ALL PRODUCTS
# FALSE = SMART BATCHING
FULL_REFRESH = False

# CATEGORY REMAP
RUN_CATEGORY_MAPPING = True

# ================= SCALING: DYNAMIC BATCH SIZE =================
# Batch size is computed per run from the actual row count and this daily
# eBay API budget, instead of a fixed number - see main() below. Stay
# comfortably under eBay's rate limit (commonly ~5,000/day on the default
# Browse API tier - check your exact allowance in the eBay Developer Portal
# and adjust this if yours differs).
EBAY_DAILY_CALL_BUDGET = int(os.getenv("EBAY_DAILY_CALL_BUDGET") or "4000")

# How many times this workflow runs per day - keep in sync with the cron
# schedule in .github/workflows/run.yml (currently every 3 hours = 8/day).
RUNS_PER_DAY = int(os.getenv("RUNS_PER_DAY") or "8")

# Optional hard override: set this env var to force a fixed batch size
# instead of the budget-derived one.
_MAX_PRODUCTS_PER_RUN_OVERRIDE = os.getenv("MAX_PRODUCTS_PER_RUN")

# ================= PRICE CHECK FLAG THRESHOLDS =================
# Total margin % over cost (the default formula gives ~40% = 20% fee + 20%
# profit). Normal = at/near default, Medium = moderately above, High = well
# above - adjust these two numbers if "a little more"/"much more" should mean
# different percentages than this.
PRICE_CHECK_NORMAL_MAX_PCT = 45
PRICE_CHECK_MEDIUM_MAX_PCT = 70

# ================= ONBUY API PUSH (permanently OFF for YRA) =================
# The YRA store updates OnBuy manually via CSV export - by explicit policy,
# this pipeline must NEVER push to OnBuy's write API. Hard-disabled (not env-
# driven) so a copied secret or stray repo Variable can never flip it on and
# push to the wrong OnBuy account by accident. The push code below is kept
# intact (it's the same battle-tested code as the original store) but can
# never execute.
ONBUY_API_PUSH_ENABLED = False
ONBUY_API_TEST_SKUS = set()

# Confirmed from the real account's API usage page: OnBuy allows 240 PUT and
# 240 POST calls per hour. The eBay-derived batch size above can now be much
# larger than 12 (up to hundreds of rows/run), which didn't exist as a risk
# when this was hardcoded at 12 - cap OnBuy pushes per run well under the
# hourly limit so one large run can't burn through it on its own. Rows beyond
# this cap still get their Sheet/Supabase update this run; they just wait
# for their next turn to reach OnBuy.
ONBUY_MAX_PUSHES_PER_RUN = int(os.getenv("ONBUY_MAX_PUSHES_PER_RUN") or "200")

# How many eBay fetch failures (after retries) in one run before we email an alert.
FETCH_FAILURE_ALERT_THRESHOLD = 3

PK_TZ = ZoneInfo("Asia/Karachi")


def should_push_to_onbuy(sku):
    if not ONBUY_API_PUSH_ENABLED:
        return False
    if ONBUY_API_TEST_SKUS:
        return sku in ONBUY_API_TEST_SKUS
    return True


def detect_supplier(url):
    """Which supplier a row's link belongs to: "eBay", "AliExpress", or None
    when the URL isn't usable yet (blank, or a site we don't support)."""
    u = str(url or "").strip().lower()
    if "ebay." in u:
        return "eBay"
    if "aliexpress." in u:
        return "AliExpress"
    return None


# ================= HELPERS =================
def col_letter(n):
    result = ""
    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result
    return result


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime(2000, 1, 1)


def is_valid_gtin(code):
    """True if `code` is a real barcode by the GS1 check-digit standard used
    for UPC-A/EAN-8/EAN-13/GTIN-14 (all the same algorithm, just different
    lengths). Being all-digits and the right length isn't enough - OnBuy
    validates the actual check digit and rejects create_product outright
    with "not a valid product code" otherwise, which happened for two SKUs
    that were numeric and 12 digits long but not real barcodes."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    body, check_digit = code[:-1], code[-1]
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return str((10 - total % 10) % 10) == check_digit


def sku_numeric_part(sku):
    """The digits of the SKU ARE the product's barcode (user policy
    2026-07-13, both stores): a SKU may carry non-digit decoration around
    the barcode ("YRA-5012345678900") and everything validated, exported or
    stored as an EAN/UPC uses only the digits. The decoration must not
    itself contain digits - a "-1" style suffix corrupts the barcode and
    will (correctly) fail the check-digit test."""
    return re.sub(r"\D", "", str(sku or ""))


# Values eBay sellers sometimes put in the "Brand" aspect that are not
# actually a brand name - typically someone answering a yes/no-style prompt
# literally ("Branded") rather than naming the brand. User's explicit policy
# (2026-07-04): normalize all of these to "Unbranded" rather than pass a
# placeholder through as if it were a real brand.
_NON_BRAND_VALUES = {"branded", "unbranded", "no brand", "none", "n/a", "na", "generic", ""}


def normalize_brand(brand):
    if str(brand).strip().lower() in _NON_BRAND_VALUES:
        return "Unbranded"
    return brand


def dedupe_rows_by_sku(rows, what):
    """Postgres/PostgREST rejects a whole bulk upsert if two rows in the same
    call share the same SKU (the conflict target) - "ON CONFLICT DO UPDATE
    command cannot affect row a second time". That only happens from a real
    duplicate SKU somewhere in the Sheet (e.g. a copy-pasted row, or the same
    value with stray whitespace), so keep the last occurrence and log which
    SKU(s) need fixing in the Sheet, rather than losing the whole batch."""
    deduped = {}
    duplicates = set()
    for row in rows:
        sku = row.get("SKU")
        if sku in deduped:
            duplicates.add(sku)
        deduped[sku] = row
    if duplicates:
        logger.warning(
            "%s: %d row(s) dropped due to duplicate SKU(s) in the Sheet - please fix these SKUs: %s",
            what, len(duplicates), ", ".join(sorted(duplicates)),
        )
    return list(deduped.values())


_RED = {"red": 0.96, "green": 0.8, "blue": 0.8}
_AMBER = {"red": 1.0, "green": 0.92, "blue": 0.7}
_WHITE = {"red": 1, "green": 1, "blue": 1}


def row_highlight_request(sheet_id, row_index, num_cols, active, pending_change=False):
    """Sheets API repeatCell request: RED for an out-of-stock row, AMBER for
    a row with a change an employee still needs to apply on OnBuy (or a
    flagged variant link), WHITE otherwise. Red wins over amber - going out
    of stock is the more urgent signal, and its Change Alert text says what
    to do anyway."""
    if not active:
        color = _RED
    elif pending_change:
        color = _AMBER
    else:
        color = _WHITE
    return {
        "repeatCell": {
            "range": {
                "sheetId": sheet_id,
                "startRowIndex": row_index - 1,
                "endRowIndex": row_index,
                "startColumnIndex": 0,
                "endColumnIndex": num_cols,
            },
            "cell": {"userEnteredFormat": {"backgroundColor": color}},
            "fields": "userEnteredFormat.backgroundColor",
        }
    }


def tokenize(text):
    return set(re.findall(r"\w+", str(text).lower()))


# Words too generic to say anything about a product's category - they appear
# in almost every eBay title/description ("premium quality", "free shipping",
# "brand new", "UK stock"...) and were a major source of wrong matches.
_CATEGORY_STOPWORDS = {
    "and", "the", "for", "with", "from", "this", "that", "your", "our", "you",
    "are", "not", "new", "brand", "pack", "pcs", "set", "free", "shipping",
    "delivery", "returns", "quality", "premium", "high", "best", "top", "hot",
    "sale", "gift", "uni", "unisex", "universal", "portable", "durable",
    "stock", "fast", "included", "includes", "colour", "color", "size", "uk",
    "usa", "our", "use", "product", "products", "item", "items", "piece",
    "pieces", "note", "please", "buy", "seller", "customer", "support",
    "service", "day", "days", "one", "two", "three", "all", "small", "large",
    "mini", "big", "medium", "extra", "travel",
}

# Category subtrees that share everyday vocabulary with ordinary physical
# products and kept stealing them in testing: a microwave FOOD COVER matched
# "Cooking Books" and "Kitchen Role Play Toys", a SLEEP MASK matched "BDSM
# Masks & Blindfolds". Each subtree is only allowed when the product
# explicitly uses one of its own words (stemmed forms, matching
# category_match_tokens' output - hence "dres" for dress).
_GUARDED_SUBTREES = (
    ("books, movies & music",
     {"book", "dvd", "blu", "vinyl", "movie", "film", "music", "album", "magazine", "novel"}),
    ("health & beauty > sex & adult",
     {"adult", "bdsm", "erotic", "bondage", "sex"}),
    ("toys & games > pretend play & fancy dress",
     {"toy", "pretend", "costume", "fancy", "dres", "kid", "child", "children"}),
)


def _stem(word):
    # Bridge singular/plural ("adapter" <-> "Adapters", "sling" <-> "Slings")
    # without a real stemmer - enough for category-path matching.
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def category_match_tokens(text):
    """Meaningful whole-word tokens for category matching: 3+ characters,
    not a stopword, not a bare number, plural-normalized."""
    return {_stem(w) for w in tokenize(text)
            if len(w) >= 3 and w not in _CATEGORY_STOPWORDS and not w.isdigit()}


def clean_category(cat):
    if not cat:
        return ""
    cat = str(cat).replace("\n", " ").strip()
    cat = re.sub(r"\s+", " ", cat).strip()
    return cat


def to_jpg(url):
    if not url:
        return ""
    url = re.sub(r"\.webp.*$", ".jpg", url)
    url = re.sub(r"\.(png|jpeg).*?$", ".jpg", url)
    return url


def empty_ebay_response():
    return {
        "stock": 0,
        "price": 0,
        "description": "",
        "main_image": "",
        "additional_images": [],
        "title": "",
        "brand": "",
        "product_code": "",
        "condition": "",
        "variant_group": "",
        "variant_detail": "",
    }


_BARCODE_ASPECT_NAMES = ("EAN", "GTIN", "UPC", "ISBN")


def extract_product_code(data):
    """Look for a real barcode (EAN/GTIN/UPC/ISBN) in eBay's item aspects -
    same array already parsed for Brand, no extra API call. Returns "" when
    the listing has no barcode specified. This is purely informational (the
    Sheet/Supabase "EAN" column) - it is NOT what gets sent to OnBuy as the
    product code. OnBuy uses the seller's own SKU for that instead (see the
    main loop), since the eBay item ID looked plausible as a fallback here
    but isn't a real barcode and got create_product rejected outright with
    "not a valid product code" when tried.
    """
    for aspect in data.get("localizedAspects", []):
        name = aspect.get("name", "").strip().upper()
        if name in _BARCODE_ASPECT_NAMES:
            values = aspect.get("value", "")
            raw = values[0] if isinstance(values, list) else values
            digits = re.sub(r"\D", "", str(raw))
            if digits:
                return digits
    return ""


# ================= EBAY TOKEN =================
def get_ebay_token():
    def _do_token():
        encoded = base64.b64encode(f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()).decode()
        resp = requests.post(
            "https://api.ebay.com/identity/v1/oauth2/token",
            headers={"Authorization": f"Basic {encoded}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
            timeout=30,
        )
        raise_for_status(resp, what="ebay token")
        token = resp.json().get("access_token")
        if not token:
            raise AuthError("ebay token response missing access_token")
        return token

    try:
        return with_retry(_do_token, what="ebay token", max_attempts=3)
    except (AuthError, PermanentError) as exc:
        logger.error("eBay authentication failed: %s", exc)
        return None
    except Exception as exc:
        logger.error("eBay authentication failed after retries: %s", exc)
        return None


ITEM_GROUP_ERROR_ID = 11006  # "The legacy Id is invalid... use get_items_by_item_group"


def _is_item_group_error(resp):
    try:
        errors = resp.json().get("errors", [])
    except ValueError:
        return False
    return any(e.get("errorId") == ITEM_GROUP_ERROR_ID for e in errors)


def _fetch_item_group_as_item(item_group_id, token):
    """Some eBay listings are multi-variation ("item group") listings - e.g.
    a listing with size/color options - which get_item_by_legacy_id rejects
    with errorId 11006, pointing at this endpoint instead.

    item_group_id here is the specific legacy item ID the Sheet row's URL
    actually linked to (that's what triggered the 11006 error in the first
    place), so match it back against the group's returned items and use that
    *exact* variation's own title/description/images/price. Only falls back
    to the first item in the group if no exact match is found - previously
    this always used the first item regardless of which variation was
    linked, which is the likely cause of unrelated rows appearing to share
    one variation's description (they'd all resolve to whichever variation
    the API happened to list first for that group).
    """
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params={"item_group_id": item_group_id},
        timeout=20,
    )
    raise_for_status(resp, what=f"ebay item group {item_group_id}")
    group_data = resp.json()

    items = group_data.get("items", [])
    if not items:
        return None

    chosen = next(
        (item for item in items if str(item.get("legacyItemId") or "") == str(item_group_id)),
        None,
    )
    if chosen is not None:
        logger.info("Item %s is a multi-variation listing - using its own linked variation", item_group_id)
    else:
        chosen = items[0]
        logger.warning(
            "Item %s is a multi-variation listing but its own variation wasn't found among "
            "the %d returned - falling back to variation %s, title/description/price may not "
            "match what this row's link actually points to",
            item_group_id, len(items), chosen.get("legacyItemId") or chosen.get("itemId"),
        )

    description = chosen.get("description")
    if not description:
        for common in group_data.get("commonDescriptions", []):
            if chosen.get("itemId") in common.get("itemIds", []):
                description = common.get("description", "")
                break
    chosen["description"] = description or ""

    return chosen


# Variant products are ON HOLD by store decision (2026-07-13): the feature
# is fully built and tested (Variant Choice matching, group fetching, the
# parent/variant columns in the export) but disabled - only single-option
# products are listed for now. Multi-option links get flagged
# "VARIANT - NOT SUPPORTED" exactly like before the feature existed.
# To re-enable: flip this AND the same flag in export_onbuy_upload.py to
# True, and restore the variant instructions in the employee guide/README.
VARIANTS_ENABLED = False


# ================= EBAY FETCH =================
# One get_items_by_item_group per listing per run: sibling variant rows all
# share the same base link, so the second..Nth row reuses the first row's
# group response instead of re-calling eBay.
_EBAY_GROUP_CACHE = {}


def _item_aspects(item):
    """{aspect name: value} for one variation item."""
    aspects = {}
    for aspect in item.get("localizedAspects", []) or []:
        name = str(aspect.get("name") or "").strip()
        value = aspect.get("value", "")
        if isinstance(value, list):
            value = value[0] if value else ""
        if name:
            aspects[name] = str(value).strip()
    return aspects


def _fetch_item_group(item_group_id, token):
    if item_group_id in _EBAY_GROUP_CACHE:
        return _EBAY_GROUP_CACHE[item_group_id]
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item/get_items_by_item_group",
        headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
        params={"item_group_id": item_group_id},
        timeout=20,
    )
    raise_for_status(resp, what=f"ebay item group {item_group_id}")
    group = resp.json()
    _EBAY_GROUP_CACHE[item_group_id] = group
    return group


def _resolve_group_variation(group, item_id, var_id, variant_choice):
    """Pick ONE variation out of a multi-option listing.

    The variation is chosen by (in order): the ?var= id from the pasted URL,
    then the row's "Variant Choice" text matched against each combination's
    option values. Returns (chosen_item, variant_detail) on success,
    (None, flag_data) when an employee still needs to choose, or (None, None)
    for an empty group (treat as removed).

    Which aspects are the *options* (Colour/Size/...) isn't marked by the
    API - it's computed here: any aspect whose value differs between items
    in the group is a variation axis; shared aspects (Brand, Material that's
    the same everywhere, ...) are not.
    """
    items = group.get("items", [])
    if not items:
        return None, None
    per_item = [(item, _item_aspects(item)) for item in items]

    value_sets = {}
    for _, aspects in per_item:
        for name, value in aspects.items():
            value_sets.setdefault(name, set()).add(value)
    first_aspects = list(per_item[0][1])
    varying = sorted(
        [name for name, vals in value_sets.items() if len(vals) > 1],
        key=lambda n: first_aspects.index(n) if n in first_aspects else len(first_aspects),
    )

    if not varying or len(items) == 1:
        # A "group" with effectively one option - treat as a normal product.
        return items[0], ""

    candidates = [(i, [aspects.get(n, "") for n in varying]) for i, (_, aspects) in enumerate(per_item)]

    chosen_idx, reason = None, None
    if var_id:
        # Browse API itemId format is "v1|<listing id>|<variation id>".
        chosen_idx = next((i for i, (item, _) in enumerate(per_item)
                           if str(item.get("itemId") or "").endswith(f"|{var_id}")), None)
    if chosen_idx is None and str(variant_choice or "").strip():
        chosen_idx, reason = match_variant_choice(variant_choice, candidates)
    if chosen_idx is None:
        flag = empty_ebay_response()
        flag["is_variant"] = True
        flag["variant_options"] = options_text(candidates)
        flag["variant_reason"] = reason or ("no_match" if (var_id or str(variant_choice or "").strip()) else "missing")
        return None, flag

    item, aspects = per_item[chosen_idx]
    detail = "; ".join(f"{n}={aspects[n]}" for n in varying if aspects.get(n))
    return item, detail


def get_ebay_data(url, token, variant_choice=""):
    """Returns (available, data). available=False with empty_ebay_response()
    means eBay gave us a definitive "not available" answer (404 / no price /
    out of stock) - a real signal, not a failure.

    Raises TransientError/PermanentError if the fetch itself failed after
    retries. Callers MUST NOT treat that the same as "removed" - the previous
    version's bare `except Exception` did exactly that and zeroed live
    listings on ordinary network blips.

    Multi-variation listings: the variation is picked by the ?var= id when
    the pasted link has one, else by the row's "Variant Choice" text. No
    pick -> is_variant flag listing the options for the employee.
    """
    match = re.search(r"/itm/(\d+)", url)
    if not match:
        return False, empty_ebay_response()
    item_id = match.group(1)
    var_match = re.search(r"[?&]var=(\d+)", url)
    var_id = var_match.group(1) if var_match else None

    def _do_fetch():
        resp = requests.get(
            "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
            headers={"Authorization": f"Bearer {token}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"},
            params={"legacy_item_id": item_id},
            timeout=20,
        )
        if resp.status_code == 404:
            return None  # confirmed removed - a real signal, not an error
        if resp.status_code == 400 and _is_item_group_error(resp):
            return {"__item_group__": _fetch_item_group(item_id, token)}
        raise_for_status(resp, what=f"ebay item {item_id}")
        return resp.json()

    data = with_retry(_do_fetch, what=f"ebay item {item_id}", max_attempts=3)

    variant_group, variant_detail = "", ""
    if isinstance(data, dict) and "__item_group__" in data:
        # Mirrors the AliExpress behavior in both modes: a "group" listing
        # with effectively one variation is just a product; a link that
        # names its variation (?var=) resolves to it; and with variants on
        # hold, anything the link can't settle gets flagged WITH the option
        # list (Variant Choice matching stays enabled-mode only).
        group = data["__item_group__"]
        chosen, extra = _resolve_group_variation(
            group, item_id, var_id, variant_choice if VARIANTS_ENABLED else "")
        if chosen is None and extra is None:
            logger.info("REMOVED LISTING (empty variation group): %s", item_id)
            return False, empty_ebay_response()
        if chosen is None:
            if not VARIANTS_ENABLED:
                extra["variant_reason"] = "disabled"
            logger.info("VARIANT NEEDS A CHOICE: %s", item_id)
            return False, extra
        if not chosen.get("description"):
            for common in group.get("commonDescriptions", []):
                if chosen.get("itemId") in common.get("itemIds", []):
                    chosen["description"] = common.get("description", "")
                    break
        if VARIANTS_ENABLED:
            variant_group, variant_detail = item_id, extra
        elif extra:
            logger.info("MULTI-VARIATION LISTING RESOLVED AS SINGLE (variants on hold): %s", item_id)
        data = chosen

    if data is None:
        logger.info("REMOVED LISTING: %s", item_id)
        return False, empty_ebay_response()

    price_data = data.get("price", {}) or {}
    price = float(price_data.get("value", 0) or 0)
    if price <= 0:
        logger.info("NO PRICE: %s", item_id)
        return False, empty_ebay_response()

    estimated = data.get("estimatedAvailabilities", [])
    stock = 5
    if estimated:
        est = estimated[0]
        status = est.get("estimatedAvailabilityStatus", "")
        if status in ("OUT_OF_STOCK", "UNAVAILABLE"):
            logger.info("OUT OF STOCK: %s", item_id)
            return False, empty_ebay_response()
        stock = est.get("estimatedAvailableQuantity")
        if not stock:
            # eBay hides the exact count above a threshold - the listing
            # shows "More than 10 available" and the API sends only
            # availabilityThresholdType=MORE_THAN + the threshold, no
            # quantity. Use that known floor ("at least 10") rather than
            # a made-up number; never inflate past what eBay confirms -
            # overselling is the costlier mistake.
            if str(est.get("availabilityThresholdType") or "") == "MORE_THAN":
                stock = est.get("estimatedAvailabilityThreshold") or 0
    if not stock or stock <= 0:
        stock = 5

    html_description = sanitize_description(data.get("description", ""))

    main_image = ""
    if data.get("image"):
        main_image = to_jpg(data["image"].get("imageUrl", ""))

    additional_images = []
    for img in data.get("additionalImages", []):
        img_url = to_jpg(img.get("imageUrl", ""))
        if img_url:
            additional_images.append(img_url)

    all_images = validate_images([main_image] + additional_images, max_images=11)
    main_image = all_images[0] if all_images else ""
    additional_images = all_images[1:11]

    title = data.get("title", "")

    brand = ""
    for aspect in data.get("localizedAspects", []):
        if aspect.get("name", "").lower() == "brand":
            values = aspect.get("value", "")
            brand = values[0] if isinstance(values, list) else values
    brand = normalize_brand(brand)

    product_code = extract_product_code(data)
    condition = data.get("condition") or "New"

    return True, {
        "stock": stock,
        "price": price,
        "description": html_description,
        "main_image": main_image,
        "additional_images": additional_images,
        "title": title,
        "brand": brand,
        "product_code": product_code,
        "condition": condition,
        "variant_group": variant_group,
        "variant_detail": variant_detail,
    }


def main():
    run_had_errors = False
    fetch_failures = 0

    # ================= GOOGLE SHEET =================
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open(SHEET_NAME).sheet1

    # Header hygiene BEFORE reading the data: one stray space typed into a
    # header cell ("SKU ") makes that whole column unreadable - row.get("SKU")
    # returns None on every row - which showed up as a sheet-wide MISSING SKU
    # scare on 2026-07-13. Strip every header name, and refuse to run at all
    # on missing/duplicated critical headers: one clear email beats a run
    # that half-works and flags every row.
    headers = [str(h).strip() for h in sheet.row_values(1)]
    col_map = {col: idx + 1 for idx, col in enumerate(headers) if col}

    required = ["SKU", "Supplier URL", "Title", "Status", "Last Checked Time"]
    missing = [h for h in required if h not in col_map]
    duplicates = sorted({h for h in headers if h and headers.count(h) > 1})
    if missing or duplicates:
        problems = []
        if missing:
            problems.append("missing header(s): " + ", ".join(missing))
        if duplicates:
            problems.append("duplicated header(s): " + ", ".join(duplicates))
        message = ("The Sheet's header row (row 1) is broken - " + "; ".join(problems)
                   + f". Row 1 currently reads: {[h for h in headers if h]}. "
                   "Fix row 1 to match sheet_headers.csv (one name per cell, spelled exactly) "
                   "and run the sync again. No rows were touched this run.")
        logger.error(message)
        notify.send_alert_email("YRA sheet header row needs fixing", message)
        sys.exit(1)

    data = sheet.get_all_records()
    # Same hygiene on the row dicts (their keys come from the header row).
    data = [{str(k).strip(): v for k, v in row.items()} for row in data]

    logger.info("TOTAL ROWS IN SHEET: %d", len(data))

    # ================= DYNAMIC BATCH SIZE =================
    # Sized from the actual row count and the eBay daily call budget, so the
    # same code scales from a 150-row catalog to a 5,000-row one without
    # needing a manual reconfiguration each time it grows - see the comment
    # on EBAY_DAILY_CALL_BUDGET/RUNS_PER_DAY above.
    if _MAX_PRODUCTS_PER_RUN_OVERRIDE:
        MAX_PRODUCTS_PER_RUN = max(1, int(_MAX_PRODUCTS_PER_RUN_OVERRIDE))
    else:
        MAX_PRODUCTS_PER_RUN = max(1, EBAY_DAILY_CALL_BUDGET // RUNS_PER_DAY)

    cycle_runs = -(-len(data) // MAX_PRODUCTS_PER_RUN) if data else 0  # ceil division
    cycle_days = cycle_runs / RUNS_PER_DAY if RUNS_PER_DAY else 0
    logger.info(
        "Batch size: %d products/run (budget %d eBay calls/day over %d runs/day) "
        "- a full refresh cycle over %d rows takes ~%.1f day(s)",
        MAX_PRODUCTS_PER_RUN, EBAY_DAILY_CALL_BUDGET, RUNS_PER_DAY, len(data), cycle_days,
    )

    # ================= CATEGORY FILE =================
    onbuy_categories = []
    category_id_by_path = {}

    with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as csvfile:
        reader = csv.DictReader(csvfile)
        for row in reader:
            category = row.get("OnBuy Category Path")
            if category:
                onbuy_categories.append(category)
                try:
                    category_id_by_path[category.strip().lower()] = int(row.get("Category ID"))
                except (TypeError, ValueError):
                    category_id_by_path[category.strip().lower()] = None

    logger.info("Loaded %d OnBuy categories", len(onbuy_categories))

    valid_onbuy_categories = set(cat.strip().lower() for cat in onbuy_categories)

    def is_valid_onbuy_category(category):
        return str(category).strip().lower() in valid_onbuy_categories

    # Precomputed token sets per category path (and per leaf segment) - both
    # for speed and correctness. The old scorer gave +2 whenever a product
    # word appeared as a SUBSTRING anywhere in the path text, so a
    # DisplayPort adapter (description mentioning "home", "supports" - and
    # "port" is literally a substring of "Supports") landed in "Braces,
    # Splints & Slings > Arm, Hand & Finger Supports". This version matches
    # whole words only, weights title words over description words (titles
    # identify the product; descriptions are marketing text), weights the
    # leaf segment over ancestors, and refuses to guess without at least one
    # strong title-level match.
    category_tokens = {}
    category_leaf_tokens = {}
    category_guard = {}  # path -> required word set (None = unguarded)
    for _path in onbuy_categories:
        category_tokens[_path] = category_match_tokens(_path)
        category_leaf_tokens[_path] = category_match_tokens(_path.split(">")[-1])
        _low = _path.strip().lower()
        category_guard[_path] = next(
            (req for prefix, req in _GUARDED_SUBTREES if _low.startswith(prefix)), None)

    def map_onbuy_category(title, current_category, description=""):
        title_words = category_match_tokens(f"{title}\n{current_category}")
        desc_words = category_match_tokens(description) - title_words
        all_words = title_words | desc_words
        if not all_words:
            return current_category

        def weight(word):
            # Longer words are more specific; title words count triple.
            return len(word) * (3 if word in title_words else 1)

        best_match = None
        best_score = 0
        best_has_title_hit = False
        for category_path in onbuy_categories:
            required = category_guard[category_path]
            if required and not (all_words & required):
                continue
            hits = all_words & category_tokens[category_path]
            if not hits:
                continue
            leaf_hits = hits & category_leaf_tokens[category_path]
            score = sum(weight(w) for w in hits) + 2 * sum(weight(w) for w in leaf_hits)
            if score > best_score:
                best_score = score
                best_match = category_path
                best_has_title_hit = bool(hits & title_words)

        # Refuse to guess unless at least one TITLE word matched (titles
        # identify the product; a description-only match is marketing noise).
        # An unmatched row keeps a blank category, which holds it back from
        # the OnBuy export until a human sets one.
        if best_match and best_score >= 9 and best_has_title_hit:
            return best_match
        return current_category

    # ================= ONBUY CLIENT =================
    onbuy = OnBuyClient()
    onbuy_ready = False
    if ONBUY_API_PUSH_ENABLED:
        onbuy_ready = onbuy.authenticate()
        if not onbuy_ready:
            run_had_errors = True
            logger.error("ONBUY_API_PUSH_ENABLED is true but OnBuy authentication failed - skipping all OnBuy API pushes this run")

    # ================= CATEGORY MAPPING =================
    # Cheap full-catalog pass (no eBay calls) for rows that already have a
    # Title/Description from a previous run. A brand-new row (employee only
    # pasted the URL) still has blank Title/Description at this point, so it
    # can't be mapped yet here - the main loop below re-checks category using
    # the freshly-fetched eBay data for exactly that case.
    if RUN_CATEGORY_MAPPING:
        logger.info("Updating categories...")
        category_updates = []
        for idx, row in enumerate(data):
            i = idx + 2
            current_category = str(row.get("Category") or "").strip()
            if is_valid_onbuy_category(current_category):
                continue
            mapped = map_onbuy_category(row.get("Title"), current_category, row.get("Description"))
            if mapped != current_category:
                category_updates.append({"range": f"{col_letter(col_map['Category'])}{i}", "values": [[mapped]]})
                logger.info("Mapped row %d", i)
        if category_updates:
            sheet.batch_update(category_updates)

    # ================= PRODUCT ORDER =================
    # Rows with no usable Supplier URL yet (e.g. a SKU pre-filled ahead of the
    # rest of the row) can never actually be processed - the main loop below
    # just silently `continue`s past them without ever setting Last Checked
    # Time. Left in the sort, they never age out of "oldest first," so once
    # there are more of them than MAX_PRODUCTS_PER_RUN they permanently
    # occupy every run's entire batch and starve real, fully-filled-in rows
    # of any processing at all (confirmed: 770 SKU-only rows blocked all 500
    # slots in a run, so none of the 376 real rows were even reached).
    # Filtering them out before the sort/slice means batch capacity is only
    # ever spent on rows that can actually make progress.
    # ================= REMOVE-TICKED ROWS (whole sheet) =================
    # An employee ticks the "Remove" column to delete a product permanently
    # (rule: deactivate it on OnBuy first if it was ever uploaded). The run
    # does the deleting at its safe end-of-run moment - Supabase first, Sheet
    # rows in descending order - so nobody hand-deletes rows and risks the
    # mid-run row-shift accident again. Unticked checkbox reads "FALSE",
    # which must never match.
    _REMOVE_TRUTHY = {"TRUE", "YES", "1", "DONE", "X", "REMOVE"}
    removal_rows = set()
    removal_skus = []
    for _idx, _row in enumerate(data):
        if str(_row.get("Remove") or "").strip().upper() in _REMOVE_TRUTHY:
            removal_rows.add(_idx + 2)
            _sku = str(_row.get("SKU") or "").strip()
            if _sku:
                removal_skus.append(_sku)
    if removal_rows:
        logger.info("Remove ticked on %d row(s) - they will be deleted at the end of this run (SKUs: %s)",
                    len(removal_rows), ", ".join(removal_skus) or "none")

    processable = [(idx, row) for idx, row in enumerate(data)
                   if (idx + 2) not in removal_rows and detect_supplier(row.get("Supplier URL", ""))]
    skipped_incomplete = len(data) - len(processable)
    if skipped_incomplete:
        logger.info("Skipping %d row(s) with no usable Supplier URL yet (eBay/AliExpress) - not counted against this run's batch", skipped_incomplete)

    if FULL_REFRESH:
        sorted_data = processable
    else:
        sorted_data = sorted(processable, key=lambda x: parse_time(x[1].get("Last Checked Time", "")))

    # While testing the OnBuy API push against a specific SKU allowlist, move
    # those SKUs to the front of the queue - otherwise a manual test run can
    # easily land on a batch that doesn't include any of them (oldest-checked
    # rows win by default), making it look like the push silently did nothing
    # when really it just never got a chance to run.
    if ONBUY_API_PUSH_ENABLED and ONBUY_API_TEST_SKUS:
        sorted_data = sorted(
            sorted_data,
            key=lambda x: str(x[1].get("SKU") or "").strip() not in ONBUY_API_TEST_SKUS,
        )

    logger.info("Processing %d products", min(len(sorted_data), MAX_PRODUCTS_PER_RUN))

    # ================= MAIN UPDATE LOOP =================
    # Only authenticate with the suppliers this batch actually needs - an
    # AliExpress-only batch must not abort because eBay keys are absent, and
    # vice versa.
    batch_suppliers = {detect_supplier(str(r.get("Supplier URL", "")))
                       for _, r in sorted_data[:MAX_PRODUCTS_PER_RUN]}
    token = None
    if "eBay" in batch_suppliers:
        token = get_ebay_token()
        if not token:
            # Abort instead of proceeding to call every row with a bad/missing
            # token - the old code sent "Authorization: Bearer None" per row,
            # which zeroed price/stock for the entire batch on a single auth failure.
            logger.error("Could not obtain an eBay token - aborting run without touching any rows")
            notify.send_alert_email(
                "eBay authentication failed - run aborted",
                "generate_xml.py could not obtain an eBay OAuth token this run. "
                "No sheet rows were touched. Check EBAY_CLIENT_ID/EBAY_CLIENT_SECRET.",
            )
            sys.exit(1)
    if "AliExpress" in batch_suppliers and not ali_ready():
        # Not a run failure: a store can be eBay-only on purpose (keys not
        # set up yet) while employees paste AliExpress links anyway. Those
        # rows get flagged amber on the sheet below - failing the whole run
        # every 3 hours just buries real failures under alarm fatigue.
        logger.warning(
            "Batch contains AliExpress rows but ALI_APP_KEY/ALI_APP_SECRET/ALI_ACCESS_TOKEN "
            "are not all set - those rows are flagged on the sheet until the keys exist")

    updated_count = 0
    change_log = []  # (sku, note) per change an employee must apply on OnBuy - emailed after the run
    variant_rows = 0  # multi-variation links flagged for replacement this run
    unreadable_rows = 0  # supplier links with no readable product id, flagged for replacement
    missing_sku_rows = 0  # rows with a working link but no SKU yet, flagged for filling in
    ali_unconfigured_rows = 0  # AliExpress rows on a store whose Ali keys aren't set up
    duplicate_link_rows_flagged = 0  # rows repeating an earlier row's supplier product
    invalid_sku_rows = 0  # SKUs whose digits fail the GS1 check-digit test
    duplicate_sku_rows_flagged = 0  # rows whose SKU digits repeat an earlier row's barcode

    # ================= DUPLICATE SKU DETECTION (whole sheet, by digits) =================
    # Two rows must never share a barcode: the SKU's digits ARE the EAN, and
    # the same EAN twice would be the same product twice on OnBuy - so
    # "YRA-5012345678900" and "5012345678900" count as duplicates. The FIRST
    # row with a given barcode keeps working normally (it's the original,
    # possibly already live on OnBuy); every later row with the same digits
    # is flagged and skipped - no fetch spent on it - until an employee
    # gives it its own barcode.
    first_row_by_digits = {}
    duplicate_sku_rows = {}  # sheet row number -> (digits, row number of the original)
    for _idx, _row in enumerate(data):
        _digits = sku_numeric_part(_row.get("SKU"))
        if not _digits:
            continue
        _rownum = _idx + 2
        if _digits in first_row_by_digits:
            duplicate_sku_rows[_rownum] = (_digits, first_row_by_digits[_digits])
        else:
            first_row_by_digits[_digits] = _rownum

    # ================= DUPLICATE LINK DETECTION (whole sheet) =================
    # Sibling of the duplicate-SKU guard for the OTHER copy-paste mistake:
    # the same supplier product pasted on several rows, each with its own
    # barcode - the SKU guard can't see it, and OnBuy would end up with
    # duplicate listings of one product. Keyed statically (no API calls) on
    # supplier + product id + the exact version the link names (sku_id/var),
    # so two different versions of one listing are legitimately allowed.
    first_row_by_link = {}
    duplicate_link_rows = {}  # sheet row number -> row number of the original
    for _idx, _row in enumerate(data):
        _url = str(_row.get("Supplier URL") or "").strip()
        _sup = detect_supplier(_url)
        _key = None
        if _sup == "eBay":
            _m = re.search(r"/itm/(\d+)", _url)
            if _m:
                _v = re.search(r"[?&]var=(\d+)", _url)
                _key = ("eBay", _m.group(1), _v.group(1) if _v else "")
        elif _sup == "AliExpress":
            _pid = extract_product_id(_url)
            if _pid:
                _key = ("AliExpress", _pid, extract_sku_id(_url) or "")
        if _key is None:
            continue
        _rownum = _idx + 2
        if _key in first_row_by_link:
            duplicate_link_rows[_rownum] = first_row_by_link[_key]
        else:
            first_row_by_link[_key] = _rownum
    onbuy_created = 0
    onbuy_updated = 0
    onbuy_failed = 0
    onbuy_removed = 0
    onbuy_deferred = 0  # created earlier, listing not yet updatable on OnBuy's side
    onbuy_postponed = 0  # transient OnBuy/transport trouble - status left untouched, retried next run
    onbuy_halt_reason = None  # set when pushing must stop for the rest of the run (rate limit / dead token)
    onbuy_pushes_this_run = 0
    rows_to_delete = []  # Sheet row numbers to remove entirely - see the
    # "supplied brand is owned by another seller" check below. Applied after
    # every other Sheet write this run, in descending row order, so deleting
    # one doesn't shift the row numbers the other writes/highlights already
    # targeted.
    removed_skus = []  # matching SKUs, for the Supabase delete + summary log
    supabase_rows = []  # one upsert for the whole run - every row must have
    # identical keys (PostgREST's bulk-upsert requirement) AND every NOT NULL
    # column must be present (Postgres validates that on the candidate insert
    # row before it even checks ON CONFLICT, so a partial-column "tracking
    # only" upsert can never work here - see fetch_existing_fields()).
    highlight_requests = []
    all_sheet_updates = []  # accumulated across every row, written in ONE batch_update
    # after the loop instead of one call per row - a run can now process
    # hundreds of rows (see dynamic batch sizing above), and one Sheets API
    # write call per row at that scale risks Google's own rate limits, which
    # weren't a concern back when this was capped at a hardcoded 12/run.
    num_cols = len(headers)

    batch = sorted_data[:MAX_PRODUCTS_PER_RUN]

    # Within the selected batch, hand the limited OnBuy push slots
    # (ONBUY_MAX_PUSHES_PER_RUN) to rows that have never been pushed first
    # (blank "Last OnBuy Sync" parses to year 2000), then oldest-pushed.
    # Without this, processing order == Last Checked Time order, which is
    # stable across runs - so the same ~200 rows won the push slots every
    # run and rows beyond the cap (including genuinely new, never-listed
    # products) never reached OnBuy at all. Batch *selection* above stays
    # based on Last Checked Time (eBay refresh fairness); this only reorders
    # within the same set, so it changes who gets OnBuy slots, not which
    # rows get their eBay/Sheet refresh. Skipped while a test-SKU allowlist
    # is active so those keep absolute front-of-queue priority.
    if not (ONBUY_API_PUSH_ENABLED and ONBUY_API_TEST_SKUS):
        batch.sort(key=lambda x: parse_time(str(x[1].get("Last OnBuy Sync") or "")))

    # Pre-fetch OPC + OnBuy-tracking fields already on record for this run's
    # batch, so the single Supabase upsert (below) can carry forward real
    # values instead of blanking them out for rows not pushed to OnBuy this
    # run - see fetch_existing_fields() for why this has to be a single
    # always-full-row upsert rather than a separate partial-column one.
    skus_in_batch = [str(row.get("SKU") or "").strip() for _, row in batch]
    skus_in_batch = [s for s in skus_in_batch if s]
    existing_fields = supabase_db.fetch_existing_fields(skus_in_batch)

    for idx, row in batch:
        i = idx + 2
        url = str(row.get("Supplier URL", "")).strip()

        supplier = detect_supplier(url)
        if supplier is None:
            continue
        if supplier == "AliExpress" and not ali_ready():
            # Flag it ON THE ROW like every other fix-me state, and stamp
            # Last Checked Time - unstamped rows never age out of "oldest
            # first" and permanently hog the front of every batch (the
            # starvation lesson, AliExpress edition: 57 of 125 slots were
            # burning on untouched rows before this). Self-heals: once the
            # keys exist, ali_ready() is true and the row processes.
            ali_unconfigured_rows += 1
            now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
            ali_updates = [
                {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["ALIEXPRESS NOT CONNECTED"]]},
                {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
            ]
            if "Supplier" in col_map:
                ali_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
            if "Change Alert" in col_map:
                ali_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}",
                                    "values": [["AliExpress is not connected on this store yet - use an eBay "
                                                "link for this product, or ask for the AliExpress keys to be added"]]})
            if "Change Time" in col_map:
                ali_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[now_str]]})
            all_sheet_updates.extend(ali_updates)
            highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, active=True, pending_change=True))
            continue

        if i in duplicate_sku_rows:
            # Flagged before fetching - a duplicate barcode can never go to
            # OnBuy, so the API call would be wasted. The original row (the
            # first with these digits) is untouched and keeps updating.
            dup_digits, original_row = duplicate_sku_rows[i]
            duplicate_sku_rows_flagged += 1
            now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
            dup_updates = [
                {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["DUPLICATE SKU"]]},
                {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
            ]
            if "Supplier" in col_map:
                dup_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
            if "Change Alert" in col_map:
                dup_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}",
                                    "values": [[f"DUPLICATE SKU - barcode {dup_digits} is already used on row {original_row}. "
                                                "Every product needs its own unique barcode"]]})
            if "Change Time" in col_map:
                dup_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[now_str]]})
            all_sheet_updates.extend(dup_updates)
            highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, active=True, pending_change=True))
            logger.warning("Row %d: SKU digits %s duplicate row %d - flagged DUPLICATE SKU and skipped",
                           i, dup_digits, original_row)
            continue

        if i in duplicate_link_rows:
            # Same supplier product as an earlier row - flagged before any
            # fetch, original row keeps working normally.
            link_original = duplicate_link_rows[i]
            duplicate_link_rows_flagged += 1
            now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
            dupl_updates = [
                {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["DUPLICATE LINK"]]},
                {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
            ]
            if "Supplier" in col_map:
                dupl_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
            if "Change Alert" in col_map:
                dupl_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}",
                                     "values": [[f"DUPLICATE LINK - this is the same supplier product as row {link_original}. "
                                                 "A product may only be listed once - remove this row or change its link"]]})
            if "Change Time" in col_map:
                dupl_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[now_str]]})
            all_sheet_updates.extend(dupl_updates)
            highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, active=True, pending_change=True))
            logger.warning("Row %d: same supplier product as row %d - flagged DUPLICATE LINK and skipped",
                           i, link_original)
            continue

        # SKU checks run BEFORE the supplier fetch: a row that cannot export
        # should not spend an API call every run (56 zero-stripped barcodes
        # were burning ~450 calls/day of a 1,000/day budget when this sat
        # after the fetch).
        sku = str(row.get("SKU") or "").strip()
        if not sku:
            # Flag it ON THE ROW, not just in the log, and stamp Last
            # Checked Time so these rows age through batch rotation
            # (starvation lesson).
            missing_sku_rows += 1
            now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
            sku_updates = [
                {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["MISSING SKU"]]},
                {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
            ]
            if "Supplier" in col_map:
                sku_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
            if "Change Alert" in col_map:
                sku_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}",
                                    "values": [["ADD SKU - this row needs its own unique UPC barcode in the SKU column"]]})
            if "Change Time" in col_map:
                sku_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[now_str]]})
            all_sheet_updates.extend(sku_updates)
            highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, active=True, pending_change=True))
            logger.warning("Row %d: no SKU provided - flagged MISSING SKU (OnBuy requires a unique SKU per product)", i)
            continue
        if not is_valid_gtin(sku_numeric_part(sku)):
            # A number that fails the GS1 check digit would be rejected by
            # OnBuy at upload - surface it within one sync cycle instead of
            # at export day. Commonest cause in practice is not invention:
            # Google Sheets strips leading zeros off digit-only cells.
            invalid_sku_rows += 1
            now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
            inv_updates = [
                {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["INVALID SKU"]]},
                {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
            ]
            if "Supplier" in col_map:
                inv_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
            if "Change Alert" in col_map:
                inv_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}",
                                    "values": [[f"INVALID SKU - '{sku}' is not a real barcode (must be 8, 12, 13 or 14 "
                                                "digits ending in a correct check digit). If the number looks right, "
                                                "leading zeros were probably stripped - format the SKU column as "
                                                "Plain text and re-type it with its zeros"]]})
            if "Change Time" in col_map:
                inv_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[now_str]]})
            all_sheet_updates.extend(inv_updates)
            highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, active=True, pending_change=True))
            logger.warning("Row %d: SKU %s fails the barcode check - flagged INVALID SKU and skipped", i, sku)
            continue

        variant_choice = str(row.get("Variant Choice") or "").strip() if VARIANTS_ENABLED else ""

        try:
            if supplier == "AliExpress":
                available, ebay_data = get_aliexpress_data(url, variant_choice, variants_enabled=VARIANTS_ENABLED)
            else:
                available, ebay_data = get_ebay_data(url, token, variant_choice)
        except (TransientError, PermanentError) as exc:
            fetch_failures += 1
            run_had_errors = True
            logger.error("Row %d (%s): fetch failed after retries, leaving existing values untouched - %s", i, url, exc)
            continue

        if ebay_data.get("is_variant"):
            # Multi-option listing and this row hasn't picked its option yet
            # (or the typed choice didn't match). Write the available options
            # into the Change Alert so the employee can copy one into the
            # "Variant Choice" column, stamp Last Checked Time so the row
            # still ages through batch rotation, touch nothing else.
            variant_rows += 1
            now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
            options = ebay_data.get("variant_options") or ""
            reason = ebay_data.get("variant_reason") or "missing"
            if reason == "disabled":
                # Variants on hold. When the supplier told us what the hidden
                # options are, show them - a "no variants!" page can still be
                # multi-SKU underneath (Ships From, pack quantity).
                status_text = "VARIANT - NOT SUPPORTED"
                if options:
                    alert_text = ("VARIANT LINK - this listing has options underneath (" + options + "). "
                                  "Open the product page, pick one, and copy the address-bar link again - "
                                  "or replace with a single-product link")[:1500]
                else:
                    alert_text = "VARIANT LINK - replace with a single-product link (no colour/size options)"
            elif reason == "ambiguous":
                status_text = "CHOOSE VARIANT"
                alert_text = ("VARIANT CHOICE MATCHES MORE THAN ONE OPTION - make 'Variant Choice' more "
                              "specific. Options: " + options)
            elif reason == "no_match":
                status_text = "CHOOSE VARIANT"
                alert_text = ("VARIANT CHOICE NOT FOUND - copy one of these into 'Variant Choice': " + options)
            else:
                status_text = "CHOOSE VARIANT"
                alert_text = ("CHOOSE VARIANT - this listing has options. Copy one of these into "
                              "'Variant Choice': " + options)
            variant_updates = [
                {"range": f"{col_letter(col_map['Status'])}{i}", "values": [[status_text]]},
                {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
            ]
            if "Supplier" in col_map:
                variant_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
            if "Change Alert" in col_map:
                variant_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}",
                                        "values": [[alert_text[:1500]]]})
            if "Change Time" in col_map:
                variant_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[now_str]]})
            all_sheet_updates.extend(variant_updates)
            highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, active=True, pending_change=True))
            logger.info("Row %d: variant needs a choice - options written to Change Alert", i)
            time.sleep(0.2)
            continue

        if ebay_data.get("is_unreadable_link"):
            # AliExpress link with no readable product id (share-link, browse
            # page, bundle page). Same treatment as variants: flag it for an
            # employee to replace, stamp Last Checked Time so the row keeps
            # aging through batch rotation, touch nothing else.
            unreadable_rows += 1
            now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
            unreadable_updates = [
                {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["LINK NOT READABLE"]]},
                {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
            ]
            if "Supplier" in col_map:
                unreadable_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
            if "Change Alert" in col_map:
                unreadable_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}",
                                           "values": [["UNREADABLE LINK - open the product page and paste its full link (address must contain /item/)"]]})
            if "Change Time" in col_map:
                unreadable_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[now_str]]})
            all_sheet_updates.extend(unreadable_updates)
            highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, active=True, pending_change=True))
            logger.info("Row %d: unreadable supplier link flagged for replacement - skipping", i)
            time.sleep(0.2)
            continue

        stock = ebay_data["stock"]
        cost_price = ebay_data["price"]

        # When eBay reports the item unavailable (removed/no price/out of
        # stock), ebay_data's descriptive fields are all blank - previously
        # those blanks got written straight over the Sheet's existing good
        # data, making the row look emptied out. Only stock/price/status
        # should reflect "unavailable"; title/description/images/brand keep
        # whatever was already there.
        if available:
            title = ebay_data["title"]
            description = ebay_data["description"]
            # normalize_brand is idempotent - eBay data arrives already
            # normalized, AliExpress data arrives raw.
            brand = normalize_brand(ebay_data["brand"])
            main_image = ebay_data["main_image"]
            additional_images = ebay_data["additional_images"]
            variant_group = ebay_data.get("variant_group") or ""
            variant_detail = ebay_data.get("variant_detail") or ""
        else:
            title = str(row.get("Title") or "")
            description = str(row.get("Description") or "")
            brand = normalize_brand(str(row.get("Brand") or ""))
            main_image = str(row.get("Image URL") or "")
            additional_images = [img.strip() for img in str(row.get("Additional Images") or "").split(",") if img.strip()]
            # A variant that goes out of stock is still the same variant.
            variant_group = str(row.get("Variant Group") or "")
            variant_detail = str(row.get("Variant") or "")

        # ================= SKU (must be entered manually - OnBuy requires unique
        # SKUs, and two different sourcing links can share the same barcode/item
        # ID, so auto-deriving one risks a collision between two real products) ==

        # ================= CATEGORY (re-checked here with fresh title/description so a
        # brand-new row gets categorized on this same pass, not just the upfront
        # full-catalog remap above, which ran before this row's eBay data existed) ====
        current_category = str(row.get("Category") or "").strip()
        if is_valid_onbuy_category(current_category):
            category = current_category
            category_needs_write = False
        else:
            category = map_onbuy_category(title, current_category, description)
            category_needs_write = category != current_category
        category_id = category_id_by_path.get(category.strip().lower())

        # ================= PRICING =================
        # Default margin is a floor, not a fixed price: if a product's price
        # already implies more than the default 40% total margin (20% fee +
        # 20% profit), leave it alone - only bump prices UP that currently
        # imply less than the default, never silently lower a price someone
        # deliberately set higher.
        shipping_cost = float(row.get("Shipping Cost (£)") or 0)
        formula_price = pricing.calculate_selling_price(cost_price, shipping_cost)
        existing_price = float(row.get("Selling Price (£)") or 0)
        selling_price = 0 if stock == 0 else max(existing_price, formula_price)

        # ================= PRICE CHECK FLAG =================
        # Normal = at/near the default margin, Medium = moderately above it,
        # High = well above it. Thresholds are a judgment call on "a little
        # more" / "much more" - adjust PRICE_CHECK_MEDIUM_MAX_PCT /
        # PRICE_CHECK_HIGH_MIN_PCT below if these don't match what you meant.
        if stock == 0 or cost_price <= 0:
            price_check_flag = ""
        else:
            margin_pct = (selling_price - cost_price) / cost_price * 100
            if margin_pct <= PRICE_CHECK_NORMAL_MAX_PCT:
                price_check_flag = "Normal"
            elif margin_pct <= PRICE_CHECK_MEDIUM_MAX_PCT:
                price_check_flag = "Medium"
            else:
                price_check_flag = "High"

        additional_images_str = ",".join(additional_images)
        now_str = datetime.now(PK_TZ).strftime("%Y-%m-%d %H:%M:%S")
        is_active = stock > 0

        # ================= CHANGE ALERTS (YRA manual-update workflow) =====
        # OnBuy is updated by hand on this store, so every change an employee
        # must act on is written to the Change Alert column, the row turns
        # amber (red if out of stock), and one summary email goes out after
        # the run. The alert stays until the employee ticks "Applied on
        # OnBuy", which clears it on the next run. Only actionable events
        # alert: going out of stock, coming back in stock, and the selling
        # price changing. Routine stock-count wobble (e.g. 47 -> 45) is
        # deliberately ignored - alerting on every fluctuation would bury
        # the changes that matter.
        prev_checked = str(row.get("Last Checked Time") or "").strip()
        try:
            old_stock = int(float(str(row.get("Stock") or "").strip() or 0))
        except (TypeError, ValueError):
            old_stock = 0
        try:
            old_selling = float(str(row.get("Selling Price (£)") or "").strip() or 0)
        except (TypeError, ValueError):
            old_selling = 0.0

        change_notes = []
        if prev_checked:  # never alert on a row's very first fill-in
            if old_stock > 0 and stock == 0:
                change_notes.append("OUT OF STOCK - deactivate on OnBuy")
            elif old_stock == 0 and stock > 0 and old_selling > 0:
                change_notes.append(f"BACK IN STOCK ({stock}) - reactivate on OnBuy")
            if stock > 0 and old_selling > 0 and selling_price > 0 and abs(selling_price - old_selling) >= 0.01:
                change_notes.append(f"PRICE £{old_selling:.2f} -> £{selling_price:.2f} - update on OnBuy")

        applied_ticked = str(row.get("Applied on OnBuy") or "").strip().upper() in ("TRUE", "YES", "1", "DONE")
        existing_alert = str(row.get("Change Alert") or "").strip()
        # Fix-me FLAG alerts (bad link, bad SKU, duplicate, variant...) are
        # cleared by FIXING THE ROW, not by ticking "Applied on OnBuy" - if
        # this code is running, the row fetched successfully, so whatever the
        # flag complained about is resolved: alert clears, colour returns to
        # white on this same run. Real change alerts (OUT OF STOCK / BACK IN
        # STOCK / PRICE) still wait for the tick, as designed - they need a
        # human to act on OnBuy, not on the row.
        _FLAG_PREFIXES = ("variant", "choose variant", "unreadable link", "add sku",
                          "invalid sku", "duplicate", "aliexpress")
        is_flag_alert = existing_alert.lower().startswith(_FLAG_PREFIXES)
        if change_notes:
            alert_for_row = "; ".join(change_notes)
            alert_time_for_row = now_str
            change_log.append((sku, alert_for_row))
        elif existing_alert and (applied_ticked or is_flag_alert):
            # Cleared: either the employee confirmed the change was applied
            # on OnBuy, or a fix-me flag just proved itself fixed.
            alert_for_row = ""
            alert_time_for_row = ""
        else:
            # No new change: keep whatever alert state is already there
            # (an unacknowledged alert stays visible run after run).
            alert_for_row = existing_alert
            alert_time_for_row = str(row.get("Change Time") or "").strip()
        pending_change = bool(alert_for_row)

        # ================= ONBUY API PUSH (gated, see ONBUY_API_PUSH_ENABLED) =================
        # Runs before the sheet write below so the outcome (Sync Status, OPC
        # placeholder, etc.) can go into the SAME batch_update call instead of
        # a second Sheets API round-trip per row.
        # EAN column (Sheet/Supabase): the SKU's numeric part IS the EAN
        # (user policy 2026-07-13 - every product is a new listing under the
        # seller's own barcode; matches what the CSV export sends). The
        # supplier's own barcode is only a fallback for rows whose SKU
        # somehow has no digits.
        ean = sku_numeric_part(sku) or ebay_data.get("product_code") or ""
        sync_status = None
        onbuy_product_created = None
        onbuy_listing_active = None
        onbuy_product_id = None
        last_onbuy_sync = None

        if (sku and onbuy_ready and onbuy_halt_reason is None
                and should_push_to_onbuy(sku) and onbuy_pushes_this_run < ONBUY_MAX_PUSHES_PER_RUN):
            existing = existing_fields.get(sku, {})
            # Supabase first, Sheet as fallback - the Sheet carries the same
            # tracking columns (backfill writes both), so the guard below
            # still works on a run where the Supabase pre-fetch failed.
            last_sync_status = str(existing.get("Sync Status") or row.get("Sync Status") or "")

            # A brand that's a registered trademark another seller already
            # owns on OnBuy isn't a bug to route around - it's a real product
            # this business isn't allowed to list under that brand at all.
            # User's explicit policy (2026-07-06, superseding the earlier
            # "mark it Unbranded and relist" policy): remove the row entirely
            # instead of relisting it as Unbranded.
            if "supplied brand is owned by another seller" in last_sync_status:
                rows_to_delete.append(i)
                removed_skus.append(sku)
                onbuy_removed += 1
                logger.info(
                    "Row %d (SKU %s): removing - OnBuy rejected the brand as owned "
                    "by another seller; not relisting as Unbranded", i, sku,
                )
                continue

            onbuy_pushes_this_run += 1
            # OnBuy's product code = the seller's own SKU, not the eBay-sourced
            # EAN above - SKUs here are the seller's pre-validated UPCs. Being
            # numeric and the right length isn't enough on its own - confirmed
            # two real SKUs got rejected ("not a valid product code") despite
            # both being 12-digit numbers, because their check digit isn't a
            # real GS1/UPC checksum. Only forward it if it actually passes
            # that check; otherwise send blank rather than repeat the rejection.
            sku_digits = sku_numeric_part(sku)
            upc_for_onbuy = sku_digits if is_valid_gtin(sku_digits) else ""

            # OnBuy's own brand-matching backend can also crash outright on a
            # brand it doesn't recognize ("MatchedBrandData...Argument #1
            # ($id) must be of type int, null given") - that's a bug on their
            # end, unrelated to trademark ownership, so it still gets retried
            # as Unbranded rather than repeating the same crash.
            brand_for_onbuy = brand
            if "MatchedBrandData::__construct" in last_sync_status:
                brand_for_onbuy = "Unbranded"

            # "SKU does not exist" from update_listing does NOT always mean
            # the product was never created. OnBuy's queue confirms a creation
            # as success (OPC issued, findable in the Add Listing search) days
            # before the listing becomes addressable via PUT /listings/by-sku
            # - and falling back to create_product in that window re-submits
            # the same product, which OnBuy answers with a NEW OPC instead of
            # matching the existing record (confirmed 2026-07-06: most of the
            # 07-04 rollout's products got duplicated this way on the next
            # full run). So the create fallback is only allowed when our own
            # records say this SKU was never successfully submitted: no real
            # OPC on record and no submitted/synced status. Rows whose last
            # submission outright Failed keep the fallback - re-creating is
            # exactly how those recover.
            opc_on_record = str(existing.get("OPC") or row.get("OPC") or "").strip()
            already_created = (
                opc_on_record.upper() not in ("", "PENDING")
                or last_sync_status.startswith(("Synced", "Pending Approval", "Awaiting OnBuy go-live"))
            )

            try:
                if already_created:
                    result = onbuy.update_listing(sku=sku, price=selling_price, stock=stock)
                    action = "updated"
                else:
                    action, result = onbuy.sync_product(
                        sku=sku,
                        ean=upc_for_onbuy,
                        title=title or str(row.get("Title") or ""),
                        description=description,
                        brand=brand_for_onbuy,
                        category_id=category_id,
                        price=selling_price,
                        stock=stock,
                        main_image=main_image,
                        additional_images=additional_images,
                    )
                logger.info("OnBuy %s: %s", action, sku)
                last_onbuy_sync = now_str
                if action == "created":
                    onbuy_created += 1
                    # Accepted into OnBuy's async approval queue - not confirmed live yet.
                    # The real OPC/approval status only appears later via
                    # OnBuyClient.check_queue(); this pipeline doesn't poll for it, so
                    # these reflect "submitted", not "confirmed active".
                    sync_status = "Pending Approval"
                    onbuy_product_created = "TRUE"
                    onbuy_listing_active = "FALSE"
                    onbuy_product_id = str(result.get("queue_id", "")) if isinstance(result, dict) else ""
                else:
                    onbuy_updated += 1
                    sync_status = "Synced"
                    onbuy_product_created = "TRUE"
                    onbuy_listing_active = "TRUE"
            except (TransientError, AuthError, requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
                # OnBuy-side or transport trouble (rate limit, 5xx after
                # retries, expired token even after the client's one re-auth,
                # network blip) - the product itself was NOT rejected, so
                # leave Sync Status exactly as it was: writing "Failed" here
                # would reopen the create fallback for a recently created row
                # whose real OPC hasn't been backfilled yet, and re-creating
                # is exactly how the 07-06 duplicates were minted. No Last
                # OnBuy Sync stamp either, so these rows keep their place at
                # the front of the push order next run.
                # (AuthError subclasses PermanentError, so it must be listed
                # here, before the PermanentError handler below.)
                onbuy_postponed += 1
                run_had_errors = True
                logger.warning("OnBuy push postponed for SKU %s: %s", sku, exc)
                if isinstance(exc, (RateLimitError, AuthError)):
                    # The hourly quota won't come back mid-run, and a token
                    # that couldn't be refreshed won't start working again -
                    # pushing on would just burn time failing row after row.
                    onbuy_halt_reason = str(exc)[:200]
                    logger.warning(
                        "Halting OnBuy pushes for the rest of this run (%s) - remaining rows "
                        "still get their Sheet/Supabase refresh and will push next run",
                        onbuy_halt_reason,
                    )
            except PermanentError as exc:
                if already_created and "SKU does not exist" in str(exc):
                    # Created earlier, OnBuy just hasn't made the listing
                    # addressable yet - not a failure, and NOT a reason to
                    # re-create. Stamp Last OnBuy Sync so this row rotates to
                    # the back of the push-priority order (batch sort above)
                    # instead of holding a front slot every run; the update
                    # will simply succeed on a later attempt once OnBuy makes
                    # the listing live.
                    onbuy_deferred += 1
                    sync_status = "Awaiting OnBuy go-live (created earlier - listing not yet updatable)"
                    last_onbuy_sync = now_str
                    logger.info(
                        "Row %d (SKU %s): created earlier (OPC %s) but OnBuy's listing isn't "
                        "updatable yet - deferring, not re-creating",
                        i, sku, opc_on_record or "pending",
                    )
                else:
                    onbuy_failed += 1
                    run_had_errors = True
                    sync_status = f"Failed: {str(exc)[:300]}"
                    logger.error("OnBuy push failed for SKU %s: %s", sku, exc)
            except Exception as exc:
                onbuy_failed += 1
                run_had_errors = True
                # Previously just "Failed" with no reason - the actual cause
                # only ever reached the run's log, not anywhere the user could
                # see it without downloading that specific Actions run's log.
                sync_status = f"Failed: {str(exc)[:300]}"
                logger.error("OnBuy push failed for SKU %s: %s", sku, exc)
            # Confirmed from the account's own API usage page: 240 PUT/POST per
            # hour. Paired with ONBUY_MAX_PUSHES_PER_RUN above, this keeps a
            # single large run from bursting through the hourly limit on its own.
            time.sleep(0.5)

        row_updates = [
            {"range": f"{col_letter(col_map['Cost Price (£)'])}{i}", "values": [[cost_price]]},
            {"range": f"{col_letter(col_map['Stock'])}{i}", "values": [[stock]]},
            {"range": f"{col_letter(col_map['Selling Price (£)'])}{i}", "values": [[selling_price]]},
            {"range": f"{col_letter(col_map['Status'])}{i}", "values": [["ACTIVE" if is_active else "INACTIVE"]]},
            {"range": f"{col_letter(col_map['Description'])}{i}", "values": [[description]]},
            {"range": f"{col_letter(col_map['Image URL'])}{i}", "values": [[main_image]]},
            {"range": f"{col_letter(col_map['Additional Images'])}{i}", "values": [[additional_images_str]]},
            {"range": f"{col_letter(col_map['Brand'])}{i}", "values": [[brand]]},
            {"range": f"{col_letter(col_map['Title'])}{i}", "values": [[title]]},
            {"range": f"{col_letter(col_map['Last Updated'])}{i}", "values": [[now_str]]},
            {"range": f"{col_letter(col_map['Last Checked Time'])}{i}", "values": [[now_str]]},
        ]
        if category_needs_write:
            row_updates.append({"range": f"{col_letter(col_map['Category'])}{i}", "values": [[category]]})
        if "Price Check Flag" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Price Check Flag'])}{i}", "values": [[price_check_flag]]})
        if "EAN" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['EAN'])}{i}", "values": [[ean]]})
        if "Supplier" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Supplier'])}{i}", "values": [[supplier]]})
        if VARIANTS_ENABLED and "Variant Group" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Variant Group'])}{i}", "values": [[variant_group]]})
        if VARIANTS_ENABLED and "Variant" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Variant'])}{i}", "values": [[variant_detail]]})
        # Change-alert columns (YRA manual-update workflow). "Applied on
        # OnBuy" is only ever written when its state must change (a new
        # change resets it, an acknowledged one clears it) so a tick an
        # employee makes mid-run is never accidentally overwritten.
        if "Change Alert" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Change Alert'])}{i}", "values": [[alert_for_row]]})
        if "Change Time" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Change Time'])}{i}", "values": [[alert_time_for_row]]})
        if "Applied on OnBuy" in col_map and (change_notes or (applied_ticked and existing_alert)):
            row_updates.append({"range": f"{col_letter(col_map['Applied on OnBuy'])}{i}", "values": [[""]]})
        # OnBuy-provided tracking fields, written to the Sheet only if those
        # columns exist there and only when a push actually happened this run
        # - otherwise leaving them out preserves whatever was already there.
        if sync_status and "Sync Status" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Sync Status'])}{i}", "values": [[sync_status]]})
        if onbuy_product_created and "OnBuy Product Created" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['OnBuy Product Created'])}{i}", "values": [[onbuy_product_created]]})
        if onbuy_listing_active and "OnBuy Listing Active" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['OnBuy Listing Active'])}{i}", "values": [[onbuy_listing_active]]})
        if onbuy_product_id and "OnBuy Product ID" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['OnBuy Product ID'])}{i}", "values": [[onbuy_product_id]]})
        if last_onbuy_sync and "Last OnBuy Sync" in col_map:
            row_updates.append({"range": f"{col_letter(col_map['Last OnBuy Sync'])}{i}", "values": [[last_onbuy_sync]]})

        all_sheet_updates.extend(row_updates)
        updated_count += 1
        logger.info("Processed row %d", i)
        highlight_requests.append(row_highlight_request(sheet.id, i, num_cols, is_active, pending_change))
        time.sleep(0.2)  # light pacing on eBay fetches; OnBuy pushes are paced separately below

        # ================= SUPABASE EXPORT ROW (upserted once after the loop) =================
        # Every row - including OnBuy-tracking fields - goes in this one list,
        # and every row must have identical keys AND real values for every NOT
        # NULL column (see fetch_existing_fields() for why a separate
        # partial-column upsert doesn't work here).
        existing = existing_fields.get(sku, {})
        supabase_row = {
            "SKU": sku,
            "Title": title or str(row.get("Title") or ""),
            "Description": description,
            "Brand": brand,
            "Category": category,
            "Category ID": str(category_id) if category_id is not None else None,
            "Supplier URL": url,
            "Supplier": supplier,
            "Cost Price (£)": cost_price,
            "Shipping Cost (£)": str(shipping_cost) if shipping_cost else None,
            "Profit %": str(pricing.MIN_PROFIT_PERCENT),
            "Fee %": str(pricing.PLATFORM_FEE_PERCENT),
            "Stock": stock,
            "Selling Price (£)": selling_price,
            "Status": "ACTIVE" if stock > 0 else "INACTIVE",
            "Last Updated": datetime.now(PK_TZ).isoformat(),
            "Image URL": main_image,
            "Additional Images": additional_images_str,
            "Condition": ebay_data.get("condition") or "New",
            "Last Checked Time": datetime.now(PK_TZ).isoformat(),
            "EAN": ean,
            "Listing ID": str(row.get("Listing ID") or "").strip() or None,
            # OPC (OnBuy's permanent product code) is only known once the async
            # queue clears - see OnBuyClient.check_queue(). This column is NOT
            # NULL, so a genuinely new row needs a placeholder - but reuse the
            # real value from Supabase if backfill_onbuy_status.py already
            # found one, instead of stomping it back to "PENDING" every run.
            "OPC": existing.get("OPC") or "PENDING",
            # OnBuy-tracking fields: use this run's fresh value if a push was
            # attempted, otherwise carry forward whatever was already there
            # (never blank it out) - see fetch_existing_fields() for why
            # these have to live on the same row as the fields above rather
            # than a separate partial-column upsert.
            "Sync Status": sync_status or existing.get("Sync Status") or "",
            "OnBuy Product Created": onbuy_product_created or existing.get("OnBuy Product Created") or "",
            "OnBuy Listing Active": onbuy_listing_active or existing.get("OnBuy Listing Active") or "",
            "OnBuy Product ID": onbuy_product_id or existing.get("OnBuy Product ID") or "",
            "Last OnBuy Sync": last_onbuy_sync or existing.get("Last OnBuy Sync") or "",
            # YRA change-alert state, mirrored so Supabase holds the same
            # picture employees see in the Sheet.
            "Change Alert": alert_for_row,
            "Change Time": alert_time_for_row,
        }
        # Variant columns exist only once both the Sheet headers and the
        # Supabase ALTER TABLE have been applied - keyed off the Sheet so
        # every row in one upsert call has identical keys (PGRST102).
        if VARIANTS_ENABLED and "Variant Group" in col_map:
            supabase_row["Variant Group"] = variant_group
            supabase_row["Variant"] = variant_detail
            supabase_row["Variant Choice"] = variant_choice
        supabase_rows.append(supabase_row)

    # ================= APPLY ALL SHEET VALUE UPDATES (one call for the whole run) =================
    if all_sheet_updates:
        # gspread's batch_update() mutates each dict's "range" in place
        # (unconditionally re-qualifying it with the sheet name, even if
        # already qualified - confirmed from its source). Passing the same
        # list to a retried call would double-qualify the range on the 2nd
        # attempt ('Sheet1'!'Sheet1'!I35), which is invalid and fails outright.
        # Keep the original (range, values) pairs immutable and rebuild fresh
        # dicts on every attempt so a retry never sees an already-mutated one.
        original_pairs = [(u["range"], u["values"]) for u in all_sheet_updates]

        def _do_sheet_update():
            fresh_updates = [{"range": r, "values": v} for r, v in original_pairs]
            return sheet.batch_update(fresh_updates)

        try:
            with_retry(_do_sheet_update, what="sheet batch update", max_attempts=3)
        except Exception as exc:
            run_had_errors = True
            # This is an all-or-nothing commit for the whole run's Sheet writes -
            # a real trade-off against doing one API call per row (which risked
            # Google's own rate limits once batch sizes grew past a hardcoded
            # 12/run). OnBuy/Supabase may already reflect this run's changes
            # even if this call fails - retried 3x before giving up, so a
            # transient blip is unlikely to lose everything.
            logger.error("Sheet batch update failed after retries - this run's Sheet changes may not be saved: %s", exc)

    supabase_rows = dedupe_rows_by_sku(supabase_rows, "Supabase export")
    supabase_ok = supabase_db.upsert_products(supabase_rows)

    if highlight_requests:
        try:
            with_retry(
                sheet.spreadsheet.batch_update,
                {"requests": highlight_requests},
                what="row highlight formatting",
                max_attempts=3,
            )
        except Exception as exc:
            logger.error("Row highlighting failed (values were still updated correctly): %s", exc)

    # ================= REMOVE BRAND-REJECTED ROWS ENTIRELY =================
    # Runs after every other Sheet write/highlight above so deleting these
    # rows can't shift row numbers out from under one of those, which only
    # ever target rows that are staying (a row queued for deletion `continue`d
    # past building any Sheet update for itself). Supabase is deleted first,
    # not the Sheet row - if the Sheet delete then fails, the row survives to
    # be retried (and re-rejected, re-detected, re-deleted) next run; the
    # reverse order would risk a permanently orphaned Supabase row with no
    # Sheet row left to ever trigger cleaning it up.
    rows_to_delete.extend(removal_rows)
    removed_skus.extend(removal_skus)
    if rows_to_delete:
        supabase_db.delete_products(removed_skus)
        delete_requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet.id,
                        "dimension": "ROWS",
                        "startIndex": row_num - 1,
                        "endIndex": row_num,
                    }
                }
            }
            for row_num in sorted(set(rows_to_delete), reverse=True)
        ]
        try:
            with_retry(
                sheet.spreadsheet.batch_update,
                {"requests": delete_requests},
                what="delete brand-rejected rows",
                max_attempts=3,
            )
            logger.info(
                "Removed %d row(s) entirely - Remove ticked or brand-rejected (SKUs: %s)",
                len(set(rows_to_delete)), ", ".join(removed_skus),
            )
        except Exception as exc:
            run_had_errors = True
            logger.error(
                "Failed to delete brand-rejected row(s) from the Sheet (Supabase row(s) "
                "already removed) - SKUs %s: %s", ", ".join(removed_skus), exc,
            )

    # ================= GENERATE XML (kept as fallback) =================
    root = ET.Element("products")
    feed_count = 0
    skipped_feed = 0

    for row in sheet.get_all_records():
        try:
            sku = str(row.get("SKU") or "").strip()
            title = str(row.get("Title") or "").strip()
            desc = str(row.get("Description") or "").strip()
            brand = str(row.get("Brand") or "").strip()
            category = clean_category(row.get("Category"))
            image = to_jpg(row.get("Image URL"))
            additional_images = [img.strip() for img in str(row.get("Additional Images") or "").split(",") if img.strip()][:10]
            price = float(row.get("Selling Price (£)") or 0)
            stock = int(row.get("Stock") or 0)

            if not all([sku, title, category]):
                skipped_feed += 1
                continue

            product = ET.SubElement(root, "product")
            ET.SubElement(product, "sku").text = sku
            ET.SubElement(product, "product_name").text = title[:150]
            ET.SubElement(product, "description").text = desc
            ET.SubElement(product, "image_url").text = image

            for img_idx, img in enumerate(additional_images):
                ET.SubElement(product, f"additional_image_url_{img_idx + 1}").text = img

            ET.SubElement(product, "brand").text = brand
            ET.SubElement(product, "category").text = category
            ET.SubElement(product, "condition").text = "New"
            ET.SubElement(product, "ean").text = sku
            ET.SubElement(product, "price").text = str(price)
            ET.SubElement(product, "quantity").text = str(stock)

            feed_count += 1
        except Exception:
            skipped_feed += 1

    ET.ElementTree(root).write("feed.xml", encoding="utf-8", xml_declaration=True)
    feed_url = storage.upload_feed()

    # ================= CHANGE-ALERT EMAIL (YRA manual-update workflow) =================
    # One summary email per run listing every change an employee must apply
    # on OnBuy by hand. Not an error condition - just the to-do list.
    if change_log:
        notify.send_alert_email(
            f"YRA: {len(change_log)} product change(s) need action on OnBuy",
            "These products changed at the supplier and need a manual update on OnBuy:\n\n"
            + "\n".join(f"- SKU {sku}: {note}" for sku, note in change_log)
            + "\n\nThe same rows are highlighted in the YRA sheet (amber = change pending, "
            "red = out of stock). Tick 'Applied on OnBuy' on each row once done - "
            "the highlight clears on the next run.",
        )

    # ================= FINAL LOGS + ALERTS =================
    logger.info("DONE")
    logger.info("Updated rows: %d", updated_count)
    logger.info("Change alerts raised: %d, variants needing a choice: %d, unreadable links: %d, "
                "missing SKUs: %d, invalid SKUs: %d, duplicate SKUs: %d, duplicate links: %d, "
                "rows removed by request: %d, AliExpress rows awaiting keys: %d",
                len(change_log), variant_rows, unreadable_rows, missing_sku_rows, invalid_sku_rows,
                duplicate_sku_rows_flagged, duplicate_link_rows_flagged, len(removal_rows), ali_unconfigured_rows)
    logger.info("OnBuy: %d created, %d updated, %d deferred (awaiting go-live), %d postponed (transient), "
                 "%d failed, %d removed (brand rejected)",
                 onbuy_created, onbuy_updated, onbuy_deferred, onbuy_postponed, onbuy_failed, onbuy_removed)
    if onbuy_halt_reason:
        logger.warning("OnBuy pushes were halted early this run: %s", onbuy_halt_reason)
    logger.info("Feed products: %d, skipped: %d", feed_count, skipped_feed)
    logger.info("Feed URL: %s", feed_url or "(not uploaded - see SUPABASE_URL/SUPABASE_SERVICE_KEY)")
    logger.info("Supabase database export: %s (%d rows)", "OK" if supabase_ok else "skipped/failed", len(supabase_rows))

    if fetch_failures >= FETCH_FAILURE_ALERT_THRESHOLD or onbuy_failed > 0 or onbuy_removed > 0 or onbuy_postponed > 0 or removal_rows:
        notify.send_alert_email(
            "Sync run finished with errors" if (fetch_failures or onbuy_failed or onbuy_postponed) else "Sync run removed product row(s)",
            f"eBay fetch failures: {fetch_failures}\n"
            f"OnBuy push failures: {onbuy_failed} (created {onbuy_created}, updated {onbuy_updated}, "
            f"deferred awaiting go-live {onbuy_deferred})\n"
            f"OnBuy pushes postponed (rate limit/token/network - auto-retried next run): {onbuy_postponed}"
            + (f" - pushing halted early: {onbuy_halt_reason}" if onbuy_halt_reason else "") + "\n"
            f"Rows removed (Remove column or brand-rejected): {len(set(rows_to_delete))}"
            + (f" - SKUs: {', '.join(removed_skus)}" if removed_skus else "") + "\n"
            f"Updated rows: {updated_count}\n"
            f"Feed products: {feed_count}, skipped: {skipped_feed}\n"
            "Check the GitHub Actions run log for details.",
        )

    if run_had_errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Run crashed")
        notify.send_alert_email("Run crashed", "generate_xml.py raised an unhandled exception - see the GitHub Actions log.")
        raise
