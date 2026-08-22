import os
import re
import csv
import json
import time
import base64
import mimetypes
import threading
from datetime import datetime, timezone, timedelta
import requests
from flask import Flask, request, Response

BOT_VERSION = "V19-FULL-AUDIT"

app = Flask(__name__)

# =========================
# ENVIRONMENT
# =========================
VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_URL = os.environ.get("GOOGLE_SHEET_URL")

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v25.0")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

PRODUCT_REFRESH_SECONDS = int(os.environ.get("PRODUCT_REFRESH_SECONDS", "60"))
ADMIN_PAUSE_MINUTES = int(os.environ.get("ADMIN_PAUSE_MINUTES", "30"))

DEFAULT_YANGON_DELIVERY = int(os.environ.get("YANGON_DELIVERY", "5000"))
DEFAULT_OTHER_DELIVERY = int(os.environ.get("OTHER_DELIVERY", "7500"))

# =========================
# IN-MEMORY STATE
# =========================
PRODUCTS = {}
LAST_PRODUCT_REFRESH = 0.0

ORDER_SESSIONS = {}
PROCESSED_MESSAGE_IDS = set()
BOT_SENT_MESSAGE_IDS = set()
ADMIN_PAUSE_UNTIL = {}
MANYCHAT_ECHO_IGNORE_UNTIL = {}
MANYCHAT_ECHO_GRACE_SECONDS = int(os.environ.get("MANYCHAT_ECHO_GRACE_SECONDS", "20"))
PRODUCT_REFRESH_LOCK = threading.Lock()
PRODUCT_REFRESH_RUNNING = False


# =========================
# BASIC HELPERS
# =========================
def now_ts():
    return time.time()


def normalize_code(value):
    value = str(value or "").strip()
    if not value:
        return ""

    value = re.sub(r"\.0$", "", value)

    if value.isdigit():
        return value.zfill(4)

    return value.lower()


def parse_int_amount(value, default=0):
    try:
        text = str(value if value not in (None, "") else default)
        text = text.replace(",", "").replace("Ks", "").replace("ks", "").strip()
        return int(float(text))
    except Exception:
        return int(default)


def get_row_value(row, *keys):
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def mark_bot_message_id(response):
    try:
        data = response.json()
        mid = str(data.get("message_id", "") or "").strip()
        if mid:
            BOT_SENT_MESSAGE_IDS.add(mid)
            if len(BOT_SENT_MESSAGE_IDS) > 5000:
                BOT_SENT_MESSAGE_IDS.clear()
                BOT_SENT_MESSAGE_IDS.add(mid)
    except Exception:
        pass


# =========================
# GOOGLE SHEET
# =========================
def sheet_export_url(url):
    url = str(url or "").strip()
    if not url:
        return ""

    if "/edit" in url:
        return url.split("/edit")[0] + "/export?format=csv"

    return url


def _fetch_products_from_sheet():
    if not GOOGLE_SHEET_URL:
        print("GOOGLE_SHEET_URL IS MISSING", flush=True)
        return None

    response = requests.get(sheet_export_url(GOOGLE_SHEET_URL), timeout=12)
    response.raise_for_status()
    reader = csv.DictReader(response.text.splitlines())
    products = {}
    for row in reader:
        code = normalize_code(get_row_value(row, "Code", "code", "CODE"))
        if code:
            products[code] = row
    return products


def _background_product_refresh():
    global PRODUCTS, LAST_PRODUCT_REFRESH, PRODUCT_REFRESH_RUNNING
    try:
        products = _fetch_products_from_sheet()
        if products is not None:
            PRODUCTS = products
            LAST_PRODUCT_REFRESH = now_ts()
            print("PRODUCTS LOADED:", len(PRODUCTS), flush=True)
            print("PRODUCT CODES:", list(PRODUCTS.keys()), flush=True)
    except Exception as e:
        print("SHEET BACKGROUND ERROR:", str(e), flush=True)
    finally:
        with PRODUCT_REFRESH_LOCK:
            PRODUCT_REFRESH_RUNNING = False


def load_products(force=False):
    """
    Keep ManyChat fast. If we already have a catalog, a stale refresh happens in
    the background instead of making the live customer request wait on Google.
    A forced/first load is synchronous so the bot has a catalog after startup.
    """
    global PRODUCTS, LAST_PRODUCT_REFRESH, PRODUCT_REFRESH_RUNNING

    stale = (not PRODUCTS) or (now_ts() - LAST_PRODUCT_REFRESH >= PRODUCT_REFRESH_SECONDS)
    if not stale and not force:
        return

    if force or not PRODUCTS:
        try:
            products = _fetch_products_from_sheet()
            if products is not None:
                PRODUCTS = products
                LAST_PRODUCT_REFRESH = now_ts()
                print("PRODUCTS LOADED:", len(PRODUCTS), flush=True)
                print("PRODUCT CODES:", list(PRODUCTS.keys()), flush=True)
        except Exception as e:
            print("SHEET ERROR:", str(e), flush=True)
        return

    with PRODUCT_REFRESH_LOCK:
        if PRODUCT_REFRESH_RUNNING:
            return
        PRODUCT_REFRESH_RUNNING = True

    threading.Thread(target=_background_product_refresh, daemon=True).start()


load_products(force=True)


