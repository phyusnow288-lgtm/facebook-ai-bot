import os
import re
import csv
import json
import requests
from flask import Flask, request

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_URL = os.environ.get("GOOGLE_SHEET_URL")

PRODUCTS = {}
ORDER_SESSIONS = {}



def normalize_code(value):
    value = str(value or "").strip()
    if not value:
        return ""
    value = re.sub(r"\.0$", "", value)
    if value.isdigit():
        return value.zfill(4)
    return value.lower()


def load_products():
    global PRODUCTS
    try:
        if not GOOGLE_SHEET_URL:
            print("GOOGLE_SHEET_URL IS MISSING", flush=True)
            return

        url = GOOGLE_SHEET_URL
        if "/edit" in url:
            url = url.split("/edit")[0] + "/export?format=csv"

        response = requests.get(url, timeout=20)
        response.raise_for_status()
        reader = csv.DictReader(response.text.splitlines())

        products = {}
        for row in reader:
            code = normalize_code(row.get("Code", ""))
            if code:
                products[code] = row

        PRODUCTS = products
        print("PRODUCTS LOADED:", len(PRODUCTS), flush=True)
        print("PRODUCT CODES:", list(PRODUCTS.keys()), flush=True)

    except Exception as e:
        print("SHEET ERROR:", str(e), flush=True)


load_products()


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


def find_product_by_code(message):
    if not message:
        return None, None

    text = str(message).strip()

    for number in re.findall(r"\d+", text):
        code = normalize_code(number)
        if code in PRODUCTS:
            return code, PRODUCTS[code]

    for code, product in PRODUCTS.items():
        if code in text:
            return code, product

    return None, None


def find_product_by_name(message):
    if not message:
        return None, None

    text = str(message).lower().strip()
    best_code = None
    best_product = None
    best_length = 0

    for code, product in PRODUCTS.items():
        names = [
            product.get("Product Name", ""),
            product.get("Name", ""),
            product.get("English Name", ""),
            product.get("Myanmar Name", ""),
            product.get("Chinese Name", ""),
        ]
        for name in names:
            name = str(name or "").lower().strip()
            if len(name) >= 3 and name in text and len(name) > best_length:
                best_length = len(name)
                best_code = code
                best_product = product

    return best_code, best_product


def product_catalog_for_ai():
    rows = []
    for code, product in PRODUCTS.items():
        name = product.get("Product Name", "")
        price = product.get("Price", "")
        description = product.get("Description", "")
        rows.append(
            f"Code {code} | Name: {name} | Price: {price} | Description: {description}"
        )
    return "\n".join(rows)


def google_drive_direct_url(url):
    url = str(url or "").strip()
    if not url:
        return ""

    match = re.search(r"/file/d/([^/]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    match = re.search(r"[?&]id=([^&]+)", url)
    if match:
        file_id = match.group(1)
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    return url


def image_catalog_for_ai(limit=16):
    items = []
    for code, product in PRODUCTS.items():
        image_url = (
            product.get("Image URL", "")
            or product.get("ImageURL", "")
            or product.get("image_url", "")
        )
        image_url = google_drive_direct_url(image_url)
        if not image_url:
            continue

        items.append(
            {
                "code": code,
                "name": str(product.get("Product Name", "")).strip(),
                "image_url": image_url,
            }
        )

        if len(items) >= limit:
            break

    return items


def download_image_as_data_url(url):
    if not url:
        return ""

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "image/jpeg")
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"

        import base64
        encoded = base64.b64encode(response.content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"
    except Exception as e:
        print("IMAGE DOWNLOAD ERROR:", str(e), flush=True)
        return ""


def get_product_image_url(product):
    image_url = (
        product.get("Image URL", "")
        or product.get("ImageURL", "")
        or product.get("image_url", "")
    )
    return google_drive_direct_url(image_url)


def product_reply(code, product):
    name = str(product.get("Product Name", "")).strip()

    def amount(key, default):
        try:
            return int(float(str(product.get(key, default)).replace(",", "").strip() or default))
        except Exception:
            return default

    price = amount("Price", 0)
    yangon_delivery = amount("Yangon Delivery", 5000)
    other_delivery = amount("Other City Delivery", 7500)

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

def parse_json_answer(answer):
    answer = str(answer or "").strip()

    # Avoid markdown-fence replacement code that was breaking during copy/paste.
    start = answer.find("{")
    end = answer.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {}
    try:
        return json.loads(answer[start:end + 1])
    except Exception:
        return {}


def ai_find_product(message):
    if not OPENAI_API_KEY:
        return None, None

    catalog = product_catalog_for_ai()
    system_prompt = f"""
You identify which shop product the customer means.
The customer may write in Burmese, English, Chinese, misspell a name,
or describe the product instead of giving a code.

PRODUCT CATALOG:
{catalog}

Return only a JSON object.
If one product is clearly identified, return a code field.
If uncertain, return a null code.
Never invent a product code.
"""

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": str(message)},
        ],
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        print("AI PRODUCT SEARCH STATUS:", response.status_code, flush=True)
        if response.status_code != 200:
            print(response.text, flush=True)
            return None, None

        answer = response.json()["choices"][0]["message"]["content"]
        result = parse_json_answer(answer)
        code = result.get("code")

        if not code:
            return None, None

        code = normalize_code(code)
        if code in PRODUCTS:
            return code, PRODUCTS[code]

    except Exception as e:
        print("AI PRODUCT SEARCH ERROR:", str(e), flush=True)

    return None, None


