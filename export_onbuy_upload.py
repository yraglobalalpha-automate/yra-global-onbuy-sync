"""Builds the OnBuy product-upload CSV for the YRA store.

OnBuy's bulk upload for this account takes a flat, single-sheet CSV. The
base columns are confirmed against the account's upload format (2026-07-09);
the five variant columns follow the field names in OnBuy's own Product
Create Template (Parent_Group / Variant_One_Name / ... - see its
Explanation tab), written in the same snake_case style as the rest:

  sku, product_name, description, price, quantity, brand, image_url,
  additional_images, category, condition, mpn, ean, handling_time,
  dispatch_time, parent_group, variant_1_name, variant_1_value,
  variant_2_name, variant_2_value

Variant products (ON HOLD - see VARIANTS_ENABLED below; while False the
CSV has only the 14 base columns and this section does not apply.
OnBuy's model, from the template's Explanation tab):
- Each variant is its own full row - own SKU, own EAN, own price/stock -
  plus parent_group naming the group and variant name/value pairs (allowed
  names: Colour, Length, Material, Pack Quantity, Size, Style, Width;
  other names get mapped by OnBuy itself).
- The parent is ONE extra row per group holding only the shared
  descriptive fields (no price/stock/EAN). This script generates parent
  rows automatically from the group's first exported variant - employees
  never create them. Parent SKU = "P-" + the Variant Group id, so it can
  never collide with a real UPC SKU.
- Single products leave all five variant columns empty - uploads behave
  exactly as before.

What gets exported:
- Only rows with Status ACTIVE (in stock) and a Title (fully fetched).
- Rows already marked "Exported to OnBuy" = TRUE are skipped, so each export
  contains only new products. Mark rows TRUE after uploading - this script
  never writes to the Sheet. (Parent rows are re-emitted whenever any of
  their group exports - re-uploading a parent is a harmless update.)
- Rows failing the GS1/UPC check-digit test on their SKU, or missing a
  Category, are SKIPPED and listed at the end (OnBuy would reject them).
  Fix in the Sheet and re-export.

Column values:
  sku <- SKU exactly as typed        ean <- the SKU's digits only (the
  pre-validated UPC - non-digit decoration like "ARD-" is allowed in the
  SKU and stripped for the ean; any EXTRA digits corrupt the barcode and
  fail the check)
  product_name <- Title (150-char cap)   description <- Description
  (falls back to Title if empty)          price <- Selling Price (£)
  quantity <- Stock                       brand <- Brand ("Unbranded" if blank)
  image_url <- Image URL                  additional_images <- Additional
  Images (comma-separated, quoted as one CSV field)
  category <- Category (full OnBuy path)  condition <- "New"
  mpn <- left blank (not tracked)
  handling_time / dispatch_time <- by supplier: eBay 2/2, AliExpress 5/5
  (HANDLING_DISPATCH_BY_SUPPLIER below; unknown supplier gets 5/5).
  parent_group / variant_* <- from the Sheet's "Variant Group" and
  "Variant" columns (filled automatically by the sync).

Usage: same GOOGLE_CREDENTIALS env as generate_xml.py.
    python export_onbuy_upload.py     -> writes YRA_OnBuy_Upload.csv
"""
import csv
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from generate_xml import SHEET_NAME, detect_supplier, is_valid_gtin, sku_numeric_part

OUTPUT_FILE = "YRA_OnBuy_Upload.csv"

# Variant products are ON HOLD by store decision (2026-07-13) - flip this
# AND generate_xml.py's VARIANTS_ENABLED to True together to re-enable.
# While False the CSV is exactly the proven 14-column single-product format.
VARIANTS_ENABLED = False

BASE_HEADERS = [
    "sku", "product_name", "description", "price", "quantity", "brand",
    "image_url", "additional_images", "category", "condition", "mpn",
    "ean", "handling_time", "dispatch_time",
]
VARIANT_HEADERS = [
    "parent_group", "variant_1_name", "variant_1_value",
    "variant_2_name", "variant_2_value",
]
HEADERS = BASE_HEADERS + (VARIANT_HEADERS if VARIANTS_ENABLED else [])

