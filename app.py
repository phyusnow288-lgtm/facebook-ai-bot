import os
import re
import csv
import json
import time
import base64
import mimetypes
import requests
from flask import Flask, request

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


def load_products(force=False):
    global PRODUCTS, LAST_PRODUCT_REFRESH

    if not GOOGLE_SHEET_URL:
        print("GOOGLE_SHEET_URL IS MISSING", flush=True)
        return

    if not force and PRODUCTS and now_ts() - LAST_PRODUCT_REFRESH < PRODUCT_REFRESH_SECONDS:
        return

    try:
        response = requests.get(sheet_export_url(GOOGLE_SHEET_URL), timeout=60)
        response.raise_for_status()

        reader = csv.DictReader(response.text.splitlines())

        products = {}

        for row in reader:
            code = normalize_code(get_row_value(row, "Code", "code", "CODE"))
            if code:
                products[code] = row

        PRODUCTS = products
        LAST_PRODUCT_REFRESH = now_ts()

        print("PRODUCTS LOADED:", len(PRODUCTS), flush=True)
        print("PRODUCT CODES:", list(PRODUCTS.keys()), flush=True)

    except Exception as e:
        print("SHEET ERROR:", str(e), flush=True)


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
    url = str(url or "").strip()
    if not url:
        return ""

    file_id = google_drive_file_id(url)
    if file_id:
        return f"https://drive.google.com/uc?export=view&id={file_id}"

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
            timeout=60,
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
        get_row_value(product, "Stock status", "Stock Status", "Status")
    ).strip().lower()


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

    status = product_status(product)

    if status in ("out of stock", "sold out", "out"):
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းကုန်နေပါတယ်ရှင်။"

    if status in ("coming soon", "coming"):
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းမရောက်သေးပါရှင်။"

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
    # Download the Google Drive product photo server-side and upload it to Meta.
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

        # Fallback if multipart upload fails.
        if not sent:
            send_facebook_image_url(recipient_id, image_url)

    send_facebook_text(recipient_id, product_reply(code, product))


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
    A Page-sent message creates an echo webhook event.
    Bot-sent message IDs are tracked and ignored.
    Any other Page-sent message is treated as a manual Admin reply,
    so the bot pauses for that customer.
    """
    mid = str(message_data.get("mid", "") or "").strip()

    if mid and mid in BOT_SENT_MESSAGE_IDS:
        BOT_SENT_MESSAGE_IDS.discard(mid)
        print("BOT ECHO IGNORED:", mid, flush=True)
        return

    customer_id = (
        event.get("recipient", {}).get("id")
        or event.get("sender", {}).get("id")
    )

    pause_for_admin(customer_id)

    print("MANUAL ADMIN MESSAGE DETECTED:", customer_id, flush=True)


# =========================
# ORDER SESSION
# =========================
def new_order_session():
    return {
        "name": "",
        "address": "",
        "phone": "",
        "delivery_area": "",
        "items": {},
        "last_product_code": "",
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


def merge_order_message(sender_id, message):
    session = get_order_session(sender_id)

    for code, qty in find_codes_and_quantities(message).items():
        session["items"][code] = qty

    extracted = extract_order_fields_with_ai(message)

    for field in ("name", "address", "phone", "delivery_area"):
        value = str(extracted.get(field, "") or "").strip()

        if value:
            session[field] = value

    for item in extracted.get("items", []) or []:
        code = normalize_code(item.get("code", ""))

        if code not in PRODUCTS:
            continue

        try:
            qty = max(1, int(item.get("quantity", 1)))
        except Exception:
            qty = 1

        session["items"][code] = qty

    return session


def order_missing_fields(session):
    missing = []

    if not session.get("name"):
        missing.append("အမည်")

    if not session.get("address"):
        missing.append("လိပ်စာ")

    if not session.get("phone"):
        missing.append("ဖုန်းနံပါတ်")

    if not session.get("items"):
        missing.append("ပစ္စည်း")

    if not session.get("delivery_area"):
        missing.append("ရန်ကုန်/နယ်")

    return missing


def order_prompt_for_missing(missing):
    if not missing:
        return ""

    if missing == ["ရန်ကုန်/နယ်"]:
        return "ပို့ရမယ့်နေရာက ရန်ကုန်လား၊ နယ်လားရှင်။"

    return (
        "အော်ဒါအတွက် "
        + " / ".join(missing)
        + " လေးပို့ပေးပါရှင်။"
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


def build_telegram_order(session):
    delivery_area = str(session.get("delivery_area", "")).strip().lower()
    delivery_fee = delivery_fee_for_area(delivery_area)

    name = session.get("name", "")
    address = session.get("address", "")
    phone = session.get("phone", "")

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
                f"{code} {item_name} {unit_price:,} Ks"
            )
        else:
            item_parts.append(
                f"{code} {item_name} x{qty} = {item_total:,} Ks"
            )

    grand_total = subtotal + delivery_fee

    items_text = " + ".join(item_parts)

    if len(session.get("items", {})) > 1:
        cod_text = f"COD {len(session['items'])} Items"

        if total_pcs != len(session["items"]):
            cod_text += f" / {total_pcs} PCS"

    elif total_pcs > 1:
        cod_text = f"COD {total_pcs} PCS"

    else:
        cod_text = "COD"

    return (
        f"{name} / {address} / {phone} / "
        f"{items_text} + Deli {delivery_fee:,} Ks "
        f"= Total {grand_total:,} Ks / {cod_text}"
    )


# =========================
# INTENT / HANDOFF
# =========================
ORDER_WORDS = (
    "order",
    "မှာယူ",
    "မှာမယ်",
    "ယူမယ်",
    "အော်ဒါ",
    "ယူပါမယ်",
    "လိုချင်တယ်",
    "လိုချင်ပါတယ်",
    "ယူချင်တယ်",
)

GREETING_WORDS = (
    "hi",
    "hello",
    "မင်္ဂလာပါ",
    "ဟလို",
)


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
        "Admin ကို လွှဲပေးထားပါတယ်ရှင်။"
    )

    # Stay out of the conversation while admin handles it.
    pause_for_admin(sender_id)


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
    return "Facebook AI Bot is running!", 200


@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200

    return "Verification failed", 403


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)

    print("=== WEBHOOK POST RECEIVED ===", flush=True)
    print(data, flush=True)

   if not data or data.get("object") != "page":
        return {
            "version": "v2",
            "content": {
                "messages": [
                    {
                        "type": "text",
                        "text": "EVENT_RECEIVED"
                    }
                ]
            }
        }, 200

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
            # PRODUCT FOUND
            # ---------------------------------
            if product:
                session["last_product_code"] = code
                send_product_response(sender_id, code, product)

                # If the same message also says "ယူမယ် / order", start order.
                if is_order_message(text):
                    session["items"].setdefault(code, 1)

            # ---------------------------------
            # ORDER COLLECTION
            # ---------------------------------
            active_order = bool(
                session.get("items")
                or session.get("name")
                or session.get("address")
                or session.get("phone")
            )

            if is_order_message(text) or active_order:
                session = merge_order_message(sender_id, text)

                # Customer says "ယူမယ်" after viewing a product.
                if (
                    is_order_message(text)
                    and not session["items"]
                    and session.get("last_product_code") in PRODUCTS
                ):
                    session["items"][session["last_product_code"]] = 1

                missing = order_missing_fields(session)

                if missing:
                    # If product details were already sent, this is the only extra line.
                    send_facebook_text(
                        sender_id,
                        order_prompt_for_missing(missing),
                    )

                else:
                    telegram_text = build_telegram_order(session)

                    if send_telegram_message(telegram_text):
                        send_facebook_text(
                            sender_id,
                            "အော်ဒါတင်ပြီးပါပြီရှင်။ ကျေးဇူးတင်ပါတယ်ရှင်။",
                        )

                        ORDER_SESSIONS[sender_id] = new_order_session()

                continue

            # ---------------------------------
            # IF PRODUCT WAS FOUND, WE ARE DONE.
            # ---------------------------------
            if product:
                continue

            # ---------------------------------
            # SIMPLE GREETING ONLY
            # ---------------------------------
            greeting = simple_greeting(text)

            if greeting:
                send_facebook_text(sender_id, greeting)
                continue

            # ---------------------------------
            # ANY OTHER QUESTION -> ADMIN
            # No extra product-use explanation from AI.
            # ---------------------------------
            handoff_to_admin(sender_id)

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
    )