def ai_find_product_from_image(image_url, caption=""):
    if not OPENAI_API_KEY:
        return None, None

    customer_data_url = download_image_as_data_url(image_url)
    if not customer_data_url:
        return None, None

    catalog_items = image_catalog_for_ai()

    content = [
        {
            "type": "text",
            "text": (
                "You are matching a customer's product image to this shop's catalog. "
                "First inspect the CUSTOMER IMAGE. Then compare it with the REFERENCE "
                "IMAGES below. Return only one JSON object with a code field. "
                "If no reference is a confident match, use null. Never invent a code. "
                f"Customer text: {caption}"
            ),
        },
        {
            "type": "text",
            "text": "CUSTOMER IMAGE:",
        },
        {
            "type": "image_url",
            "image_url": {"url": customer_data_url},
        },
    ]

    for item in catalog_items:
        ref_data_url = download_image_as_data_url(item["image_url"])
        if not ref_data_url:
            continue

        content.append(
            {
                "type": "text",
                "text": f'REFERENCE PRODUCT: Code {item["code"]} | {item["name"]}',
            }
        )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": ref_data_url},
            }
        )

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": content,
            }
        ],
        "max_tokens": 100,
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )

        print("IMAGE AI STATUS:", response.status_code, flush=True)
        print("IMAGE AI RESPONSE:", response.text, flush=True)

        if response.status_code != 200:
            return None, None

        answer = response.json()["choices"][0]["message"]["content"]
        result = parse_json_answer(answer)
        code = result.get("code")

        if not code:
            return None, None

        code = normalize_code(code)
        if code in PRODUCTS:
            return code, PRODUCTS[code]

    except Exception as e:
        print("IMAGE AI ERROR:", str(e), flush=True)

    return None, None


def normal_ai_reply(message):
    if not OPENAI_API_KEY:
        return "ခဏလေးစောင့်ပေးပါရှင်။"

    catalog = product_catalog_for_ai()
    system_prompt = f"""
You are the sales assistant for Snow Phyu online shopping.
Always reply in natural Burmese. Keep replies short and polite.

Never invent prices, product codes, delivery fees, or stock information.
Use only this product catalog:

{catalog}

If the customer clearly asks about a product, answer using catalog information.
If the product is unclear, ask which product they want.
If the customer wants to buy, ask for name, address, phone number,
product code, and quantity.
"""

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": str(message or "")},
        ],
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        print("OPENAI STATUS:", response.status_code, flush=True)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"].strip()

        print("OPENAI ERROR:", response.text, flush=True)

    except Exception as e:
        print("OPENAI EXCEPTION:", str(e), flush=True)

    return "ခဏလေးစောင့်ပေးပါရှင်။"


def get_reply(message=None, image_url=None):
    message = str(message or "").strip()

    if message:
        code, product = find_product_by_code(message)
        if product:
            print("PRODUCT FOUND BY CODE:", code, flush=True)
            return {
                "type": "product",
                "code": code,
                "product": product,
            }

        code, product = find_product_by_name(message)
        if product:
            print("PRODUCT FOUND BY NAME:", code, flush=True)
            return {
                "type": "product",
                "code": code,
                "product": product,
            }

    if image_url:
        code, product = ai_find_product_from_image(image_url, message)
        if product:
            print("PRODUCT FOUND FROM IMAGE:", code, flush=True)
            return {
                "type": "product",
                "code": code,
                "product": product,
            }

        return {
            "type": "text",
            "text": (
                "ပုံထဲကပစ္စည်းကို သေချာမခွဲနိုင်သေးပါရှင်။ "
                "ပစ္စည်းနာမည် သို့မဟုတ် Code လေးပို့ပေးပါရှင်။"
            ),
        }

    if message:
        code, product = ai_find_product(message)
        if product:
            print("PRODUCT FOUND BY AI:", code, flush=True)
            return {
                "type": "product",
                "code": code,
                "product": product,
            }

    return {
        "type": "text",
        "text": normal_ai_reply(message),
    }




