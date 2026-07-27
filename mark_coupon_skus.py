"""One-off: paint the SKU cell RED on every row whose SKU digits form a
coupon-range code (12 digits starting 5, or its 13-digit 05-padded form) so
they are easy to find and change by hand. Formatting only - no cell values,
no colours besides the SKU cell text, nothing else touched. Re-run safe.
User request 2026-07-28 after OnBuy rejected 23 YRA + 74 Arden feed rows.
"""
import json
import logging
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

try:
    from generate_xml import SHEET_NAME
except ImportError:
    SHEET_NAME = "OnBuy_Feed_Master"
from generate_xml import sku_numeric_part

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")


def main():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    book = gspread.authorize(creds).open(SHEET_NAME)
    sheet = book.sheet1
    data = sheet.get_all_records()
    # Headers may carry stray whitespace - the pipeline strips them the
    # same way (generate_xml col_map does str(h).strip()).
    headers = [str(h).strip() for h in sheet.row_values(1)]
    sku_col = headers.index("SKU")

    requests, matched = [], []
    for idx, row in enumerate(data, start=2):
        digits = sku_numeric_part(row.get("SKU"))
        if ((len(digits) == 12 and digits.startswith("5"))
                or (len(digits) == 13 and digits.startswith("05"))):
            matched.append((idx, str(row.get("SKU")).strip()))
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet.id, "startRowIndex": idx - 1, "endRowIndex": idx,
                          "startColumnIndex": sku_col, "endColumnIndex": sku_col + 1},
                "cell": {"userEnteredFormat": {"textFormat": {
                    "foregroundColor": {"red": 1.0, "green": 0.0, "blue": 0.0}}}},
                "fields": "userEnteredFormat.textFormat.foregroundColor",
            }})

    logger.info("Coupon-range SKUs found: %d (of %d rows)", len(matched), len(data))
    for idx, sku in matched:
        logger.info("  row %d: %s", idx, sku)
    if requests:
        book.batch_update({"requests": requests})
        logger.info("SKU cells painted red: %d", len(requests))
    else:
        logger.info("Nothing to mark - sheet is clean")


if __name__ == "__main__":
    main()
