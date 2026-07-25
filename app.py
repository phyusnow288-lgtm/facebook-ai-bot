import os
import requests
from flask import Flask, request

app = Flask(name)

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN")
PAGE_ACCESS_TOKEN = os.environ.get("PAGE_ACCESS_TOKEN")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


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

    if data.get("object") == "page":

        for entry in data.get("entry", []):

            for messaging_event in entry.get("messaging", []):

                sender_id = messaging_event.get("sender", {}).get("id")
                message = messaging_event.get("message", {}).get("text")

                print("=== CUSTOMER MESSAGE ===", flush=True)
                print("SENDER ID:", sender_id, flush=True)
                print("MESSAGE:", message, flush=True)

                if sender_id and message:

                    print("=== PROCESSING CUSTOMER MESSAGE ===", flush=True)

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

    url = "https://api.openai.com/v1/chat/completions"

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    data = {
        "model": "gpt-4o-mini",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a helpful sales assistant for a Myanmar online shop. "
                    "Reply politely in Burmese. "
                    "Help customers with product questions and orders."
                )
            },
            {
                "role": "user",
                "content": message
            }
        ]
    }

    response = requests.post(
        url,
        headers=headers,
        json=data
    )

    print(
        "OPENAI STATUS:",
        response.status_code,
        flush=True
    )

    if response.status_code == 200:

        return response.json()["choices"][0]["message"]["content"]

    print(
        "OPENAI ERROR:",
        response.text,
        flush=True
    )

    return "တောင်းပန်ပါတယ်။ ခဏနေရင် ပြန်လည်ဖြေကြားပေးပါမယ်။"


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

    response = requests.post(
        url,
        params=params,
        json=data
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


def send_telegram_message(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message
    }

    response = requests.post(
        url,
        json=data
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


if name == "main":

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

