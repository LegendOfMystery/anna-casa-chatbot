"""
ANNA CASA REPLY BOT — rule-based, không dùng AI
Stack: Python + Flask + Meta Webhook + Google Sheets (lead tracking)
Features: keyword rules → gửi link/ảnh sản phẩm, catalog PDF, bot toggle
"""

import os
import time
import threading
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
SHEET_ID            = os.environ["SHEET_ID"]


# ── IN-MEMORY STORE ───────────────────────────────────────────────────────────
import json as _json
processed_messages: set = set()

_MESSAGE_LOG_LOGGED_FILE = Path(__file__).parent / "message_log_logged.json"

class _PersistedSet:
    """Set backed by a JSON file so it survives server restarts/redeploys."""
    def __init__(self, path: Path):
        self._path = path
        try:
            self._data = set(_json.loads(path.read_text())) if path.exists() else set()
        except Exception:
            self._data = set()
    def _save(self):
        try:
            self._path.write_text(_json.dumps(list(self._data)))
        except Exception as e:
            print(f"{self._path.name} save error: {e}")
    def add(self, sid):
        self._data.add(sid)
        self._save()
    def __contains__(self, sid):
        return sid in self._data
    def __iter__(self):
        return iter(self._data)

bot_enabled = True
gbar_catalog_sent: set = set()
nook_sent: set = set()

# ── LEAD TRACKING ─────────────────────────────────────────────────────────────
ref_store:   dict[str, str] = {}  # psid -> ref code từ ad
ad_id_store: dict[str, str] = {}  # psid -> ad_id từ Click-to-Messenger ad
LEAD_SHEET_NAME      = "Lead%20Register"
FB_TRACKING_SHEET_ID = "1n4MA99rflm55JiieyTa102cWnR5nqlBKRPheP42Ywgs"
FB_MESSAGE_LOG_TAB   = "Facebook%20Message%20Log"
message_log_logged   = _PersistedSet(_MESSAGE_LOG_LOGGED_FILE)

def log_lead_to_sheet(psid: str, ref_code: str, phone: str = "", name: str = ""):
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chat_link = f"https://business.facebook.com/latest/inbox/messenger?selected_thread_id={psid}"
    row = [timestamp, psid, name, phone, ref_code, "new", "", "", chat_link]
    ok = sheets_post(
        f"/values/{LEAD_SHEET_NAME}!A:I:append?valueInputOption=USER_ENTERED",
        {"values": [row]}
    )
    if ok:
        print(f"[LEAD] {ref_code} | {psid} | {name}")
    else:
        print(f"[LEAD ERROR] Failed to write to sheet")

def log_new_lead_to_message_log(psid: str, name: str, ad_id: str = ""):
    """Ghi lead mới (1 dòng/psid) vào tab Facebook Message Log của sheet tracking ads chính."""
    from datetime import datetime
    today = datetime.now().strftime("%-d/%-m/%Y")
    # CREATE DATE, TÊN KH, NHU CẦU, SALE NAME, KÊNH LIÊN LẠC, AD ID, TÌNH TRẠNG KH, ƯU TIÊN, GIÁ TRỊ ĐƠN HÀNG, SỐ ĐIỆN THOẠI, GHI CHÚ
    row = [today, name or "", "", "Long", "Facebook", ad_id, "Đang kết nối", "THẤP", "", "", ""]
    ok = sheets_post(
        f"/values/{FB_MESSAGE_LOG_TAB}!A:K:append?valueInputOption=USER_ENTERED",
        {"values": [row]},
        sheet_id=FB_TRACKING_SHEET_ID
    )
    if ok:
        print(f"[MSG LOG] {name} | ad_id={ad_id} | {psid}")
    else:
        print(f"[MSG LOG ERROR] Failed to write to sheet for {psid}")




# ── GOOGLE SHEETS AUTH ────────────────────────────────────────────────────────
import json
import google.auth.transport.requests
from google.oauth2 import service_account