# =========================
# DRIVE / IMAGE HELPERS
# =========================
def google_drive_file_id(url):
    url = str(url or "").strip()

    patterns = [
        r"/file/d/([^/]+)",
        r"[?&]id=([^&]+)",
        r"/d/([^/]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return ""


def google_drive_direct_url(url):
    url = str(url or "").strip()
    if not url:
        return ""

    file_id = google_drive_file_id(url)
    if file_id:
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    return url


def google_drive_view_url(url):
    """
    Convert Google Drive share/view links to a direct image-serving URL.
    ManyChat/Facebook often fails on drive.google.com/uc redirect URLs,
    so use Googleusercontent directly when a Drive file id is available.
    """
    url = str(url or "").strip()
    if not url:
        return ""

    file_id = google_drive_file_id(url)
    if file_id:
        return f"https://lh3.googleusercontent.com/d/{file_id}"

    return url


def product_image_url(product):
    return str(
        get_row_value(
            product,
            "Image URL",
            "ImageURL",
            "image_url",
            "Image",
            "image",
        )
        or ""
    ).strip()



def product_public_image_url(code, product):
    """
    Return a stable public HTTPS image URL served by this Render app.

    IMPORTANT: never download/verify the Google Drive image inside the ManyChat
    Dynamic Block request. That extra network call can make ManyChat time out even
    when Render eventually logs HTTP 200. The /product-image/<code> endpoint will
    fetch the image only when ManyChat/Meta requests the image itself.
    """
    source = product_image_url(product)
    if not source:
        return ""

    base = request.host_url.rstrip("/")
    return f"{base}/product-image/{normalize_code(code)}"


def looks_like_order_details(message):
    """
    Detect a customer message that likely contains order details even if they
    did not explicitly type 'order' / 'ယူမယ်'.
    """
    value = str(message or "").strip()
    if not value:
        return False

    # Myanmar phone numbers / common local phone formatting.
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 9:
        return True

    # Slash-separated copy-friendly order details.
    if value.count("/") >= 2:
        return True

    address_words = (
        "လမ်း", "ရပ်ကွက်", "မြို့နယ်", "မြို့", "ရွာ", "အမှတ်",
        "တာမွေ", "သင်္ဃန်းကျွန်း", "ရန်ကုန်", "မန္တလေး",
        "road", "street", "township", "yangon", "mandalay",
    )
    low = value.lower()
    return any(word.lower() in low for word in address_words)


def extract_order_fields_from_image(image_url, caption=""):
    """
    Read Name / Address / Phone / delivery area / optional item+qty from a
    customer screenshot or photo. Returns only fields visible in the image.
    """
    if not OPENAI_API_KEY or not image_url:
        return {}

    customer_data_url = image_as_data_url(image_url)
    if not customer_data_url:
        return {}

    load_products()

    catalog_lines = []
    for code, product in PRODUCTS.items():
        catalog_lines.append(
            f'{code}: {get_row_value(product, "Product Name", "Name")}'
        )

    content = [
        {
            "type": "text",
            "text": (
                "Read this CUSTOMER ORDER SCREENSHOT/PHOTO. Extract only visible "
                "customer order information. Do not invent anything. "
                "Return ONLY one JSON object in this exact shape:\n"
                '{'
                '"name":"","address":"","phone":"","delivery_area":"",'
                '"items":[{"code":"0001","quantity":1}]'
                '}\n'
                'delivery_area must be "yangon", "other", or "". '
                "Use Yangon for Yangon addresses such as Tarmwe/Tamwe/Thingangyun. "
                "Use other for clearly non-Yangon Myanmar addresses. "
                "Never invent a product code. If no product is visible, items must be [].\n\n"
                f"PRODUCTS:\n{chr(10).join(catalog_lines)}\n\n"
                f"CAPTION/TEXT: {caption}"
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": customer_data_url},
        },
    ]

    answer = openai_chat(
        [{"role": "user", "content": content}],
        max_tokens=350,
        temperature=0,
    )

    result = parse_json_answer(answer)
    return result if isinstance(result, dict) else {}


def merge_extracted_order_data(session, extracted):
    if not isinstance(extracted, dict):
        return session

    for field in ("name", "address", "phone", "delivery_area"):
        value = str(extracted.get(field, "") or "").strip()
        if value:
            session[field] = value

    extracted_items = extracted.get("items", []) or []

    for item in extracted_items:
        code = normalize_code(item.get("code", ""))
        if code not in PRODUCTS:
            continue

        try:
            qty = max(1, int(item.get("quantity", 1)))
        except Exception:
            qty = 1

        session["items"][code] = qty

    # Image/AI extraction only confirms quantity if an item/quantity was
    # actually present in the order data, not merely because a product was viewed.
    if extracted_items:
        session["quantity_confirmed"] = True

    return session



def buyer_order_confirmation(session):
    return (
        build_telegram_order(session)
        + "\n\nအော်ဒါတင်ပြီးပါပြီရှင်။ ကျေးဇူးတင်ပါတယ်ရှင်။"
    )



def download_image_bytes(url):
    url = google_drive_direct_url(url)
    if not url:
        return None, None

    try:
        response = requests.get(
            url,
            timeout=30,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").split(";")[0].strip()

        if not content_type.startswith("image/"):
            guessed, _ = mimetypes.guess_type(url)
            if guessed and guessed.startswith("image/"):
                content_type = guessed
            else:
                content_type = "image/jpeg"

        return response.content, content_type

    except Exception as e:
        print("IMAGE DOWNLOAD ERROR:", str(e), flush=True)
        return None, None


def image_as_data_url(url):
    image_bytes, content_type = download_image_bytes(url)
    if not image_bytes:
        return ""

    encoded = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


# =========================
# PRODUCT SEARCH
# =========================
def find_product_by_code(message):
    load_products()

    if not message:
        return None, None

    text = str(message).strip()

    # Finds 1, 01, 001, 0001, 12, 0012 etc.
    for number in re.findall(r"\d+", text):
        code = normalize_code(number)
        if code in PRODUCTS:
            return code, PRODUCTS[code]

    for code, product in PRODUCTS.items():
        if code in text:
            return code, product

    return None, None


def find_product_by_name(message):
    load_products()

    if not message:
        return None, None

    text = str(message).lower().strip()

    best_code = None
    best_product = None
    best_score = 0

    for code, product in PRODUCTS.items():
        candidates = [
            get_row_value(product, "Product Name", "Name", "product_name"),
            get_row_value(product, "Myanmar Name", "MyanmarName"),
            get_row_value(product, "English Name", "EnglishName"),
            get_row_value(product, "Chinese Name", "ChineseName"),
        ]

        for candidate in candidates:
            name = str(candidate or "").lower().strip()
            if len(name) >= 3 and name in text and len(name) > best_score:
                best_score = len(name)
                best_code = code
                best_product = product

    return best_code, best_product


def product_catalog_text():
    load_products()

    rows = []

    for code, product in PRODUCTS.items():
        name = str(get_row_value(product, "Product Name", "Name")).strip()
        details = str(
            get_row_value(
                product,
                "Description",
                "Details",
                "Detail",
                "အသေးစိတ်",
            )
        ).strip()

        rows.append(
            f"Code {code} | Product: {name} | Details: {details}"
        )

    return "\n".join(rows)


def reference_image_items():
    load_products()

    items = []

    for code, product in PRODUCTS.items():
        url = product_image_url(product)
        if not url:
            continue

        items.append(
            {
                "code": code,
                "name": str(get_row_value(product, "Product Name", "Name")).strip(),
                "url": url,
            }
        )

    return items


# =========================
# OPENAI JSON HELPER
# =========================
def parse_json_answer(answer):
    answer = str(answer or "").strip()

    start = answer.find("{")
    end = answer.rfind("}")

    if start == -1 or end == -1 or end < start:
        return {}

    try:
        return json.loads(answer[start:end + 1])
    except Exception:
        return {}


def openai_chat(messages, max_tokens=200, temperature=0):
    if not OPENAI_API_KEY:
        return None

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": OPENAI_MODEL,
                "temperature": temperature,
                "messages": messages,
                "max_tokens": max_tokens,
            },
            timeout=8,
        )

        print("OPENAI STATUS:", response.status_code, flush=True)

        if response.status_code != 200:
            print("OPENAI ERROR:", response.text, flush=True)
            return None

        return response.json()["choices"][0]["message"]["content"]

    except Exception as e:
        print("OPENAI EXCEPTION:", str(e), flush=True)
        return None


# =========================
# TEXT PRODUCT IDENTIFICATION
# =========================
def ai_find_product_from_text(message):
    if not OPENAI_API_KEY or not message:
        return None, None

    catalog = product_catalog_text()

    system_prompt = f"""
You identify which product a customer means in an online shop.

The customer may:
- write Burmese, English, Chinese, or mixed languages
- misspell a product name
- describe the product instead of giving its code
- paste text from a screenshot or advertisement

CATALOG:
{catalog}

Return ONLY one JSON object:
{{"code":"0001"}}

If no catalog product is a confident match:
{{"code":null}}

Never invent a code.
"""

    answer = openai_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": str(message)},
        ],
        max_tokens=80,
        temperature=0,
    )

    result = parse_json_answer(answer)
    code = normalize_code(result.get("code", ""))

    if code in PRODUCTS:
        return code, PRODUCTS[code]

    return None, None


# =========================
# IMAGE PRODUCT IDENTIFICATION
# =========================
def ai_find_product_from_image(image_url, caption=""):
    if not OPENAI_API_KEY or not image_url:
        return None, None

    load_products()

    customer_data_url = image_as_data_url(image_url)
    if not customer_data_url:
        return None, None

    catalog = product_catalog_text()

    # Stage 1: identify from the customer's image + catalog text.
    content = [
        {
            "type": "text",
            "text": (
                "Identify which product from this shop catalog is shown in the CUSTOMER IMAGE. "
                "The image may be a direct product photo, screenshot, Facebook post screenshot, "
                "photo of another screen, or image containing Burmese/English/Chinese text. "
                "Use the visual appearance, visible text, labels, shape, and catalog descriptions. "
                "Return ONLY one JSON object with a code field. "
                "If one catalog item is the best clear match, return its code. "
                "If truly uncertain, return null. Never invent a code.\n\n"
                f"CATALOG:\n{catalog}\n\n"
                f"CUSTOMER TEXT: {caption}"
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": customer_data_url},
        },
    ]

    answer = openai_chat(
        [{"role": "user", "content": content}],
        max_tokens=80,
        temperature=0,
    )

    result = parse_json_answer(answer)
    code = normalize_code(result.get("code", ""))

    if code in PRODUCTS:
        print("IMAGE MATCH STAGE 1:", code, flush=True)
        return code, PRODUCTS[code]

    # Stage 2: compare against Google Sheet reference images in small batches.
    references = reference_image_items()

    batch_size = 5

    for offset in range(0, len(references), batch_size):
        batch = references[offset:offset + batch_size]

        compare_content = [
            {
                "type": "text",
                "text": (
                    "Match the CUSTOMER IMAGE to one of the REFERENCE PRODUCTS below. "
                    "The customer image can have a different angle, crop, background, lighting, "
                    "screenshot frame, or text overlay. Look for the same underlying product. "
                    "Return ONLY one JSON object with a code field. "
                    "If none of this batch matches, return null."
                ),
            },
            {
                "type": "text",
                "text": "CUSTOMER IMAGE",
            },
            {
                "type": "image_url",
                "image_url": {"url": customer_data_url},
            },
        ]

        for item in batch:
            ref_data = image_as_data_url(item["url"])
            if not ref_data:
                continue

            compare_content.append(
                {
                    "type": "text",
                    "text": f'REFERENCE Code {item["code"]}: {item["name"]}',
                }
            )
            compare_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": ref_data},
                }
            )

        answer = openai_chat(
            [{"role": "user", "content": compare_content}],
            max_tokens=80,
            temperature=0,
        )

        result = parse_json_answer(answer)
        code = normalize_code(result.get("code", ""))

        if code in PRODUCTS:
            print("IMAGE MATCH STAGE 2:", code, flush=True)
            return code, PRODUCTS[code]

    return None, None


# =========================
# PRODUCT REPLY
# =========================
def product_status(product):
    return str(
        get_row_value(
            product,
            "Stock status", "Stock Status", "StockStatus", "stock_status",
            "Availability", "availability", "Status", "status",
            "ပစ္စည်းအခြေအနေ", "လက်ကျန်အခြေအနေ",
        )
        or ""
    ).strip().lower()


