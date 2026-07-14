# YRA eBay → OnBuy Sync (manual-OnBuy variant)

Second-store pipeline, fully separate from the original store: its own
GitHub repo, Google Sheet, Google service account, Supabase project, and
eBay developer account.

**The key difference from the original store: this pipeline never touches
OnBuy's API.** It fetches from eBay, fills the Sheet, raises change alerts,
and mirrors to Supabase. OnBuy is updated by a human: products are created
by uploading a CSV (OnBuy's approved template), and later price/stock
changes are applied by hand, driven by the Sheet's highlights and alert
emails. The API push code is hard-disabled in `generate_xml.py` and cannot
be switched on by any secret or variable.

## Daily flow

1. An employee adds a row: **SKU** (its **digits must form a real,
   check-digit-valid UPC** — letters/dashes around the number are fine and
   are ignored, e.g. `ARD-5012345678900`; the digits become the product's
   EAN on OnBuy, so never add extra digits like a `-1` suffix) +
   **Supplier URL** (a single-product
   **eBay or AliExpress** product link — **no listings with options/
   variants** on either site; those get flagged `VARIANT - NOT SUPPORTED`
   for the link to be replaced. AliExpress links must be the full product
   page, `…/item/…`; shortened `a.aliexpress.com` share-links can't be
   read).
2. Every 3 hours the sync fills in title, description (sanitized), brand
   (normalized), category (auto-matched to OnBuy's category list), images,
   cost, stock, and selling price (40% margin floor — never lowers a
   manually raised price).
3. **Change alerts**: when a product goes out of stock, comes back in
   stock, or its selling price changes, the row gets a `Change Alert` note,
   turns **amber** (red if out of stock), and one summary email lists
   everything needing action. An employee applies the change on OnBuy, then
   ticks `Applied on OnBuy` — the alert clears on the next run. Brand-new
   rows and routine stock-count wobble (47 → 45) don't alert.
4. **Creating products on OnBuy**: run the "Export OnBuy Upload" workflow
   from the Actions tab and download the `yra-onbuy-upload` artifact — a
   flat CSV containing only product data, with exactly the columns OnBuy's
   upload accepts: `sku, product_name, description, price, quantity, brand,
   image_url, additional_images, category, condition, mpn, ean,
   handling_time, dispatch_time` (ean = the SKU's digits, i.e. the
   pre-validated UPC; handling/dispatch time by supplier — eBay 2/2,
   AliExpress 5/5, set in `HANDLING_DISPATCH_BY_SUPPLIER` in
   `export_onbuy_upload.py`). Rows already marked
   `Exported to OnBuy = TRUE` are excluded; rows with an invalid-UPC SKU or
   no Category yet are skipped and named in the run log (fix, re-export).
   After uploading in OnBuy's seller portal, mark the rows
   `Exported to OnBuy = TRUE`.

**Variant products are ON HOLD** (store decision 2026-07-13): the feature
is fully built and locally tested — Variant Choice matching, `?var=`/
`sku_id=` links, automatic parent rows and the five variant columns in the
export following OnBuy's Product Create Template model — but switched off.
To re-enable, flip `VARIANTS_ENABLED` to `True` in BOTH `generate_xml.py`
and `export_onbuy_upload.py`, and restore the variant instructions in the
employee guide. While off, multi-option links are flagged
`VARIANT - NOT SUPPORTED` and the Variant Choice / Variant Group / Variant
columns are ignored (harmless to leave in the Sheet and Supabase).

## Setup checklist

**Google Sheet** — name it `YRA_Feed_Master` (or set the `SHEET_NAME`
Variable), paste the header row from `sheet_headers.csv` into row 1, and
share the Sheet (Editor) with the service account's email (the
`client_email` inside the credentials JSON).

**GitHub → Settings → Secrets and variables → Actions → Secrets:**

| Secret | Purpose |
|---|---|
| `GOOGLE_CREDENTIALS` | full service-account JSON for the Sheet |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | eBay Browse API (add once eBay approves the developer account) |
| `ALI_APP_KEY` / `ALI_APP_SECRET` / `ALI_ACCESS_TOKEN` | AliExpress Dropshipping API (second supplier). Key/secret from the app console on open.aliexpress.com; the access token comes from a one-time browser authorization — full walkthrough at the top of `get_aliexpress_token.py`. Rows with AliExpress links are simply left untouched until all three exist |
| `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` | Supabase project URL + the **secret/service_role key** (Settings → API). NOT the publishable/anon key - that one can't write to the table |
| `SMTP_USER` / `SMTP_APP_PASSWORD` / `ALERT_EMAIL_TO` | change-alert + failure emails - see email options below |

**Email options** — either provider works, pick one:
- **Resend**: secrets `SMTP_USER` = the literal word `resend`,
  `SMTP_APP_PASSWORD` = your Resend API key, `ALERT_EMAIL_TO` = where alerts
  go; Variables `SMTP_HOST` = `smtp.resend.com`, `ALERT_EMAIL_FROM` = a
  sender address on a domain verified in Resend (or `onboarding@resend.dev`
  while testing - that one can only deliver to your own Resend account
  email).
- **Gmail**: secrets `SMTP_USER` = the Gmail address, `SMTP_APP_PASSWORD` =
  an app password (not the normal password), `ALERT_EMAIL_TO` = where alerts
  go. No variables needed.

**Variables (not secrets):** `SHEET_NAME` (optional), `SMTP_HOST` /
`SMTP_PORT` / `ALERT_EMAIL_FROM` (only for Resend, see above),
`SUPABASE_FEED_BUCKET` (optional, e.g. `yra-feeds` — create a public
Storage bucket with that name if set), `EBAY_DAILY_CALL_BUDGET` /
`RUNS_PER_DAY` / `MAX_PRODUCTS_PER_RUN` (optional batch tuning; defaults
4000 / 8 / auto).

**Supabase table** — run in the SQL editor (everything nullable except SKU,
which avoids the partial-upsert NOT NULL trap by design):

```sql
create table "YRA_Feed_Master" (
  "SKU" text primary key,
  "Title" text, "Description" text, "Brand" text, "Category" text,
  "Category ID" text, "Supplier URL" text, "Supplier" text,
  "Cost Price (£)" numeric, "Shipping Cost (£)" text,
  "Profit %" text, "Fee %" text,
  "Stock" numeric, "Selling Price (£)" numeric, "Status" text,
  "Last Updated" text, "Image URL" text, "Additional Images" text,
  "Condition" text, "Last Checked Time" text, "EAN" text,
  "Listing ID" text, "OPC" text, "Sync Status" text,
  "OnBuy Product Created" text, "OnBuy Listing Active" text,
  "OnBuy Product ID" text, "Last OnBuy Sync" text,
  "Change Alert" text, "Change Time" text,
  "Variant Choice" text, "Variant Group" text, "Variant" text
);
```

If the table already exists without the three variant columns, add them
with:

```sql
alter table "YRA_Feed_Master"
  add column "Variant Choice" text,
  add column "Variant Group" text,
  add column "Variant" text;
```

The code already defaults to this table name (`YRA_Feed_Master`) — only
set a `SUPABASE_TABLE_NAME` env var if you named the table differently.

**eBay** — the developer account is pending approval; until the two eBay
secrets exist, runs will fail at the eBay-auth step by design (nothing is
touched). Add the keys when approved and the pipeline starts working with
no other changes.

## Row colours

- **White** — normal, in stock, nothing to do.
- **Amber** — a change needs applying on OnBuy (see `Change Alert`), or the
  row itself needs fixing: a variant/unreadable link, a missing SKU, or a
  **duplicate SKU** (two rows sharing the same barcode digits — the first
  row keeps working, later ones are flagged and skipped until given their
  own barcode). The `Change Alert` column always says exactly what to do.
  Clears after `Applied on OnBuy` is ticked (or the row is fixed).
- **Red** — out of stock at the supplier. Deactivate it on OnBuy, tick
  `Applied on OnBuy`. It turns amber "BACK IN STOCK" later if it returns.

**At thousands of products**, don't scroll for colours — create a saved
**filter view** in the Sheet (Data → Filter views) with the condition
`Change Alert is not empty`: one click shows only the rows needing action,
newest first if sorted by `Change Time`. The per-run email is the other
half — it lists every new alert by SKU, so employees work from the email or
the filter view, never by scanning the full sheet. Each product is
re-checked roughly every `rows ÷ (batch size × 8)` days (e.g. 3,000 products
at the default 500/run ≈ every 18 hours) — raise `EBAY_DAILY_CALL_BUDGET`
once eBay confirms the account's real allowance to tighten that.
