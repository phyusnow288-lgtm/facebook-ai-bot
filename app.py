
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


def product_reply(code, product):
    name = str(product.get("Product Name", "")).strip()
    price = str(product.get("Price", "")).strip()
    yangon = str(product.get("Yangon Delivery", "")).strip()
    other = str(product.get("Other City Delivery", "")).strip()
    status = str(product.get("Stock status", "")).strip().lower()

    if status in ("out of stock", "sold out"):
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းကုန်နေပါတယ်ရှင်။"

    if status == "coming soon":
        return f"Code {code} {name}\nလက်ရှိ ပစ္စည်းမရောက်သေးပါရှင်။"

    reply = f"Code {code} {name}\nဈေးနှုန်း - {price} Ks"
    if yangon:
        reply += f"\nရန်ကုန်ပိုခ - {yangon} Ks"
    if other:
        reply += f"\nနယ်ပိုခ - {other} Ks"
    reply += "\nပစ္စည်းရောက်မှ ငွေချေ (COD) ရပါတယ်ရှင်။"
    return reply


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

    catalog = product_catalog_for_ai()
    instruction = f"""
A customer sent this image to an online shop.
Identify which product from the catalog is shown.
It may be a product photo, Facebook screenshot, Burmese text,
English text, Chinese text, product code, or advertisement.

PRODUCT CATALOG:
{catalog}

Customer text:
{caption}

Return only a JSON object containing the matching code.
If uncertain, return a null code.
Never invent a product code.
"""

    payload = {
        "model": "gpt-4o-mini",
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
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
            timeout=45,
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
            return product_reply(code, product)

        code, product = find_product_by_name(message)
        if product:
            print("PRODUCT FOUND BY NAME:", code, flush=True)
            return product_reply(code, product)

    if image_url:
        code, product = ai_find_product_from_image(image_url, message)
        if product:
            print("PRODUCT FOUND FROM IMAGE:", code, flush=True)
            return product_reply(code, product)

        return (
            "ပုံထဲကပစ္စည်းကို သေချာမခွဲနိုင်သေးပါရှင်။ "
            "ပစ္စည်းနာမည် သို့မဟုတ် Code လေးပို့ပေးပါရှင်။"
        )

    if message:
        code, product = ai_find_product(message)
        if product:
            print("PRODUCT FOUND BY AI:", code, flush=True)
            return product_reply(code, product)

    return normal_ai_reply(message)


def send_facebook_message(recipient_id, message_text):
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
        print("FACEBOOK SEND STATUS:", response.status_code, flush=True)
        print("FACEBOOK SEND RESPONSE:", response.text, flush=True)
    except Exception as e:
        print("FACEBOOK SEND ERROR:", str(e), flush=True)


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

            if sender_id and (text or image_url):
                reply = get_reply(text, image_url)
                print("BOT REPLY:", reply, flush=True)
                send_facebook_message(sender_id, reply)

                lower_text = str(text).lower()
                order_words = ("order", "မှာယူ", "မှာမယ်", "ယူမယ်")
                if any(word in lower_text for word in order_words):
                    send_telegram_message(
                        f"New Order\n\nCustomer ID: {sender_id}\nMessage: {text}"
                    )

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