def get_sheets_token() -> str:
    """Lấy access token từ service account JSON trong env var."""
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not sa_json:
        return ""
    try:
        sa_info = json.loads(sa_json)
        creds = service_account.Credentials.from_service_account_info(
            sa_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception as e:
        print(f"[AUTH ERROR] {e}")
        return ""

def sheets_post(url_path: str, payload: dict, sheet_id: str = None) -> bool:
    """POST tới Sheets API dùng service account token."""
    token = get_sheets_token()
    if not token:
        print("[SHEETS] No service account token")
        return False
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id or SHEET_ID}{url_path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        print(f"[SHEETS POST] status={resp.status_code} | body={resp.text[:500]}")
        if resp.status_code in (200, 201):
            return True
        return False
    except Exception as e:
        print(f"[SHEETS POST ERROR] {e}")
        return False

# ── PRODUCT CATALOG ───────────────────────────────────────────────────────────
import json as _json

_catalog_cache: dict[str, list] = {}
_catalog_loaded_at: float = 0
_CATALOG_FILES = {
    "tham": Path(__file__).parent / "products.json",
    "giay_dan_tuong": Path(__file__).parent / "wallpaper_products.json",
    "ghe_bar": Path(__file__).parent / "ghe_bar_products.json",
}
_PRODUCTS_TTL = 3600

def fetch_all_products() -> list[dict]:
    global _catalog_cache, _catalog_loaded_at
    now = time.time()
    if now - _catalog_loaded_at < _PRODUCTS_TTL and _catalog_cache:
        return [p for cat in _catalog_cache.values() for p in cat]
    for key, path in _CATALOG_FILES.items():
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
            for p in data:
                p["category"] = key
            _catalog_cache[key] = data
            print(f"[PRODUCTS] Loaded {len(data)} from {path.name}")
        except Exception as e:
            print(f"[PRODUCTS] Error loading {path.name}: {e}")
            _catalog_cache.setdefault(key, [])
    _catalog_loaded_at = now
    return [p for cat in _catalog_cache.values() for p in cat]

def fetch_products_by_category(category: str) -> list[dict]:
    fetch_all_products()  # ensure cache loaded
    return _catalog_cache.get(category, [])


# ── SEND HELPERS ──────────────────────────────────────────────────────────────
def send_text(recipient_id, text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"send_text failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")


def get_sender_name(sender_id):
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name&access_token={META_PAGE_TOKEN}"
        return requests.get(url, timeout=5).json().get("name", "")
    except:
        return ""


# ── CATALOGUES ───────────────────────────────────────────────────────────────
CATALOGUES = {
    "wallpaper_1": "https://drive.google.com/uc?export=download&id=1lcuuGuGpWh7lclBW-Kpxc3VV39cldQes&confirm=t",
    "wallpaper_2": "https://drive.google.com/uc?export=download&id=1TdGLS_6u2FVCNJMEhL2FhQ5T1_cQ7Xn9&confirm=t",
}

def send_image(recipient_id, image_url):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True}
            }
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"send_image failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")

def send_file(recipient_id, file_url):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "file",
                "payload": {"url": file_url, "is_reusable": True}
            }
        }
    }
    try:
        r = requests.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except Exception as e:
        print(f"send_file failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")
FEMALE_MIDDLE = {"thị", "ngọc", "thùy", "thanh", "thu", "mai", "lan", "hương", "linh", "thi"}
FEMALE_FIRST  = {"hoa", "lan", "linh", "hương", "trang", "thảo", "ngân", "vy", "ly", "my",
                 "mai", "yến", "vân", "nhung", "loan", "hằng", "nga", "phương", "hiền", "dung",
                 "trinh", "châu", "nhi", "khánh", "trâm", "tuyền", "quỳnh", "diệu", "thúy",
                 "hạnh", "lý", "tiên", "xuân", "diễm", "giang", "thư", "bích", "kim", "cúc"}
MALE_MIDDLE   = {"văn", "hữu", "đức", "công", "quốc", "minh", "trung", "anh", "bá", "gia"}
MALE_FIRST    = {"hùng", "dũng", "tuấn", "nam", "long", "đức", "thành", "hải", "sơn", "bình",
                 "trung", "khoa", "lâm", "phong", "quân", "khải", "tùng", "cường", "kiên", "đạt",
                 "nghĩa", "nhân", "phát", "thắng", "vinh", "khánh", "huy", "minh", "hoàng", "tâm",
                 "toàn", "thiện", "phúc", "bảo", "khang", "duy", "quang", "tú", "lộc", "tài"}

def is_lead_form(text: str) -> bool:
    """Detect Facebook Lead Form auto-messages — không cần bot reply."""
    t = text.lower()
    lead_signals = [
        "tôi đã điền mẫu", "toi da dien mau",
        "i filled out a form", "i submitted a form",
        "phone number:", "email:", "first name:", "last name:",
        "tên dự án cần báo giá", "ten du an can bao gia",
        "căn hộ của bạn có bao nhiêu", "can ho cua ban co bao nhieu",
        "số điện thoại:", "họ và tên:", "ho va ten:",
        "diện tích:", "dien tich:", "địa chỉ dự án", "dia chi du an",
        "ngân sách:", "ngan sach:", "thời gian thi công", "thoi gian thi cong",
    ]
    return sum(1 for s in lead_signals if s in t) >= 2

