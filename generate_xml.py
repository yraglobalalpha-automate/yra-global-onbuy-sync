import gspread
from oauth2client.service_account import ServiceAccountCredentials
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
import time
import json
import os
import re
import xml.etree.ElementTree as ET
import base64
import csv

# ================= CONFIG =================
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET")

# ================= SETTINGS =================

# TRUE = FETCH ALL PRODUCTS
# FALSE = SMART BATCHING
FULL_REFRESH = True

# AFTER FIRST FULL FETCH
# CHANGE TO 12 OR 20
MAX_PRODUCTS_PER_RUN = 12

# CATEGORY REMAP
RUN_CATEGORY_MAPPING = True

# PRICING
MIN_PROFIT_PERCENT = 15
DEFAULT_MARKUP_PERCENT = 40

PK_TZ = ZoneInfo("Asia/Karachi")

# ================= GOOGLE SHEET =================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

creds_dict = json.loads(
    os.environ["GOOGLE_CREDENTIALS"]
)

creds = ServiceAccountCredentials.from_json_keyfile_dict(
    creds_dict,
    scope
)

client = gspread.authorize(creds)

sheet = client.open(
    "YRA_OnBuy_Master"
).sheet1

data = sheet.get_all_records()

headers = sheet.row_values(1)

col_map = {
    col: idx + 1
    for idx, col in enumerate(headers)
}

print(f"📊 TOTAL ROWS IN SHEET: {len(data)}")

# ================= CATEGORY FILE =================
ONBUY_CATEGORIES = []

with open(
    "onbuy_categories_only.csv",
    newline='',
    encoding='utf-8'
) as csvfile:

    reader = csv.DictReader(csvfile)

    for row in reader:

        category = row.get(
            "OnBuy Category Path"
        )

        if category:
            ONBUY_CATEGORIES.append(category)

print(
    f"📂 Loaded {len(ONBUY_CATEGORIES)} "
    f"OnBuy categories"
)

VALID_ONBUY_CATEGORIES = set(
    cat.strip().lower()
    for cat in ONBUY_CATEGORIES
)

# ================= HELPERS =================
def col_letter(n):

    result = ""

    while n:
        n, r = divmod(n - 1, 26)
        result = chr(65 + r) + result

    return result

def parse_time(value):

    try:
        return datetime.strptime(
            value,
            "%Y-%m-%d %H:%M:%S"
        )

    except:
        return datetime(2000, 1, 1)

def tokenize(text):

    return set(
        re.findall(
            r'\w+',
            str(text).lower()
        )
    )

def clean_category(cat):

    if not cat:
        return ""

    cat = str(cat).replace(
        "\n",
        " "
    ).strip()

    cat = re.sub(
        r"\s+",
        " ",
        cat
    ).strip()

    return cat

def is_valid_onbuy_category(category):

    return (
        str(category).strip().lower()
        in VALID_ONBUY_CATEGORIES
    )

def to_jpg(url):

    if not url:
        return ""

    url = re.sub(
        r"\.webp.*$",
        ".jpg",
        url
    )

    url = re.sub(
        r"\.(png|jpeg).*?$",
        ".jpg",
        url
    )

    return url

def clean_images(images):

    if not images:
        return ""

    imgs = [
        to_jpg(i.strip())
        for i in str(images).split(",")
        if i.strip()
    ]

    return ",".join(imgs[:10])

# ================= HTML SAFE LIMIT =================
def trim_html_description(desc, limit=45000):

    if not desc:
        return ""

    desc = str(desc)

    desc = re.sub(
        r"\s+",
        " ",
        desc
    )

    if len(desc) > limit:

        desc = desc[:limit]

    return desc

# ================= EMPTY RESPONSE =================
def empty_ebay_response():

    return {
        "stock": 0,
        "price": 0,
        "description": "",
        "main_image": "",
        "additional_images": "",
        "title": "",
        "brand": ""
    }

# ================= CATEGORY MAPPING =================
def map_onbuy_category(
    title,
    current_category,
    description=""
):

    product_text = f"""
    {title}
    {current_category}
    {description}
    """.lower()

    product_words = tokenize(product_text)

    best_match = None
    best_score = 0

    for category_path in ONBUY_CATEGORIES:

        category_words = tokenize(
            category_path
        )

        score = len(
            product_words.intersection(
                category_words
            )
        )

        for word in product_words:

            if word in category_path.lower():
                score += 2

        if score > best_score:

            best_score = score
            best_match = category_path

    if best_match and best_score >= 2:
        return best_match

    return current_category