def product_availability(product):
    """Normalize Sheet stock wording into in_stock / coming / out / unknown."""
    raw = product_status(product)
    compact = re.sub(r"[\s_\-]+", " ", raw).strip()
    joined = re.sub(r"[^a-z0-9က-အ]+", "", compact)

    # Check unavailable states BEFORE any positive word such as "stock" / "ရှိ".
    out_words = (
        "out of stock", "outofstock", "sold out", "soldout", "no stock",
        "nostock", "unavailable", "ကုန်နေ", "ပစ္စည်းကုန်", "ကုန်ပြီ",
        "လက်ကျန်မရှိ", "stock out", "stockout",
    )
    coming_words = (
        "coming soon", "comingsoon", "coming", "not arrived", "notarrived",
        "not arrive", "not yet arrived", "on the way", "ontheway",
        "preorder", "pre order", "မရောက်သေး", "ပစ္စည်းမရောက်သေး",
        "လမ်းမှာ", "လမ်းတွင်", "ကြိုတင်မှာ", "မှာထားဆဲ",
    )
    in_words = (
        "in stock", "instock", "available", "ready stock", "readystock",
        "ready", "လက်ကျန်ရှိ", "ပစ္စည်းရှိ", "ရှိပါတယ်", "ရှိ",
    )

    def hit(words):
        return any(w in compact or re.sub(r"[^a-z0-9က-အ]+", "", w) in joined for w in words)

    if compact in ("out", "ကုန်", "ကုန်ပြီ"):
        return "out"
    if compact in ("coming", "coming soon", "မရောက်သေး"):
        return "coming"
    if compact in ("in stock", "instock", "ရှိ", "available"):
        return "in_stock"

    if hit(out_words):
        return "out"
    if hit(coming_words):
        return "coming"
    if hit(in_words):
        return "in_stock"

    # Preserve compatibility with existing rows that predate the Stock Status column.
    if not compact:
        return "in_stock"
    return "unknown"


def product_is_sellable(product):
    return product_availability(product) == "in_stock"


def product_detail(product):
    return str(
        get_row_value(
            product,
            "Description",
            "Detail",
            "Details",
            "Product Detail",
            "Product Details",
            "Description Myanmar",
            "Myanmar Description",
            "အသေးစိတ်",
        )
        or ""
    ).strip()



def product_reply(code, product):
    name = str(get_row_value(product, "Product Name", "Name")).strip()

    price = parse_int_amount(get_row_value(product, "Price"), 0)

    yangon_delivery = parse_int_amount(
        get_row_value(product, "Yangon Delivery", "YangonDelivery"),
        DEFAULT_YANGON_DELIVERY,
    )

    other_delivery = parse_int_amount(
        get_row_value(product, "Other City Delivery", "Other Delivery", "OtherCityDelivery"),
        DEFAULT_OTHER_DELIVERY,
    )

    if not yangon_delivery:
        yangon_delivery = DEFAULT_YANGON_DELIVERY

    if not other_delivery:
        other_delivery = DEFAULT_OTHER_DELIVERY

    availability = product_availability(product)

    if availability == "out":
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းကုန်နေပါတယ်ရှင်။"

    if availability == "coming":
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းမရောက်သေးပါရှင်။"

    if availability == "unknown":
        return f"Code {code} {name}\nပစ္စည်းအခြေအနေကို Admin က စစ်ဆေးပေးပါမယ်ရှင်။"

    yangon_total = price + yangon_delivery
    other_total = price + other_delivery

    return (
        f"Code {code} {name}\n\n"
        f"ဈေးနှုန်း - {price:,} Ks\n\n"
        f"ရန်ကုန်ပို့ခ - {yangon_delivery:,} Ks\n"
        f"ရန်ကုန် စုစုပေါင်း - {yangon_total:,} Ks\n\n"
        f"နယ်ပို့ခ - {other_delivery:,} Ks\n"
        f"နယ် စုစုပေါင်း - {other_total:,} Ks\n\n"
        "ပစ္စည်းရောက်မှ ငွေချေ (COD) ရပါတယ်ရှင်။"
    )


# =========================
# FACEBOOK SEND
# =========================
def facebook_messages_url():
    return f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"


def send_facebook_text(recipient_id, message_text):
    if not PAGE_ACCESS_TOKEN or not recipient_id or not message_text:
        return False

    try:
        response = requests.post(
            facebook_messages_url(),
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient": {"id": recipient_id},
                "message": {"text": message_text},
            },
            timeout=30,
        )

        print("FACEBOOK TEXT STATUS:", response.status_code, flush=True)
        print("FACEBOOK TEXT RESPONSE:", response.text, flush=True)

        mark_bot_message_id(response)

        return response.status_code == 200

    except Exception as e:
        print("FACEBOOK TEXT ERROR:", str(e), flush=True)
        return False


def send_facebook_image_bytes(recipient_id, image_bytes, content_type="image/jpeg"):
    if not PAGE_ACCESS_TOKEN or not recipient_id or not image_bytes:
        return False

    extension = ".jpg"
    if content_type == "image/png":
        extension = ".png"
    elif content_type == "image/webp":
        extension = ".webp"

    try:
        response = requests.post(
            facebook_messages_url(),
            params={"access_token": PAGE_ACCESS_TOKEN},
            data={
                "recipient": json.dumps({"id": recipient_id}),
                "message": json.dumps(
                    {
                        "attachment": {
                            "type": "image",
                            "payload": {"is_reusable": True},
                        }
                    }
                ),
            },
            files={
                "filedata": (
                    "product" + extension,
                    image_bytes,
                    content_type,
                )
            },
            timeout=45,
        )

        print("FACEBOOK IMAGE UPLOAD STATUS:", response.status_code, flush=True)
        print("FACEBOOK IMAGE UPLOAD RESPONSE:", response.text, flush=True)

        mark_bot_message_id(response)

        return response.status_code == 200

    except Exception as e:
        print("FACEBOOK IMAGE UPLOAD ERROR:", str(e), flush=True)
        return False


def send_facebook_image_url(recipient_id, image_url):
    if not PAGE_ACCESS_TOKEN or not recipient_id or not image_url:
        return False

    try:
        response = requests.post(
            facebook_messages_url(),
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient": {"id": recipient_id},
                "message": {
                    "attachment": {
                        "type": "image",
                        "payload": {
                            "url": google_drive_view_url(image_url),
                            "is_reusable": True,
                        },
                    }
                },
            },
            timeout=30,
        )

        print("FACEBOOK IMAGE URL STATUS:", response.status_code, flush=True)
        print("FACEBOOK IMAGE URL RESPONSE:", response.text, flush=True)

        mark_bot_message_id(response)

        return response.status_code == 200

    except Exception as e:
        print("FACEBOOK IMAGE URL ERROR:", str(e), flush=True)
        return False


def send_product_response(recipient_id, code, product):
    image_url = product_image_url(product)

    if image_url:
        image_bytes, content_type = download_image_bytes(image_url)
        sent = False

        if image_bytes:
            sent = send_facebook_image_bytes(
                recipient_id,
                image_bytes,
                content_type,
            )

        if not sent:
            send_facebook_image_url(recipient_id, image_url)

    detail = product_detail(product)
    if detail:
        send_facebook_text(recipient_id, detail)

    send_facebook_text(recipient_id, product_reply(code, product))

    send_facebook_text(
        recipient_id,
        (
            'မှာယူလိုပါက အမည် / လိပ်စာအပြည့်အစုံ / ဖုန်းနံပါတ် ကို အပြည့်အစုံရေးပို့ပေးပါရှင်။'
        ),
    )


# =========================
# TELEGRAM
# =========================

# =========================
# TELEGRAM
# =========================

# =========================
# TELEGRAM
# =========================

# =========================
# TELEGRAM
# =========================
def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM ENV IS MISSING", flush=True)
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=30,
        )

        print("TELEGRAM STATUS:", response.status_code, flush=True)
        print("TELEGRAM RESPONSE:", response.text, flush=True)

        return response.status_code == 200

    except Exception as e:
        print("TELEGRAM ERROR:", str(e), flush=True)
        return False


# =========================
# ADMIN TAKEOVER
# =========================
def pause_for_admin(customer_id):
    if not customer_id:
        return

    ADMIN_PAUSE_UNTIL[str(customer_id)] = (
        now_ts() + ADMIN_PAUSE_MINUTES * 60
    )

    print(
        "ADMIN PAUSE:",
        customer_id,
        "UNTIL",
        ADMIN_PAUSE_UNTIL[str(customer_id)],
        flush=True,
    )


def admin_is_active(customer_id):
    customer_id = str(customer_id or "")

    until = ADMIN_PAUSE_UNTIL.get(customer_id, 0)

    if until <= now_ts():
        ADMIN_PAUSE_UNTIL.pop(customer_id, None)
        return False

    return True