def detect_gender(full_name: str) -> str:
    """Trả về 'anh', 'chị', hoặc 'bạn' nếu không xác định được."""
    if not full_name:
        return "bạn"
    parts = [p.lower() for p in full_name.strip().split()]

    # Ưu tiên tên chính (cuối) trước
    first = parts[-1]
    if first in FEMALE_FIRST: return "chị"
    if first in MALE_FIRST:   return "anh"

    # Nếu tên chính không xác định được → mới xét tên đệm
    if len(parts) >= 3:
        middle = parts[-2]
        if middle in MALE_MIDDLE:   return "anh"
        if middle in FEMALE_MIDDLE: return "chị"

    return "bạn"


# ── ARMCHAIR NOOK ─────────────────────────────────────────────────────────────
NOOK_KEYWORDS = ["armchair nook", "nook", "armchair", "arm chair", "ghế nook", "ghe nook"]
NOOK_IMAGES = [
    "https://bizweb.dktcdn.net/100/435/602/products/gheapsley-ezgif-com-png-to-webp-converter-1.webp?v=1787398044733",
    "https://bizweb.dktcdn.net/100/435/602/products/gheapsley1-ezgif-com-png-to-webp-converter.webp?v=1787398044733",
    "https://bizweb.dktcdn.net/100/435/602/products/gheapsley2-ezgif-com-png-to-webp-converter.webp?v=1787398044733",
]

def is_nook_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in NOOK_KEYWORDS)

def nook_reply(sender_id: str, pronoun: str, first_name: str):
    """Flow tư vấn ghế Armchair Nook: chào → 3 hình + giá → mô tả → hỏi phòng."""
    if sender_id in nook_sent:
        send_text(sender_id, f"Dạ {pronoun} xem thêm ghế Armchair Nook tại: https://annacasavn.com/ghe-armchair-nook ạ")
        return
    nook_sent.add(sender_id)
    name_part = f" {first_name}" if first_name else ""
    send_text(sender_id, f"Dạ em chào {pronoun}{name_part}, em là Long sẽ hỗ trợ tư vấn mình ngày hôm nay ạ")
    time.sleep(0.8)
    for img in NOOK_IMAGES:
        send_image(sender_id, img)
        time.sleep(0.5)
    send_text(sender_id, "Dạ ghế Armchair Nook hiện bên em có giá là 22.925.000")
    time.sleep(0.8)
    send_text(sender_id, "Armchair Nook được thiết kế bởi Anna Casa. Mềm mại khi ngồi, chân xoay linh hoạt, bo cong mọi góc cho không gian mềm mại. Kích thước ghế là 890 x 860 x 730 mm")
    time.sleep(0.8)
    send_text(sender_id, "Mình đang cần ghế này để ở phòng nào ạ?")


# ── RULES ENGINE ──────────────────────────────────────────────────────────────
def _send_bot(sender_id, *msgs):
    """Gửi một hoặc nhiều tin nhắn liên tiếp."""
    for i, m in enumerate(msgs):
        send_text(sender_id, m)
        if i < len(msgs) - 1:
            time.sleep(0.8)

