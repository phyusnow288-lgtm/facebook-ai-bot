
# =========================
# V51 BUILD MARKER
# Restores the pre-V46 Admin pause path while preserving V50 Sheet-first,
# multi-code, multi-photo, product image/detail/price, order and Telegram logic.
# ON/OFF commands are intentionally NOT restored.
# =========================
BOT_BUILD = "V52_SAFE_ADMIN_PAUSE_ALIAS_SYNC_NO_DATA_LOSS"
print("BOT BUILD:", BOT_BUILD, flush=True)

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
from flask import Flask, request

BOT_VERSION = "V49-SAFE-SHEET-FIRST-ADMIN-ORDER-FLOW-NO-DATA-LOSS"

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
BOT_VERSION = "V50_SAFE_DUAL_ADMIN_TAKEOVER_SHEET_FIRST_NO_DATA_LOSS"
ADMIN_PAUSE_MINUTES = int(os.environ.get("ADMIN_PAUSE_MINUTES", "30"))
POST_ORDER_ACK_TTL_SECONDS = int(os.environ.get("POST_ORDER_ACK_TTL_SECONDS", "86400"))

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
POST_ORDER_COMPLETED_AT = {}
MANYCHAT_ECHO_IGNORE_UNTIL = {}
RECENT_MANYCHAT_REPLY_TEXTS = {}
RECENT_MANYCHAT_REPLY_LOCK = threading.Lock()
MANYCHAT_REPLY_TEXT_TTL_SECONDS = int(os.environ.get("MANYCHAT_REPLY_TEXT_TTL_SECONDS", "120"))
RECENT_TELEGRAM_ORDERS = {}

# V38: ManyChat Contact Id and Meta PSID are not always the same identifier.
# Keep a short-lived correlation map so Admin ON/OFF from a Meta echo controls
# the exact same customer when the next buyer message arrives through ManyChat.
CUSTOMER_ID_ALIASES = {}
RECENT_MANYCHAT_INBOUND = []
RECENT_MANYCHAT_INBOUND_LOCK = threading.Lock()
RECENT_MANYCHAT_INBOUND_TTL_SECONDS = 90

# V41: Meta and ManyChat can reach this server in either order for the same
# customer message.  Keep the Meta side too, so Contact ID <-> PSID binding is
# bidirectional instead of depending on webhook arrival order.
RECENT_META_INBOUND = []
RECENT_META_INBOUND_LOCK = threading.Lock()
RECENT_META_INBOUND_TTL_SECONDS = int(os.environ.get("RECENT_META_INBOUND_TTL_SECONDS", "90"))

# V42: Admin echoes from Messenger use the customer's Meta PSID, while the
# ManyChat Dynamic Block often gives only Contact Id + Full Name. Keep a
# conservative recent Full Name -> Contact Id bridge. It is used only when the
# PSID is not already bound and Meta's User Profile API returns the same name.
RECENT_MANYCHAT_IDENTITIES = {}
RECENT_MANYCHAT_IDENTITIES_LOCK = threading.Lock()
RECENT_MANYCHAT_IDENTITIES_TTL_SECONDS = int(
    os.environ.get("RECENT_MANYCHAT_IDENTITIES_TTL_SECONDS", "86400")
)

# V41: remember referral/ad context delivered by Meta.  This is intentionally
# data-driven: an ad becomes a product only when its ID/name/ref is present in a
# Google Sheet ad-related column, or ManyChat explicitly supplies a product code.
META_AD_CONTEXT = {}
META_AD_CONTEXT_LOCK = threading.Lock()
META_AD_CONTEXT_TTL_SECONDS = int(os.environ.get("META_AD_CONTEXT_TTL_SECONDS", "86400"))

RECENT_TELEGRAM_LOCK = threading.Lock()
TELEGRAM_ORDER_DEDUP_SECONDS = 300

# Suppress the same ManyChat input being answered twice when ManyChat retries a
# Dynamic Block request. Keyed per contact + exact input/context.
RECENT_MANYCHAT_INPUTS = {}
RECENT_MANYCHAT_LOCK = threading.Lock()
MANYCHAT_INPUT_DEDUP_SECONDS = int(os.environ.get("MANYCHAT_INPUT_DEDUP_SECONDS", "30"))
MANYCHAT_ECHO_GRACE_SECONDS = int(os.environ.get("MANYCHAT_ECHO_GRACE_SECONDS", "20"))
PRODUCT_REFRESH_LOCK = threading.Lock()
PRODUCT_REFRESH_RUNNING = False

# V39: keep every recently observed customer image for a few seconds.
# Messenger/ManyChat can deliver a multi-photo send as separate webhook events,
# while Last Text Input may expose only the newest attachment.  This short-lived
# buffer lets the next ManyChat request recover all photos without changing any
# order/session behavior from V38.
RECENT_CUSTOMER_IMAGES = {}
RECENT_CUSTOMER_IMAGES_LOCK = threading.Lock()
RECENT_CUSTOMER_IMAGE_TTL_SECONDS = int(os.environ.get("RECENT_CUSTOMER_IMAGE_TTL_SECONDS", "20"))


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
# V39 MULTI-PHOTO BUFFER
# =========================
def _flatten_image_urls(value):
    """Return every plausible image URL found in a ManyChat/Meta payload value."""
    out = []

    def add(v):
        if isinstance(v, str):
            text = v.strip()
            if text.startswith(("http://", "https://", "data:")):
                low = text.lower()
                if any(x in low for x in (".jpg", ".jpeg", ".png", ".webp", ".gif", "scontent", "fbcdn", "googleusercontent", "drive.google")):
                    if text not in out:
                        out.append(text)
            return
        if isinstance(v, dict):
            # Known attachment/url keys first, then recurse safely through nested data.
            for key in ("url", "image_url", "attachment_url", "last_image_url", "customer_image_url", "file_url", "src"):
                if key in v:
                    add(v.get(key))
            for key, nested in v.items():
                if key not in ("url", "image_url", "attachment_url", "last_image_url", "customer_image_url", "file_url", "src"):
                    if isinstance(nested, (dict, list, tuple)):
                        add(nested)
            return
        if isinstance(v, (list, tuple)):
            for item in v:
                add(item)

    add(value)
    return out


def extract_manychat_image_urls(data, message=""):
    """Collect ALL image URLs ManyChat may provide, not only Last Image URL."""
    urls = []

    def add_urls(value):
        # Existing recursive extractor handles normal ManyChat/Meta objects.
        for url in _flatten_image_urls(value):
            if url not in urls:
                urls.append(url)

        # Some ManyChat custom fields arrive as one JSON/string value containing
        # several attachment URLs. Extract every URL instead of keeping only the
        # last attachment.
        if isinstance(value, str):
            for match in re.findall(r'https?://[^\s"\'<>\],}]+', value):
                clean = match.rstrip(").,;")
                low = clean.lower()
                if any(token in low for token in (
                    ".jpg", ".jpeg", ".png", ".webp", ".gif",
                    "scontent", "fbcdn", "googleusercontent", "drive.google",
                )):
                    if clean not in urls:
                        urls.append(clean)

    if isinstance(data, dict):
        # Explicit known fields.
        for key in (
            "image_urls", "images", "attachments", "attachment_urls", "files",
            "image_url", "attachment_url", "last_image_url", "customer_image_url",
            "image", "file_url", "last_attachment", "last_attachments",
            "image_url_1", "image_url_2", "image_url_3", "image_url_4", "image_url_5",
            "attachment_1", "attachment_2", "attachment_3", "attachment_4", "attachment_5",
        ):
            if key in data:
                add_urls(data.get(key))

        # Also inspect nested payload fields so future ManyChat mappings do not
        # require another code change.
        add_urls(data)

    # ManyChat sometimes places Facebook CDN URL(s) in Last Text Input.
    add_urls(str(message or "").strip())
    return urls


def remember_customer_images(customer_id, urls):
    cid = str(customer_id or "").strip()
    if not cid:
        return
    now = now_ts()
    clean = [str(u or "").strip() for u in (urls or []) if str(u or "").strip()]
    if not clean:
        return
    ids = _identity_aliases(cid) or {cid}
    with RECENT_CUSTOMER_IMAGES_LOCK:
        # prune stale buffers globally
        for old_id, rows in list(RECENT_CUSTOMER_IMAGES.items()):
            live = [(u, ts) for u, ts in (rows or []) if now - ts <= RECENT_CUSTOMER_IMAGE_TTL_SECONDS]
            if live:
                RECENT_CUSTOMER_IMAGES[old_id] = live
            else:
                RECENT_CUSTOMER_IMAGES.pop(old_id, None)
        for ident in ids:
            bucket = RECENT_CUSTOMER_IMAGES.setdefault(str(ident), [])
            known = {u for u, ts in bucket if now - ts <= RECENT_CUSTOMER_IMAGE_TTL_SECONDS}
            for url in clean:
                if url not in known:
                    bucket.append((url, now))
                    known.add(url)


def recent_customer_images(customer_id):
    cid = str(customer_id or "").strip()
    if not cid:
        return []
    now = now_ts()
    ids = _identity_aliases(cid) or {cid}
    out = []
    with RECENT_CUSTOMER_IMAGES_LOCK:
        for ident in ids:
            rows = RECENT_CUSTOMER_IMAGES.get(str(ident), []) or []
            live = []
            for url, ts in rows:
                if now - ts <= RECENT_CUSTOMER_IMAGE_TTL_SECONDS:
                    live.append((url, ts))
                    if url not in out:
                        out.append(url)
            if live:
                RECENT_CUSTOMER_IMAGES[str(ident)] = live
            else:
                RECENT_CUSTOMER_IMAGES.pop(str(ident), None)
    return out



def clear_recent_customer_images(customer_id, only_urls=None):
    """Remove processed URLs so an old product photo cannot bleed into a later event."""
    cid = str(customer_id or "").strip()
    if not cid:
        return
    ids = _identity_aliases(cid) or {cid}
    wanted = None
    if only_urls is not None:
        wanted = {str(u or "").strip() for u in only_urls if str(u or "").strip()}
    with RECENT_CUSTOMER_IMAGES_LOCK:
        for ident in ids:
            key = str(ident)
            rows = RECENT_CUSTOMER_IMAGES.get(key, []) or []
            if wanted is None:
                RECENT_CUSTOMER_IMAGES.pop(key, None)
                continue
            kept = [(u, ts) for u, ts in rows if u not in wanted]
            if kept:
                RECENT_CUSTOMER_IMAGES[key] = kept
            else:
                RECENT_CUSTOMER_IMAGES.pop(key, None)


def _image_match_cache_key(image_url):
    value = str(image_url or "").strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        value = value.split("?", 1)[0]
    return value[-500:]


def _get_cached_image_match(image_url):
    key = _image_match_cache_key(image_url)
    if not key:
        return None
    now = now_ts()
    with IMAGE_MATCH_CACHE_LOCK:
        for old_key, row in list(IMAGE_MATCH_CACHE.items()):
            if now - float(row.get("ts", 0) or 0) > IMAGE_MATCH_CACHE_TTL_SECONDS:
                IMAGE_MATCH_CACHE.pop(old_key, None)
        row = IMAGE_MATCH_CACHE.get(key)
        if row is None:
            return None
        return list(row.get("codes", []) or [])


def _set_cached_image_match(image_url, codes):
    key = _image_match_cache_key(image_url)
    if not key:
        return
    with IMAGE_MATCH_CACHE_LOCK:
        IMAGE_MATCH_CACHE[key] = {"ts": now_ts(), "codes": list(codes or [])}


def _warm_reference_images_worker():
    global _REFERENCE_WARM_RUNNING
    try:
        catalog_reference_content_parts()
    except Exception as e:
        print("REFERENCE WARM WARNING:", str(e), flush=True)
    finally:
        with _REFERENCE_WARM_LOCK:
            _REFERENCE_WARM_RUNNING = False


def warm_reference_images_async():
    """Prepare cached reference images in background; never block a live webhook."""
    global _REFERENCE_WARM_RUNNING
    if not PRODUCTS:
        return
    key = _catalog_image_cache_key()
    if key and _REFERENCE_IMAGES_CACHE.get("key") == key and _REFERENCE_IMAGES_CACHE.get("parts"):
        return
    with _REFERENCE_WARM_LOCK:
        if _REFERENCE_WARM_RUNNING:
            return
        _REFERENCE_WARM_RUNNING = True
    threading.Thread(target=_warm_reference_images_worker, daemon=True).start()


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
    for raw_row in reader:
        # Normalize Google Sheet headers once. Stray spaces/BOMs in headers were
        # enough to make some rows look as if their Image URL/Detail was missing.
        row = {}
        for raw_key, value in (raw_row or {}).items():
            key = str(raw_key or "").replace("\ufeff", "").strip()
            row[key] = value
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
            warm_reference_images_async()
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
                warm_reference_images_async()
        except Exception as e:
            print("SHEET ERROR:", str(e), flush=True)
        return

    with PRODUCT_REFRESH_LOCK:
        if PRODUCT_REFRESH_RUNNING:
            return
        PRODUCT_REFRESH_RUNNING = True

    threading.Thread(target=_background_product_refresh, daemon=True).start()


def refresh_catalog_for_customer_request(source="REQUEST"):
    """Synchronously refresh Google Sheet before deciding any customer product reply.

    V49 strict source-of-truth gate:
    - code/name/photo/ad recognition must use the newest Sheet rows available now;
    - newly-added Sheet items work without a Python edit/deploy;
    - if refresh fails, keep the last known catalog rather than erasing working data.
    """
    before_codes = tuple(PRODUCTS.keys())
    load_products(force=True)
    after_codes = tuple(PRODUCTS.keys())
    print(
        "V49 SHEET-FIRST CATALOG CHECK:",
        source,
        "COUNT", len(PRODUCTS),
        "CHANGED" if before_codes != after_codes else "UNCHANGED",
        flush=True,
    )
    return bool(PRODUCTS)


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
    """Return a product image from common Google Sheet header variants."""
    if not isinstance(product, dict):
        return ""

    # First use the known historical headers.
    value = get_row_value(
        product,
        "Image URL", "ImageURL", "image_url", "Image Url", "Image url",
        "Image Link", "ImageLink", "Product Image", "Product Image URL",
        "Photo", "Photo URL", "PhotoURL", "image",
    )
    if value not in (None, ""):
        return str(value).strip()

    # Last-resort normalized header lookup protects future Sheet edits such as
    # "Image URL " or different punctuation/case without requiring Python edits.
    for key, raw_value in product.items():
        normalized_key = re.sub(r"[^a-z0-9]+", "", str(key or "").lower())
        if normalized_key in {
            "image", "imageurl", "imagelink",
            "productimage", "productimageurl",
            "photo", "photourl",
        } and raw_value not in (None, ""):
            return str(raw_value).strip()
    return ""