# ================= EBAY TOKEN =================
def get_ebay_token():

    encoded = base64.b64encode(
        f"{EBAY_CLIENT_ID}:{EBAY_CLIENT_SECRET}".encode()
    ).decode()

    res = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={
            "Authorization": f"Basic {encoded}",
            "Content-Type": "application/x-www-form-urlencoded"
        },
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope"
        }
    )

    return res.json().get(
        "access_token"
    )

# ================= EBAY FETCH =================
def get_ebay_data(url, token):

    try:

        match = re.search(
            r"/itm/(\d+)",
            url
        )

        if not match:
            return empty_ebay_response()

        item_id = match.group(1)

        res = requests.get(
            "https://api.ebay.com/buy/browse/v1/item/get_item_by_legacy_id",
            headers={
                "Authorization": f"Bearer {token}",
                "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"
            },
            params={
                "legacy_item_id": item_id
            }
        )

        # ================= REMOVED =================
        if res.status_code == 404:

            print(f"REMOVED LISTING: {item_id}")

            return empty_ebay_response()

        data = res.json()

        # ================= PRICE =================
        price_data = data.get(
            "price",
            {}
        )

        price = float(
            price_data.get(
                "value",
                0
            ) or 0
        )

        if price <= 0:

            print(f"NO PRICE: {item_id}")

            return empty_ebay_response()

        # ================= STOCK =================
        estimated = data.get(
            "estimatedAvailabilities",
            []
        )

        stock = 5

        if estimated:

            status = estimated[0].get(
                "estimatedAvailabilityStatus",
                ""
            )

            if status in [
                "OUT_OF_STOCK",
                "UNAVAILABLE"
            ]:

                print(f"OUT OF STOCK: {item_id}")

                return empty_ebay_response()

            stock = estimated[0].get(
                "estimatedAvailableQuantity",
                5
            )

        if not stock or stock <= 0:
            stock = 5

        # ================= DESCRIPTION =================
        html_description = trim_html_description(
            data.get(
                "description",
                ""
            )
        )

        # ================= MAIN IMAGE =================
        main_image = ""

        if data.get("image"):

            main_image = to_jpg(
                data["image"].get(
                    "imageUrl",
                    ""
                )
            )

        # ================= ADDITIONAL IMAGES =================
        additional_images = []

        for img in data.get(
            "additionalImages",
            []
        ):

            img_url = to_jpg(
                img.get(
                    "imageUrl",
                    ""
                )
            )

            if img_url:
                additional_images.append(
                    img_url
                )

        additional_images = ",".join(
            additional_images[:10]
        )

        # ================= TITLE =================
        title = data.get(
            "title",
            ""
        )

        # ================= BRAND =================
        brand = ""

        aspects = data.get(
            "localizedAspects",
            []
        )

        for aspect in aspects:

            if aspect.get(
                "name",
                ""
            ).lower() == "brand":

                values = aspect.get(
                    "value",
                    ""
                )

                if isinstance(values, list):

                    brand = values[0]

                else:

                    brand = values

        return {
            "stock": stock,
            "price": price,
            "description": html_description,
            "main_image": main_image,
            "additional_images": additional_images,
            "title": title,
            "brand": brand
        }

    except Exception as e:

        print(f"eBay fetch error: {e}")

        return empty_ebay_response()

# ================= CATEGORY MAPPING =================
if RUN_CATEGORY_MAPPING:

    print("\n🔄 Updating Categories...")

    category_updates = []

    for idx, row in enumerate(data):

        i = idx + 2

        current_category = str(
            row.get("Category") or ""
        ).strip()

        if is_valid_onbuy_category(
            current_category
        ):
            continue

        mapped = map_onbuy_category(
            row.get("Title"),
            current_category,
            row.get("Description")
        )

        if mapped != current_category:

            category_updates.append({
                "range": f"{col_letter(col_map['Category'])}{i}",
                "values": [[mapped]]
            })

            print(f"Mapped row {i}")

    if category_updates:
        sheet.batch_update(category_updates)

# ================= PRODUCT ORDER =================
if FULL_REFRESH:

    sorted_data = list(
        enumerate(data)
    )

else:

    sorted_data = sorted(
        enumerate(data),
        key=lambda x: parse_time(
            x[1].get(
                "Last Checked Time",
                ""
            )
        )
    )

print(
    f"🔁 Processing "
    f"{min(len(sorted_data), MAX_PRODUCTS_PER_RUN)} products"
)

# ================= MAIN UPDATE LOOP =================
token = get_ebay_token()

updated_count = 0