def rules_reply(sender_id: str, text: str, pronoun: str) -> bool:
    """Khớp text với rules — gửi reply nếu match, trả True. False nếu không khớp."""
    t = text.lower().strip()

    # Ghế bar → gửi catalog ảnh lần đầu, link sau
    if any(k in t for k in ["ghế bar", "ghe bar", "bar chair", "barstool", "bar stool", "ghế bếp", "ghe bep"]):
        if sender_id not in gbar_catalog_sent:
            prods = fetch_products_by_category("ghe_bar")
            if prods:
                send_text(sender_id, f"Dạ bên em có {len(prods)} mẫu ghế bar, {pronoun} xem ảnh từng mẫu nhé ạ:")
                for prod in prods:
                    if prod.get("img"):
                        time.sleep(0.5)
                        send_text(sender_id, f"• {prod['name']} — {prod['price']}")
                        time.sleep(0.5)
                        send_image(sender_id, prod["img"])
                gbar_catalog_sent.add(sender_id)
            else:
                send_text(sender_id, f"Dạ {pronoun} xem ghế bar bên em tại: https://annacasavn.com/ghe-bar ạ")
        else:
            send_text(sender_id, f"Dạ {pronoun} xem thêm mẫu ghế bar tại: https://annacasavn.com/ghe-bar ạ")
        return True

    # Grandeco Inia
    if any(k in t for k in ["grandeco", "inia"]):
        _send_bot(sender_id, f"Dạ bộ sưu tập Grandeco Inia từ Bỉ, {pronoun} xem tại:", "https://annacasavn.com/giay-dan-tuong-grandeco-inia")
        return True

    # Giấy dán tường
    if any(k in t for k in ["giấy dán tường", "giay dan tuong", "wallpaper", "giấy dán", "giay dan", "dán tường", "dan tuong"]):
        _send_bot(sender_id, f"Dạ bên em có nhiều mẫu giấy dán tường ạ, {pronoun} xem tại:", "https://annacasavn.com/giay-dan-tuong")
        return True

    # Thảm
    if any(k in t for k in ["thảm", "tham", "carpet", "rug"]):
        _send_bot(sender_id, f"Dạ bên em có nhiều mẫu thảm đẹp ạ, {pronoun} xem tại:", "https://annacasavn.com/tham")
        return True

    # Địa chỉ / showroom
    if any(k in t for k in ["địa chỉ", "dia chi", "ở đâu", "o dau", "showroom", "cửa hàng", "cua hang", "chỗ nào", "cho nao"]):
        _send_bot(sender_id, "Dạ showroom bên em ở 12 Nguyễn Ư Dĩ, phường An Khánh, TPHCM ạ. Mở cửa 10h sáng đến 7h tối.")
        return True

    # Giờ mở cửa
    if any(k in t for k in ["giờ mở", "gio mo", "mấy giờ", "may gio", "mở cửa lúc", "khi nào mở", "open"]):
        _send_bot(sender_id, "Dạ bên em mở cửa từ 10h sáng đến 7h tối hàng ngày ạ.")
        return True

    # Ship / giao hàng
    if any(k in t for k in ["ship", "giao hàng", "giao hang", "vận chuyển", "van chuyen", "có ship", "co ship"]):
        _send_bot(sender_id, f"Dạ bên em ship toàn quốc {pronoun} ơi, phí ship tùy khu vực ạ.")
        return True

    # Zalo
    if "zalo" in t:
        _send_bot(sender_id, f"Dạ {pronoun} để lại số Zalo bên em liên hệ lại ngay nha ạ.")
        return True

    # Giá chung
    if any(k in t for k in ["giá bao", "gia bao", "bao nhiêu tiền", "bao nhieu tien", "giá thế", "gia the"]):
        _send_bot(sender_id, f"Dạ {pronoun} hỏi về sản phẩm nào để em báo giá ạ, thảm hay giấy dán tường ạ?")
        return True

    return False


# ── PROCESS TEXT MESSAGE ──────────────────────────────────────────────────────
def process_message(sender_id, text):
    try:
        sender_name = get_sender_name(sender_id)
        first_name = sender_name.split()[-1] if sender_name else ""
        pronoun = detect_gender(sender_name)
        print(f"[MSG] name='{sender_name}' pronoun='{pronoun}'")

        if sender_id not in message_log_logged:
            message_log_logged.add(sender_id)
            threading.Thread(
                target=log_new_lead_to_message_log,
                args=(sender_id, sender_name),
                kwargs={"ad_id": ad_id_store.get(sender_id, "")},
                daemon=True
            ).start()

        # Wallpaper catalogue trigger — rule cứng
        WP_TRIGGERS = ["catalogue giấy dán tường", "catalog giấy dán tường",
                       "catalogue giay dan tuong", "catalog giay dan tuong",
                       "nhận catalogue", "nhận catalog",
                       "xin catalog", "xin catalogue", "gửi catalog", "gửi catalogue",
                       "cho xin catalog", "cho xin catalogue",
                       "xem catalog", "xem catalogue"]
        if any(t in text.lower() for t in WP_TRIGGERS):
            send_text(sender_id, "Dạ em gửi catalog giấy dán tường đang sale ạ.")
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_1"])
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_2"])
            return

        # Armchair Nook — flow tư vấn có sẵn, tự chào khách
        if is_nook_question(text):
            nook_reply(sender_id, pronoun, first_name)
            return

        # Generic greeting thuần → hỏi nhu cầu, không cần reply thêm
        _generic = {"hi", "hello", "chào", "chao", "hey", "alo", "ơi", "oi",
                    "xin chào", "xin chao", "get started", "bắt đầu", "bat dau"}
        _t = text.strip().lower().rstrip("!. ")
        if _t in _generic or len(_t) <= 4:
            send_text(sender_id, f"Dạ {pronoun} cần tư vấn sản phẩm gì ạ? Bên em có thảm, giấy dán tường, ghế bar ạ.")
            return

        # Rules engine — gửi link/ảnh theo từ khóa
        matched = rules_reply(sender_id, text, pronoun)
        if not matched:
            send_text(sender_id, f"Dạ {pronoun} cho em biết thêm {pronoun} cần tư vấn sản phẩm gì ạ? Bên em có thảm, giấy dán tường, ghế bar ạ.")

    except Exception as e:
        print(f"process_message error: {e}")