def handle_echo_message(event, message_data):
    """
    Page/Admin outgoing message handling.

    - Python-bot messages are ignored by message id.
    - ManyChat-generated replies are ignored for a short grace period so they
      are not mistaken for a human Admin reply.
    - A real manual Page/Admin reply pauses ONLY that one customer.
    """
    mid = str(message_data.get("mid", "") or "").strip()

    if mid and mid in BOT_SENT_MESSAGE_IDS:
        BOT_SENT_MESSAGE_IDS.discard(mid)
        print("BOT ECHO IGNORED:", mid, flush=True)
        return

    customer_id = str(
        event.get("recipient", {}).get("id")
        or event.get("sender", {}).get("id")
        or ""
    ).strip()

    if not customer_id:
        return

    ignore_until = MANYCHAT_ECHO_IGNORE_UNTIL.get(customer_id, 0)
    if ignore_until > now_ts():
        print("MANYCHAT ECHO IGNORED:", customer_id, flush=True)
        return

    MANYCHAT_ECHO_IGNORE_UNTIL.pop(customer_id, None)
    pause_for_admin(customer_id)

    print(
        "MANUAL ADMIN MESSAGE DETECTED - BOT PAUSED:",
        customer_id,
        f"{ADMIN_PAUSE_MINUTES} minutes",
        flush=True,
    )


def new_order_session():
    return {
        "name": "",
        "address": "",
        "phone": "",
        "delivery_area": "",
        "items": {},
        "last_product_code": "",
        "quantity_confirmed": False,
    }



def get_order_session(sender_id):
    return ORDER_SESSIONS.setdefault(
        str(sender_id),
        new_order_session(),
    )