# Handling/dispatch days per supplier (user policy 2026-07-14): eBay stock
# moves fast, AliExpress needs the longer window. Unknown supplier gets the
# longer times - never promise faster than can be delivered.
HANDLING_DISPATCH_BY_SUPPLIER = {
    "eBay": ("2", "2"),
    "AliExpress": ("5", "5"),
}
FALLBACK_HANDLING_DISPATCH = ("5", "5")


def handling_dispatch(row):
    """(handling_time, dispatch_time) for a row, by its supplier. Uses the
    Supplier column the sync fills; falls back to reading the URL directly
    so a not-yet-synced row still gets sensible values."""
    supplier = str(row.get("Supplier") or "").strip() \
        or (detect_supplier(str(row.get("Supplier URL") or "")) or "")
    return HANDLING_DISPATCH_BY_SUPPLIER.get(supplier, FALLBACK_HANDLING_DISPATCH)

# OnBuy accepts exactly these variant names (template Explanation tab);
# anything else it maps itself, or creates the products as singles. Map the
# common supplier aspect names onto OnBuy's list; unknown names pass through
# for OnBuy to handle.
ONBUY_VARIANT_NAMES = {
    "colour": "Colour", "color": "Colour", "main colour": "Colour",
    "main color": "Colour", "shade": "Colour",
    "size": "Size", "uk size": "Size", "shoe size": "Size",
    "clothing size": "Size", "dress size": "Size",
    "length": "Length", "width": "Width",
    "material": "Material", "fabric": "Material",
    "style": "Style", "type": "Style", "model": "Style", "design": "Style",
    "pack quantity": "Pack Quantity", "pack size": "Pack Quantity",
    "number in pack": "Pack Quantity", "quantity per pack": "Pack Quantity",
}


def map_variant_name(name):
    return ONBUY_VARIANT_NAMES.get(str(name).strip().lower(), str(name).strip())


def parse_variant_detail(detail):
    """'Colour=Army Green; Size=XL' -> [('Colour', 'Army Green'), ('Size', 'XL')]
    (names already mapped onto OnBuy's allowed list)."""
    pairs = []
    for part in str(detail or "").split(";"):
        if "=" in part:
            name, value = part.split("=", 1)
            if name.strip() and value.strip():
                pairs.append((map_variant_name(name), value.strip()))
    return pairs


def parent_sku(group):
    return f"P-{str(group).strip()}"


def build_row(row):
    """Sheet row dict -> list of values in HEADERS order. Pure function so
    the mapping is testable without Google credentials."""
    sku = str(row.get("SKU") or "").strip()
    title = str(row.get("Title") or "").strip()
    description = str(row.get("Description") or "").strip() or title
    values = [
        sku,
        title[:150],
        description,
        row.get("Selling Price (£)") or "",
        row.get("Stock") or 0,
        str(row.get("Brand") or "").strip() or "Unbranded",
        str(row.get("Image URL") or "").strip(),
        str(row.get("Additional Images") or "").strip(),
        str(row.get("Category") or "").strip(),
        "New",
        "",  # mpn - not tracked
        sku_numeric_part(sku),  # ean = the SKU's digits (the validated UPC)
        *handling_dispatch(row),
    ]
    if VARIANTS_ENABLED:
        group = str(row.get("Variant Group") or "").strip()
        pairs = parse_variant_detail(row.get("Variant")) if group else []
        v1 = pairs[0] if len(pairs) > 0 else ("", "")
        v2 = pairs[1] if len(pairs) > 1 else ("", "")
        values += [parent_sku(group) if group else "", v1[0], v1[1], v2[0], v2[1]]
    return values