def get_order_session(sender_id):
    return ORDER_SESSIONS.setdefault(
        sender_id,
        {
            "name": "",
            "address": "",
            "phone": "",
            "delivery_area": "",
            "items": {},
            "last_product_code": "",
        },
    )


def parse_int_amount(value, default=0):
    try:
        return int(float(str(value or default).replace(",", "").strip()))
    except Exception:
        return default


def product_price(code):
    product = PRODUCTS.get(code, {})
    return parse_int_amount(product.get("Price", 0), 0)


def delivery_fee_for_area(area):
    area = str(area or "").lower().strip()
    if area == "yangon":
        return 5000
    if area == "other":
        return 7500
    return 0


def find_codes_and_quantities(text):
    text = str(text or "")
    found = {}

    # Examples supported:
    # 0002, 0002 x2, 0002*2, 0002 2pcs, 0002 2ခု, 0002 နှစ်ခု
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

    catalog_lines = []
    for code, product in PRODUCTS.items():
        catalog_lines.append(
            f'{code}: {product.get("Product Name", "")}'
        )

    prompt = f"""
Extract order information from this Burmese/English customer message.

PRODUCTS:
{chr(10).join(catalog_lines)}

Return only one JSON object using this shape:
{{
  "name": "",
  "address": "",
  "phone": "",
  "delivery_area": "",
  "items": [{{"code": "0001", "quantity": 1}}]
}}

Rules:
- delivery_area must be "yangon", "other", or "".
- Use "yangon" only when the address clearly indicates Yangon.
- Use "other" when the address clearly indicates outside Yangon.
- If unknown, use "".
- quantity must be an integer at least 1.
- Never invent customer details or product codes.
- If a field is absent, leave it empty.
"""

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": str(message)},
        ],
        "max_tokens": 250,
    }

    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )

        if response.status_code != 200:
            print("ORDER EXTRACT STATUS:", response.status_code, flush=True)
            print("ORDER EXTRACT RESPONSE:", response.text, flush=True)
            return {}

        answer = response.json()["choices"][0]["message"]["content"]
        result = parse_json_answer(answer)
        return result if isinstance(result, dict) else {}

    except Exception as e:
        print("ORDER EXTRACT ERROR:", str(e), flush=True)
        return {}


def merge_order_message(sender_id, message):
    session = get_order_session(sender_id)

    # Direct code/quantity parsing first.
    for code, qty in find_codes_and_quantities(message).items():
        session["items"][code] = qty

    # AI extraction for name/address/phone/area and more flexible item wording.
    extracted = extract_order_fields_with_ai(message)

    for field in ("name", "address", "phone", "delivery_area"):
        value = str(extracted.get(field, "") or "").strip()
        if value:
            session[field] = value

    for item in extracted.get("items", []) or []:
        code = normalize_code(item.get("code", ""))
        if code in PRODUCTS:
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


def build_telegram_order(session):
    delivery_area = session.get("delivery_area", "")
    delivery_fee = delivery_fee_for_area(delivery_area)

    name = session.get("name", "")
    address = session.get("address", "")
    phone = session.get("phone", "")

    # First line exactly in the requested compact style.
    first_line = f"{name} / {address} / {phone}"

    item_parts = []
    subtotal = 0
    total_pcs = 0

    for code, qty in session.get("items", {}).items():
        product = PRODUCTS.get(code, {})
        item_name = str(product.get("Product Name", "")).strip()
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

    if len(item_parts) == 1:
        item_text = item_parts[0]
    else:
        item_text = " + ".join(item_parts)

    second_line = (
        f"{item_text} + Deli {delivery_fee:,} Ks "
        f"= Total {grand_total:,} Ks"
    )

    if len(session.get("items", {})) > 1:
        cod_text = f"COD {len(session['items'])} Items"
        if total_pcs != len(session["items"]):
            cod_text += f" / {total_pcs} PCS"
    elif total_pcs > 1:
        cod_text = f"COD {total_pcs} PCS"
    else:
        cod_text = "COD"

    return f"{first_line} / {second_line} / {cod_text}"



def order_prompt_for_missing(missing):
    if not missing:
        return ""

    if "ရန်ကုန်/နယ်" in missing:
        return "ပို့ရမယ့်နေရာက ရန်ကုန်လား၊ နယ်လားရှင်။"

    return (
        "အော်ဒါအတွက် "
        + " / ".join(missing)
        + " လေးပို့ပေးပါရှင်။"
    )