def find_codes_and_quantities(text):
    load_products()

    text = str(text or "")
    found = {}

    for code in PRODUCTS.keys():
        if code not in text:
            continue

        qty = 1
        escaped = re.escape(code)

        patterns = [
            rf"{escaped}\s*[xX*]\s*(\d+)",
            rf"{escaped}\s+(\d+)\s*(?:pcs|pc|ခု|စုံ)",
            rf"{escaped}\s*[-:]\s*(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)

            if match:
                qty = max(1, int(match.group(1)))
                break

        found[code] = qty

    return found


def extract_explicit_quantity(text):
    value = str(text or "")

    patterns = [
        r"[xX*]\s*(\d+)",
        r"(\d+)\s*(?:pcs|pc|ခု|ကဒ်|စုံ)",
        r"(?:qty|quantity)\s*[:=]?\s*(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            return max(1, int(match.group(1)))

    return None


def is_pure_product_query(message, code, product):
    """
    A bare code/name request should always show the product, even if an old
    incomplete order session exists. This prevents stale order state from
    swallowing later product questions such as 0010 -> 0016.
    """
    value = str(message or "").strip()
    if not value or not product:
        return False

    if is_order_message(value):
        return False

    if looks_like_order_details(value):
        return False

    if extract_explicit_quantity(value) is not None:
        return False

    normalized = normalize_code(value)
    if normalized == code:
        return True

    # Product-name-only browsing also counts as a product question.
    found_code, found_product = find_product_by_name(value)
    return bool(found_product and found_code == code)


def extract_order_fields_with_ai(message):
    if not OPENAI_API_KEY or not message:
        return {}

    load_products()

    catalog_lines = []

    for code, product in PRODUCTS.items():
        catalog_lines.append(
            f'{code}: {get_row_value(product, "Product Name", "Name")}'
        )

    prompt = f"""
Extract customer order information from this Burmese/English/mixed-language message.

PRODUCTS:
{chr(10).join(catalog_lines)}

Return ONLY one JSON object:
{{
  "name": "",
  "address": "",
  "phone": "",
  "delivery_area": "",
  "items": [
    {{"code": "0001", "quantity": 1}}
  ]
}}

Rules:
- delivery_area must be "yangon", "other", or "".
- Yangon/Tarmwe/Tamwe/Kyaukmyaung/Thingangyun etc. should be "yangon".
- Clearly non-Yangon Myanmar addresses should be "other".
- quantity must be integer >= 1.
- Do not invent missing information.
- Never invent product codes.
"""

    answer = openai_chat(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": str(message)},
        ],
        max_tokens=300,
        temperature=0,
    )

    result = parse_json_answer(answer)

    return result if isinstance(result, dict) else {}



def detect_delivery_area_from_text(value):
    """
    Detect delivery area only when the text gives a useful location clue.

    Yangon customers often write only township/ward/road and omit the word
    "ရန်ကုန်".  Therefore Yangon township names count as Yangon.  For non-Yangon
    orders we prefer explicit city/state/town names.  If no area can be detected,
    a later finalization step may safely default a COMPLETE order to Yangon.
    """
    low = str(value or "").lower().strip()
    if not low:
        return ""

    yangon_words = (
        # City / common spellings
        "ရန်ကုန်", "yangon", "rangoon",
        # Yangon townships / common customer spellings
        "တာမွေ", "tamwe", "tarmwe",
        "ဗဟန်း", "bahan",
        "စမ်းချောင်း", "sanchaung",
        "ကမာရွတ်", "kamayut", "kamaryut",
        "လှိုင်", "hlaing",
        "မရမ်းကုန်း", "mayangone", "mayangon",
        "အင်းစိန်", "inseIn".lower(),
        "မင်္ဂလာဒုံ", "mingaladon",
        "ရွှေပြည်သာ", "shwepyithar", "shwe pyi thar",
        "လှိုင်သာယာ", "hlaingthaya", "hlaing tharyar", "hlaing thar yar",
        "သင်္ဃန်းကျွန်း", "သဃ်ကျွန်း", "သင်္ဃန်း", "thingangyun", "thingangyun",
        "တောင်ဥက္ကလာ", "တောင်ဥက္ကလာပ", "south okkalapa", "south okkala",
        "မြောက်ဥက္ကလာ", "မြောက်ဥက္ကလာပ", "north okkalapa", "north okkala",
        "သာကေတ", "thaketa",
        "ဒေါပုံ", "dawbon",
        "ပုဇွန်တောင်", "pazundaung",
        "မင်္ဂလာတောင်ညွန့်", "mingala taungnyunt", "mingalar taung nyunt",
        "ဗိုလ်တထောင်", "botahtaung", "botataung",
        "ကျောက်တံတား", "kyauktada",
        "ပန်းဘဲတန်း", "pabedan",
        "လမ်းမတော်", "lanmadaw",
        "လသာ", "latha",
        "အလုံ", "ahlone", "alon",
        "ကြည့်မြင်တိုင်", "kyimyindaing", "kyimyindine",
        "ဒဂုံ", "dagon",
        "ဒဂုံမြို့သစ်မြောက်ပိုင်း", "မြောက်ဒဂုံ", "north dagon",
        "ဒဂုံမြို့သစ်တောင်ပိုင်း", "တောင်ဒဂုံ", "south dagon",
        "ဒဂုံမြို့သစ်အရှေ့ပိုင်း", "အရှေ့ဒဂုံ", "east dagon",
        "ဒဂုံဆိပ်ကမ်း", "dagon seikkan", "dagon seikkan",
        "ဒလ", "dala",
        "ဆိပ်ကြီးခနောင်တို", "seikgyikanaungto", "seikgyi kanaungto",
        "ကိုကိုးကျွန်း", "cocokyun", "coco island",
        "ထန်းတပင်", "htantabin",
        "မှော်ဘီ", "hmawbi",
        "လှည်းကူး", "hleGu".lower(), "hlegu",
        "တိုက်ကြီး", "taikkyi",
        "ကော့မှူး", "kawhmu",
        "ကွမ်းခြံကုန်း", "kungyangon",
        "သန်လျင်", "thanlyin",
        "ကျောက်တန်း", "kyauktan",
        "သုံးခွ", "thongwa",
        "ခရမ်း", "khayan",
        "တွံတေး", "twantay",
        "ကျောက်မြောင်း", "kyaukmyaung",
    )
    if any(word in low for word in yangon_words):
        return "yangon"

    # Explicit non-Yangon places. This list intentionally contains common
    # destination cities; AI can still recognize less-common ones.
    other_words = (
        "နယ်", "မန္တလေး", "mandalay", "နေပြည်တော်", "naypyidaw", "nay pyi taw",
        "ပဲခူး", "bago", "မော်လမြိုင်", "mawlamyine", "မော်လမြိုင်မြို့",
        "တောင်ကြီး", "taunggyi", "မကွေး", "magway", "စစ်ကိုင်း", "sagaing",
        "ပုသိမ်", "pathein", "ပြည်", "pyay", "မုံရွာ", "monywa",
        "မိတ္ထီလာ", "meiktila", "မြင်းခြံ", "myingyan", "ကျောက်ဆည်", "kyaukse",
        "မြိတ်", "myeik", "ထားဝယ်", "dawei", "ကော့သောင်း", "kawthaung",
        "ဘားအံ", "hpa-an", "hpaan", "မြဝတီ", "myawaddy", "tachileik", "တာချီလိတ်",
        "လားရှိုး", "lashio", "ကျိုင်းတုံ", "kengtung", "မူဆယ်", "muse",
        "လွိုင်ကော်", "loikaw", "ဟားခါး", "hakha", "ဖားကန့်", "hpakant",
        "မြစ်ကြီးနား", "myitkyina", "ဗန်းမော်", "bhamo", "စစ်တွေ", "sittwe",
        "သံတွဲ", "thandwe", "ကျောက်ဖြူ", "kyaukphyu", "မောင်တော", "maungdaw",
        "မော်လမြိုင်ကျွန်း", "mawlamyinegyun", "ဟင်္သာတ", "h is not used",
    )
    # Remove a deliberately impossible sentinel without complicating matching.
    other_words = tuple(w for w in other_words if w != "h is not used")
    if any(word in low for word in other_words):
        return "other"

    # Region/state clues are strong non-Yangon signals once Yangon names above
    # have already been ruled out.
    other_region_words = (
        "ကချင်", "ကယား", "ကရင်", "ချင်း", "မွန်", "ရခိုင်", "ရှမ်း",
        "စစ်ကိုင်းတိုင်း", "မကွေးတိုင်း", "မန္တလေးတိုင်း", "ပဲခူးတိုင်း",
        "ဧရာဝတီတိုင်း", "တနင်္သာရီတိုင်း", "နေပြည်တော်",
        "kachin", "kayah", "kayin", "chin", "mon state", "rakhine", "shan",
        "sagaing region", "magway region", "mandalay region", "bago region",
        "ayeyarwady", "tanintharyi",
    )
    if any(word in low for word in other_region_words):
        return "other"

    # A literal city marker such as "...မြို့" (but not only "မြို့နယ်") is
    # usually how up-country buyers identify their town.
    without_township = low.replace("မြို့နယ်", "")
    if "မြို့" in without_township:
        return "other"

    return ""


def infer_delivery_area_for_complete_order(session):
    """
    Final delivery-area rule requested for the shop:

    * explicit Yangon clue -> Yangon
    * explicit non-Yangon clue -> Other
    * if Name + Address + Phone are complete but the address gives no city clue,
      default to Yangon because Yangon customers commonly omit the word Yangon,
      while customers from other cities usually include their city/town.

    The default is applied ONLY to a complete order so a partial address is not
    prematurely classified.
    """
    address = str(session.get("address", "") or "").strip()
    if not address:
        return session

    detected = detect_delivery_area_from_text(address)
    if detected:
        session["delivery_area"] = detected
        return session

    if (
        session.get("name")
        and session.get("address")
        and session.get("phone")
        and session.get("items")
    ):
        session["delivery_area"] = "yangon"
        print("DELIVERY AREA DEFAULTED TO YANGON:", address, flush=True)

    return session


def looks_like_order_progress(message, session=None):
    """Recognize incremental order details after a product has been selected."""
    value = str(message or "").strip()
    if not value:
        return False

    # Product questions / greetings should not be swallowed as order fields.
    if simple_greeting(value) or wants_admin(value) or asks_delivery_time(value):
        return False

    if looks_like_order_details(value):
        return True

    # Phone-only follow-up.
    digits = re.sub(r"\D", "", value)
    if 9 <= len(digits) <= 13:
        return True

    # Slash/newline separated customer details.
    if "/" in value or "\n" in value or "|" in value:
        return True

    session = session or {}
    # After a product has been selected, a short plain-text first message is
    # commonly the buyer's name; the next plain-text message is commonly address.
    if session.get("last_product_code") in PRODUCTS:
        if not session.get("name") and 1 < len(value) <= 80:
            return True
        if session.get("name") and not session.get("address") and len(value) >= 4:
            return True

    return False

def is_order_noise_segment(text):
    """True for purchase/quantity filler that must never become Name or Address."""
    low = str(text or "").strip().lower()
    if not low:
        return True
    compact = re.sub(r"\s+", "", low)
    phrases = (
        "ယူမယ်", "ယူပါမယ်", "ယူချင်တယ်", "ယူချင်ပါတယ်", "လိုချင်တယ်",
        "လိုချင်ပါတယ်", "မှာမယ်", "မှာယူမယ်", "မှာယူပါမယ်", "အော်ဒါတင်မယ်",
        "တခုယူ", "တစ်ခုယူ", "၁ခုယူ", "၁ ခုယူ", "တခုယူမယ်",
        "တစ်ခုယူမယ်", "one", "1pc", "1pcs", "x1", "order", "buy",
    )
    phrase_compact = tuple(re.sub(r"\s+", "", p.lower()) for p in phrases)
    if compact in phrase_compact:
        return True
    if re.fullmatch(r"[xX*]?\s*[0-9၀-၉]+\s*(?:pcs?|ခု|စုံ)?", low, flags=re.IGNORECASE):
        return True
    return False


def extract_order_fields_locally(message, session=None):
    """Fast deterministic parser for one-shot and multi-message orders."""
    value = str(message or "").strip()
    current = session or {}
    result = {
        "name": "",
        "address": "",
        "phone": "",
        "delivery_area": "",
        "items": [],
    }
    if not value:
        return result

    parts = [p.strip() for p in re.split(r"[/\n|]+", value) if p.strip()]

    # Phone: Myanmar 09... or a reasonable 9-13 digit phone-like number.
    phone_match = re.search(r"(?<![0-9])(09[0-9 \t-]{7,12}[0-9])(?![0-9])", value)
    if not phone_match:
        phone_match = re.search(r"(?<![0-9])([0-9][0-9 \t-]{7,12}[0-9])(?![0-9])", value)
    if phone_match:
        result["phone"] = re.sub(r"[^\d+]", "", phone_match.group(1))

    for code, qty in find_codes_and_quantities(value).items():
        if code in PRODUCTS:
            result["items"].append({"code": code, "quantity": qty})

    clean_parts = []
    for part in parts:
        normalized_part_digits = re.sub(r"[^\d+]", "", part)
        if result["phone"] and result["phone"] == normalized_part_digits:
            continue
        if find_codes_and_quantities(part):
            continue
        if is_order_noise_segment(part):
            continue
        clean_parts.append(part)

    # Multi-part: first non-address-looking segment is usually name.
    if clean_parts:
        first = clean_parts[0]
        if not current.get("name") and len(first) <= 80 and not looks_like_order_details(first):
            result["name"] = first
            clean_parts = clean_parts[1:]

    # If the name was already collected in a previous message, all remaining
    # non-phone text is address. This fixes customers sending Name / Address /
    # Phone as three separate messages.
    if clean_parts:
        if current.get("name") or result.get("name"):
            result["address"] = " / ".join(clean_parts).strip()
        elif len(clean_parts) >= 2:
            result["name"] = clean_parts[0]
            result["address"] = " / ".join(clean_parts[1:]).strip()
        elif looks_like_order_details(clean_parts[0]):
            result["address"] = clean_parts[0]
        elif not current.get("name"):
            result["name"] = clean_parts[0]

    # A single address-like line after name should be accepted even if it omits
    # Yangon and does not contain the exact word "လိပ်စာ".
    if current.get("name") and not current.get("address") and not result["address"]:
        leftovers = []
        for part in parts:
            pd = re.sub(r"[^\d+]", "", part)
            if result["phone"] and pd == result["phone"]:
                continue
            if find_codes_and_quantities(part):
                continue
            if is_order_noise_segment(part):
                continue
            leftovers.append(part)
        if leftovers:
            candidate = " / ".join(leftovers).strip()
            if candidate and candidate != current.get("name"):
                result["address"] = candidate

    result["delivery_area"] = detect_delivery_area_from_text(
        result["address"] or value
    )
    return result

def is_quantity_only_message(text):
    value = str(text or "").strip()

    patterns = [
        r"^[xX*]\s*\d+$",
        r"^\d+\s*(?:pcs|pc|ခု|ကဒ်|စုံ)$",
        r"^(?:qty|quantity)\s*[:=]?\s*\d+$",
    ]

    return any(
        re.fullmatch(pattern, value, flags=re.IGNORECASE)
        for pattern in patterns
    )


def merge_order_message(sender_id, message):
    session = get_order_session(sender_id)
    explicit_qty = extract_explicit_quantity(message)

    if not is_quantity_only_message(message):
        local = extract_order_fields_locally(message, session=session)
        session = merge_extracted_order_data(session, local)

        # AI only fills genuinely missing core fields. Do not force AI just
        # because delivery_area is blank; complete unknown-city orders are
        # defaulted to Yangon by the shop rule below.
        still_missing_core = (
            not session.get("name")
            or not session.get("address")
            or not session.get("phone")
        )

        if still_missing_core and OPENAI_API_KEY:
            try:
                extracted = extract_order_fields_with_ai(message)
                session = merge_extracted_order_data(session, extracted)
            except Exception as e:
                print("ORDER AI FALLBACK ERROR:", str(e), flush=True)

    if explicit_qty is not None:
        target_code = session.get("last_product_code")
        coded = find_codes_and_quantities(message)

        if coded:
            for code, qty in coded.items():
                if code in PRODUCTS:
                    session["items"][code] = qty
        elif target_code in PRODUCTS:
            session["items"][target_code] = explicit_qty

        session["quantity_confirmed"] = True

    session = infer_delivery_area_for_complete_order(session)
    return session

def order_missing_fields(session):
    infer_delivery_area_for_complete_order(session)
    missing = []

    if not session.get("name"):
        missing.append("အမည်")

    if not session.get("address"):
        missing.append("လိပ်စာအပြည့်အစုံ")

    if not session.get("phone"):
        missing.append("ဖုန်းနံပါတ်")

    if not session.get("items"):
        missing.append("ပစ္စည်း")

    # Do not make Yangon buyers type "ရန်ကုန်". Once core order details are
    # complete, infer_delivery_area_for_complete_order() resolves the area; an
    # unknown complete address defaults to Yangon by the shop rule.
    return missing



def order_prompt_for_missing(missing):
    if not missing:
        return ""

    if missing == ["ရန်ကုန်/နယ်"]:
        return "ပို့ရမယ့်နေရာက ရန်ကုန်လား၊ နယ်လားရှင်။"

    if missing == ["အရေအတွက်"]:
        return "မှာယူမယ့် အရေအတွက် ဘယ်နှခုလဲရှင်။ ဥပမာ 2 ခု / x2 လို့ ပို့ပေးပါရှင်။"

    return (
        "အော်ဒါတင်ပေးဖို့ "
        + " / ".join(missing)
        + " ကို အပြည့်အစုံပို့ပေးပါရှင်။"
    )



def product_price(code):
    product = PRODUCTS.get(code, {})
    return parse_int_amount(get_row_value(product, "Price"), 0)


def delivery_fee_for_area(area):
    if str(area).lower().strip() == "yangon":
        return DEFAULT_YANGON_DELIVERY

    if str(area).lower().strip() == "other":
        return DEFAULT_OTHER_DELIVERY

    return 0


def first_unavailable_session_item(session):
    """Re-check stock immediately before Telegram so stale sessions cannot order it."""
    for code in list(session.get("items", {}).keys()):
        product = PRODUCTS.get(code)
        if not product:
            return code, "unknown"
        availability = product_availability(product)
        if availability != "in_stock":
            return code, availability
    return "", ""


def unavailable_order_reply(code, availability):
    product = PRODUCTS.get(code, {})
    name = str(get_row_value(product, "Product Name", "Name")).strip()
    if availability == "coming":
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းမရောက်သေးပါရှင်။"
    if availability == "out":
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းကုန်နေပါတယ်ရှင်။"
    return f"Code {code} {name}\nပစ္စည်းအခြေအနေကို Admin က စစ်ဆေးပေးပါမယ်ရှင်။"


def build_telegram_order(session):
    delivery_area = str(session.get("delivery_area", "")).strip().lower()
    delivery_fee = delivery_fee_for_area(delivery_area)

    name = str(session.get("name", "")).strip()
    address = str(session.get("address", "")).strip()
    phone = str(session.get("phone", "")).strip()

    item_parts = []
    subtotal = 0
    total_pcs = 0

    for code, qty in session.get("items", {}).items():
        product = PRODUCTS.get(code, {})
        item_name = str(
            get_row_value(product, "Product Name", "Name")
        ).strip()

        unit_price = product_price(code)
        item_total = unit_price * qty
        subtotal += item_total
        total_pcs += qty

        if qty == 1:
            item_parts.append(
                f"(Code {code}) {item_name} စျေးနှုန်း {unit_price} Ks"
            )
        else:
            item_parts.append(
                f"(Code {code}) {item_name} စျေးနှုန်း {unit_price} Ks * {qty}"
            )

    grand_total = subtotal + delivery_fee
    items_text = " + ".join(item_parts)

    if delivery_area == "yangon":
        delivery_text = f"ပို့ဆောင်ခ ရန်ကုန် {delivery_fee}Ks"
    else:
        delivery_text = f"ပို့ဆောင်ခ နယ် {delivery_fee}Ks"

    cod_text = "COD"
    if total_pcs > 1:
        cod_text = f"COD {total_pcs} PCS"

    return (
        f"{name}/{address}/{phone}/"
        f"{items_text} + {delivery_text} = {grand_total}Ks / {cod_text}"
    )



# =========================
# INTENT / HANDOFF
# =========================
ORDER_WORDS = (
    "order",
    "place an order",
    "make a purchase",
    "can i make a purchase",
    "purchase",
    "buy",
    "i want it",
    "i want this",
    "want this",
    "မှာယူ",
    "မှာမယ်",
    "ယူမယ်",
    "အော်ဒါ",
    "ယူပါမယ်",
    "လိုချင်တယ်",
    "လိုချင်ပါတယ်",
    "ယူချင်တယ်",
    "တစ်ခုယူမယ်",
    "တခုယူမယ်",
    "တစ်ခုယူ",
    "တခုယူ",
    "၁ခုယူမယ်",
    "၁ ခုယူမယ်",
    "၁ခုယူ",
    "၁ ခုယူ",
    "ယူ",
)


GREETING_WORDS = (
    "hi",
    "hello",
    "မင်္ဂလာပါ",
    "ဟလို",
)

ADMIN_TRIGGER_WORDS = (
    "admin",
    "အက်မင်",
    "အက်ဒမင်",
    "လူနဲ့ပြော",
    "လူနဲ့ပြောမယ်",
    "လူနဲ့ပြောချင်",
    "လူနဲ့ဆက်သွယ်",
    "လူပြန်",
    "လူကိုခေါ်",
    "လူခေါ်",
    "ဝန်ထမ်း",
    "တာဝန်ရှိသူ",
    "ပိုင်ရှင်",
    "ဆိုင်ရှင်",
    "staff",
    "human",
    "agent",
    "customer service",
    "ဖုန်းပြော",
    "ဖုန်းဆက်",
    "complaint",
    "တိုင်မယ်",
)


def wants_admin(text):
    lower = str(text or "").lower()
    return any(word in lower for word in ADMIN_TRIGGER_WORDS)


def is_order_message(text):
    lower = str(text or "").lower()
    return any(word in lower for word in ORDER_WORDS)


def simple_greeting(text):
    lower = str(text or "").lower().strip()

    if any(word in lower for word in GREETING_WORDS):
        return "မင်္ဂလာပါရှင်။ ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"

    return ""


def handoff_to_admin(sender_id):
    send_facebook_text(
        sender_id,
        "ဒီမေးခွန်းကို Admin က ဆက်လက်ဖြေကြားပေးပါမယ်ရှင်။"
    )

    # After this exact sentence is sent, a human Admin should take over.
    # The bot stays silent for ADMIN_PAUSE_MINUTES after a manual Page reply.
    pause_for_admin(sender_id)




def generic_purchase_intent(text):
    """Order/purchase wording that does not identify a product by itself."""
    low = str(text or "").strip().lower()
    return bool(low and is_order_message(low))


def extract_product_context_from_manychat(data):
    """
    Read product context supplied by ManyChat/Meta ad flows.

    IMPORTANT: the Python service cannot see Messenger's visual "View ad" card
    by itself. ManyChat must include an ad/code/ref/title/custom-field value in
    the POST body. Once any such value is sent, this function resolves it to a
    catalog product and keeps the product in the customer's order session.
    """
    if not isinstance(data, dict):
        return "", None

    preferred_keys = (
        "product_code", "code", "ad_code", "source_code", "ref_code",
        "ad_context", "ad_id", "ad_ref", "referral", "referral_payload",
        "ad_name", "ad_title", "ad_headline", "ad_message",
        "ad_description", "source", "ref", "campaign_name", "adset_name",
        "manychat_ad_product", "last_ad_product",
    )

    def match_value(value):
        text = str(value or "").strip()
        if not text:
            return "", None
        code, product = find_product_by_code(text)
        if product:
            return code, product
        code, product = find_product_by_name(text)
        if product:
            return code, product
        return "", None

    for key in preferred_keys:
        if key not in data:
            continue
        value = data.get(key)
        # Referral/context objects may contain nested ad payloads.
        if isinstance(value, dict):
            for nested_key in ("ref", "payload", "ad_id", "ad_name", "title", "headline", "product_code", "code"):
                if nested_key in value:
                    code, product = match_value(value.get(nested_key))
                    if product:
                        print("AD CONTEXT MATCH:", key, nested_key, code, flush=True)
                        return code, product
        else:
            code, product = match_value(value)
            if product:
                print("AD CONTEXT MATCH:", key, code, flush=True)
                return code, product

    # Last-resort scan of all simple scalar fields except the customer's text/id.
    for key, value in data.items():
        if key in ("message", "contact_id", "image_url", "attachment_url"):
            continue
        if isinstance(value, (str, int, float)):
            code, product = match_value(value)
            if product:
                print("AD CONTEXT SCALAR MATCH:", key, code, flush=True)
                return code, product

    return "", None


def finalize_manychat(contact_id, response):
    """
    Remember that ManyChat is about to send a reply for this customer.
    This prevents the Page echo of that automated reply from being mistaken
    for a manual Admin takeover.
    """
    try:
        messages = (
            response.get("content", {}).get("messages", [])
            if isinstance(response, dict)
            else []
        )
        if contact_id and messages:
            MANYCHAT_ECHO_IGNORE_UNTIL[str(contact_id)] = (
                now_ts() + MANYCHAT_ECHO_GRACE_SECONDS
            )
    except Exception as e:
        print("MANYCHAT FINALIZE WARNING:", str(e), flush=True)

    return response


# =========================
# MANYCHAT DYNAMIC BLOCK
# =========================
def manychat_response(messages=None):
    return {
        "version": "v2",
        "content": {
            "messages": messages or [],
            "actions": [],
            "quick_replies": [],
        },
    }


def manychat_text(text):
    return manychat_response(
        [{"type": "text", "text": str(text or "")}]
    )


def manychat_product_response(code, product):
    messages = []

    image_url = product_public_image_url(code, product)
    if image_url:
        print("MANYCHAT PUBLIC IMAGE URL:", image_url, flush=True)
        messages.append({"type": "image", "url": image_url})

    detail = product_detail(product)
    if detail:
        messages.append({"type": "text", "text": detail})

    messages.append({"type": "text", "text": product_reply(code, product)})

    # Never invite an order for stock that is not sellable.
    if product_is_sellable(product):
        messages.append(
            {
                "type": "text",
                "text": (
                    'မှာယူလိုပါက အမည် / လိပ်စာအပြည့်အစုံ / ဖုန်းနံပါတ် ကို အပြည့်အစုံရေးပို့ပေးပါရှင်။'
                ),
            }
        )

    return manychat_response(messages)



def asks_delivery_time(text):
    low = str(text or "").strip().lower()

    phrases = (
        "ဘယ်လောက်ကြာ",
        "ဘယ်နှရက်",
        "ဘယ်နရက်",
        "ဘယ်တော့ရောက်",
        "ဘယ်တော့ရောက်မလဲ",
        "ဘယ်အချိန်ရောက်",
        "ကြာမလား",
        "ရောက်ဖို့ကြာ",
        "delivery time",
        "how long",
        "how many days",
        "when arrive",
        "when will arrive",
    )

    return any(phrase in low for phrase in phrases)


def delivery_time_reply(text):
    low = str(text or "").lower()

    yangon_words = (
        "ရန်ကုန်", "တာမွေ", "တောင်ဥက္ကလာ", "မြောက်ဥက္ကလာ",
        "သင်္ဃန်းကျွန်း", "လှိုင်", "ကမာရွတ်", "မရမ်းကုန်း",
        "yangon", "tamwe", "tarmwe", "thingangyun",
    )

    other_words = (
        "နယ်", "မန္တလေး", "နေပြည်တော်", "ပဲခူး", "မော်လမြိုင်",
        "တောင်ကြီး", "မကွေး", "စစ်ကိုင်း", "ပုသိမ်", "ပြည်",
        "mandalay", "naypyidaw", "bago", "mawlamyine",
    )

    if any(word in low for word in yangon_words):
        return "ရန်ကုန်ဆို ၃ ရက်မှ ၅ ရက်အတွင်း ရောက်ပါတယ်ရှင်။"

    if any(word in low for word in other_words):
        return "နယ်ဆို ၄ ရက်မှ ၁၀ ရက်အတွင်း ရောက်ပါတယ်ရှင်။"

    return (
        "ရန်ကုန်ဆို ၃ ရက်မှ ၅ ရက်အတွင်း၊ "
        "နယ်ဆို ၄ ရက်မှ ၁၀ ရက်အတွင်း ရောက်ပါတယ်ရှင်။"
    )


def handle_manychat_request(data):
    """
    ManyChat body:
      message      = Last Text Input
      contact_id   = Contact Id
      image_url    = optional customer attachment URL
      product_code/ad_code/ad_context/ad_name = optional Ad context from ManyChat

    Rules:
    - Unclear product => exactly "ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"
    - Product => image, detail, price, then exact order-info prompt.
    - No quantity supplied => defaults to 1 item.
    - Name/address/phone completion => buyer confirmation + Telegram.
    - Explicit Admin request only => Admin handoff/pause for that customer.
    """
    load_products()

    message = str(data.get("message", "") or "").strip()
    contact_id = str(data.get("contact_id", "") or "").strip()
    incoming_image_url = str(
        data.get("image_url", "")
        or data.get("attachment_url", "")
        or data.get("last_image_url", "")
        or data.get("customer_image_url", "")
        or data.get("image", "")
        or data.get("file_url", "")
        or ""
    ).strip()

    # ManyChat can sometimes put a Facebook CDN image URL into Last Text Input.
    # Treat that as an image instead of customer text.
    low_message = message.lower()
    if (
        not incoming_image_url
        and low_message.startswith(("http://", "https://"))
        and any(token in low_message for token in ("scontent", ".jpg", ".jpeg", ".png", ".webp", "fbcdn"))
    ):
        incoming_image_url = message
        message = ""
        print("MANYCHAT MESSAGE PROMOTED TO IMAGE URL", flush=True)

    print(
        "MANYCHAT INPUT:",
        {"message": message, "contact_id": contact_id, "image_url": incoming_image_url},
        flush=True,
    )

    ad_debug = {k: data.get(k) for k in (
        "product_code", "ad_code", "ad_context", "ad_id", "ad_ref",
        "referral", "ad_name", "ad_title", "ad_headline", "campaign_name",
        "manychat_ad_product", "last_ad_product"
    ) if data.get(k) not in (None, "", {})}
    if ad_debug:
        print("MANYCHAT AD CONTEXT:", ad_debug, flush=True)

    def done(response):
        return finalize_manychat(contact_id, response)

    if not contact_id:
        return manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။")

    # Pause is per customer only. Other customers continue normally.
    if admin_is_active(contact_id):
        print("MANYCHAT ADMIN ACTIVE - BOT SILENT:", contact_id, flush=True)
        return manychat_response([])

    session = get_order_session(contact_id)

    # Explicit human/Admin request only.
    if wants_admin(message):
        pause_for_admin(contact_id)
        return done(manychat_text(
            "ဒီမေးခွန်းကို Admin က ဆက်လက်ဖြေကြားပေးပါမယ်ရှင်။"
        ))

    if asks_delivery_time(message):
        return done(manychat_text(delivery_time_reply(message)))

    # -------- Product recognition --------
    # 0) Optional ad/product context.
    context_code, context_product = extract_product_context_from_manychat(data)

    # 1) Direct code/name from current message.
    code, product = find_product_by_code(message)
    if not product:
        code, product = find_product_by_name(message)

    # 2) Customer image/screenshot.
    if not product and incoming_image_url:
        code, product = ai_find_product_from_image(
            incoming_image_url,
            message,
        )

    # If this is only a generic purchase phrase and there is no remembered/ad
    # product context, DO NOT ask OpenAI to guess a product.
    generic_order = generic_purchase_intent(message)

    # 3) Description / Burmese / English / Chinese text.
    if not product and message and not generic_order:
        code, product = ai_find_product_from_text(message)

    # Use optional Ad context if current message did not identify a product.
    if not product and context_product:
        code, product = context_code, context_product

    # A bare product code/name is browsing and refreshes the current product.
    if product and is_pure_product_query(message, code, product):
        ORDER_SESSIONS[contact_id] = new_order_session()
        if product_is_sellable(product):
            ORDER_SESSIONS[contact_id]["last_product_code"] = code
        else:
            print("PRODUCT NOT SELLABLE:", code, product_status(product), flush=True)
        return done(manychat_product_response(code, product))

    if product:
        if product_is_sellable(product):
            session["last_product_code"] = code
        else:
            # Unavailable products can be shown, but never become an order item.
            session["last_product_code"] = ""
            session["items"] = {}
            return done(manychat_product_response(code, product))

    last_product_ready = session.get("last_product_code") in PRODUCTS

    active_order = bool(
        session.get("items")
        or session.get("name")
        or session.get("address")
        or session.get("phone")
        or session.get("quantity_confirmed")
    )

    # Generic "လိုချင်ပါတယ်" / "Can I make a purchase?" with no product known:
    # never hand off to Admin and never guess a product.
    if generic_order and not product and not last_product_ready and not active_order:
        return done(manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"))

    order_intent = (
        generic_order
        or active_order
        or (last_product_ready and looks_like_order_details(message))
        or (last_product_ready and looks_like_order_progress(message, session))
        or (last_product_ready and bool(incoming_image_url))
        or (extract_explicit_quantity(message) is not None and last_product_ready)
    )

    # -------- Order collection --------
    if order_intent:
        if product and generic_order:
            session["items"].setdefault(code, 1)

        # If one product was browsed/identified, quantity omitted means 1 piece.
        if not session["items"] and last_product_ready:
            session["items"][session["last_product_code"]] = 1
            session["quantity_confirmed"] = True

        # If no product is known yet, ask only which product.
        if not session["items"] and not last_product_ready:
            return done(manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"))

        if message and not (
            generic_order
            and not looks_like_order_details(message)
            and extract_explicit_quantity(message) is None
        ):
            print("ORDER TEXT RECEIVED:", message, flush=True)
            session = merge_order_message(contact_id, message)
            print("ORDER SESSION AFTER TEXT:", session, flush=True)

        if incoming_image_url:
            extracted_from_image = extract_order_fields_from_image(
                incoming_image_url,
                message,
            )
            session = merge_extracted_order_data(
                session,
                extracted_from_image,
            )
            session = infer_delivery_area_for_complete_order(session)

        explicit_qty = extract_explicit_quantity(message)
        if explicit_qty is not None:
            target_code = session.get("last_product_code")
            coded = find_codes_and_quantities(message)

            if coded:
                for item_code, qty in coded.items():
                    if item_code in PRODUCTS:
                        session["items"][item_code] = qty
            elif target_code in PRODUCTS:
                session["items"][target_code] = explicit_qty

            session["quantity_confirmed"] = True

        session = infer_delivery_area_for_complete_order(session)
        missing = order_missing_fields(session)
        print("ORDER MISSING:", missing, flush=True)

        if missing:
            return done(manychat_text(order_prompt_for_missing(missing)))

        bad_code, bad_status = first_unavailable_session_item(session)
        if bad_code:
            ORDER_SESSIONS[contact_id] = new_order_session()
            return done(manychat_text(unavailable_order_reply(bad_code, bad_status)))

        telegram_text = build_telegram_order(session)
        print("TELEGRAM ORDER TEXT:", telegram_text, flush=True)

        if send_telegram_message(telegram_text):
            buyer_text = buyer_order_confirmation(session)
            ORDER_SESSIONS[contact_id] = new_order_session()
            return done(manychat_text(buyer_text))

        # Telegram failure should not permanently silence the bot.
        # Keep the order data and tell the buyer Admin will handle it.
        return done(manychat_text(
            "အော်ဒါအချက်အလက် ရရှိပါပြီရှင်။ Admin က ဆက်လက်စစ်ဆေးပေးပါမယ်ရှင်။"
        ))

    # Product recognized from non-pure text/image.
    if product:
        return done(manychat_product_response(code, product))

    greeting = simple_greeting(message)
    if greeting:
        return done(manychat_text(greeting))

    # User's required unclear-product fallback. Never auto-pause Admin here.
    return done(manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"))


# =========================
# DEDUP
# =========================
def is_duplicate_message(message_data):
    mid = str(message_data.get("mid", "") or "").strip()

    if not mid:
        return False

    if mid in PROCESSED_MESSAGE_IDS:
        print("DUPLICATE CUSTOMER MESSAGE IGNORED:", mid, flush=True)
        return True

    PROCESSED_MESSAGE_IDS.add(mid)

    if len(PROCESSED_MESSAGE_IDS) > 5000:
        PROCESSED_MESSAGE_IDS.clear()
        PROCESSED_MESSAGE_IDS.add(mid)

    return False


# =========================
# ROUTES
# =========================
@app.route("/", methods=["GET"])
def home():
    return f"Facebook AI Bot is running! {BOT_VERSION}", 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403



@app.route("/product-image/<code>", methods=["GET"])
def product_image_proxy(code):
    load_products()

    code = normalize_code(code)
    product = PRODUCTS.get(code)

    if not product:
        return "Not found", 404

    source_url = product_image_url(product)
    image_bytes, content_type = download_image_bytes(source_url)

    if not image_bytes:
        return "Image unavailable", 404

    response = Response(image_bytes, mimetype=content_type or "image/jpeg")
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)

    print("=== WEBHOOK POST RECEIVED ===", flush=True)
    print(data, flush=True)

    # ManyChat Dynamic Block request.
    if isinstance(data, dict) and "message" in data and "contact_id" in data:
        return handle_manychat_request(data), 200

    # Unknown non-Meta request.
    if not data or data.get("object") != "page":
        return manychat_text("EVENT_RECEIVED"), 200

    load_products()

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            message_data = event.get("message", {})

            if not message_data:
                continue

            # Page/Admin/Bot outgoing messages.
            if message_data.get("is_echo"):
                handle_echo_message(event, message_data)
                continue

            if is_duplicate_message(message_data):
                continue

            sender_id = str(
                event.get("sender", {}).get("id", "")
            ).strip()

            if not sender_id:
                continue

            # If Admin is handling this customer, bot stays completely silent.
            if admin_is_active(sender_id):
                print("ADMIN ACTIVE - BOT SILENT:", sender_id, flush=True)
                continue

            text = str(message_data.get("text", "") or "").strip()
            attachments = message_data.get("attachments", []) or []

            incoming_image_url = ""

            for attachment in attachments:
                if attachment.get("type") == "image":
                    incoming_image_url = str(
                        attachment.get("payload", {}).get("url", "")
                    ).strip()

                    if incoming_image_url:
                        break

            print("CUSTOMER:", sender_id, flush=True)
            print("TEXT:", text, flush=True)
            print("IMAGE:", incoming_image_url, flush=True)

            session = get_order_session(sender_id)

            # ---------------------------------
            # 1) DIRECT CODE
            # ---------------------------------
            code, product = find_product_by_code(text)

            # ---------------------------------
            # 2) DIRECT PRODUCT NAME
            # ---------------------------------
            if not product:
                code, product = find_product_by_name(text)

            # ---------------------------------
            # 3) CUSTOMER IMAGE / SCREENSHOT
            # ---------------------------------
            if not product and incoming_image_url:
                code, product = ai_find_product_from_image(
                    incoming_image_url,
                    text,
                )

            # ---------------------------------
            # 4) DESCRIPTION / OTHER LANGUAGE
            # ---------------------------------
            if not product and text:
                code, product = ai_find_product_from_text(text)

            # ---------------------------------
            # EXPLICIT ADMIN REQUEST
            # ---------------------------------
            if wants_admin(text):
                handoff_to_admin(sender_id)
                continue

            if asks_delivery_time(text):
                send_facebook_text(
                    sender_id,
                    delivery_time_reply(text),
                )
                continue

            # ---------------------------------
            # PRODUCT / ORDER HANDLING
            # Keep one customer-facing text reply per incoming message.
            # Product photo + its single text reply still count as one product response.
            # ---------------------------------
            if product and is_pure_product_query(text, code, product):
                ORDER_SESSIONS[sender_id] = new_order_session()
                ORDER_SESSIONS[sender_id]["last_product_code"] = code
                send_product_response(sender_id, code, product)
                continue

            if product:
                session["last_product_code"] = code

            active_order = bool(
                session.get("items")
                or session.get("name")
                or session.get("address")
                or session.get("phone")
            )

            last_product_ready = session.get("last_product_code") in PRODUCTS

            generic_order = generic_purchase_intent(text)

            if generic_order and not product and not last_product_ready and not active_order:
                send_facebook_text(
                    sender_id,
                    "ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။",
                )
                continue

            order_intent = (
                generic_order
                or active_order
                or (last_product_ready and looks_like_order_details(text))
                or (last_product_ready and bool(incoming_image_url))
                or (extract_explicit_quantity(text) is not None and last_product_ready)
            )

            if order_intent:
                if product and is_order_message(text):
                    session["items"].setdefault(code, 1)

                if not session["items"] and last_product_ready:
                    session["items"][session["last_product_code"]] = 1
                    session["quantity_confirmed"] = True

                if text:
                    session = merge_order_message(sender_id, text)

                if incoming_image_url:
                    extracted_from_image = extract_order_fields_from_image(
                        incoming_image_url,
                        text,
                    )
                    session = merge_extracted_order_data(
                        session,
                        extracted_from_image,
                    )

                explicit_qty = extract_explicit_quantity(text)
                if explicit_qty is not None:
                    target_code = session.get("last_product_code")
                    coded = find_codes_and_quantities(text)

                    if coded:
                        for item_code, qty in coded.items():
                            if item_code in PRODUCTS:
                                session["items"][item_code] = qty
                    elif target_code in PRODUCTS:
                        session["items"][target_code] = explicit_qty

                    session["quantity_confirmed"] = True

                missing = order_missing_fields(session)

                if missing:
                    send_facebook_text(
                        sender_id,
                        order_prompt_for_missing(missing),
                    )
                else:
                    telegram_text = build_telegram_order(session)

                    if send_telegram_message(telegram_text):
                        send_facebook_text(
                            sender_id,
                            buyer_order_confirmation(session),
                        )
                        ORDER_SESSIONS[sender_id] = new_order_session()
                    else:
                        handoff_to_admin(sender_id)

                continue

            # Product question only: send product image + one text reply.
            if product:
                send_product_response(sender_id, code, product)
                continue

            # ---------------------------------
            # SIMPLE GREETING ONLY
            # ---------------------------------
            greeting = simple_greeting(text)

            if greeting:
                send_facebook_text(sender_id, greeting)
                continue

            # ---------------------------------
            # UNCLEAR PRODUCT
            # User requirement: do not guess and do not auto-handoff.
            # ---------------------------------
            send_facebook_text(
                sender_id,
                "ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။",
            )

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
    )