def manychat_product_image_url(product):
    """Return the catalog image URL directly for ManyChat/Messenger.

    Google Drive share links are converted to a stable googleusercontent URL.
    Avoid routing product images back through Render: the extra proxy/download
    step was unnecessary and could make Dynamic Content return text while the
    image itself failed or timed out.
    """
    source = product_image_url(product)
    if not source:
        return ""
    return google_drive_view_url(source)


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
                "Read this CUSTOMER ORDER SCREENSHOT/PHOTO. It may be a screenshot/photo "
                "of a Burmese, English, or mixed-language delivery address, phone number, "
                "or full order details. Extract only visible customer order information. "
                "Do not invent anything. The buyer name is supplied separately from the "
                "Facebook account, so do not guess a name from conversational text. "
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
        if not value:
            continue
        # ManyChat orders always use the Facebook/ManyChat account name.
        # Never let typed text, OCR, or AI overwrite that locked name.
        if field == "name" and session.get("_account_name_locked"):
            continue
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
def explicit_catalog_code_candidates(message):
    """Return code-looking tokens the buyer explicitly supplied.

    Labelled 1-4 digit codes are always candidates. Bare four-digit tokens are
    candidates unless the whole message clearly looks like delivery/address data.
    This catches an unknown/new code such as 0020 without confusing a quantity
    such as 2 or x5 with Product Code 0002/0005.
    """
    text = _western_digits(str(message or "")).strip()
    if not text:
        return []

    found = []
    for pattern in (
        r"(?i)(?:code|product\s*code)\s*[:#-]?\s*(\d{1,4})(?!\d)",
        r"ကုဒ်\s*[:#-]?\s*(\d{1,4})(?!\d)",
    ):
        for match in re.finditer(pattern, text):
            code = normalize_code(match.group(1))
            if code and code not in found:
                found.append(code)

    # A normal order address can legitimately contain a 4-digit house/postal number.
    # Do not reinterpret it as a product code when strong order/address structure exists.
    address_like = False
    try:
        address_like = looks_like_order_details(text) or is_likely_delivery_address(text)
    except Exception:
        address_like = False

    if not address_like:
        for match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", text):
            code = normalize_code(match.group(1))
            if code and code not in found:
                found.append(code)
    return found


def reset_order_context_for_unknown_product(customer_id, account_name=""):
    """Clear stale product/order state after an explicit code is not in the Sheet.

    Keep the Facebook/ManyChat account name lock, but never let an older product,
    address or quantity make an unknown code continue a previous order.
    """
    session = new_order_session()
    if account_name:
        lock_facebook_account_name(session, account_name)
    ORDER_SESSIONS[str(customer_id or "").strip()] = session
    print("V49 UNKNOWN CODE - STALE ORDER CONTEXT CLEARED:", customer_id, flush=True)
    return session


def find_product_by_code(message):
    """Find only an EXPLICIT catalog code; never reinterpret quantity as a code.

    Global rule:
    - ``2 ခု``, ``5 ထုပ်``, ``10 pcs``, ``x3`` and similar numbers are quantities.
    - A short numeric code (1..999) is accepted only when explicitly labelled
      ``Code`` / ``Product Code`` / ``ကုဒ်``.
    - A four-digit catalog code such as ``0017`` is explicit enough by itself.

    This intentionally means a bare message like ``2`` is NOT treated as Code 0002.
    Customers can identify products by name/photo, or use ``0002`` / ``Code 2``.
    """
    load_products()
    if not message:
        return None, None

    text = _western_digits(str(message)).strip()
    if not text:
        return None, None

    # Explicitly-labelled short or long code.
    label_patterns = (
        r"(?i)(?:code|product\s*code)\s*[:#-]?\s*(\d{1,4})(?!\d)",
        r"ကုဒ်\s*[:#-]?\s*(\d{1,4})(?!\d)",
    )
    for pattern in label_patterns:
        for match in re.finditer(pattern, text):
            code = normalize_code(match.group(1))
            if code in PRODUCTS:
                return code, PRODUCTS[code]

    # Four digits are the shop's canonical code format and are safe even when
    # embedded in normal text. Quantity parsing elsewhere is limited to 1-3 digits.
    for match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", text):
        code = normalize_code(match.group(1))
        if code in PRODUCTS:
            return code, PRODUCTS[code]

    return None, None


def _compact_product_match_text(value):
    """Normalize harmless spacing/punctuation differences for product-name matching."""
    return re.sub(r"[^0-9a-zA-Zက-႟一-鿿]+", "", str(value or "").casefold())


def find_product_by_name(message):
    load_products()

    if not message:
        return None, None

    text = str(message).casefold().strip()
    compact_text = _compact_product_match_text(text)

    best_code = None
    best_product = None
    best_score = 0

    for code, product in PRODUCTS.items():
        candidates = [
            get_row_value(product, "Product Name", "Name", "product_name"),
            get_row_value(product, "Myanmar Name", "MyanmarName", "Burmese Name", "MM Name"),
            get_row_value(product, "English Name", "EnglishName", "EN Name"),
            get_row_value(product, "Chinese Name", "ChineseName", "CN Name"),
            get_row_value(product, "Model", "Model No", "Model Number", "SKU"),
        ]

        # Sheet-maintained aliases/keywords are split into independent candidates.
        # This stays data-driven, so future products still require no Python edit.
        alias_blob = str(get_row_value(product, "Alias", "Aliases", "Keywords", "Keyword") or "")
        if alias_blob:
            candidates.extend(
                part.strip()
                for part in re.split(r"[,;|\n]+", alias_blob)
                if part.strip()
            )

        for candidate in candidates:
            name = str(candidate or "").casefold().strip()
            if len(name) < 2:
                continue

            compact_name = _compact_product_match_text(name)
            direct_match = name in text
            compact_match = len(compact_name) >= 3 and compact_name in compact_text

            if direct_match or compact_match:
                score = max(len(name), len(compact_name))
                if score > best_score:
                    best_score = score
                    best_code = code
                    best_product = product

    return best_code, best_product


def find_product_by_rich_sheet_text(message):
    """Data-driven local matcher using names, aliases AND detail fields.

    This does not hard-code any product/code.  It only uses Google Sheet text,
    so future products participate automatically.  It is deliberately conservative:
    a match is returned only when one product has a clearly stronger useful phrase.
    """
    load_products()
    text = str(message or "").casefold().strip()
    compact_text = _compact_product_match_text(text)
    if len(compact_text) < 3:
        return None, None

    field_keys = (
        "Product Name", "Name", "product_name",
        "Myanmar Name", "MyanmarName", "Burmese Name", "MM Name",
        "English Name", "EnglishName", "EN Name",
        "Chinese Name", "ChineseName", "CN Name",
        "Alias", "Aliases", "Keywords", "Keyword",
        "Model", "Model No", "Model Number", "SKU",
        "Description", "Details", "Detail", "Product Detail", "Product Details",
        "Description Myanmar", "Myanmar Description", "အသေးစိတ်",
    )
    scored = []
    for code, product in PRODUCTS.items():
        best = 0
        for key in field_keys:
            raw = str(get_row_value(product, key) or "").casefold().strip()
            if not raw:
                continue
            # Whole field / alias fragments.
            fragments = [raw]
            fragments += [x.strip() for x in re.split(r"[,;|/\n•·]+", raw) if x.strip()]
            # Useful word/phrase fragments from long descriptions.
            fragments += [x.strip() for x in re.split(r"[\s()\[\]{}:：,;|/\n]+", raw) if x.strip()]
            for frag in fragments:
                cf = _compact_product_match_text(frag)
                if len(cf) < 3:
                    continue
                if cf in compact_text:
                    best = max(best, min(len(cf), 60))
                elif len(compact_text) >= 5 and compact_text in cf:
                    best = max(best, min(len(compact_text), 45))
        if best:
            scored.append((best, code, product))

    if not scored:
        return None, None
    scored.sort(reverse=True, key=lambda x: x[0])
    top = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0
    # Require a meaningful phrase and avoid ambiguous equal/near-equal detail words.
    if top[0] >= 4 and (second == 0 or top[0] >= second + 2):
        print("RICH SHEET TEXT MATCH:", top[1], "SCORE", top[0], flush=True)
        return top[1], top[2]
    return None, None


def find_named_products_and_quantities(message):
    """Return every distinct catalog product explicitly named in one message."""
    load_products()
    text = str(message or "")
    if not text:
        return {}
    low = text.casefold()
    compact_text = _compact_product_match_text(low)
    matches = []
    for code, product in PRODUCTS.items():
        candidates = [
            get_row_value(product, "Product Name", "Name", "product_name"),
            get_row_value(product, "Myanmar Name", "MyanmarName", "Burmese Name", "MM Name"),
            get_row_value(product, "English Name", "EnglishName", "EN Name"),
            get_row_value(product, "Chinese Name", "ChineseName", "CN Name"),
            get_row_value(product, "Model", "Model No", "Model Number", "SKU"),
        ]
        alias_blob = str(get_row_value(product, "Alias", "Aliases", "Keywords", "Keyword") or "")
        if alias_blob:
            candidates.extend(x.strip() for x in re.split(r"[,;|\n]+", alias_blob) if x.strip())
        best = None
        for candidate in candidates:
            cand = str(candidate or "").casefold().strip()
            if len(cand) < 2:
                continue
            pos = low.find(cand)
            if pos >= 0:
                score = len(cand)
                if not best or score > best[0]:
                    best = (score, pos, pos + len(cand))
                continue
            cc = _compact_product_match_text(cand)
            if len(cc) >= 3 and cc in compact_text:
                # Compact matches have no exact original offset; still include product.
                if not best:
                    best = (len(cc), 10**6, 10**6)
        if best:
            matches.append((best[1], -best[0], code, best[2]))

    matches.sort()
    found = {}
    for pos, _negscore, code, endpos in matches:
        qty = 1
        if pos < 10**6:
            tail = text[endpos:endpos + 30]
            q = extract_explicit_quantity(tail)
            if q is not None:
                qty = q
        found[code] = qty
    return found



def product_catalog_text():
    """Build a rich text catalog directly from Google Sheet rows.

    V35 uses every useful name/alias/detail field that may exist in the Sheet.
    This keeps text recognition global: newly added products are automatically
    available without adding Python product-specific rules.
    """
    load_products()
    rows = []

    name_keys = (
        "Product Name", "Name", "product_name",
        "Myanmar Name", "MyanmarName", "Burmese Name", "MM Name",
        "English Name", "EnglishName", "EN Name",
        "Chinese Name", "ChineseName", "CN Name",
        "Alias", "Aliases", "Keywords", "Keyword",
        "Model", "Model No", "Model Number", "SKU",
    )
    detail_keys = (
        "Description", "Details", "Detail", "Product Detail", "Product Details",
        "Description Myanmar", "Myanmar Description", "အသေးစိတ်",
    )

    for code, product in sorted(PRODUCTS.items()):
        names = []
        for key in name_keys:
            value = str(get_row_value(product, key) or "").strip()
            if value and value not in names:
                names.append(value)

        details = []
        for key in detail_keys:
            value = str(get_row_value(product, key) or "").strip()
            if value and value not in details:
                details.append(value)

        rows.append(
            f"Code {code} | Names/Aliases: {' ; '.join(names)} | "
            f"Details: {' ; '.join(details)}"
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


def openai_chat(messages, max_tokens=200, temperature=0, timeout_seconds=7):
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
            timeout=timeout_seconds,
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
    """Global catalog text matcher for Burmese/English/Chinese/mixed buyer text."""
    if not OPENAI_API_KEY or not message:
        return None, None

    catalog = product_catalog_text()

    system_prompt = f"""
You are a strict product matcher for one online-shop catalog.

The customer may:
- write Burmese, English, Chinese, or mixed languages
- misspell a product name
- describe appearance, purpose, model number, package text, or use case
- paste text copied from an advertisement or screenshot
- include an order quantity such as 2 ခု, 5 ထုပ်, 10 pcs, x3

IMPORTANT RULES:
- Match ONLY a product that exists in CATALOG. Never invent a code.
- Quantity numbers are NOT product codes. For example, "2 ခု" must never mean Code 0002.
- Use names, aliases, model markings, details, product purpose, category, and common everyday/colloquial names together.
- Translate meaning across Burmese, English and Chinese when needed; exact word overlap is NOT required.
- A customer may describe what the product is normally called in daily speech rather than the formal catalog name.
- If exactly one catalog product clearly fits that meaning/use, return it.
- If two or more products are genuinely plausible, or the text is too generic, return null.

CATALOG:
{catalog}

Return ONLY one JSON object, with no explanation:
{{"code":"0001"}}
Or if not confident:
{{"code":null}}
"""

    answer = openai_chat(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": str(message)},
        ],
        max_tokens=80,
        temperature=0,
        timeout_seconds=5.8,
    )

    result = parse_json_answer(answer)
    code = normalize_code(result.get("code", ""))
    if code in PRODUCTS:
        print("TEXT AI MATCH:", code, flush=True)
        return code, PRODUCTS[code]

    print("TEXT AI MATCH: NONE", flush=True)
    return None, None


# =========================
# CATALOG REFERENCE IMAGES (V35 - NO PILLOW REQUIRED)
# =========================
_REFERENCE_IMAGES_CACHE = {"key": "", "parts": []}
_REFERENCE_IMAGES_LOCK = threading.Lock()

# V44: avoid repeated expensive vision calls for duplicate/retried image events.
IMAGE_MATCH_CACHE = {}
IMAGE_MATCH_CACHE_LOCK = threading.Lock()
IMAGE_MATCH_CACHE_TTL_SECONDS = int(os.environ.get("IMAGE_MATCH_CACHE_TTL_SECONDS", "900"))

# Warm labelled catalog images after Sheet load so the first buyer photo is faster.
_REFERENCE_WARM_LOCK = threading.Lock()
_REFERENCE_WARM_RUNNING = False


def _catalog_image_cache_key():
    """Change automatically whenever a Sheet product code or image URL changes."""
    return "|".join(
        f"{code}={product_image_url(product)}"
        for code, product in sorted(PRODUCTS.items())
        if product_image_url(product)
    )


