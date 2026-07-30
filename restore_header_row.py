"""One-off: restore the Sheet's header row. An employee deleted row 1
(2026-07-29), leaving a product where the column names belong; the header
hygiene guard has been refusing every run since (by design - positional
writes against a headerless sheet corrupt every column). This inserts a
fresh row 1 from sheet_headers.csv. Aborts if row 1 already looks like
headers, so it cannot double-insert.
"""
import csv
import json
import logging
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

from generate_xml import SHEET_NAME

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("onbuy_sync")


def main():
    with open("sheet_headers.csv", newline="", encoding="utf-8-sig") as f:
        headers = [h.strip() for h in next(csv.reader(f)) if h.strip()]
    logger.info("Canonical headers (%d): %s", len(headers), ", ".join(headers))

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    sheet = gspread.authorize(creds).open(SHEET_NAME).sheet1

    row1 = [str(c).strip() for c in sheet.row_values(1)]
    if "SKU" in row1 and "Supplier URL" in row1:
        logger.info("Row 1 already contains headers - nothing to do. Row 1: %s", row1[:6])
        return
    logger.info("Row 1 is NOT a header row (starts: %s...) - inserting headers above it", row1[:3])
    sheet.insert_row(headers, 1)
    check = [str(c).strip() for c in sheet.row_values(1)]
    assert "SKU" in check and "Supplier URL" in check, "insert failed: " + str(check[:6])
    logger.info("Header row restored - the next sync will run normally")


if __name__ == "__main__":
    main()