def build_parent_row(row):
    """The group's parent line, generated from its first exported variant:
    shared descriptive fields only - no price, stock, EAN or variant pair
    (mirrors the parent rows in OnBuy's own example template)."""
    title = str(row.get("Title") or "").strip()
    description = str(row.get("Description") or "").strip() or title
    return [
        parent_sku(row.get("Variant Group")),
        title[:150],
        description,
        "", "",  # price / quantity - parents are not sellable lines
        str(row.get("Brand") or "").strip() or "Unbranded",
        str(row.get("Image URL") or "").strip(),
        str(row.get("Additional Images") or "").strip(),
        str(row.get("Category") or "").strip(),
        "New",
        "", "",  # mpn / ean - a parent has no barcode
        "", "",  # handling / dispatch
        "", "", "", "", "",  # no parent_group or variant pair on the parent itself
    ]


def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    data = client.open(SHEET_NAME).sheet1.get_all_records()
    # Strip header whitespace - same hygiene as generate_xml.py, so a "SKU "
    # header cell can't silently blank out a column here either.
    data = [{str(k).strip(): v for k, v in row.items()} for row in data]

    exported = 0
    parent_rows = 0
    skipped_not_ready = 0
    skipped_already = 0
    bad_skus = []
    no_category = []
    dup_skus = []
    extra_axes = []
    emitted_parents = set()
    seen_digits = set()

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(HEADERS)
        for row in data:
            sku = str(row.get("SKU") or "").strip()
            title = str(row.get("Title") or "").strip()
            status = str(row.get("Status") or "").strip().upper()
            already = str(row.get("Exported to OnBuy") or "").strip().upper() in ("TRUE", "YES", "1", "DONE")
            if not sku or not title or status != "ACTIVE":
                skipped_not_ready += 1
                continue
            if already:
                skipped_already += 1
                continue
            if not is_valid_gtin(sku_numeric_part(sku)):
                bad_skus.append(sku)
                continue
            if sku_numeric_part(sku) in seen_digits:
                # Same barcode twice = the same product twice on OnBuy. The
                # first occurrence exported; later ones are skipped and named.
                dup_skus.append(sku)
                continue
            seen_digits.add(sku_numeric_part(sku))
            if not str(row.get("Category") or "").strip():
                no_category.append(sku)
                continue
            group = str(row.get("Variant Group") or "").strip() if VARIANTS_ENABLED else ""
            if group and group not in emitted_parents:
                # First exported variant of its group: emit the parent line
                # just above it (mirrors OnBuy's example template layout).
                writer.writerow(build_parent_row(row))
                emitted_parents.add(group)
                parent_rows += 1
            if group and len(parse_variant_detail(row.get("Variant"))) > 2:
                extra_axes.append(sku)
            writer.writerow(build_row(row))
            exported += 1

    print(f"Wrote {OUTPUT_FILE}: {exported} product(s)"
          + (f" + {parent_rows} variant parent row(s)" if parent_rows else ""))
    print(f"Skipped: {skipped_not_ready} not ready (no SKU/title or not ACTIVE), "
          f"{skipped_already} already exported")
    if extra_axes:
        print(f"NOTE: {len(extra_axes)} variant row(s) have more than two option types - OnBuy's "
              f"format carries two, the first two were used: {', '.join(extra_axes)}")
    if bad_skus:
        print(f"SKIPPED {len(bad_skus)} row(s) whose SKU is not a valid UPC (OnBuy would reject "
              f"them) - fix in the Sheet and re-export: {', '.join(bad_skus)}")
    if no_category:
        print(f"SKIPPED {len(no_category)} row(s) with no Category yet - wait for the next sync "
              f"run or set one manually: {', '.join(no_category)}")
    if dup_skus:
        print(f"SKIPPED {len(dup_skus)} row(s) whose SKU digits duplicate an earlier row's barcode "
              f"(same barcode = same product on OnBuy) - give each row its own: {', '.join(dup_skus)}")


if __name__ == "__main__":
    main()