def send_facebook_text(recipient_id, message_text):
    if not PAGE_ACCESS_TOKEN:
        print("PAGE_ACCESS_TOKEN MISSING", flush=True)
        return

    try:
        response = requests.post(
            "https://graph.facebook.com/v25.0/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient": {"id": recipient_id},
                "message": {"text": message_text},
            },
            timeout=30,
        )
        print("FACEBOOK TEXT STATUS:", response.status_code, flush=True)
        print("FACEBOOK TEXT RESPONSE:", response.text, flush=True)
    except Exception as e:
        print("FACEBOOK TEXT ERROR:", str(e), flush=True)


def send_facebook_image(recipient_id, image_url):
    if not PAGE_ACCESS_TOKEN or not image_url:
        return

    try:
        response = requests.post(
            "https://graph.facebook.com/v25.0/me/messages",
            params={"access_token": PAGE_ACCESS_TOKEN},
            json={
                "recipient": {"id": recipient_id},
                "message": {
                    "attachment": {
                        "type": "image",
                        "payload": {
                            "url": image_url,
                            "is_reusable": True,
                        },
                    }
                },
            },
            timeout=30,
        )
        print("FACEBOOK IMAGE STATUS:", response.status_code, flush=True)
        print("FACEBOOK IMAGE RESPONSE:", response.text, flush=True)
    except Exception as e:
        print("FACEBOOK IMAGE ERROR:", str(e), flush=True)


def send_product_response(recipient_id, code, product):
    image_url = get_product_image_url(product)
    if image_url:
        send_facebook_image(recipient_id, image_url)

    send_facebook_text(recipient_id, product_reply(code, product))



def send_telegram_message(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=30,
        )
        print("TELEGRAM STATUS:", response.status_code, flush=True)
        print("TELEGRAM RESPONSE:", response.text, flush=True)
    except Exception as e:
        print("TELEGRAM ERROR:", str(e), flush=True)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    print("=== WEBHOOK POST RECEIVED ===", flush=True)
    print(data, flush=True)

    if not data or data.get("object") != "page":
        return "EVENT_RECEIVED", 200

    order_words = (
        "order",
        "မှာယူ",
        "မှာမယ်",
        "ယူမယ်",
        "အော်ဒါ",
        "ယူပါမယ်",
        "လိုချင်တယ်",
    )

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            message_data = event.get("message", {})

            if message_data.get("is_echo"):
                continue

            text = message_data.get("text", "")
            attachments = message_data.get("attachments", [])
            image_url = None

            for attachment in attachments:
                if attachment.get("type") == "image":
                    image_url = attachment.get("payload", {}).get("url")
                    if image_url:
                        break

            print("SENDER:", sender_id, flush=True)
            print("TEXT:", text, flush=True)
            print("IMAGE:", image_url, flush=True)

            if not sender_id or not (text or image_url):
                continue

            session = get_order_session(sender_id)

            # First identify/respond to the product.
            reply = get_reply(text, image_url)
            print("BOT REPLY:", reply, flush=True)

            if reply.get("type") == "product":
                code = reply["code"]
                product = reply["product"]
                session["last_product_code"] = code
                send_product_response(sender_id, code, product)
            else:
                send_facebook_text(
                    sender_id,
                    reply.get("text", "ခဏလေးစောင့်ပေးပါရှင်။"),
                )

            lower_text = str(text).lower()
            is_order_message = any(word in lower_text for word in order_words)

            # If there is already an active order, keep collecting details
            # from following messages too.
            active_order = bool(
                session.get("items")
                or session.get("name")
                or session.get("address")
                or session.get("phone")
            )

            if is_order_message or active_order:
                session = merge_order_message(sender_id, text)

                # "ယူမယ်" after viewing a product means 1 of last viewed item.
                if (
                    is_order_message
                    and not session["items"]
                    and session.get("last_product_code") in PRODUCTS
                ):
                    session["items"][session["last_product_code"]] = 1

                missing = order_missing_fields(session)

                if missing:
                    send_facebook_text(
                        sender_id,
                        order_prompt_for_missing(missing),
                    )
                else:
                    telegram_text = build_telegram_order(session)
                    send_telegram_message(telegram_text)

                    send_facebook_text(
                        sender_id,
                        "အော်ဒါတင်ပြီးပါပြီရှင်။ ကျေးဇူးတင်ပါတယ်ရှင်။",
                    )

                    # Clear completed order so the next order starts fresh.
                    ORDER_SESSIONS[sender_id] = {
                        "name": "",
                        "address": "",
                        "phone": "",
                        "delivery_area": "",
                        "items": {},
                        "last_product_code": session.get(
                            "last_product_code", ""
                        ),
                    }

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