for idx, row in sorted_data[:MAX_PRODUCTS_PER_RUN]:

    i = idx + 2

    url = str(
        row.get("Supplier URL", "")
    ).strip()

    if "ebay." not in url.lower():
        continue

    ebay_data = get_ebay_data(
        url,
        token
    )

    stock = ebay_data["stock"]
    cost_price = ebay_data["price"]

    # ================= PRICING =================
    if stock == 0:

        selling_price = 0

    else:

        minimum_price = cost_price * (
            1 + (MIN_PROFIT_PERCENT / 100)
        )

        calculated_price = cost_price * (
            1 + (DEFAULT_MARKUP_PERCENT / 100)
        )

        selling_price = round(
            max(
                minimum_price,
                calculated_price
            ),
            2
        )

    updates = [
        {
            "range": f"{col_letter(col_map['Cost Price (£)'])}{i}",
            "values": [[cost_price]]
        },
        {
            "range": f"{col_letter(col_map['Stock'])}{i}",
            "values": [[stock]]
        },
        {
            "range": f"{col_letter(col_map['Selling Price (£)'])}{i}",
            "values": [[selling_price]]
        },
        {
            "range": f"{col_letter(col_map['Status'])}{i}",
            "values": [[
                "ACTIVE"
                if stock > 0
                else "INACTIVE"
            ]]
        },
        {
            "range": f"{col_letter(col_map['Description'])}{i}",
            "values": [[
                trim_html_description(
                    ebay_data["description"]
                )
            ]]
        },
        {
            "range": f"{col_letter(col_map['Image URL'])}{i}",
            "values": [[
                ebay_data["main_image"]
            ]]
        },
        {
            "range": f"{col_letter(col_map['Additional Images'])}{i}",
            "values": [[
                ebay_data["additional_images"]
            ]]
        },
        {
            "range": f"{col_letter(col_map['Brand'])}{i}",
            "values": [[
                ebay_data["brand"]
            ]]
        },
        {
            "range": f"{col_letter(col_map['Title'])}{i}",
            "values": [[
                ebay_data["title"]
            ]]
        },
        {
            "range": f"{col_letter(col_map['Last Updated'])}{i}",
            "values": [[
                datetime.now(PK_TZ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            ]]
        },
        {
            "range": f"{col_letter(col_map['Last Checked Time'])}{i}",
            "values": [[
                datetime.now(PK_TZ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            ]]
        }
    ]

    sheet.batch_update(updates)

    updated_count += 1

    print(f"Updated row {i}")

    time.sleep(0.5)

# ================= GENERATE XML =================
root = ET.Element("products")

feed_count = 0
skipped_feed = 0

for row in sheet.get_all_records():

    try:

        sku = str(
            row.get("SKU") or ""
        ).strip()

        title = str(
            row.get("Title") or ""
        ).strip()

        desc = str(
            row.get("Description") or ""
        ).strip()

        brand = str(
            row.get("Brand") or ""
        ).strip()

        category = clean_category(
            row.get("Category")
        )

        image = to_jpg(
            row.get("Image URL")
        )

        additional_images = clean_images(
            row.get("Additional Images")
        )

        price = float(
            row.get("Selling Price (£)") or 0
        )

        stock = int(
            row.get("Stock") or 0
        )

        if not all([
            sku,
            title,
            category
        ]):

            skipped_feed += 1
            continue

        product = ET.SubElement(
            root,
            "product"
        )

        ET.SubElement(
            product,
            "sku"
        ).text = sku

        ET.SubElement(
            product,
            "product_name"
        ).text = title[:150]

        description_element = ET.SubElement(
            product,
            "description"
        )

        description_element.text = desc

        ET.SubElement(
            product,
            "image_url"
        ).text = image

        # ================= ADDITIONAL IMAGES =================
        additional_images_list = []

        if additional_images:

            additional_images_list = [
                img.strip()
                for img in additional_images.split(",")
                if img.strip()
            ]

        for idx, img in enumerate(
            additional_images_list[:10]
        ):

            ET.SubElement(
                product,
                f"additional_image_url_{idx + 1}"
            ).text = img

        ET.SubElement(
            product,
            "brand"
        ).text = brand

        ET.SubElement(
            product,
            "category"
        ).text = category

        ET.SubElement(
            product,
            "condition"
        ).text = "New"

        ET.SubElement(
            product,
            "ean"
        ).text = sku

        ET.SubElement(
            product,
            "price"
        ).text = str(price)

        ET.SubElement(
            product,
            "quantity"
        ).text = str(stock)

        feed_count += 1

    except:

        skipped_feed += 1

# ================= SAVE XML =================
ET.ElementTree(root).write(
    "feed.xml",
    encoding="utf-8",
    xml_declaration=True
)

# ================= FINAL LOGS =================
print("\n✅ DONE")
print(f"📦 Updated rows: {updated_count}")
print(f"📦 Feed products: {feed_count}")
print(f"⚠ Skipped in feed: {skipped_feed}")