# ── PROCESS IMAGE MESSAGE ─────────────────────────────────────────────────────
def process_image(sender_id, image_url, caption=""):
    try:
        sender_name = get_sender_name(sender_id)
        first_name = sender_name.split()[-1] if sender_name else ""
        pronoun = detect_gender(sender_name)

        # Caption hỏi Armchair Nook → flow riêng
        if caption and is_nook_question(caption):
            nook_reply(sender_id, pronoun, first_name)
            return

        # Nếu caption có keywords → rules_reply luôn
        if caption and rules_reply(sender_id, caption, pronoun):
            return

        send_text(sender_id, f"Dạ em nhận được hình ạ. {pronoun} đang tìm thảm hay giấy dán tường ạ?")

    except Exception as e:
        print(f"process_image error: {e}")


# ── WEBHOOK VERIFY ────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403


# ── WEBHOOK RECEIVE ───────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_webhook():
    data = request.get_json()
    if not data:
        return jsonify({"status": "no data"}), 200

    import json as _dbgjson
    print(f"[RAW] {_dbgjson.dumps(data)[:800]}")

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id  = event.get("sender", {}).get("id")
            message    = event.get("message", {})
            text       = message.get("text", "")
            message_id = message.get("mid", "")
            is_echo    = message.get("is_echo", False)
            attachments = message.get("attachments", [])

            if attachments:
                print(f"[ATTACH] types={[a.get('type') for a in attachments]} | echo={is_echo} | sid={sender_id}")

            if not sender_id:
                continue

            # ── REF TRACKING (Click-to-Messenger ad) ─────────────────────────
            # Lấy ref từ postback.payload (mỗi ad set payload khác nhau)
            postback = event.get("postback", {})
            postback_payload = postback.get("payload", "").strip()
            # Fallback: thử referral object nếu có
            referral = event.get("referral") or postback.get("referral") or {}
            ref = referral.get("ref", "").strip() or postback_payload
            ad_id_from_referral = referral.get("ad_id", "").strip()
            if ad_id_from_referral and sender_id not in ad_id_store:
                ad_id_store[sender_id] = ad_id_from_referral

            if ref and sender_id not in ref_store:
                ref_store[sender_id] = ref
                sender_name = get_sender_name(sender_id)
                threading.Thread(
                    target=log_lead_to_sheet,
                    args=(sender_id, ref),
                    kwargs={"name": sender_name},
                    daemon=True
                ).start()
                print(f"[LEAD] Logged ref={ref} | {sender_id} | {sender_name}")
            # ─────────────────────────────────────────────────────────────────

            if is_echo:
                continue

            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            if not bot_enabled:
                continue

            if text and is_lead_form(text):
                print(f"[SKIP] Lead form message from {sender_id}")
                continue

            # Xử lý ảnh / video / share
            if attachments:
                has_image = False
                for att in attachments:
                    att_type = att.get("type")
                    if att_type == "image":
                        has_image = True
                        image_url = att.get("payload", {}).get("url", "")
                        if image_url:
                            threading.Thread(
                                target=process_image,
                                args=(sender_id, image_url, text or ""),
                                daemon=True
                            ).start()
                if not has_image:
                    # Video/Reel/sticker — bot không xem được, xử lý theo caption (nếu có)
                    threading.Thread(
                        target=process_message,
                        args=(sender_id, text or ""),
                        daemon=True
                    ).start()
                continue

            # Xử lý text
            if not text:
                continue

            threading.Thread(
                target=process_message,
                args=(sender_id, text),
                daemon=True
            ).start()

    return jsonify({"status": "ok"}), 200


# ── API ENDPOINTS ─────────────────────────────────────────────────────────────
@app.route("/api/status")
def api_status():
    return jsonify({"bot_enabled": bot_enabled})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    global bot_enabled
    bot_enabled = not bot_enabled
    print(f"[TOGGLE] Bot {'enabled' if bot_enabled else 'disabled'}")
    return jsonify({"bot_enabled": bot_enabled})


# ── SERVE WEB ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