def _image_mime_from_bytes(raw, header=""):
    """Detect common vision-compatible image MIME types without Pillow."""
    header = str(header or "").split(";")[0].strip().lower()
    if header in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return header
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _download_one_reference(item):
    """Return (code, name, data_url) with a short bounded network timeout."""
    code, name, url = item
    try:
        response = requests.get(
            google_drive_direct_url(url),
            timeout=2.0,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        raw = response.content or b""
        # Protect the 10-second ManyChat request and OpenAI payload from accidental
        # giant files. Normal shop product images are far below this threshold.
        if not raw or len(raw) > 2_500_000:
            print("REFERENCE IMAGE SKIP SIZE:", code, len(raw), flush=True)
            return code, name, ""
        mime = _image_mime_from_bytes(raw, response.headers.get("Content-Type", ""))
        if not mime:
            print("REFERENCE IMAGE SKIP TYPE:", code, response.headers.get("Content-Type", ""), flush=True)
            return code, name, ""
        data_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        return code, name, data_url
    except Exception as e:
        print("REFERENCE IMAGE DOWNLOAD SKIP:", code, str(e), flush=True)
        return code, name, ""


def catalog_reference_content_parts():
    """Cache ALL available Google Sheet reference images as labelled vision parts.

    Unlike V34, this does not need Pillow and does not build a contact sheet.
    Each real product image stays paired with its exact Code label, and all
    references are sent in ONE OpenAI vision request. This mirrors the common
    visual-search pattern of comparing a query image against a cached reference
    catalog, while keeping the current small catalog compatible with ManyChat's
    fixed 10-second Dynamic Block timeout.
    """
    load_products()
    key = _catalog_image_cache_key()
    if not key:
        return []

    if _REFERENCE_IMAGES_CACHE.get("key") == key and _REFERENCE_IMAGES_CACHE.get("parts"):
        return list(_REFERENCE_IMAGES_CACHE["parts"])

    with _REFERENCE_IMAGES_LOCK:
        if _REFERENCE_IMAGES_CACHE.get("key") == key and _REFERENCE_IMAGES_CACHE.get("parts"):
            return list(_REFERENCE_IMAGES_CACHE["parts"])

        refs = []
        for code, product in sorted(PRODUCTS.items()):
            url = product_image_url(product)
            if not url:
                continue
            name = str(get_row_value(product, "Product Name", "Name") or "").strip()
            refs.append((code, name, url))

        if not refs:
            return []

        downloaded = {}
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=min(10, len(refs))) as pool:
                futures = [pool.submit(_download_one_reference, item) for item in refs]
                for future in as_completed(futures):
                    code, name, data_url = future.result()
                    if data_url:
                        downloaded[code] = (name, data_url)
        except Exception as e:
            print("REFERENCE IMAGE CACHE ERROR:", str(e), flush=True)

        parts = []
        for code, name, _url in refs:
            cached = downloaded.get(code)
            if not cached:
                continue
            ref_name, data_url = cached
            parts.append({
                "type": "text",
                "text": f"REFERENCE PRODUCT — CODE {code} — {ref_name}",
            })
            parts.append({
                "type": "image_url",
                "image_url": {"url": data_url, "detail": "low"},
            })

        _REFERENCE_IMAGES_CACHE["key"] = key
        _REFERENCE_IMAGES_CACHE["parts"] = list(parts)
        print(
            "REFERENCE IMAGES READY:",
            len(parts) // 2,
            "/",
            len(refs),
            "products",
            flush=True,
        )
        return parts

# Initial catalog load must happen only after image/cache helpers are defined.
load_products(force=True)
for _catalog_code, _catalog_product in PRODUCTS.items():
    if not product_image_url(_catalog_product):
        print("CATALOG IMAGE MISSING:", _catalog_code, flush=True)



