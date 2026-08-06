"""One-off Sheet hardening (2026-08-06). Three protections against the
employee-edit accidents that caused this week's incidents (a blanked A1
header no-op'd a store; a deleted header row froze another in July; and
uncategorized entries feed the permanent category worklist):

1. A hidden 'OnBuy Categories' tab holding every valid category leaf path
   (from onbuy_categories_only.csv - identical across all stores).
2. A strict dropdown on the Category column fed from that tab: employees
   pick a valid category at entry time; typed garbage is rejected. The
   pipeline and autofill only ever write paths from the same file, so
   automation is unaffected. Existing invalid/blank cells just show a
   warning triangle.
3. Warn-on-edit protection on the header row and on the hidden tab -
   an "are you sure?" popup on edit, deterring accidents without ever
   locking anyone out.

Idempotent: existing tab/dropdown/protections are detected and skipped.
DRY_RUN honoured (default on).
"""
import csv
import json
import os

import gspread
from oauth2client.service_account import ServiceAccountCredentials

SHEET_NAME = "YRA_Feed_Master"
CATEGORY_TAB = "OnBuy Categories"
HEADER_GUARD_DESC = "Header row - managed by the sync automation (warn-only guard)"
TAB_GUARD_DESC = "Category dropdown list - managed by automation (warn-only guard)"
DRY_RUN = (os.getenv("DRY_RUN") or "1").strip().lower() not in ("0", "no", "false", "")


def col_letter(n):
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def main():
    with open("onbuy_categories_only.csv", newline="", encoding="utf-8") as f:
        paths = [row["OnBuy Category Path"] for row in csv.DictReader(f)
                 if row.get("OnBuy Category Path")]
    print(f"{len(paths)} category paths loaded from onbuy_categories_only.csv")

    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        json.loads(os.environ["GOOGLE_CREDENTIALS"]), scope)
    ss = gspread.authorize(creds).open(SHEET_NAME)
    main_ws = ss.sheet1

    headers = [str(h).strip() for h in main_ws.row_values(1)]
    cat_col = headers.index("Category") + 1 if "Category" in headers else None

    meta = ss.fetch_sheet_metadata(
        {"fields": "sheets(properties(sheetId,title,hidden),protectedRanges)"})
    sheets_meta = {s["properties"]["title"]: s for s in meta["sheets"]}
    main_id = sheets_meta[main_ws.title]["properties"]["sheetId"]
    have_tab = CATEGORY_TAB in sheets_meta
    prot_descs = {p.get("description") for s in meta["sheets"]
                  for p in s.get("protectedRanges", [])}

    plan = []
    if have_tab:
        plan.append(f"tab '{CATEGORY_TAB}' exists - will refresh its {len(paths)} paths")
    else:
        plan.append(f"create hidden tab '{CATEGORY_TAB}' with {len(paths)} paths")
    if cat_col:
        plan.append(f"strict dropdown on Category (column {col_letter(cat_col)}, rows 2 to end)")
    else:
        plan.append("NO 'Category' header found - dropdown SKIPPED")
    plan.append("protect header row (warn-only)"
                if HEADER_GUARD_DESC not in prot_descs else "header row already protected - skip")
    plan.append("protect category tab (warn-only)"
                if TAB_GUARD_DESC not in prot_descs else "category tab already protected - skip")
    for p in plan:
        print("PLAN:", p)
    if DRY_RUN:
        print("DRY RUN - nothing changed. Re-run with dry_run=no to apply.")
        return

    if have_tab:
        cat_ws = ss.worksheet(CATEGORY_TAB)
        if cat_ws.row_count < len(paths):
            cat_ws.resize(rows=len(paths) + 10)
    else:
        cat_ws = ss.add_worksheet(CATEGORY_TAB, rows=len(paths) + 10, cols=1)
    cat_ws.update(values=[[p] for p in paths], range_name=f"A1:A{len(paths)}")
    tab_id = cat_ws.id

    requests = [{
        "updateSheetProperties": {
            "properties": {"sheetId": tab_id, "hidden": True},
            "fields": "hidden",
        }
    }]
    if cat_col:
        requests.append({
            "setDataValidation": {
                "range": {"sheetId": main_id, "startRowIndex": 1,
                          "startColumnIndex": cat_col - 1, "endColumnIndex": cat_col},
                "rule": {
                    "condition": {
                        "type": "ONE_OF_RANGE",
                        "values": [{"userEnteredValue": f"='{CATEGORY_TAB}'!$A$1:$A${len(paths)}"}],
                    },
                    "showCustomUi": True,
                    "strict": True,
                },
            }
        })
    if HEADER_GUARD_DESC not in prot_descs:
        requests.append({
            "addProtectedRange": {
                "protectedRange": {
                    "range": {"sheetId": main_id, "startRowIndex": 0, "endRowIndex": 1},
                    "description": HEADER_GUARD_DESC,
                    "warningOnly": True,
                }
            }
        })
    if TAB_GUARD_DESC not in prot_descs:
        requests.append({
            "addProtectedRange": {
                "protectedRange": {
                    "range": {"sheetId": tab_id},
                    "description": TAB_GUARD_DESC,
                    "warningOnly": True,
                }
            }
        })

    ss.batch_update({"requests": requests})
    print(f"DONE - applied {len(requests)} change(s). Verify in the Sheet: header edit "
          "shows a warning popup; Category cells show a dropdown; typed garbage is rejected.")


if __name__ == "__main__":
    main()
