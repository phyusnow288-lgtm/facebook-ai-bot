import os
import requests
from flask import Flask, request
import csv

app = Flask(__name__)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GOOGLE_SHEET_URL = os.environ.get("GOOGLE_SHEET_URL")
PRODUCTS = {}
def load_products():
    global PRODUCTS
    try:
        url = GOOGLE_SHEET_URL
        if "/edit" in url:
            url = url.split("/edit")[0] + "/export?format=csv"

        response = requests.get(url, timeout=10)
        response.raise_for_status()

        lines = response.text.splitlines()
        reader = csv.DictReader(lines)

        PRODUCTS = {}
        for row in reader:
            code = str(row.get("Code", "")).strip()
            if code:
                PRODUCTS[code] = row

        print("PRODUCTS LOADED:", len(PRODUCTS), flush=True)

    except Exception as e:
        print("SHEET ERROR:", e, flush=True)
        
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


@app.route("/webhook", methods=["POST"])
def webhook():
    print("=== WEBHOOK POST RECEIVED ===", flush=True)

    data = request.get_json(silent=True)

    print("=== FULL DATA ===", flush=True)
    print(data, flush=True)

    if not data:
        print("NO DATA RECEIVED", flush=True)
        return "EVENT_RECEIVED", 200

    if data.get("object") != "page":
        return "EVENT_RECEIVED", 200

    for entry in data.get("entry", []):
        for messaging_event in entry.get("messaging", []):

            sender_id = messaging_event.get("sender", {}).get("id")
            message = messaging_event.get("message", {}).get("text")

            print("=== CUSTOMER MESSAGE ===", flush=True)
            print("SENDER ID:", sender_id, flush=True)
            print("MESSAGE:", message, flush=True)

            if sender_id and message:

                print(
                    "=== PROCESSING CUSTOMER MESSAGE ===",
                    flush=True
                )

                reply = get_ai_reply(message)

                print("=== AI REPLY ===", flush=True)
                print(reply, flush=True)

                send_facebook_message(
                    sender_id,
                    reply
                )

                if "order" in message.lower() or "မှာယူ" in message:
                    send_telegram_message(
                        f"📦 New Order\n\n"
                        f"Customer: {sender_id}\n"
                        f"Message: {message}"
                    )

    return "EVENT_RECEIVED", 200


def get_ai_reply(message):
    print("=== CALLING OPENAI API ===", flush=True)

    if not OPENAI_API_KEY:
        print(
            "ERROR: OPENAI_API_KEY IS MISSING",
            flush=True
        )

        return (
            "တောင်းပန်ပါတယ်။ "
            "AI စနစ်ကို ချိတ်ဆက်၍မရသေးပါ။"
        )
        for code, product in PRODUCTS.items():
        if code in message:
            name = product.get("Product Name", "")
            price = product.get("Price", "")
            yangon = product.get("Yangon Delivery", "")
            other = product.get("Other City Delivery", "")
            status = product.get("Stock status", "").strip().lower()

            if status == "out of stock":
                return f"Code {code} {name} လက်ရှိပစ္စည်းကုန်နေပါတယ်ရှင်။"

            if status == "coming soon":
                return f"Code {code} {name} လက်ရှိမရောက်သေးပါရှင်။"

            return (
                f"Code {code} {name}\n"
                f"ဈေးနှုန်း - {price} Ks\n"
                f"ရန်ကုန်ပိုခ - {yangon} Ks\n"
                f"နယ်ပိုခ - {other} Ks\n"
                f"ပစ္စည်းရောက်မှ ငွေချေ (COD) ရပါတယ်ရှင်။"
            )

    url = "https://api.openai.com/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                  "Always reply in Burmese. Keep every reply very short. "
"If the customer has not clearly said which product they want, reply exactly: "
"ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။ "
"Do not explain extra information unless the customer specifically asks for it.")
           },
            {
                "role": "user",
                "content": message
            }
        ]
    }

    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30
        )

        print(
            "OPENAI STATUS:",
            response.status_code,
            flush=True
        )

        print(
            "OPENAI RESPONSE:",
            response.text,
            flush=True
        )

        if response.status_code == 200:
            result = response.json()

            return result["choices"][0]["message"]["content"]

        return (
            "တောင်းပန်ပါတယ်။ "
            "ခဏနေရင် ပြန်လည်ဖြေကြားပေးပါမယ်။"
        )

    except Exception as e:
        print(
            "OPENAI EXCEPTION:",
            str(e),
            flush=True
        )

        return (
            "တောင်းပန်ပါတယ်။ "
            "ခဏနေရင် ပြန်လည်ဖြေကြားပေးပါမယ်။"
        )


def send_facebook_message(recipient_id, message_text):
    url = "https://graph.facebook.com/v25.0/me/messages"

    params = {
        "access_token": PAGE_ACCESS_TOKEN
    }

    data = {
        "recipient": {
            "id": recipient_id
        },
        "message": {
            "text": message_text
        }
    }

    try:
        response = requests.post(
            url,
            params=params,
            json=data,
            timeout=30
        )

        print(
            "FACEBOOK SEND STATUS:",
            response.status_code,
            flush=True
        )

        print(
            "FACEBOOK SEND RESPONSE:",
            response.text,
            flush=True
        )

    except Exception as e:
        print(
            "FACEBOOK SEND ERROR:",
            str(e),
            flush=True
        )


def send_telegram_message(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    try:
        response = requests.post(
            url,
            json=data,
            timeout=30
        )

        print(
            "TELEGRAM STATUS:",
            response.status_code,
            flush=True
        )

        print(
            "TELEGRAM RESPONSE:",
            response.text,
            flush=True
        )

    except Exception as e:
        print(
            "TELEGRAM ERROR:",
            str(e),
            flush=True
        )


if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            10000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