# =========================
# IMAGE PRODUCT IDENTIFICATION
# =========================
def ai_find_product_from_image(image_url, caption=""):
    """Match a customer image against ALL available Sheet product images.

    One request contains:
      customer image + rich text catalog + labelled product reference images.
    If references cannot be downloaded, the rich text catalog still remains as
    a fallback. No product-specific code is hard-coded, so new Sheet products
    join recognition automatically after the normal catalog refresh.
    """
    if not OPENAI_API_KEY or not image_url:
        return None, None

    load_products()
    catalog = product_catalog_text()
    source_url = str(image_url or "").strip()
    if not source_url.startswith(("http://", "https://", "data:")):
        return None, None

    # Short-lived Facebook CDN URLs are safest when copied into the request as
    # bytes immediately. If that fails, OpenAI can still try the public URL.
    customer_vision_url = source_url
    if source_url.startswith(("http://", "https://")):
        try:
            r = requests.get(
                source_url,
                timeout=2.0,
                allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            r.raise_for_status()
            raw = r.content or b""
            mime = _image_mime_from_bytes(raw, r.headers.get("Content-Type", ""))
            if raw and mime:
                customer_vision_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
                print("IMAGE INPUT: DOWNLOADED FB/CDN BYTES", len(raw), flush=True)
        except Exception as e:
            print("IMAGE FAST DOWNLOAD FALLBACK TO URL:", str(e), flush=True)

    content = [
        {
            "type": "text",
            "text": (
                "Identify exactly ONE shop product shown in the CUSTOMER IMAGE. "
                "The image can be the physical item, packaging, screenshot, ad image, "
                "or a photo of another phone screen. Compare it against every labelled "
                "REFERENCE PRODUCT image below. Use visual shape, packaging layout, "
                "logos, colors, model numbers, and visible Burmese/English/Chinese text. "
                "The rich text catalog is supporting evidence. A quantity visible in text "
                "is never a product code. Return ONLY JSON {\"code\":\"0001\"}. "
                "If no single catalog product is a confident match, return "
                "{\"code\":null}. Never invent a code.\n\n"
                f"TEXT CATALOG:\n{catalog}\n\nCUSTOMER CAPTION: {caption}"
            ),
        },
        {
            "type": "text",
            "text": "CUSTOMER IMAGE:",
        },
        {
            "type": "image_url",
            "image_url": {"url": customer_vision_url, "detail": "auto"},
        },
    ]

    refs = []
    try:
        refs = catalog_reference_content_parts()
    except Exception as e:
        print("REFERENCE IMAGES FALLBACK:", str(e), flush=True)

    if refs:
        content.append({
            "type": "text",
            "text": "REFERENCE CATALOG IMAGES. Each image belongs to the CODE label immediately before it:",
        })
        content.extend(refs)
        print("IMAGE MATCH MODE: CUSTOMER + ALL REFERENCE IMAGES", len(refs) // 2, flush=True)
    else:
        print("IMAGE MATCH MODE: CUSTOMER + RICH TEXT CATALOG FALLBACK", flush=True)

    answer = openai_chat(
        [{"role": "user", "content": content}],
        max_tokens=60,
        temperature=0,
        timeout_seconds=5.8,
    )
    result = parse_json_answer(answer)
    code = normalize_code(result.get("code", ""))
    if code in PRODUCTS:
        print("IMAGE MATCH:", code, flush=True)
        return code, PRODUCTS[code]

    print("IMAGE MATCH: NONE", flush=True)
    return None, None


def ai_find_products_from_image(image_url, caption=""):
    """Identify ALL distinct catalog products visible in one customer image/screenshot."""
    if not OPENAI_API_KEY or not image_url:
        return []
    load_products()
    catalog = product_catalog_text()
    source_url = str(image_url or "").strip()
    if not source_url.startswith(("http://", "https://", "data:")):
        return []

    cached_codes = _get_cached_image_match(source_url)
    if cached_codes is not None:
        print("IMAGE MATCH CACHE HIT:", cached_codes, flush=True)
        return [c for c in cached_codes if c in PRODUCTS]

    customer_vision_url = source_url
    if source_url.startswith(("http://", "https://")):
        try:
            r = requests.get(source_url, timeout=2.0, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw = r.content or b""
            mime = _image_mime_from_bytes(raw, r.headers.get("Content-Type", ""))
            if raw and mime:
                customer_vision_url = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
        except Exception as e:
            print("MULTI IMAGE DOWNLOAD FALLBACK:", str(e), flush=True)

    content = [
        {"type": "text", "text": (
            "Identify ALL distinct shop products visible in the CUSTOMER IMAGE. "
            "This may be a Messenger/Facebook screenshot containing 1, 2, 3, 4, 5 or more products. "
            "Compare every visible product with the labelled reference catalog. "
            "Return each catalog code at most once. Do not treat quantities, prices, phone numbers or dates as codes. "
            "Return ONLY JSON like {\"codes\":[\"0001\",\"0014\"]}. "
            "If none is confident return {\"codes\":[]}. Never invent a code.\n\n"
            f"TEXT CATALOG:\n{catalog}\n\nCUSTOMER CAPTION: {caption}"
        )},
        {"type": "text", "text": "CUSTOMER IMAGE:"},
        {"type": "image_url", "image_url": {"url": customer_vision_url, "detail": "auto"}},
    ]
    try:
        refs = catalog_reference_content_parts()
    except Exception as e:
        refs = []
        print("MULTI IMAGE REFERENCES FALLBACK:", str(e), flush=True)
    if refs:
        content.append({"type": "text", "text": "REFERENCE CATALOG IMAGES; each image belongs to the code label before it:"})
        content.extend(refs)

    answer = openai_chat(
        [{"role": "user", "content": content}],
        max_tokens=120, temperature=0, timeout_seconds=7.5,
    )
    result = parse_json_answer(answer)
    raw_codes = result.get("codes", []) if isinstance(result, dict) else []
    if isinstance(raw_codes, str):
        raw_codes = re.findall(r"\d{1,4}", raw_codes)
    out = []
    for raw_code in raw_codes or []:
        code = normalize_code(raw_code)
        if code in PRODUCTS and code not in out:
            out.append(code)
    _set_cached_image_match(source_url, out)
    print("MULTI IMAGE MATCH CODES:", out, flush=True)
    return out



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




def send_telegram_order_now(message):
    """Send one order to Telegram synchronously with a short timeout.

    This path is intentionally simple and is used only after all order fields
    are already complete, so the customer order is not lost in a daemon thread.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM ENV IS MISSING", flush=True)
        return False

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=5,
        )
        print("TELEGRAM ORDER STATUS:", response.status_code, flush=True)
        print("TELEGRAM ORDER RESPONSE:", response.text, flush=True)
        return response.status_code == 200
    except Exception as e:
        print("TELEGRAM ORDER ERROR:", str(e), flush=True)
        return False


def _telegram_order_worker(order_key, message):
    ok = False
    for attempt in range(1, 4):
        print(f"TELEGRAM ORDER ATTEMPT {attempt}:", order_key, flush=True)
        if send_telegram_message(message):
            ok = True
            break
        time.sleep(1.5 * attempt)
    print("TELEGRAM ORDER FINAL:", order_key, "OK" if ok else "FAILED", flush=True)


def queue_telegram_order(contact_id, message):
    """Queue Telegram in background so ManyChat always gets its reply inside 10 seconds."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM ENV IS MISSING - ORDER NOT QUEUED", flush=True)
        return False

    key = f"{contact_id}|{message}"
    now = now_ts()
    with RECENT_TELEGRAM_LOCK:
        # prune old entries
        for old_key, old_ts in list(RECENT_TELEGRAM_ORDERS.items()):
            if now - old_ts > TELEGRAM_ORDER_DEDUP_SECONDS:
                RECENT_TELEGRAM_ORDERS.pop(old_key, None)
        if key in RECENT_TELEGRAM_ORDERS:
            print("TELEGRAM DUPLICATE ORDER SUPPRESSED:", contact_id, flush=True)
            return True
        RECENT_TELEGRAM_ORDERS[key] = now

    threading.Thread(
        target=_telegram_order_worker,
        args=(key, message),
        daemon=True,
    ).start()
    return True


# =========================
# MANYCHAT INPUT DEDUP
# =========================
def manychat_input_key(data, message, contact_id, image_url):
    context_bits = []
    if isinstance(data, dict):
        for key in (
            "product_code", "code", "ad_code", "source_code", "ref_code",
            "ad_context", "ad_id", "ad_ref", "referral", "referral_payload",
            "ad_name", "ad_title", "ad_headline", "campaign_name",
            "manychat_ad_product", "last_ad_product",
        ):
            value = data.get(key)
            if value not in (None, "", {}):
                try:
                    context_bits.append(f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}")
                except Exception:
                    context_bits.append(f"{key}={value}")
    return "|".join([
        str(contact_id or "").strip(),
        str(message or "").strip(),
        str(image_url or "").strip(),
        *context_bits,
    ])


def is_duplicate_manychat_input(data, message, contact_id, image_url):
    if not contact_id:
        return False
    key = manychat_input_key(data, message, contact_id, image_url)
    now = now_ts()
    with RECENT_MANYCHAT_LOCK:
        for old_key, old_ts in list(RECENT_MANYCHAT_INPUTS.items()):
            if now - old_ts > MANYCHAT_INPUT_DEDUP_SECONDS:
                RECENT_MANYCHAT_INPUTS.pop(old_key, None)
        old = RECENT_MANYCHAT_INPUTS.get(key)
        if old and now - old <= MANYCHAT_INPUT_DEDUP_SECONDS:
            print("MANYCHAT DUPLICATE INPUT SUPPRESSED:", contact_id, flush=True)
            return True
        RECENT_MANYCHAT_INPUTS[key] = now
    return False



def _normalize_reply_fingerprint(value):
    value = str(value or "").strip().lower()
    return re.sub(r"\s+", " ", value)


def remember_manychat_reply_text(value):
    fp = _normalize_reply_fingerprint(value)
    if not fp:
        return
    now = now_ts()
    expiry = now + MANYCHAT_REPLY_TEXT_TTL_SECONDS
    with RECENT_MANYCHAT_REPLY_LOCK:
        for old_fp, expiries in list(RECENT_MANYCHAT_REPLY_TEXTS.items()):
            live = [x for x in (expiries or []) if x > now]
            if live:
                RECENT_MANYCHAT_REPLY_TEXTS[old_fp] = live
            else:
                RECENT_MANYCHAT_REPLY_TEXTS.pop(old_fp, None)
        RECENT_MANYCHAT_REPLY_TEXTS.setdefault(fp, []).append(expiry)


def is_recent_manychat_reply_text(value):
    fp = _normalize_reply_fingerprint(value)
    if not fp:
        return False
    now = now_ts()
    with RECENT_MANYCHAT_REPLY_LOCK:
        expiries = [x for x in RECENT_MANYCHAT_REPLY_TEXTS.get(fp, []) if x > now]
        if not expiries:
            RECENT_MANYCHAT_REPLY_TEXTS.pop(fp, None)
            return False

        # Consume only one expected echo. The same standard reply can be sent
        # to several customers at nearly the same time.
        expiries.sort()
        expiries.pop(0)
        if expiries:
            RECENT_MANYCHAT_REPLY_TEXTS[fp] = expiries
        else:
            RECENT_MANYCHAT_REPLY_TEXTS.pop(fp, None)
        return True


def _identity_aliases(customer_id):
    cid = str(customer_id or "").strip()
    if not cid:
        return set()
    aliases = {cid}
    pending = [cid]
    while pending:
        cur = pending.pop()
        for other in CUSTOMER_ID_ALIASES.get(cur, set()):
            other = str(other or "").strip()
            if other and other not in aliases:
                aliases.add(other)
                pending.append(other)
    return aliases


def bind_customer_identities(*ids):
    clean = [str(x or "").strip() for x in ids if str(x or "").strip()]
    if len(clean) < 2:
        return
    merged = set()
    for cid in clean:
        merged.update(_identity_aliases(cid) or {cid})

    # Keep identity correlation because it is useful for recovering sibling
    # Meta photo attachments that ManyChat may not expose in Dynamic Content.

    # Preserve a remembered ad product across the same customer's IDs too.
    ad_product_code = ""
    for cid in merged:
        sess = ORDER_SESSIONS.get(cid)
        candidate = normalize_code(sess.get("ad_product_code", "")) if isinstance(sess, dict) else ""
        if candidate in PRODUCTS:
            ad_product_code = candidate
            break

    # V52: carry an existing Admin-pause window across newly correlated IDs.
    # Meta manual replies are keyed by PSID, while ManyChat buyer requests can be
    # keyed by Contact ID. If the alias is learned after the Admin reply, failing
    # to copy this state makes the bot resume for the same customer.
    active_admin_pause_until = 0
    for cid in merged:
        active_admin_pause_until = max(
            active_admin_pause_until,
            float(ADMIN_PAUSE_UNTIL.get(cid, 0) or 0),
        )

    for cid in merged:
        CUSTOMER_ID_ALIASES[cid] = set(merged - {cid})
        if ad_product_code:
            get_order_session(cid)["ad_product_code"] = ad_product_code
        if active_admin_pause_until > now_ts():
            ADMIN_PAUSE_UNTIL[cid] = active_admin_pause_until

    if active_admin_pause_until > now_ts():
        print(
            "V52 ADMIN PAUSE SYNCED ACROSS CUSTOMER IDS:",
            sorted(merged),
            "UNTIL",
            active_admin_pause_until,
            flush=True,
        )
    print("CUSTOMER IDS BOUND:", sorted(merged), flush=True)


def _manychat_identity_from_payload(data, contact_id):
    ids = [contact_id]
    if isinstance(data, dict):
        for key in (
            "psid", "facebook_psid", "fb_psid", "page_scoped_id", "page_scoped_user_id",
            "messenger_id", "facebook_id", "sender_id", "user_psid"
        ):
            value = str(data.get(key, "") or "").strip()
            if value:
                ids.append(value)
        for obj_key in ("contact", "subscriber", "user"):
            obj = data.get(obj_key)
            if isinstance(obj, dict):
                for key in ("psid", "facebook_psid", "page_scoped_id", "messenger_id"):
                    value = str(obj.get(key, "") or "").strip()
                    if value:
                        ids.append(value)
    bind_customer_identities(*ids)
    return ids


def _normalize_person_identity_name(value):
    value = str(value or "").strip().casefold()
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", value)
    value = re.sub(r"[^0-9a-zက-႟一-鿿]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def remember_manychat_identity(contact_id, account_name=""):
    """Remember a recent ManyChat Full Name -> Contact Id mapping."""
    cid = str(contact_id or "").strip()
    name_key = _normalize_person_identity_name(account_name)
    if not cid or not name_key:
        return
    now = now_ts()
    with RECENT_MANYCHAT_IDENTITIES_LOCK:
        for old_key, row in list(RECENT_MANYCHAT_IDENTITIES.items()):
            if now - float(row.get("ts", 0) or 0) > RECENT_MANYCHAT_IDENTITIES_TTL_SECONDS:
                RECENT_MANYCHAT_IDENTITIES.pop(old_key, None)
        RECENT_MANYCHAT_IDENTITIES[name_key] = {
            "contact_id": cid,
            "ts": now,
            "display_name": str(account_name or "").strip(),
        }


def _facebook_profile_name(psid):
    """Best-effort PSID -> Facebook profile name using the Page access token."""
    sid = str(psid or "").strip()
    if not sid or not PAGE_ACCESS_TOKEN:
        return ""
    try:
        url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{sid}"
        r = requests.get(
            url,
            params={"fields": "name,first_name,last_name", "access_token": PAGE_ACCESS_TOKEN},
            timeout=8,
        )
        if r.status_code != 200:
            print("V42 PROFILE LOOKUP FAILED:", sid, r.status_code, r.text[:200], flush=True)
            return ""
        payload = r.json() if r.content else {}
        name = str(payload.get("name", "") or "").strip()
        if not name:
            name = " ".join(
                x for x in (
                    str(payload.get("first_name", "") or "").strip(),
                    str(payload.get("last_name", "") or "").strip(),
                ) if x
            ).strip()
        return name
    except Exception as e:
        print("V42 PROFILE LOOKUP ERROR:", sid, str(e), flush=True)
        return ""


def resolve_admin_psid_to_manychat_contact(psid):
    """Bind an Admin echo PSID to the same ManyChat Contact Id before ON/OFF."""
    sid = str(psid or "").strip()
    if not sid:
        return ""

    # Already correlated: nothing else to do.
    aliases = _identity_aliases(sid)
    if len(aliases) > 1:
        for alias in aliases:
            if alias != sid:
                return alias

    profile_name = _facebook_profile_name(sid)
    name_key = _normalize_person_identity_name(profile_name)
    if not name_key:
        return ""

    now = now_ts()
    row = None
    with RECENT_MANYCHAT_IDENTITIES_LOCK:
        for old_key, old_row in list(RECENT_MANYCHAT_IDENTITIES.items()):
            if now - float(old_row.get("ts", 0) or 0) > RECENT_MANYCHAT_IDENTITIES_TTL_SECONDS:
                RECENT_MANYCHAT_IDENTITIES.pop(old_key, None)
        row = RECENT_MANYCHAT_IDENTITIES.get(name_key)

    if not row:
        print("V42 ADMIN PSID NAME NOT FOUND IN MANYCHAT:", sid, profile_name, flush=True)
        return ""

    cid = str(row.get("contact_id", "") or "").strip()
    if not cid:
        return ""

    bind_customer_identities(sid, cid)
    print(
        "V42 ADMIN PSID/MANYCHAT CONTACT BOUND BY FULL NAME:",
        sid, "<->", cid, profile_name,
        flush=True,
    )
    return cid


def remember_manychat_inbound(contact_id, message="", image_url=""):
    cid = str(contact_id or "").strip()
    if not cid:
        return
    text_fp = _normalize_reply_fingerprint(message)
    image_fp = str(image_url or "").strip().split("?")[0][-180:]
    now = now_ts()
    with RECENT_MANYCHAT_INBOUND_LOCK:
        RECENT_MANYCHAT_INBOUND[:] = [
            row for row in RECENT_MANYCHAT_INBOUND
            if now - row.get("ts", 0) <= RECENT_MANYCHAT_INBOUND_TTL_SECONDS
        ]
        RECENT_MANYCHAT_INBOUND.append({
            "ts": now, "contact_id": cid, "text": text_fp, "image": image_fp
        })


def correlate_meta_sender_to_manychat(sender_id, message="", image_url=""):
    sender_id = str(sender_id or "").strip()
    if not sender_id:
        return
    text_fp = _normalize_reply_fingerprint(message)
    image_fp = str(image_url or "").strip().split("?")[0][-180:]
    now = now_ts()
    best = None
    with RECENT_MANYCHAT_INBOUND_LOCK:
        RECENT_MANYCHAT_INBOUND[:] = [
            row for row in RECENT_MANYCHAT_INBOUND
            if now - row.get("ts", 0) <= RECENT_MANYCHAT_INBOUND_TTL_SECONDS
        ]
        for row in reversed(RECENT_MANYCHAT_INBOUND):
            text_match = bool(text_fp and row.get("text") == text_fp)
            image_match = bool(image_fp and row.get("image") == image_fp)
            if text_match or image_match:
                best = row.get("contact_id")
                break
    if best:
        bind_customer_identities(sender_id, best)


def remember_meta_inbound(sender_id, message="", image_url=""):
    """Remember native Meta inbound so a later ManyChat POST can bind to its PSID."""
    sid = str(sender_id or "").strip()
    if not sid:
        return
    now = now_ts()
    row = {
        "ts": now,
        "sender_id": sid,
        "text": _normalize_reply_fingerprint(message),
        "image": str(image_url or "").strip().split("?")[0][-180:],
        "claimed_by": "",
    }
    with RECENT_META_INBOUND_LOCK:
        RECENT_META_INBOUND[:] = [
            r for r in RECENT_META_INBOUND
            if now - r.get("ts", 0) <= RECENT_META_INBOUND_TTL_SECONDS
        ]
        RECENT_META_INBOUND.append(row)


def correlate_manychat_to_meta(contact_id, message="", image_url=""):
    """Bind ManyChat Contact ID to Meta PSID regardless of webhook arrival order."""
    cid = str(contact_id or "").strip()
    if not cid:
        return ""
    text_fp = _normalize_reply_fingerprint(message)
    image_fp = str(image_url or "").strip().split("?")[0][-180:]
    now = now_ts()
    best = None
    best_age = None
    with RECENT_META_INBOUND_LOCK:
        RECENT_META_INBOUND[:] = [
            r for r in RECENT_META_INBOUND
            if now - r.get("ts", 0) <= RECENT_META_INBOUND_TTL_SECONDS
        ]
        for row in RECENT_META_INBOUND:
            if row.get("claimed_by") not in ("", cid):
                continue
            text_match = bool(text_fp and row.get("text") == text_fp)
            image_match = bool(image_fp and row.get("image") == image_fp)
            if not (text_match or image_match):
                continue
            age = abs(now - row.get("ts", now))
            if best is None or age < best_age:
                best, best_age = row, age
        if best:
            best["claimed_by"] = cid
    if best:
        psid = str(best.get("sender_id", "") or "").strip()
        if psid:
            bind_customer_identities(cid, psid)
            print("V41 MANYCHAT/META IDS CORRELATED:", cid, "<->", psid, flush=True)
            return psid
    return ""


# =========================
# ADMIN TAKEOVER / PAGE ECHO
# =========================
def pause_for_admin(customer_id):
    """Pause only this customer after a real Admin/manual takeover or handoff."""
    if not customer_id:
        return
    until = now_ts() + ADMIN_PAUSE_MINUTES * 60
    aliases = _identity_aliases(customer_id) or {str(customer_id)}
    aliases.discard("")
    for cid in aliases:
        ADMIN_PAUSE_UNTIL[cid] = max(ADMIN_PAUSE_UNTIL.get(cid, 0), until)
    print("ADMIN PAUSE:", sorted(aliases), "UNTIL", until, flush=True)


def admin_is_active(customer_id):
    """Return True while this customer's Admin takeover window is active."""
    aliases = _identity_aliases(customer_id) or {str(customer_id or "")}
    aliases.discard("")
    now = now_ts()
    active_until = 0
    for cid in list(aliases):
        until = ADMIN_PAUSE_UNTIL.get(cid, 0)
        if until > now:
            active_until = max(active_until, until)
        elif cid in ADMIN_PAUSE_UNTIL:
            ADMIN_PAUSE_UNTIL.pop(cid, None)
    if active_until > now:
        for cid in aliases:
            ADMIN_PAUSE_UNTIL[cid] = active_until
        return True
    return False


def _set_manychat_echo_ignore(customer_id, seconds=None):
    """Ignore only short-lived Meta echoes generated by ManyChat/bot output."""
    if not customer_id:
        return
    ttl = max(int(seconds or MANYCHAT_ECHO_GRACE_SECONDS), 60)
    until = now_ts() + ttl
    aliases = _identity_aliases(customer_id) or {str(customer_id)}
    for cid in aliases:
        if cid:
            MANYCHAT_ECHO_IGNORE_UNTIL[cid] = max(MANYCHAT_ECHO_IGNORE_UNTIL.get(cid, 0), until)


def _manychat_echo_ignore_active(customer_id):
    aliases = _identity_aliases(customer_id) or {str(customer_id or "")}
    aliases.discard("")
    now = now_ts()
    active_until = 0
    for cid in aliases:
        until = MANYCHAT_ECHO_IGNORE_UNTIL.get(cid, 0)
        if until > now:
            active_until = max(active_until, until)
        elif cid in MANYCHAT_ECHO_IGNORE_UNTIL:
            MANYCHAT_ECHO_IGNORE_UNTIL.pop(cid, None)
    if active_until > now:
        for cid in aliases:
            MANYCHAT_ECHO_IGNORE_UNTIL[cid] = active_until
        return True
    return False


def mark_order_completed(customer_id):
    aliases = _identity_aliases(customer_id) or {str(customer_id or "")}
    now = now_ts()
    for cid in aliases:
        if cid:
            POST_ORDER_COMPLETED_AT[cid] = now


def post_order_ack_active(customer_id):
    aliases = _identity_aliases(customer_id) or {str(customer_id or "")}
    aliases.discard("")
    now = now_ts()
    latest = 0
    for cid in aliases:
        ts = POST_ORDER_COMPLETED_AT.get(cid, 0)
        if ts and now - ts <= POST_ORDER_ACK_TTL_SECONDS:
            latest = max(latest, ts)
        elif cid in POST_ORDER_COMPLETED_AT:
            POST_ORDER_COMPLETED_AT.pop(cid, None)
    if latest:
        for cid in aliases:
            POST_ORDER_COMPLETED_AT[cid] = latest
        return True
    return False


def is_post_order_ack(text):
    """Short acknowledgement after a completed order must never start a second order."""
    raw = str(text or "").strip().casefold()
    compact = re.sub(r"[\s.!?၊။,]+", "", raw)
    ack = {
        "ok", "okay", "ဟုတ်", "ဟုတ်ကဲ့", "အိုကေ", "အိုကေပါ",
        "ကျေးဇူး", "ကျေးဇူးပါ", "ကျေးဇူးတင်ပါတယ်", "ဟုတ်ပါပြီ",
        "ကောင်းပါပြီ", "အင်း", "အေး", "thanks", "thankyou", "thankyouပါ",
        "👍", "🙏",
    }
    return compact in ack


def handle_echo_message(event, message_data):
    """Restore the proven pre-V46 Admin-pause path without restoring ON/OFF commands.

    Bot/ManyChat echoes are ignored. A real manual Page/Admin outgoing message pauses
    only that customer for ADMIN_PAUSE_MINUTES. Identity aliases are resolved first so
    a Meta PSID pause propagates to the matching ManyChat Contact ID.
    """
    message_data = message_data or {}
    sender_id = str(event.get("sender", {}).get("id", "") or "").strip()
    recipient_id = str(event.get("recipient", {}).get("id", "") or "").strip()
    echo_text = str(message_data.get("text", "") or "").strip()
    mid = str(message_data.get("mid", "") or "").strip()

    print(
        "V51 PAGE ECHO RECEIVED:",
        {
            "sender": sender_id,
            "recipient": recipient_id,
            "text": echo_text[:120],
            "mid": mid,
        },
        flush=True,
    )

    # Direct Graph API messages sent by this bot are known by message id.
    if mid and mid in BOT_SENT_MESSAGE_IDS:
        BOT_SENT_MESSAGE_IDS.discard(mid)
        print("V51 BOT ECHO IGNORED:", mid, flush=True)
        return

    # ManyChat Dynamic Content text echoed back by Meta must not look like Admin.
    if echo_text and is_recent_manychat_reply_text(echo_text):
        print("V51 MANYCHAT TEXT ECHO IGNORED:", echo_text[:120], flush=True)
        return

    # For Page is_echo events recipient is normally the customer's PSID.
    customer_id = recipient_id or sender_id
    if not customer_id:
        print("V51 PAGE ECHO IGNORED: NO CUSTOMER ID", flush=True)
        return

    # Re-bind Meta PSID <-> ManyChat Contact ID before storing pause state.
    resolved_contact_id = resolve_admin_psid_to_manychat_contact(customer_id)
    if resolved_contact_id:
        bind_customer_identities(customer_id, resolved_contact_id)

    # Manual text is authoritative. Do not let a recent bot-image grace window swallow
    # a human Admin message; this was the critical post-V46 regression protection.
    if echo_text:
        pause_for_admin(customer_id)
        print(
            "V51 MANUAL ADMIN TEXT DETECTED - BOT PAUSED:",
            sorted(_identity_aliases(customer_id) or {customer_id}),
            flush=True,
        )
        return

    # Text-less image/attachment echoes need the historical grace-window check because
    # bot product-image messages may not have a useful text fingerprint.
    if _manychat_echo_ignore_active(customer_id):
        print("V51 MANYCHAT IMAGE/ATTACHMENT ECHO IGNORED:", customer_id, flush=True)
        return

    pause_for_admin(customer_id)
    print(
        "V51 MANUAL ADMIN OUTGOING DETECTED - BOT PAUSED:",
        sorted(_identity_aliases(customer_id) or {customer_id}),
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
        "ad_product_code": "",
        "quantity_confirmed": False,
        "_account_name_locked": False,
    }



def get_order_session(sender_id):
    return ORDER_SESSIONS.setdefault(
        str(sender_id),
        new_order_session(),
    )


QUANTITY_UNITS_RE = (
    r"(?:pcs?|pieces?|pc|ခု|ထုပ်|ဘူး|ချောင်း|လုံး|စုံ|ကဒ်|ကတ်|စက်|ပုံး|အိတ်|"
    r"set|sets|pack|packs|pair|pairs)"
)

BURMESE_QUANTITY_WORDS = {
    "တစ်": 1, "တ": 1, "နှစ်": 2, "သုံး": 3, "လေး": 4, "ငါး": 5,
    "ခြောက်": 6, "ခုနစ်": 7, "ရှစ်": 8, "ကိုး": 9, "ဆယ်": 10,
}

def _bounded_quantity(number):
    try:
        qty = int(number)
    except Exception:
        return None
    if qty < 1 or qty > 999:
        return None
    return qty

def _quantity_after_position(text, end_pos):
    tail = text[end_pos:end_pos + 40]
    patterns = (
        rf"^\s*[xX*]\s*(\d{{1,3}})",
        rf"^\s*(\d{{1,3}})\s*{QUANTITY_UNITS_RE}",
        r"^\s*[-:]\s*(\d{1,3})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, tail, flags=re.IGNORECASE)
        if match:
            qty = _bounded_quantity(match.group(1))
            if qty is not None:
                return qty

    compact_tail = re.sub(r"\s+", "", tail)
    for word, qty in BURMESE_QUANTITY_WORDS.items():
        if re.match(rf"^{re.escape(word)}{QUANTITY_UNITS_RE}", compact_tail, flags=re.IGNORECASE):
            return qty
    return 1

def _explicit_code_mentions(text):
    value = _western_digits(str(text or ""))
    mentions = []
    occupied = []

    label_patterns = (
        r"(?i)(?:code|product\s*code)\s*[:#-]?\s*(\d{1,4})(?!\d)",
        r"ကုဒ်\s*[:#-]?\s*(\d{1,4})(?!\d)",
    )
    for pattern in label_patterns:
        for match in re.finditer(pattern, value):
            code = normalize_code(match.group(1))
            if code in PRODUCTS:
                mentions.append((code, match.start(), match.end()))
                occupied.append((match.start(), match.end()))

    for match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", value):
        if any(a <= match.start() < b for a, b in occupied):
            continue
        code = normalize_code(match.group(1))
        if code in PRODUCTS:
            mentions.append((code, match.start(), match.end()))

    # Do NOT infer a short catalog code from a bare number. Bare 1..999 can be
    # a follow-up quantity and must never silently become 0001..0999. A bare
    # four-digit code is already collected by the canonical-code loop above.

    mentions.sort(key=lambda x: x[1])
    return mentions

def find_codes_and_quantities(text):
    load_products()
    value = _western_digits(str(text or ""))
    found = {}

    # V38: a list such as 12/13/14 or 12,13,14 is explicit multi-code context.
    # It is safe to normalize short codes here because at least two tokens are
    # deliberately separated as a product list; a bare "2" is still a quantity.
    compact_list = value.strip()
    if re.fullmatch(r"\s*\d{1,4}(?:\s*[/,+]\s*\d{1,4})+\s*", compact_list):
        for raw_code in re.findall(r"\d{1,4}", compact_list):
            c = normalize_code(raw_code)
            if c in PRODUCTS:
                found[c] = 1

    mentions = _explicit_code_mentions(value)

    for index, (code, _start, end) in enumerate(mentions):
        segment_end = mentions[index + 1][1] if index + 1 < len(mentions) else len(value)
        local_value = value[:segment_end]
        found[code] = _quantity_after_position(local_value, end)

    return found


def _western_digits(value):
    table = str.maketrans("၀၁၂၃၄၅၆၇၈၉", "0123456789")
    return str(value or "").translate(table)


def extract_explicit_quantity(text):
    """Extract explicit quantity safely; 1..10 are fully covered."""
    raw = str(text or "").strip()
    value = _western_digits(raw)

    patterns = (
        r"[xX*]\s*(\d{1,3})(?!\d)",
        rf"(?<!\d)(\d{{1,3}})\s*{QUANTITY_UNITS_RE}",
        r"(?:qty|quantity)\s*[:=]?\s*(\d{1,3})(?!\d)",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            qty = _bounded_quantity(match.group(1))
            if qty is not None:
                return qty

    compact = re.sub(r"\s+", "", value.lower())
    for word, qty in BURMESE_QUANTITY_WORDS.items():
        if re.search(rf"{re.escape(word)}{QUANTITY_UNITS_RE}", compact, flags=re.IGNORECASE):
            return qty

    burmese_one_phrases = (
        "တခုယူ", "တစ်ခုယူ", "တခုယူမယ်", "တစ်ခုယူမယ်",
        "တခုလိုချင်", "တစ်ခုလိုချင်",
    )
    if any(token in compact for token in burmese_one_phrases):
        return 1
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
- A delivery address may be written in Burmese, English, or mixed language.
- Extract address ONLY when the text is actually a delivery/location address.
- Questions or chat such as price, delivery fee, availability, how to use,
  "အော်ဒါတင်ပေးမှာလား", "ဘယ်လောက်လဲ", "ပို့ခဘယ်လောက်လဲ",
  "can I order?", "how much?", "where are you?" are NOT addresses.
- If a line mixes a phone number with an address, remove only the phone and keep the address.
- The buyer name is supplied separately from the Facebook account; do not guess a name from chat text.
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
        "ရန်ကုန်", "ရန်ကုန်တိုင်း", "yangon", "yangon region", "rangoon",
        # Yangon townships / common customer spellings
        "တာမွေ", "tamwe", "tarmwe", "tamwe township", "tarmwe township",
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
        "မော်လမြိုင်ကျွန်း", "mawlamyinegyun", "ဟင်္သာတ", "hinthada",
        "ဧရာဝတီ", "ayeyarwady", "irrawaddy",
        "အိမ်မဲ", "einme", "einme township",
        "မြောင်းမြ", "myaungmya", "ဝါးခယ်မ", "wakema", "လပွတ္တာ", "labutta",
        "မအူပင်", "maubin", "ဖျာပုံ", "pyapon", "ဘိုကလေး", "bogale",
        "ကျောင်းကုန်း", "kyaunggon", "ကျုံပျော်", "kyonpyaw", "ငပုတော", "ngapudaw",
        "ဇလွန်", "zalun", "လေးမျက်နှာ", "laymyethna", "မြန်အောင်", "myanaung",
        "ကြံခင်း", "kyangin", "h is not used",
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


def is_likely_delivery_address(value):
    """True only when text looks like a real delivery address, not a buyer question.

    Supports Burmese, English, and mixed Myanmar addresses.  Question/chat text
    is rejected BEFORE township matching so messages such as "Tamwe ပို့ခဘယ်လောက်လဲ"
    are never stored as an address just because they contain a location name.
    """
    text = str(value or "").strip()
    if not text:
        return False

    low = text.lower()
    digits = re.sub(r"\D", "", _western_digits(text))

    address_tokens = (
        "လမ်း", "လမ်းမ", "ရပ်ကွက်", "မြို့နယ်", "မြို့", "ရွာ", "ကျေးရွာ",
        "အမှတ်", "တိုက်", "အခန်း", "ထပ်", "အိမ်", "ဈေး", "စက်မှုဇုန်",
        "road", " rd", "street", " st", "township", "ward", "quarter",
        "village", "city", "state", "region", "district", "block", "lane",
        "avenue", "ave", "building", "apartment", "floor", "room", "no.",
    )
    has_address_structure = any(tok in low for tok in address_tokens)

    # Quantity numbers are not house/building numbers. Strip explicit quantity
    # expressions before the numeric-address heuristic.
    numeric_address_probe = _western_digits(low)
    numeric_address_probe = re.sub(
        rf"(?<!\d)\d{{1,3}}\s*{QUANTITY_UNITS_RE}",
        " ",
        numeric_address_probe,
        flags=re.IGNORECASE,
    )
    numeric_address_probe = re.sub(
        r"[xX*]\s*\d{1,3}(?!\d)",
        " ",
        numeric_address_probe,
        flags=re.IGNORECASE,
    )
    has_house_number = bool(re.search(r"\b\d{1,4}[/-]?[a-zA-Z]?\b", numeric_address_probe))
    has_phone = 9 <= len(digits) <= 13

    # Sales questions/chat must not become addresses, even when they mention a
    # township/city.  A genuinely structured address or address+phone can pass.
    question_tokens = (
        "လား", "လဲ", "ဘယ်", "ဘယ်လောက်", "ရမလား", "ရှိလား", "ရှိသေးလား",
        "ပို့ခ", "စျေး", "ဈေး", "အသုံးပြု", "ဘယ်လို", "အော်ဒါတင်ပေးမှာလား",
        "အော်ဒါတင်ပေးမလား", "how much", "price", "delivery fee",
        "can i", "can you", "do you", "is it", "how to", "what", "where",
        "available", "in stock", "order?", "?",
    )
    if any(tok in low for tok in question_tokens) and not (has_phone or (has_address_structure and has_house_number)):
        return False

    if is_order_noise_segment(text):
        return False

    # Explicit place/township/state clue.
    if detect_delivery_area_from_text(text):
        return True

    if has_address_structure:
        return True

    # Postal-looking text: a house/building number plus several words is often
    # an address even when the township/city is omitted.
    if has_house_number and len(text.split()) >= 3:
        return True

    return False


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
    # With ManyChat the buyer name comes from the Facebook account, so do not
    # treat arbitrary chat as a name/address. Only location-looking text advances
    # the order; ambiguous text can still be classified by the AI order parser.
    if session.get("last_product_code") in PRODUCTS:
        if session.get("name") and not session.get("address") and is_likely_delivery_address(value):
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
        "တစ်ခုယူမယ်", "အော်ဒါတင်ပေးမှာလား", "အော်ဒါတင်ပေးမလား",
        "အော်ဒါတင်မှာလား", "မှာပေးမှာလား", "လိုချင်ပါတယ်", "လိုချင်ပါသည်",
        "one", "1pc", "1pcs", "x1", "order", "buy",
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

    # Phone: support both Western and Myanmar digits, without swallowing quantity text.
    # IMPORTANT: remove ONLY the phone substring before parsing Name/Address.
    # Older versions discarded the entire segment whenever that segment also
    # contained the phone number, so a one-shot message like
    # "Hpone Myint 16 Kyaik Kasan Rd Tamwe 09777888899" lost the address and
    # forced the buyer to send the phone again separately.
    phone_source = _western_digits(value)

    phone_match = re.search(
        r"(?<![0-9])(09(?:[\s\-().]*[0-9]){7,11})(?![0-9])",
        phone_source,
        flags=re.IGNORECASE,
    )
    if not phone_match:
        phone_match = re.search(
            r"(?<![0-9])(09[0-9\s\-().]{7,22})(?![0-9])",
            phone_source,
            flags=re.IGNORECASE,
        )

    value_without_phone = phone_source
    if phone_match:
        phone_digits = re.sub(r"\D", "", phone_match.group(1))
        if 9 <= len(phone_digits) <= 13 and phone_digits.startswith("09"):
            result["phone"] = phone_digits
            start, end = phone_match.span(1)
            value_without_phone = (phone_source[:start] + " " + phone_source[end:]).strip()

    parts = [p.strip() for p in re.split(r"[/\n|]+", value_without_phone) if p.strip()]

    for code, qty in find_codes_and_quantities(value).items():
        if code in PRODUCTS:
            result["items"].append({"code": code, "quantity": qty})

    clean_parts = []
    for part in parts:
        if find_codes_and_quantities(part):
            continue
        if is_order_noise_segment(part):
            continue
        clean_parts.append(part.strip(" ,.-"))
    clean_parts = [p for p in clean_parts if p]

    # Multi-part: first non-address-looking segment is usually name.
    if clean_parts:
        first = clean_parts[0]
        if not current.get("name") and len(first) <= 80 and not looks_like_order_details(first):
            result["name"] = first
            clean_parts = clean_parts[1:]

    # If ManyChat/FB account name already supplied the customer name, NEVER
    # try to reinterpret the remaining one-shot text as another name. Everything
    # left after removing phone/code/order-noise is the delivery address.
    if current.get("name") and clean_parts and not current.get("address"):
        candidate = " / ".join(clean_parts).strip()
        if is_likely_delivery_address(candidate):
            result["address"] = candidate
            clean_parts = []

    # If the name was already collected in a previous message, all remaining
    # non-phone text is address. This fixes customers sending Name / Address /
    # Phone as three separate messages.
    if clean_parts:
        if current.get("name") or result.get("name"):
            candidate = " / ".join(clean_parts).strip()
            if is_likely_delivery_address(candidate):
                result["address"] = candidate
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
            if candidate and candidate != current.get("name") and is_likely_delivery_address(candidate):
                result["address"] = candidate

    result["delivery_area"] = detect_delivery_area_from_text(
        result["address"] or value
    )
    return result

def is_quantity_only_message(text):
    value = _western_digits(str(text or "").strip())
    compact = re.sub(r"\s+", "", value.lower())

    patterns = (
        r"^[xX*]\s*\d{1,3}$",
        rf"^\d{{1,3}}\s*{QUANTITY_UNITS_RE}$",
        r"^(?:qty|quantity)\s*[:=]?\s*\d{1,3}$",
    )
    if any(re.fullmatch(pattern, value, flags=re.IGNORECASE) for pattern in patterns):
        return True

    for word in BURMESE_QUANTITY_WORDS:
        if re.fullmatch(rf"{re.escape(word)}{QUANTITY_UNITS_RE}", compact, flags=re.IGNORECASE):
            return True
    return False


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
                if isinstance(extracted, dict):
                    ai_address = str(extracted.get("address", "") or "").strip()
                    if ai_address and not is_likely_delivery_address(ai_address):
                        print("AI ADDRESS REJECTED AS NON-ADDRESS:", ai_address, flush=True)
                        extracted["address"] = ""
                        extracted["delivery_area"] = ""

                    explicit_codes = find_codes_and_quantities(message)
                    target_code = session.get("last_product_code")
                    if (
                        extract_explicit_quantity(message) is not None
                        and not explicit_codes
                        and target_code in PRODUCTS
                    ):
                        safe_items = []
                        for item in extracted.get("items", []) or []:
                            item_code = normalize_code(item.get("code", ""))
                            if item_code == target_code:
                                safe_items.append(item)
                        extracted["items"] = safe_items

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
    """Build the exact compact order format used by the shop/admin."""
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
        item_name = str(get_row_value(product, "Product Name", "Name")).strip()
        unit_price = product_price(code)
        item_total = unit_price * qty
        subtotal += item_total
        total_pcs += qty

        if qty == 1:
            item_parts.append(
                f"Code {code} {item_name} စျေးနှုန်း - {unit_price:,} Ks"
            )
        else:
            item_parts.append(
                f"Code {code} {item_name} စျေးနှုန်း - {unit_price:,} Ks x {qty} = {item_total:,} Ks"
            )

    grand_total = subtotal + delivery_fee
    items_text = " + ".join(item_parts)

    if delivery_area == "yangon":
        delivery_text = f"ရန်ကုန်ပို့ခ - {delivery_fee:,} Ks"
    else:
        delivery_text = f"နယ်ပို့ခ - {delivery_fee:,} Ks"

    cod_text = "COD" if total_pcs <= 1 else f"COD {total_pcs} PCS"

    return (
        f"{name} / {address} / {phone} / "
        f"{items_text} + {delivery_text} / "
        f"စုစုပေါင်း - {grand_total:,} Ks / {cod_text}"
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
    "ဝယ်ချင်ပါတယ်",
    "ဝယ်ချင်တယ်",
    "ဝယ်မယ်",
    "ဝယ်ပါမယ်",
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
    # Once handed to a human, the bot must stay silent for this customer.
    pause_for_admin(sender_id)





def generic_purchase_intent(text):
    """Order/purchase wording that does not identify a product by itself."""
    low = str(text or "").strip().lower()
    return bool(low and is_order_message(low))


def has_catalog_product_clue_text(text):
    """Return True when an order sentence contains a catalog-product clue.

    This rule is GLOBAL for every current and future Google Sheet item. Product
    names, Burmese/English/Chinese names, aliases, keywords, model numbers, or
    descriptive text can be followed by quantity/order wording. A bare quantity
    such as ``2ခုယူမယ်`` still does not make AI guess a product.
    """
    value = _western_digits(str(text or "")).casefold()
    if not value:
        return False

    # Remove known purchase phrases longest-first.
    for word in sorted(ORDER_WORDS, key=len, reverse=True):
        value = value.replace(str(word).casefold(), " ")

    # Remove explicit quantities and unit words.
    value = re.sub(r"[xX*]?\s*\d{1,3}\s*" + QUANTITY_UNITS_RE, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:qty|quantity)\b\s*[:=]?\s*\d{1,3}", " ", value, flags=re.IGNORECASE)
    for word in BURMESE_QUANTITY_WORDS:
        value = value.replace(word, " ")

    # Ignore punctuation/whitespace; keep real Latin/Burmese/Chinese clue text.
    clue = re.sub(r"[^a-z0-9က-႟一-鿿]+", "", value)
    return len(clue) >= 2


def find_product_from_ad_context_value(value):
    """Resolve ad/referral metadata using Google Sheet ad-related columns only.

    Supported automatically when a Sheet column name contains words such as
    Ad ID, Ad Name, Campaign, Referral, Ref, Payload, Source, or Ad Code.
    No product-specific Python mapping is required.
    """
    load_products()
    text = str(value or "").strip()
    if not text:
        return "", None

    # Product code/name embedded directly in a value remains valid.
    code, product = find_product_by_code(text)
    if product:
        return code, product
    code, product = find_product_by_name(text)
    if product:
        return code, product

    target = _compact_product_match_text(text)
    if not target:
        return "", None
    ad_key_words = ("ad", "campaign", "ref", "referral", "payload", "source", "ကြော်ငြာ")
    for code, product in PRODUCTS.items():
        for key, raw in product.items():
            klow = str(key or "").casefold()
            if not any(word in klow for word in ad_key_words):
                continue
            raw_text = str(raw or "").strip()
            if not raw_text:
                continue
            for part in re.split(r"[,;|\n]+", raw_text):
                if _compact_product_match_text(part) == target:
                    print("SHEET AD CONTEXT MATCH:", key, text, "->", code, flush=True)
                    return code, product
    return "", None


def remember_meta_ad_context(customer_id, referral):
    cid = str(customer_id or "").strip()
    if not cid or not isinstance(referral, dict):
        return "", None
    now = now_ts()
    with META_AD_CONTEXT_LOCK:
        for old_id, row in list(META_AD_CONTEXT.items()):
            if now - row.get("ts", 0) > META_AD_CONTEXT_TTL_SECONDS:
                META_AD_CONTEXT.pop(old_id, None)
        META_AD_CONTEXT[cid] = {"ts": now, "referral": dict(referral)}

    for key in ("product_code", "code", "ad_id", "ad_name", "ref", "payload", "source"):
        if key in referral:
            code, product = find_product_from_ad_context_value(referral.get(key))
            if product:
                get_order_session(cid)["ad_product_code"] = code
                print("META AD PRODUCT REMEMBERED:", cid, code, key, flush=True)
                return code, product
    print("META AD CONTEXT SAVED (NO SHEET MAP YET):", cid, referral, flush=True)
    return "", None


def restore_meta_ad_product(customer_id):
    aliases = _identity_aliases(customer_id) or {str(customer_id or "")}
    now = now_ts()
    with META_AD_CONTEXT_LOCK:
        rows = [(cid, META_AD_CONTEXT.get(cid)) for cid in aliases]
    for cid, row in rows:
        if not row or now - row.get("ts", 0) > META_AD_CONTEXT_TTL_SECONDS:
            continue
        referral = row.get("referral", {}) or {}
        for key in ("product_code", "code", "ad_id", "ad_name", "ref", "payload", "source"):
            if key in referral:
                code, product = find_product_from_ad_context_value(referral.get(key))
                if product:
                    return code, product
    return "", None


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
        code, product = find_product_from_ad_context_value(text)
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
    """Remember outgoing ManyChat replies so their Meta echoes are never treated as manual Admin."""
    try:
        messages = (
            response.get("content", {}).get("messages", [])
            if isinstance(response, dict)
            else []
        )
        for msg in messages:
            if isinstance(msg, dict) and msg.get("type") == "text":
                remember_manychat_reply_text(msg.get("text", ""))

        if contact_id and messages:
            # Set the short ignore window for both ManyChat Contact Id and any bound
            # Meta PSID, so outgoing text/image echoes never trigger Admin takeover.
            _set_manychat_echo_ignore(contact_id)
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

    image_url = manychat_product_image_url(product)
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



def manychat_products_response(codes, include_order_prompt=True):
    """Return every recognized product, preserving image + detail + price.

    ManyChat Dynamic Content accepts at most 10 messages. For 1-3 products keep
    the historical 3-message flow (image, detail, price). For 4-5 products use
    exactly 2 messages per product (image, then combined detail+price), placing
    the order prompt inside the final text so NO product image/code is dropped.
    """
    normalized = []
    seen = set()
    for raw_code in codes or []:
        code = normalize_code(raw_code)
        if code in PRODUCTS and code not in seen:
            seen.add(code)
            normalized.append(code)

    if not normalized:
        return manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။")

    # The shop flow is designed for up to five products in one buyer selection.
    # If an AI result ever contains more, keep the first five deterministic codes
    # rather than silently overflowing ManyChat's 10-message hard limit.
    normalized = normalized[:5]
    messages = []
    order_prompt = "မှာယူလိုပါက အမည် / လိပ်စာအပြည့်အစုံ / ဖုန်းနံပါတ် ကို အပြည့်အစုံရေးပို့ပေးပါရှင်။"
    has_sellable = any(product_is_sellable(PRODUCTS[c]) for c in normalized)

    if len(normalized) <= 3:
        for code in normalized:
            product = PRODUCTS[code]
            image_url = manychat_product_image_url(product)
            if image_url:
                messages.append({"type": "image", "url": image_url})
            else:
                print("PRODUCT IMAGE MISSING:", code, flush=True)

            detail = product_detail(product)
            if detail:
                messages.append({"type": "text", "text": detail})
            messages.append({"type": "text", "text": product_reply(code, product)})

        if include_order_prompt and has_sellable:
            messages.append({"type": "text", "text": order_prompt})
    else:
        # 4 products => 8 messages; 5 products => exactly 10 messages.
        # Every product gets its own image and its own detail+price text.
        for idx, code in enumerate(normalized):
            product = PRODUCTS[code]
            image_url = manychat_product_image_url(product)
            if image_url:
                messages.append({"type": "image", "url": image_url})
            else:
                print("PRODUCT IMAGE MISSING:", code, flush=True)

            detail = product_detail(product)
            combined_text = (detail + "\n\n" if detail else "") + product_reply(code, product)
            if include_order_prompt and has_sellable and idx == len(normalized) - 1:
                combined_text += "\n\n" + order_prompt
            messages.append({"type": "text", "text": combined_text})

    return manychat_response(messages[:10])



def manychat_product_order_response(code, product, missing):
    """Show product info first, then ask only for still-missing order fields.

    Used when the buyer's *first* mention already contains an order quantity,
    e.g. "Mini vise 2 ခုယူမယ်".  The old flow jumped straight to asking for
    address/phone and never showed Code / Detail / Price.
    """
    messages = []
    image_url = manychat_product_image_url(product)
    if image_url:
        print("MANYCHAT PUBLIC IMAGE URL:", image_url, flush=True)
        messages.append({"type": "image", "url": image_url})

    detail = product_detail(product)
    if detail:
        messages.append({"type": "text", "text": detail})

    messages.append({"type": "text", "text": product_reply(code, product)})

    prompt = order_prompt_for_missing(missing) if missing else ""
    if prompt:
        messages.append({"type": "text", "text": prompt})

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


def extract_manychat_account_name(data):
    """Best-effort Facebook/ManyChat account name supplied by ManyChat.

    Preferred Body field is full_name = Full Name. Also accepts common aliases
    and first_name + last_name so the bot never has to guess a buyer name from
    conversational text.
    """
    if not isinstance(data, dict):
        return ""

    for key in ("full_name", "facebook_name", "fb_name", "contact_name", "name"):
        value = str(data.get(key, "") or "").strip()
        if value:
            return value

    first = str(data.get("first_name", "") or "").strip()
    last = str(data.get("last_name", "") or "").strip()
    combined = " ".join(x for x in (first, last) if x).strip()
    if combined:
        return combined

    # Some ManyChat payloads may include nested contact/subscriber data.
    for obj_key in ("contact", "subscriber", "user"):
        obj = data.get(obj_key)
        if isinstance(obj, dict):
            for key in ("full_name", "name"):
                value = str(obj.get(key, "") or "").strip()
                if value:
                    return value
            first = str(obj.get("first_name", "") or "").strip()
            last = str(obj.get("last_name", "") or "").strip()
            combined = " ".join(x for x in (first, last) if x).strip()
            if combined:
                return combined

    return ""


def lock_facebook_account_name(session, account_name):
    """Use Facebook/ManyChat account name as the only buyer name for ManyChat orders."""
    account_name = str(account_name or "").strip()
    if account_name:
        session["name"] = account_name
        session["_account_name_locked"] = True
    return session


def _truthy_admin_flag(value):
    """Accept common boolean/string values for an explicit human takeover signal."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().casefold() in {
        "1", "true", "yes", "on", "admin", "human", "takeover", "pause", "paused"
    }


def is_explicit_admin_takeover_payload(data):
    """Fallback Admin signal for ManyChat/external-request integrations.

    Existing Meta is_echo detection remains the primary path. This fallback is
    intentionally additive: it does nothing unless the incoming request explicitly
    identifies itself as an Admin/human takeover, so buyer traffic cannot pause the bot.
    """
    if not isinstance(data, dict):
        return False
    for key in (
        "admin_takeover", "admin_pause", "human_takeover", "human_pause",
        "pause_bot", "admin_manual_reply", "is_admin_reply",
    ):
        if key in data and _truthy_admin_flag(data.get(key)):
            return True
    action = str(data.get("action", "") or data.get("event", "") or "").strip().casefold()
    return action in {
        "admin_takeover", "admin_pause", "human_takeover", "human_pause",
        "pause_bot", "admin_manual_reply",
    }


def handle_explicit_admin_takeover(data):
    """Pause a contact from an explicit ManyChat/admin signal; no customer reply."""
    contact_id = str(
        data.get("contact_id", "")
        or data.get("customer_id", "")
        or data.get("subscriber_id", "")
        or data.get("psid", "")
        or ""
    ).strip()
    if not contact_id:
        print("V50 EXPLICIT ADMIN TAKEOVER REJECTED: NO CONTACT ID", flush=True)
        return manychat_response([])
    _manychat_identity_from_payload(data, contact_id)
    pause_for_admin(contact_id)
    print("V50 EXPLICIT ADMIN TAKEOVER - BOT PAUSED:", contact_id, flush=True)
    return manychat_response([])


def handle_manychat_request(data):
    """
    ManyChat body:
      message      = Last Text Input
      contact_id   = Contact Id
      full_name    = Full Name (recommended; Facebook/ManyChat account name)
      image_url    = optional customer attachment URL
      product_code/ad_code/ad_context/ad_name = optional Ad context from ManyChat

    Rules:
    - Unclear product => exactly "ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"
    - Product => image, detail, price, then exact order-info prompt.
    - No quantity supplied => defaults to 1 item.
    - Buyer name always comes from Facebook/ManyChat Full Name.
    - Burmese/English/mixed text or image address + phone completion => buyer confirmation + Telegram.
    - Unknown/non-shopping/unresolved => ask exactly which product; do not pause.
    - Exact duplicate ManyChat input => no second customer reply.
    - Completed order => Telegram is queued once with retry + duplicate suppression.
    """
    refresh_catalog_for_customer_request("MANYCHAT")

    message = str(data.get("message", "") or "").strip()
    contact_id = str(data.get("contact_id", "") or "").strip()
    account_name = extract_manychat_account_name(data)
    remember_manychat_identity(contact_id, account_name)
    incoming_image_urls = extract_manychat_image_urls(data, message)
    incoming_image_url = incoming_image_urls[-1] if incoming_image_urls else ""

    # ManyChat can sometimes put a Facebook CDN image URL into Last Text Input.
    # Treat that as image data instead of customer text.
    low_message = message.lower()
    if (
        low_message.startswith(("http://", "https://"))
        and any(token in low_message for token in ("scontent", ".jpg", ".jpeg", ".png", ".webp", "fbcdn"))
    ):
        message = ""
        print("MANYCHAT MESSAGE PROMOTED TO IMAGE URL", flush=True)

    print(
        "MANYCHAT INPUT:",
        {"message": message, "contact_id": contact_id, "account_name": account_name, "image_url": incoming_image_url},
        flush=True,
    )

    ad_debug = {k: data.get(k) for k in (
        "product_code", "ad_code", "ad_context", "ad_id", "ad_ref",
        "referral", "ad_name", "ad_title", "ad_headline", "campaign_name",
        "manychat_ad_product", "last_ad_product"
    ) if data.get(k) not in (None, "", {})}
    if ad_debug:
        print("MANYCHAT AD CONTEXT:", ad_debug, flush=True)

    # V38 identity bridge: bind any PSID-like field ManyChat provides and also
    # remember this inbound so the matching Meta webhook event can correlate IDs.
    _manychat_identity_from_payload(data, contact_id)

    # V46 MULTI-PHOTO RECOVERY:
    # Messenger can deliver a multi-photo send through Meta as sibling attachment
    # events while ManyChat exposes only the LAST attachment in Dynamic Content.
    # First remember the image(s) visible in this ManyChat request, then merge any
    # very recent sibling URLs already observed through the Meta webhook/identity
    # bridge. This restores the old V6-style expectation: 2 photos => both products
    # can be recognized and BOTH Detail + Price replies are returned.
    current_event_image_urls = list(incoming_image_urls)
    remember_customer_images(contact_id, current_event_image_urls)
    incoming_image_url = current_event_image_urls[-1] if current_event_image_urls else ""
    remember_manychat_inbound(contact_id, message, incoming_image_url)
    correlate_manychat_to_meta(contact_id, message, incoming_image_url)

    recovered_image_urls = []
    if current_event_image_urls:
        for buffered_url in recent_customer_images(contact_id):
            if buffered_url not in recovered_image_urls:
                recovered_image_urls.append(buffered_url)
        for current_url in current_event_image_urls:
            if current_url not in recovered_image_urls:
                recovered_image_urls.append(current_url)
    incoming_image_urls = recovered_image_urls or list(current_event_image_urls)
    incoming_image_url = incoming_image_urls[-1] if incoming_image_urls else ""
    if len(incoming_image_urls) > 1:
        print("V46 MULTI-PHOTO BATCH URLS:", len(incoming_image_urls), flush=True)

    # ManyChat can retry a Dynamic Block request. Never send the exact same
    # reply twice to the same contact inside the short dedup window. Include all
    # current/recovered photo URLs in the fingerprint so sibling photos survive.
    image_fingerprint = "||".join(incoming_image_urls)
    if is_duplicate_manychat_input(data, message, contact_id, image_fingerprint):
        return manychat_response([])

    def done(response):
        return finalize_manychat(contact_id, response)

    if not contact_id:
        return manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။")

    # A human/Admin takeover silences only this customer.
    if admin_is_active(contact_id):
        print("MANYCHAT ADMIN ACTIVE - BOT SILENT:", contact_id, flush=True)
        return manychat_response([])

    # After a successful order, short replies such as OK/ဟုတ်/ကျေးဇူး are terminal
    # acknowledgements, not a new shopping request. Never ask for the product again.
    if post_order_ack_active(contact_id) and is_post_order_ack(message) and not incoming_image_urls:
        print("POST-ORDER ACK IGNORED:", contact_id, message, flush=True)
        return manychat_response([])

    session = get_order_session(contact_id)
    initial_last_product_code = str(session.get("last_product_code", "") or "").strip()

    # Buyer name is ALWAYS the Facebook/ManyChat account name for ManyChat orders.
    # Typed names, OCR names, and AI-extracted names must never overwrite it.
    if account_name:
        lock_facebook_account_name(session, account_name)
        print("ORDER NAME FROM MANYCHAT FULL NAME (LOCKED):", account_name, flush=True)

    # V49 STRICT SHEET-FIRST CODE GATE. The catalog was refreshed synchronously
    # above. An explicit code that is still absent from Google Sheet must never
    # inherit an older product/order session and must never ask for address/phone.
    explicit_candidates_now = explicit_catalog_code_candidates(message)
    valid_candidates_now = [c for c in explicit_candidates_now if c in PRODUCTS]
    invalid_candidates_now = [c for c in explicit_candidates_now if c not in PRODUCTS]
    if invalid_candidates_now:
        print(
            "V49 CODE(S) NOT IN GOOGLE SHEET:",
            invalid_candidates_now,
            "VALID IN SAME MESSAGE:", valid_candidates_now,
            flush=True,
        )
        if not valid_candidates_now:
            reset_order_context_for_unknown_product(contact_id, account_name)
            return done(manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"))
        # Mixed valid+invalid input: process only Sheet-backed products below.
        # Invalid tokens are never added to the order session.

    # Explicit human/Admin request only.
    if wants_admin(message):
        pause_for_admin(contact_id)
        return done(manychat_text(
            "ဒီမေးခွန်းကို Admin က ဆက်လက်ဖြေကြားပေးပါမယ်ရှင်။"
        ))

    if asks_delivery_time(message):
        return done(manychat_text(delivery_time_reply(message)))

    # If a product is already selected and the buyer sends an image, first check
    # whether that image contains delivery address/phone details. This lets buyers
    # send address screenshots/photos in Burmese or English. Product-only photos
    # still continue to normal product-image recognition below.
    pre_extracted_order_image = {}
    image_has_order_details = False
    if incoming_image_urls and session.get("last_product_code") in PRODUCTS:
        try:
            for one_image_url in incoming_image_urls:
                extracted_piece = extract_order_fields_from_image(one_image_url, message)
                if not isinstance(extracted_piece, dict):
                    continue
                extracted_piece["name"] = ""  # FB account name always wins.
                has_piece = bool(
                    str(extracted_piece.get("address", "") or "").strip()
                    or str(extracted_piece.get("phone", "") or "").strip()
                )
                if has_piece:
                    image_has_order_details = True
                    session = merge_extracted_order_data(session, extracted_piece)
                    # keep the last useful extraction for the later order path
                    pre_extracted_order_image = extracted_piece
                    lock_facebook_account_name(session, account_name)
                    print("ORDER DETAILS FROM IMAGE:", extracted_piece, flush=True)
        except Exception as e:
            print("PRE-ORDER IMAGE EXTRACTION ERROR:", str(e), flush=True)

    # If the current message explicitly contains product code(s), those codes
    # are authoritative for THIS order. Do not carry an older browsed product
    # into the new order. This fixes stale-session orders such as old 0001 + new
    # explicit 0013 becoming an unintended two-item order.
    explicit_message_items = find_codes_and_quantities(message)
    if explicit_message_items:
        sellable_explicit = {
            c: q for c, q in explicit_message_items.items()
            if c in PRODUCTS
        }
        if sellable_explicit:
            old_items = dict(session.get("items", {}))
            if old_items != sellable_explicit:
                print(
                    "EXPLICIT PRODUCT OVERRIDES STALE SESSION ITEMS:",
                    old_items,
                    "->",
                    sellable_explicit,
                    flush=True,
                )
            session["items"] = dict(sellable_explicit)
            session["quantity_confirmed"] = True
            if len(sellable_explicit) == 1:
                session["last_product_code"] = next(iter(sellable_explicit))

    # V46 EXPLICIT CODE + PURCHASE INTENT (GLOBAL, CURRENT + FUTURE SHEET ITEMS)
    # Examples: "Code 0008 လေးမှာယူချင်ပါတယ်", "0013 ဝယ်ချင်တယ်".
    # An explicit valid code must NEVER fall through to Admin/unknown handling.
    # It must show Image -> Google Sheet Detail -> Price/Delivery/COD first, then
    # continue the order flow by asking only for still-missing customer fields.
    explicit_code_product = None
    explicit_code_value = ""
    if explicit_message_items:
        for _code in explicit_message_items:
            if _code in PRODUCTS:
                explicit_code_value = _code
                explicit_code_product = PRODUCTS[_code]
                break
    if len(explicit_message_items) == 1 and explicit_code_product and is_order_message(message):
        if product_is_sellable(explicit_code_product):
            qty_now = explicit_message_items.get(explicit_code_value, 1) or 1
            session["items"] = {explicit_code_value: max(1, int(qty_now))}
            session["last_product_code"] = explicit_code_value
            session["quantity_confirmed"] = True
            lock_facebook_account_name(session, account_name)
            missing_now = order_missing_fields(session)
            print("V46 EXPLICIT CODE ORDER INFO-FIRST:", explicit_code_value, qty_now, missing_now, flush=True)
            return done(manychat_product_order_response(
                explicit_code_value, explicit_code_product, missing_now
            ))
        return done(manychat_product_response(explicit_code_value, explicit_code_product))

    # V38 global multi-name/multi-code order selection.
    # A current message explicitly naming/listing multiple products is authoritative
    # for this order and cannot be mixed with stale items from an older browse.
    named_message_items = find_named_products_and_quantities(message)
    combined_explicit_items = dict(explicit_message_items)
    for c, q in named_message_items.items():
        if c in PRODUCTS:
            combined_explicit_items[c] = q
    if combined_explicit_items:
        sellable_now = {
            c: max(1, int(q)) for c, q in combined_explicit_items.items()
            if c in PRODUCTS and product_is_sellable(PRODUCTS[c])
        }
        if sellable_now:
            session["items"] = dict(sellable_now)
            session["quantity_confirmed"] = True
            if len(sellable_now) == 1:
                session["last_product_code"] = next(iter(sellable_now))
            print("V38 EXPLICIT CURRENT ITEMS:", sellable_now, flush=True)

    # V30: parse address + phone before deciding whether the message is order progress.
    pre_local_order = {}
    pre_local_has_order_fields = False
    if message and session.get("last_product_code") in PRODUCTS:
        try:
            pre_local_order = extract_order_fields_locally(message, session=session)
            pre_local_order["name"] = ""  # Facebook/ManyChat Full Name always wins.
            pre_local_has_order_fields = bool(
                str(pre_local_order.get("address", "") or "").strip()
                or str(pre_local_order.get("phone", "") or "").strip()
            )
            if pre_local_has_order_fields:
                session = merge_extracted_order_data(session, pre_local_order)
                lock_facebook_account_name(session, account_name)
                print("V30 PRE-MERGED ORDER FIELDS:", pre_local_order, flush=True)
        except Exception as e:
            print("V30 PRE-MERGE ERROR:", str(e), flush=True)

    # -------- FAST ORDER PATH (V22) --------
    # Keep this before product/AI/image recognition. Once a customer has just
    # viewed a sellable product, a message containing order details must be
    # treated as an order immediately. This avoids unrelated recognition logic
    # swallowing Name / Address / Phone / quantity messages.
    remembered_code = str(session.get("last_product_code", "") or "").strip()
    # Order progress may arrive as Burmese/English/mixed address text, a phone-only
    # follow-up, a one-shot address+phone message, or an address screenshot/photo.
    # Use the stricter order-progress classifier so ordinary product questions are
    # not swallowed as addresses.
    order_progress_now = (
        pre_local_has_order_fields
        or looks_like_order_progress(message, session=session)
    )
    if remembered_code in PRODUCTS and (
        order_progress_now
        or generic_purchase_intent(message)
        or image_has_order_details
    ):
        remembered_product = PRODUCTS.get(remembered_code, {})
        if product_is_sellable(remembered_product):
            # Default quantity is 1 when omitted.
            session["items"].setdefault(remembered_code, 1)
            session["quantity_confirmed"] = True

            if message:
                print("V22 FAST ORDER TEXT:", message, flush=True)
                session = merge_order_message(contact_id, message)
                lock_facebook_account_name(session, account_name)
                print("V22 FAST ORDER SESSION:", session, flush=True)

            explicit_qty = extract_explicit_quantity(message)
            if explicit_qty is not None and not explicit_message_items:
                session["items"][remembered_code] = explicit_qty
                session["quantity_confirmed"] = True

            session = infer_delivery_area_for_complete_order(session)
            fast_missing = order_missing_fields(session)
            print("V22 FAST ORDER MISSING:", fast_missing, flush=True)

            if not fast_missing:
                bad_code, bad_status = first_unavailable_session_item(session)
                if bad_code:
                    ORDER_SESSIONS[contact_id] = new_order_session()
                    lock_facebook_account_name(ORDER_SESSIONS[contact_id], account_name)
                    return done(manychat_text(unavailable_order_reply(bad_code, bad_status)))

                telegram_text = build_telegram_order(session)
                print("V22 TELEGRAM ORDER TEXT:", telegram_text, flush=True)
                if queue_telegram_order(contact_id, telegram_text):
                    buyer_text = buyer_order_confirmation(session)
                    mark_order_completed(contact_id)
                    ORDER_SESSIONS[contact_id] = new_order_session()
                    lock_facebook_account_name(ORDER_SESSIONS[contact_id], account_name)
                    return done(manychat_text(buyer_text))

                # Do not erase the session when Telegram configuration is missing.
                return done(manychat_text(
                    "အော်ဒါအချက်အလက် ရရှိပါပြီရှင်။ Admin က ဆက်လက်စစ်ဆေးပေးပါမယ်ရှင်။"
                ))

            # If the customer sent only part of the order, ask only for what is missing.
            return done(manychat_text(order_prompt_for_missing(fast_missing)))

    # -------- Product recognition --------
    recognized_from_image = False
    recognized_from_ad = False

    # 0) Optional ad/product context. Persist it so later Hi/photo/question messages
    # still know which advertisement brought this customer into Messenger.
    context_code, context_product = extract_product_context_from_manychat(data)
    if not context_product:
        context_code, context_product = restore_meta_ad_product(contact_id)
    if context_product and context_code in PRODUCTS:
        session["ad_product_code"] = context_code
        # mirror remembered ad product to all correlated IDs
        for ident in (_identity_aliases(contact_id) or {contact_id}):
            get_order_session(ident)["ad_product_code"] = context_code
        print("V41 AD PRODUCT REMEMBERED:", context_code, flush=True)
    else:
        remembered_ad_code = normalize_code(session.get("ad_product_code", ""))
        if remembered_ad_code in PRODUCTS:
            context_code = remembered_ad_code
            context_product = PRODUCTS[remembered_ad_code]
            print("V41 AD PRODUCT RESTORED FROM SESSION:", remembered_ad_code, flush=True)

    # 1) Direct code/name from current message.
    code, product = find_product_by_code(message)
    if not product:
        code, product = find_product_by_name(message)
    if not product:
        code, product = find_product_by_rich_sheet_text(message)

    # 2) Customer image/screenshot. V39 recognizes EVERY visible catalog item
    # across EVERY photo supplied/recovered for this customer event.
    image_codes = []
    if not product and incoming_image_urls:
        for one_image_url in incoming_image_urls:
            # One multi-capable vision call handles both single and multi-product
            # images. Avoid the old second full-catalog call on a miss.
            matched_codes = ai_find_products_from_image(one_image_url, message)
            for matched_code in matched_codes:
                matched_code = normalize_code(matched_code)
                if matched_code in PRODUCTS and matched_code not in image_codes:
                    image_codes.append(matched_code)
        if image_codes:
            code = image_codes[0]
            product = PRODUCTS.get(code)
            recognized_from_image = True
            print("V39 ALL PHOTO MATCH CODES:", image_codes, flush=True)

    # If this is only a generic purchase phrase and there is no remembered/ad
    # product context, DO NOT ask OpenAI to guess a product.
    generic_order = generic_purchase_intent(message)

    # 3) Description / Burmese / English / Chinese text.
    # GLOBAL ALL-ITEM RULE: any catalog item name/alias/description + quantity/order
    # wording must still go through recognition. Bare purchase phrases never make AI guess.
    if not product and message and (not generic_order or has_catalog_product_clue_text(message)):
        code, product = ai_find_product_from_text(message)

    # Use optional Ad context if current message did not identify a product.
    if not product and context_product:
        code, product = context_code, context_product
        recognized_from_ad = True

    # A customer image that identifies a catalog product is a PRODUCT QUERY, not
    # an order screenshot. V20 treated every incoming image as order intent and
    # therefore asked for Name/Address/Phone instead of showing the matched item.
    if product and recognized_from_image and not image_has_order_details and not looks_like_order_details(message):
        # Do NOT reset the session: five separate product photos must accumulate
        # into one eventual order. One screenshot may also contribute many items.
        sellable_image_codes = []
        for image_code in (image_codes or [code]):
            image_product = PRODUCTS.get(image_code, {})
            if product_is_sellable(image_product):
                session["items"].setdefault(image_code, 1)
                sellable_image_codes.append(image_code)
        if sellable_image_codes:
            session["last_product_code"] = sellable_image_codes[-1]
            session["quantity_confirmed"] = True
        lock_facebook_account_name(session, account_name)
        print("V38 IMAGE BASKET ITEMS:", session.get("items"), flush=True)
        clear_recent_customer_images(contact_id, incoming_image_urls)
        return done(manychat_products_response(image_codes or [code], include_order_prompt=True))

    # A bare product code/name is browsing and refreshes the current product.
    if product and is_pure_product_query(message, code, product) and len(combined_explicit_items) <= 1:
        ORDER_SESSIONS[contact_id] = new_order_session()
        lock_facebook_account_name(ORDER_SESSIONS[contact_id], account_name)
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

    # V38: if the current message explicitly identifies multiple items, show every
    # item's image/detail/price once and keep all of them in the same order session.
    if len(combined_explicit_items) > 1:
        multi_codes = [c for c in combined_explicit_items if c in PRODUCTS]
        sellable_multi = [c for c in multi_codes if product_is_sellable(PRODUCTS[c])]
        if sellable_multi:
            session["items"] = {c: max(1, int(combined_explicit_items[c])) for c in sellable_multi}
            session["last_product_code"] = sellable_multi[-1]
            session["quantity_confirmed"] = True
            lock_facebook_account_name(session, account_name)
        return done(manychat_products_response(multi_codes, include_order_prompt=True))

    # V35 GLOBAL INFO-FIRST RULE
    # Any current message that identifies a sellable product must show the product
    # image + Google Sheet detail + price/delivery/COD BEFORE order collection.
    # This is global for every catalog item and future Sheet items, not just Mini Vise.
    # A follow-up such as "2 ခုယူမယ်" after a product was already shown contains no
    # product identifier, so it correctly continues the order without re-sending info.
    explicit_qty_now = extract_explicit_quantity(message)
    current_message_identifies_product = bool(product and (
        recognized_from_image
        or recognized_from_ad
        or find_product_by_code(message)[1]
        or find_product_by_name(message)[1]
        or (message and not generic_order)  # AI text identification/description
    ))

    if product and product_is_sellable(product) and current_message_identifies_product:
        session["last_product_code"] = code

        # If this same message also expresses purchase intent, remember quantity/order
        # state first, then append only the missing-fields prompt after product info.
        purchase_now = bool(generic_order or explicit_qty_now is not None)
        if purchase_now:
            qty_now = explicit_qty_now if explicit_qty_now is not None else 1
            # Merge into the current basket instead of erasing items gathered
            # from earlier separate product photos/messages in the same order.
            session["items"][code] = qty_now
            session["quantity_confirmed"] = True
            lock_facebook_account_name(session, account_name)
            missing_now = order_missing_fields(session)
            print("V35 GLOBAL INFO FIRST + ORDER:", code, qty_now, missing_now, flush=True)
            return done(manychat_product_order_response(code, product, missing_now))

        print("V35 GLOBAL INFO FIRST - PRODUCT QUERY:", code, flush=True)
        return done(manychat_product_response(code, product))

    last_product_ready = session.get("last_product_code") in PRODUCTS

    active_order = bool(
        session.get("items")
        or session.get("address")
        or session.get("phone")
        or session.get("quantity_confirmed")
        or image_has_order_details
    )

    # Generic "လိုချင်ပါတယ်" / "Can I make a purchase?" with no product known:
    # never hand off to Admin and never guess a product.
    if generic_order and not product and not last_product_ready and not active_order:
        return done(manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"))

    order_intent = (
        generic_order
        or (last_product_ready and looks_like_order_details(message))
        or (last_product_ready and looks_like_order_progress(message, session))
        or (extract_explicit_quantity(message) is not None and last_product_ready)
        or (image_has_order_details and last_product_ready)
    )
    if active_order and not order_intent:
        print("V49 ACTIVE ORDER + NON-ORDER TEXT - DO NOT REPEAT ADDRESS PROMPT:", message, flush=True)

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
            lock_facebook_account_name(session, account_name)
            print("ORDER SESSION AFTER TEXT:", session, flush=True)

        if incoming_image_urls and not image_has_order_details:
            for one_image_url in incoming_image_urls:
                extracted_from_image = extract_order_fields_from_image(
                    one_image_url,
                    message,
                )
                if isinstance(extracted_from_image, dict):
                    extracted_from_image["name"] = ""  # FB account name always wins.
                session = merge_extracted_order_data(
                    session,
                    extracted_from_image,
                )
            lock_facebook_account_name(session, account_name)
            session = infer_delivery_area_for_complete_order(session)

        explicit_qty = extract_explicit_quantity(message)
        if explicit_qty is not None:
            target_code = session.get("last_product_code")
            coded = find_codes_and_quantities(message)

            if coded:
                # Product codes explicitly written in the current customer
                # message define the current order and replace stale items.
                session["items"] = {
                    item_code: qty
                    for item_code, qty in coded.items()
                    if item_code in PRODUCTS
                }
                if len(session["items"]) == 1:
                    session["last_product_code"] = next(iter(session["items"]))
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
            lock_facebook_account_name(ORDER_SESSIONS[contact_id], account_name)
            return done(manychat_text(unavailable_order_reply(bad_code, bad_status)))

        telegram_text = build_telegram_order(session)
        print("TELEGRAM ORDER TEXT:", telegram_text, flush=True)

        # Queue in background so ManyChat gets a fast valid response. The queue
        # has its own 5-minute duplicate-order protection and Telegram retries.
        if queue_telegram_order(contact_id, telegram_text):
            buyer_text = buyer_order_confirmation(session)
            mark_order_completed(contact_id)
            ORDER_SESSIONS[contact_id] = new_order_session()
            lock_facebook_account_name(ORDER_SESSIONS[contact_id], account_name)
            return done(manychat_text(buyer_text))

        # Keep the session when Telegram configuration is missing.
        return done(manychat_text(
            "အော်ဒါအချက်အလက် ရရှိပါပြီရှင်။ Admin က ဆက်လက်စစ်ဆေးပေးပါမယ်ရှင်။"
        ))

    # Product recognized from non-pure text/image.
    if product:
        return done(manychat_product_response(code, product))

    # V33: A product image that cannot be matched must NOT put this customer into
    # Admin pause.  Otherwise one failed vision attempt makes every later image/code
    # A failed vision attempt must not make later image/code messages silent.
    if incoming_image_urls:
        print("UNRESOLVED IMAGE - BOT REMAINS ACTIVE", flush=True)
        clear_recent_customer_images(contact_id, incoming_image_urls)
        return done(manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"))

    greeting = simple_greeting(message)
    if greeting:
        return done(manychat_text(greeting))

    # Preserve the long-standing shop rule: if the buyer is clearly talking
    # about a product but Code/Text/Image recognition still cannot identify which
    # catalog item, do NOT guess and do NOT pause the bot. Ask exactly which item.
    low_unknown = str(message or "").lower()
    productish_words = (
        "ပစ္စည်း", "ဘယ်ဟာ", "ဒီဟာ", "ဒီပစ္စည်း", "စျေး", "ဈေး", "price",
        "stock", "ရှိလား", "ပို့ခ", "delivery", "အသုံး", "ဘယ်လိုသုံး",
        "ယူ", "မှာ", "ဝယ်", "order", "buy", "want", "item", "product",
    )
    if any(word in low_unknown for word in productish_words):
        print("UNRESOLVED PRODUCT TEXT - ASK WHICH PRODUCT", flush=True)
        return done(manychat_text("ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။"))

    # Ordinary unknown buyer text is NOT an Admin takeover. Ask which product
    # instead of pausing the bot. Explicit Admin requests were handled above.
    print("UNKNOWN BUYER TEXT - ASK WHICH PRODUCT", flush=True)
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



@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)

    print("=== WEBHOOK POST RECEIVED ===", flush=True)
    print(data, flush=True)

    # V50 dual-path Admin takeover. Meta message_echoes remains the primary path;
    # an explicit ManyChat/external-request signal is a safe fallback when Meta does
    # not deliver the outgoing echo. This check MUST happen before buyer processing.
    if isinstance(data, dict) and is_explicit_admin_takeover_payload(data):
        return handle_explicit_admin_takeover(data), 200

    # ManyChat Dynamic Block request.
    if isinstance(data, dict) and "message" in data and "contact_id" in data:
        return handle_manychat_request(data), 200

    # Unknown non-Meta request.
    if not data or data.get("object") != "page":
        return manychat_text("EVENT_RECEIVED"), 200

    refresh_catalog_for_customer_request("META")

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            message_data = event.get("message", {})
            referral = event.get("referral") or (message_data.get("referral") if isinstance(message_data, dict) else None)

            # messaging_referrals may arrive without a message object. Capture it
            # before the normal message-only flow so AD attribution is not discarded.
            if isinstance(referral, dict):
                referral_sender = str(event.get("sender", {}).get("id", "") or "").strip()
                if referral_sender:
                    remember_meta_ad_context(referral_sender, referral)

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

            text = str(message_data.get("text", "") or "").strip()
            attachments = message_data.get("attachments", []) or []

            incoming_image_urls = []
            for attachment in attachments:
                if attachment.get("type") == "image":
                    url = str(attachment.get("payload", {}).get("url", "") or "").strip()
                    if url and url not in incoming_image_urls:
                        incoming_image_urls.append(url)
            incoming_image_url = incoming_image_urls[-1] if incoming_image_urls else ""

            # V41 stores the Meta side before correlation. If ManyChat arrives later,
            # its current message can still bind Contact ID <-> PSID and inherit OFF.
            remember_meta_inbound(sender_id, text, incoming_image_url)

            # Keep all Meta-delivered sibling photos so the ManyChat Dynamic Block
            # can recover them even when Last Text Input exposes only the newest URL.
            remember_customer_images(sender_id, incoming_image_urls)

            # Correlate Meta PSID with the recent ManyChat contact for the same inbound.
            correlate_meta_sender_to_manychat(sender_id, text, incoming_image_url)
            # Correlation may have just created an alias; mirror the buffer again.
            remember_customer_images(sender_id, incoming_image_urls)

            if admin_is_active(sender_id):
                print("ADMIN ACTIVE - BOT SILENT:", sender_id, flush=True)
                continue

            if post_order_ack_active(sender_id) and is_post_order_ack(text) and not incoming_image_urls:
                print("POST-ORDER ACK IGNORED (META):", sender_id, text, flush=True)
                continue

            print("CUSTOMER:", sender_id, flush=True)
            print("TEXT:", text, flush=True)
            print("IMAGE:", incoming_image_url, flush=True)

            session = get_order_session(sender_id)

            # V49 strict Sheet-first invalid-code gate for native Meta delivery.
            # Do this before stale session/order logic so 0020 cannot continue 0012.
            meta_candidates = explicit_catalog_code_candidates(text)
            meta_valid_candidates = [c for c in meta_candidates if c in PRODUCTS]
            meta_invalid_candidates = [c for c in meta_candidates if c not in PRODUCTS]
            if meta_invalid_candidates and not meta_valid_candidates:
                reset_order_context_for_unknown_product(sender_id)
                send_facebook_text(sender_id, "ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။")
                continue

            # ---------------------------------
            # 1) DIRECT CODE
            # ---------------------------------
            code, product = find_product_by_code(text)

            # ---------------------------------
            # 2) DIRECT PRODUCT NAME
            # ---------------------------------
            if not product:
                code, product = find_product_by_name(text)
            if not product:
                code, product = find_product_by_rich_sheet_text(text)

            # ---------------------------------
            # 3) CUSTOMER IMAGE / SCREENSHOT
            # ---------------------------------
            if not product and incoming_image_urls:
                meta_codes = []
                for one_image_url in incoming_image_urls:
                    for matched_code in ai_find_products_from_image(one_image_url, text):
                        if matched_code in PRODUCTS and matched_code not in meta_codes:
                            meta_codes.append(matched_code)
                if meta_codes:
                    code = meta_codes[0]
                    product = PRODUCTS.get(code)
                    for matched_code in meta_codes:
                        session["items"].setdefault(matched_code, 1)
                    session["last_product_code"] = meta_codes[-1]
                    session["quantity_confirmed"] = True

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
                or (last_product_ready and looks_like_order_details(text))
                or (last_product_ready and looks_like_order_progress(text, session))
                or (extract_explicit_quantity(text) is not None and last_product_ready)
            )
            if active_order and not order_intent:
                print("V49 META ACTIVE ORDER + NON-ORDER TEXT - NO REPEATED PROMPT:", text, flush=True)

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
                        mark_order_completed(sender_id)
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
            # TRULY UNKNOWN / NON-PRODUCT
            # V49: unknown buyer text is NOT a human takeover. Keep the bot active
            # and ask which Sheet-backed product the buyer wants.
            # ---------------------------------
            send_facebook_text(sender_id, "ဘယ်ပစ္စည်းလေး အလိုရှိပါလဲရှင်။")

    return "EVENT_RECEIVED", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
    )
