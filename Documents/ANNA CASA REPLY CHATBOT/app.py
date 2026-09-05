"""
ANNA CASA REPLY BOT — rule-based, không dùng AI
Stack: Python + Flask + Meta Webhook + Google Sheets (lead tracking)
Features: keyword rules → gửi link/ảnh sản phẩm, catalog PDF, bot toggle
"""

import os
import time
import threading
import requests
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, session, redirect, url_for, render_template_string

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

# ── CONFIG ────────────────────────────────────────────────────────────────────
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
SHEET_ID            = os.environ["SHEET_ID"]
SUPABASE_URL         = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
CRM_PASSWORD         = os.environ.get("CRM_PASSWORD", "")


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
christine_sent: set = set()
vela_sent: set = set()
tondo_sent: set = set()
milo_sent: set = set()
fetale_sent: set = set()

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


# ── SUPABASE (mini CRM: khách hàng + lịch sử hội thoại) ────────────────────────
def supabase_request(method: str, path: str, json_body: dict = None, params: dict = None, extra_headers: dict = None):
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    try:
        r = requests.request(method, url, headers=headers, json=json_body, params=params, timeout=10)
        r.raise_for_status()
        return r.json() if r.text else None
    except Exception as e:
        print(f"[SUPABASE] {method} {path} failed: {e}")
        return None

def upsert_customer(psid: str, **fields):
    body = {"psid": psid, "last_contact_at": datetime.now(timezone.utc).isoformat()}
    for k, v in fields.items():
        if v:
            body[k] = v
    supabase_request(
        "POST", "customers",
        json_body=body,
        params={"on_conflict": "psid"},
        extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"}
    )

def log_message(psid: str, direction: str, body: str):
    if not body:
        return
    supabase_request("POST", "messages", json_body={
        "psid": psid, "direction": direction, "body": body,
        "created_at": datetime.now(timezone.utc).isoformat()
    })

def get_customers():
    return supabase_request("GET", "customers", params={"order": "last_contact_at.desc", "limit": "300"}) or []

def get_customer(psid: str):
    data = supabase_request("GET", "customers", params={"psid": f"eq.{psid}", "limit": "1"})
    return data[0] if data else {"psid": psid, "name": "", "ad_id": "", "ref_code": ""}

def get_messages(psid: str):
    return supabase_request("GET", "messages", params={"psid": f"eq.{psid}", "order": "created_at.asc"}) or []


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
        threading.Thread(target=log_message, args=(recipient_id, "out", text), daemon=True).start()
    except Exception as e:
        print(f"send_text failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")


def get_sender_name(sender_id):
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name&access_token={META_PAGE_TOKEN}"
        return requests.get(url, timeout=5).json().get("name", "")
    except:
        return ""


# ── CATALOGUES ───────────────────────────────────────────────────────────────
# Host trực tiếp trên server thay vì dùng link Google Drive — Facebook fetch
# link Drive hay lỗi silent (bị giới hạn lượt tải / chặn bot fetch).
CATALOGUES = {
    "wallpaper_1": "https://anna-casa-chatbot.onrender.com/catalogs/Gi%E1%BA%A5y%20d%C3%A1n%20t%C6%B0%E1%BB%9Dng%20C%E1%BB%95%20%C4%91i%E1%BB%83n%20SALEOFF.pdf",
    "wallpaper_2": "https://anna-casa-chatbot.onrender.com/catalogs/Gi%E1%BA%A5y%20d%C3%A1n%20t%C6%B0%E1%BB%9Dng%20Hi%E1%BB%87n%20%C4%91%E1%BA%A1i%20SALEOFF.pdf",
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
        threading.Thread(target=log_message, args=(recipient_id, "out", f"[Hình ảnh] {image_url}"), daemon=True).start()
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
        threading.Thread(target=log_message, args=(recipient_id, "out", f"[File] {file_url}"), daemon=True).start()
    except Exception as e:
        print(f"send_file failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")


# Cache attachment_id sau khi upload 1 lần — tránh Facebook phải fetch lại
# URL Google Drive mỗi lần gửi (hay lỗi do redirect qua domain khác).
_attachment_id_cache: dict[str, str] = {}

def get_or_upload_attachment_id(key: str, file_url: str) -> str | None:
    if key in _attachment_id_cache:
        return _attachment_id_cache[key]
    url = f"https://graph.facebook.com/v18.0/me/message_attachments?access_token={META_PAGE_TOKEN}"
    payload = {"message": {"attachment": {"type": "file", "payload": {"url": file_url, "is_reusable": True}}}}
    try:
        r = requests.post(url, json=payload, timeout=30)
        r.raise_for_status()
        att_id = r.json().get("attachment_id")
        if att_id:
            _attachment_id_cache[key] = att_id
            return att_id
    except Exception as e:
        print(f"upload_attachment_id failed for {key}: {e} | body={r.text[:500] if 'r' in dir() else ''}")
    return None

def send_file_reusable(recipient_id, key: str, file_url: str):
    """Gửi file qua attachment_id đã cache (upload 1 lần); fallback gửi thẳng URL nếu upload lỗi."""
    att_id = get_or_upload_attachment_id(key, file_url)
    if att_id:
        url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
        payload = {
            "recipient": {"id": recipient_id},
            "message": {"attachment": {"type": "file", "payload": {"attachment_id": att_id}}}
        }
        try:
            r = requests.post(url, json=payload, timeout=15)
            r.raise_for_status()
            threading.Thread(target=log_message, args=(recipient_id, "out", f"[File] {file_url}"), daemon=True).start()
            return
        except Exception as e:
            print(f"send_file_reusable failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")
    send_file(recipient_id, file_url)
FEMALE_MIDDLE = {"thị", "ngọc", "thùy", "thanh", "thu", "mai", "lan", "hương", "linh", "thi"}
FEMALE_FIRST  = {"hoa", "lan", "linh", "hương", "trang", "thảo", "ngân", "vy", "ly", "my",
                 "mai", "yến", "vân", "nhung", "loan", "hằng", "nga", "phương", "hiền", "dung",
                 "trinh", "châu", "nhi", "khánh", "trâm", "tuyền", "quỳnh", "diệu", "thúy",
                 "hạnh", "lý", "tiên", "xuân", "diễm", "giang", "thư", "bích", "kim", "cúc", "ngọc"}
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

    # Fallback: tên hiển thị kiểu Tây (tên chính đứng đầu, họ đứng cuối)
    if len(parts) >= 2:
        last = parts[0]
        if last in FEMALE_FIRST: return "chị"
        if last in MALE_FIRST:   return "anh"

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


# ── GƯƠNG CHRISTINE ───────────────────────────────────────────────────────────
CHRISTINE_KEYWORDS = ["gương christine", "guong christine", "christine mirror", "christine"]
CHRISTINE_IMAGES = [
    "https://bizweb.dktcdn.net/100/435/602/products/1663660475-1756021435289.jpg?v=1756021455503",
    "https://bizweb.dktcdn.net/100/435/602/products/guong-fiam-2.jpg?v=1769055387680",
    "https://bizweb.dktcdn.net/100/435/602/products/guong-fiam.jpg?v=1769055387680",
]

def is_christine_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in CHRISTINE_KEYWORDS)

def christine_reply(sender_id: str, pronoun: str, first_name: str):
    """Flow tư vấn gương Christine: chào → 3 hình + giá → mô tả → hỏi không gian."""
    if sender_id in christine_sent:
        send_text(sender_id, f"Dạ {pronoun} xem thêm gương Christine tại: https://annacasavn.com/christine-mirror ạ")
        return
    christine_sent.add(sender_id)
    name_part = f" {first_name}" if first_name else ""
    send_text(sender_id, f"Dạ em chào {pronoun}{name_part}, em là Long sẽ hỗ trợ tư vấn mình ngày hôm nay ạ")
    time.sleep(0.8)
    for img in CHRISTINE_IMAGES:
        send_image(sender_id, img)
        time.sleep(0.5)
    send_text(sender_id, "Dạ gương Christine nhập khẩu Ý hiện bên em có giá là 127.148.000")
    time.sleep(0.8)
    send_text(sender_id, "Christine thuộc bộ sưu tập của Fiam Italia, do các nhà thiết kế Dante O. Benini, Luca Gonzo cùng nghệ sĩ điêu khắc Helidon Xhixha thực hiện. Chất liệu kính màu khói (fume) tráng bạc mặt sau — bề mặt kính được uốn nóng ở nhiệt độ cao rồi tráng bạc phía sau, tạo ra những đường cong tự nhiên không lặp lại giữa các sản phẩm. Kích thước gương là 110 x 110 x 23 cm")
    time.sleep(0.8)
    send_text(sender_id, "Mình đang cần đặt gương ở không gian nào ạ?")


# ── GIƯỜNG VELA ────────────────────────────────────────────────────────────────
VELA_KEYWORDS = ["giường vela", "giuong vela", "vela bed", "vela"]
VELA_IMAGES = [
    "https://bizweb.dktcdn.net/100/435/602/products/lay-anh-website-2-6c5b4ef3-d213-41a9-93b8-889b98b09ddf.jpg?v=1787298639387",
    "https://bizweb.dktcdn.net/100/435/602/products/tieu-de-phu-6.jpg?v=1787298642103",
    "https://bizweb.dktcdn.net/100/435/602/products/luxury-5.jpg?v=1787298642103",
    "https://bizweb.dktcdn.net/100/435/602/products/luxury-6.jpg?v=1787298642103",
]

def is_vela_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in VELA_KEYWORDS)

def vela_reply(sender_id: str, pronoun: str, first_name: str):
    """Flow tư vấn giường Vela: chào → 4 hình + giá → mô tả → hỏi phòng ngủ."""
    if sender_id in vela_sent:
        send_text(sender_id, f"Dạ {pronoun} xem thêm giường Vela tại: https://annacasavn.com/giuong-luxury-ms-01 ạ")
        return
    vela_sent.add(sender_id)
    name_part = f" {first_name}" if first_name else ""
    send_text(sender_id, f"Dạ em chào {pronoun}{name_part}, em là Long sẽ hỗ trợ tư vấn mình ngày hôm nay ạ")
    time.sleep(0.8)
    for img in VELA_IMAGES:
        send_image(sender_id, img)
        time.sleep(0.5)
    send_text(sender_id, "Dạ giường Vela Modern Luxury hiện bên em có giá là 35.000.000, áp dụng cho cả 2 size 1m6x2m và 1m8x2m ạ")
    time.sleep(0.8)
    send_text(sender_id, "Vela do Anna Casa thiết kế và sản xuất tại Việt Nam. Đầu giường có các đường chỉ dọc chạy song song từ trên xuống, tạo chiều sâu thị giác mà không làm phức tạp tổng thể. Hai tay vịn cong ôm nhẹ hai bên đầu giường, vừa là chi tiết thẩm mỹ vừa tạo cảm giác được bao bọc khi nằm. Khung ván MDF, bọc vải màu kem trắng ngà")
    time.sleep(0.8)
    send_text(sender_id, "Mình đang cần size 1m6x2m hay 1m8x2m ạ?")


# ── TỦ ĐẦU GIƯỜNG TONDO ───────────────────────────────────────────────────────
TONDO_KEYWORDS = ["tủ đầu giường tondo", "tu dau giuong tondo", "tủ tondo", "tu tondo", "tondo"]
TONDO_IMAGES = [
    "https://bizweb.dktcdn.net/100/435/602/products/tudaugiuongbocda-ezgif-com-png-to-webp-converter.webp?v=1787545080943",
    "https://bizweb.dktcdn.net/100/435/602/products/nanobanana2-denoisethisimage-donotchangeanythingelse-ezgif-com-png-to-webp-converter-1.webp?v=1787545292033",
    "https://bizweb.dktcdn.net/100/435/602/products/nanobanana2-denoisethisimage-donotchangeanythingelse1-ezgif-com-png-to-webp-converter.webp?v=1787545369163",
]

def is_tondo_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in TONDO_KEYWORDS)

def tondo_reply(sender_id: str, pronoun: str, first_name: str):
    """Flow tư vấn tủ đầu giường Tondo: chào → 3 hình + giá → mô tả → hỏi bộ giường đang dùng."""
    if sender_id in tondo_sent:
        send_text(sender_id, f"Dạ {pronoun} xem thêm tủ đầu giường Tondo tại: https://annacasavn.com/tu-dau-giuong-ms03 ạ")
        return
    tondo_sent.add(sender_id)
    name_part = f" {first_name}" if first_name else ""
    send_text(sender_id, f"Dạ em chào {pronoun}{name_part}, em là Long sẽ hỗ trợ tư vấn mình ngày hôm nay ạ")
    time.sleep(0.8)
    for img in TONDO_IMAGES:
        send_image(sender_id, img)
        time.sleep(0.5)
    send_text(sender_id, "Dạ tủ đầu giường Tondo bọc da yên ngựa hiện bên em có giá là 11.141.000")
    time.sleep(0.8)
    send_text(sender_id, "Tondo dáng trụ tròn bọc da yên ngựa, ôm trọn theo đường cong. Viền gỗ óc chó chạy dọc theo mép trên. Hai ngăn kéo ẩn mình trong khối tròn, không phá vỡ hình dáng tổng thể. Chất liệu ván gỗ tự nhiên phủ veneer gỗ óc chó nhập khẩu, bọc da yên ngựa cao cấp")
    time.sleep(0.8)
    send_text(sender_id, "Mình đang dùng bộ giường màu gì để em tư vấn phối màu tủ cho hợp ạ?")


# ── TỦ ĐẦU GIƯỜNG MILO ────────────────────────────────────────────────────────
MILO_KEYWORDS = ["tủ đầu giường milo", "tu dau giuong milo", "tủ milo", "tu milo", "milo"]
MILO_IMAGES = [
    "https://bizweb.dktcdn.net/100/435/602/products/1787218313000-2231804115808437945-6876353226804957740-a51cf4a4c71cf3ea2ad8fbbcef020c74-1787218348907.jpg?v=1787218351953",
    "https://bizweb.dktcdn.net/100/435/602/products/luxury-1.jpg?v=1787296066993",
    "https://bizweb.dktcdn.net/100/435/602/products/luxury-2.jpg?v=1787297016393",
]

def is_milo_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in MILO_KEYWORDS)

def milo_reply(sender_id: str, pronoun: str, first_name: str):
    """Flow tư vấn tủ đầu giường Milo: chào → 3 hình + giá → mô tả → hỏi phối màu."""
    if sender_id in milo_sent:
        send_text(sender_id, f"Dạ {pronoun} xem thêm tủ đầu giường Milo tại: https://annacasavn.com/tu-dau-giuong-luxury ạ")
        return
    milo_sent.add(sender_id)
    name_part = f" {first_name}" if first_name else ""
    send_text(sender_id, f"Dạ em chào {pronoun}{name_part}, em là Long sẽ hỗ trợ tư vấn mình ngày hôm nay ạ")
    time.sleep(0.8)
    for img in MILO_IMAGES:
        send_image(sender_id, img)
        time.sleep(0.5)
    send_text(sender_id, "Dạ tủ đầu giường Milo hiện bên em có giá là 8.391.000")
    time.sleep(0.8)
    send_text(sender_id, "Milo dáng vuông vắn, đường nét tối giản, tay nắm hình vòm bằng đồng nổi bật trên nền trắng lacquer. Chân đế bo tròn nhẹ ở góc, đứng vững mà vẫn nhẹ mắt. Chi tiết đồng ở chân và tay nắm giữ được ánh kim lâu dài. Chất liệu ván gỗ tự nhiên, ván mật độ cao chuẩn E0")
    time.sleep(0.8)
    send_text(sender_id, "Mình đang dùng bộ giường màu gì để em tư vấn phối màu tủ cho hợp ạ?")


# ── SOFA FETALE ────────────────────────────────────────────────────────────────
FETALE_KEYWORDS = ["sofa fetale", "sofa fetal", "fetale"]
FETALE_IMAGES = [
    "https://bizweb.dktcdn.net/100/435/602/products/by-sku-gia-goc-10-1755854851818.jpg?v=1755854855360",
    "https://bizweb.dktcdn.net/100/435/602/products/mis01662-1.jpg?v=1759131449993",
    "https://bizweb.dktcdn.net/100/435/602/products/mis01656.jpg?v=1759131449993",
]

def is_fetale_question(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in FETALE_KEYWORDS)

def fetale_reply(sender_id: str, pronoun: str, first_name: str):
    """Flow tư vấn sofa Fetale: chào → 3 hình + giá → mô tả → hỏi không gian phòng khách."""
    if sender_id in fetale_sent:
        send_text(sender_id, f"Dạ {pronoun} xem thêm sofa Fetale tại: https://annacasavn.com/sofa-fetale-2230-1010-790-mm-light-gray-beige-fabric-code-meg-031 ạ")
        return
    fetale_sent.add(sender_id)
    name_part = f" {first_name}" if first_name else ""
    send_text(sender_id, f"Dạ em chào {pronoun}{name_part}, em là Long sẽ hỗ trợ tư vấn mình ngày hôm nay ạ")
    time.sleep(0.8)
    for img in FETALE_IMAGES:
        send_image(sender_id, img)
        time.sleep(0.5)
    send_text(sender_id, "Dạ sofa Fetale hiện bên em có giá là 50.682.000")
    time.sleep(0.8)
    send_text(sender_id, "Fetale thuộc thương hiệu Anna Casa, sản xuất trong nước. Khung kim loại đồng xước bên ngoài, phần tay tựa có đường xếp ly dọc tinh xảo mang dấu ấn cổ điển. Kích thước 2230 x 1010 x 790 mm, màu be trung tính, đệm Polyurethane/lông vũ, vải bọc cao cấp")
    time.sleep(0.8)
    send_text(sender_id, "Mình đang cần sofa cho phòng khách diện tích khoảng bao nhiêu ạ?")


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

    return False


# ── PROCESS TEXT MESSAGE ──────────────────────────────────────────────────────
def process_message(sender_id, text):
    try:
        sender_name = get_sender_name(sender_id)
        first_name = sender_name.split()[-1] if sender_name else ""
        pronoun = detect_gender(sender_name)
        print(f"[MSG] name='{sender_name}' pronoun='{pronoun}'")

        threading.Thread(target=upsert_customer, args=(sender_id,), kwargs={"name": sender_name}, daemon=True).start()
        threading.Thread(target=log_message, args=(sender_id, "in", text), daemon=True).start()

        if sender_id not in message_log_logged:
            message_log_logged.add(sender_id)
            threading.Thread(
                target=log_new_lead_to_message_log,
                args=(sender_id, sender_name),
                kwargs={"ad_id": ad_id_store.get(sender_id, "")},
                daemon=True
            ).start()

        # Wallpaper catalogue trigger — khớp chính xác nguyên câu
        if text.strip().lower() == "nhận danh sách giấy dán tường":
            send_file_reusable(sender_id, "wallpaper_1", CATALOGUES["wallpaper_1"])
            time.sleep(1)
            send_file_reusable(sender_id, "wallpaper_2", CATALOGUES["wallpaper_2"])
            time.sleep(1)
            send_text(sender_id, "Anna Casa gửi bạn 2 catalog giấy dán tường Arte từ Pháp, nếu bạn cần thêm hình mẫu nào nhân viên tư vấn sẽ hỗ trợ mình nha")
            return

        # Armchair Nook — flow tư vấn có sẵn, tự chào khách
        if is_nook_question(text):
            nook_reply(sender_id, pronoun, first_name)
            return

        # Gương Christine — flow tư vấn có sẵn, tự chào khách
        if is_christine_question(text):
            christine_reply(sender_id, pronoun, first_name)
            return

        # Giường Vela — flow tư vấn có sẵn, tự chào khách
        if is_vela_question(text):
            vela_reply(sender_id, pronoun, first_name)
            return

        # Tủ đầu giường Tondo — flow tư vấn có sẵn, tự chào khách
        if is_tondo_question(text):
            tondo_reply(sender_id, pronoun, first_name)
            return

        # Tủ đầu giường Milo — flow tư vấn có sẵn, tự chào khách
        if is_milo_question(text):
            milo_reply(sender_id, pronoun, first_name)
            return

        # Sofa Fetale — flow tư vấn có sẵn, tự chào khách
        if is_fetale_question(text):
            fetale_reply(sender_id, pronoun, first_name)
            return

        # Rules engine — gửi link/ảnh theo từ khóa. Không khớp gì thì im lặng.
        rules_reply(sender_id, text, pronoun)

    except Exception as e:
        print(f"process_message error: {e}")


# ── PROCESS IMAGE MESSAGE ─────────────────────────────────────────────────────
def process_image(sender_id, image_url, caption=""):
    try:
        sender_name = get_sender_name(sender_id)
        first_name = sender_name.split()[-1] if sender_name else ""
        pronoun = detect_gender(sender_name)

        threading.Thread(target=upsert_customer, args=(sender_id,), kwargs={"name": sender_name}, daemon=True).start()
        threading.Thread(target=log_message, args=(sender_id, "in", f"[Khách gửi hình] {caption}" if caption else "[Khách gửi hình]"), daemon=True).start()

        # Caption hỏi Armchair Nook → flow riêng
        if caption and is_nook_question(caption):
            nook_reply(sender_id, pronoun, first_name)
            return

        # Caption hỏi gương Christine → flow riêng
        if caption and is_christine_question(caption):
            christine_reply(sender_id, pronoun, first_name)
            return

        # Caption hỏi giường Vela → flow riêng
        if caption and is_vela_question(caption):
            vela_reply(sender_id, pronoun, first_name)
            return

        # Caption hỏi tủ đầu giường Tondo → flow riêng
        if caption and is_tondo_question(caption):
            tondo_reply(sender_id, pronoun, first_name)
            return

        # Caption hỏi tủ đầu giường Milo → flow riêng
        if caption and is_milo_question(caption):
            milo_reply(sender_id, pronoun, first_name)
            return

        # Caption hỏi sofa Fetale → flow riêng
        if caption and is_fetale_question(caption):
            fetale_reply(sender_id, pronoun, first_name)
            return

        # Nếu caption có keywords → rules_reply. Không khớp gì thì im lặng.
        if caption:
            rules_reply(sender_id, caption, pronoun)

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
                threading.Thread(target=upsert_customer, args=(sender_id,), kwargs={"ad_id": ad_id_from_referral}, daemon=True).start()

            if ref and sender_id not in ref_store:
                ref_store[sender_id] = ref
                sender_name = get_sender_name(sender_id)
                threading.Thread(target=upsert_customer, args=(sender_id,), kwargs={"ref_code": ref, "name": sender_name}, daemon=True).start()
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


# ── MINI CRM ──────────────────────────────────────────────────────────────────
def crm_login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("crm_logged_in"):
            return redirect(url_for("crm_login"))
        return f(*args, **kwargs)
    return wrapper

_CRM_STYLE = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #f5f4f0; }
"""

CRM_LOGIN_HTML = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8"><title>Anna Casa CRM — Đăng nhập</title>
<style>""" + _CRM_STYLE + """
body { min-height: 100vh; display: flex; align-items: center; justify-content: center; }
.card { background: #fff; border-radius: 16px; border: 1px solid #e8e6e0; padding: 2.5rem; width: 100%; max-width: 360px; text-align: center; }
.logo { font-size: 13px; font-weight: 600; letter-spacing: 0.12em; color: #888; text-transform: uppercase; margin-bottom: 1.5rem; }
input { width: 100%; padding: 0.85rem 1rem; border: 1px solid #e0ddd5; border-radius: 10px; font-size: 15px; margin-bottom: 1rem; }
button { width: 100%; padding: 0.85rem; border: none; border-radius: 10px; background: #1a1a1a; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer; }
button:hover { background: #333; }
.error { color: #D85A30; font-size: 13px; margin-bottom: 1rem; }
</style></head><body>
<div class="card">
  <div class="logo">Anna Casa CRM</div>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="POST">
    <input type="password" name="password" placeholder="Mật khẩu" autofocus required>
    <button type="submit">Đăng nhập</button>
  </form>
</div>
</body></html>"""

CRM_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8"><title>Anna Casa CRM</title>
<style>""" + _CRM_STYLE + """
body { padding: 2rem; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.5rem; }
.logo { font-size: 13px; font-weight: 600; letter-spacing: 0.12em; color: #888; text-transform: uppercase; }
.logout { font-size: 13px; color: #888; text-decoration: none; }
table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 12px; overflow: hidden; border: 1px solid #e8e6e0; }
th, td { text-align: left; padding: 0.75rem 1rem; font-size: 14px; border-bottom: 1px solid #f0ede8; }
th { color: #aaa; font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; }
tr:last-child td { border-bottom: none; }
tr:hover { background: #faf9f6; }
a.row-link { color: #1a1a1a; text-decoration: none; }
.empty { color: #aaa; text-align: center; padding: 2rem; background: #fff; border-radius: 12px; border: 1px solid #e8e6e0; }
</style></head><body>
<div class="header">
  <div class="logo">Anna Casa CRM — {{ customers|length }} khách</div>
  <a class="logout" href="/crm/logout">Đăng xuất</a>
</div>
{% if customers %}
<table>
  <tr><th>Tên</th><th>Ad ID</th><th>Ref</th><th>Liên hệ cuối</th><th></th></tr>
  {% for c in customers %}
  <tr>
    <td>{{ c.name or 'Khách' }}</td>
    <td>{{ c.ad_id or '—' }}</td>
    <td>{{ c.ref_code or '—' }}</td>
    <td>{{ c.last_contact_at[:16].replace('T',' ') if c.last_contact_at else '—' }}</td>
    <td><a class="row-link" href="/crm/customer/{{ c.psid }}">Xem hội thoại →</a></td>
  </tr>
  {% endfor %}
</table>
{% else %}
<div class="empty">Chưa có khách nào</div>
{% endif %}
</body></html>"""

CRM_THREAD_HTML = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8"><title>{{ customer.name or 'Khách' }} — Anna Casa CRM</title>
<style>""" + _CRM_STYLE + """
body { min-height: 100vh; display: flex; flex-direction: column; }
.header { background: #fff; border-bottom: 1px solid #e8e6e0; padding: 1rem 1.5rem; display: flex; justify-content: space-between; align-items: center; }
.back { font-size: 13px; color: #888; text-decoration: none; }
.name { font-weight: 600; font-size: 15px; text-align: center; }
.meta { font-size: 12px; color: #aaa; text-align: center; }
.thread { flex: 1; padding: 1.5rem; overflow-y: auto; max-width: 700px; margin: 0 auto; width: 100%; }
.bubble { max-width: 70%; padding: 0.7rem 1rem; border-radius: 14px; margin-bottom: 0.2rem; font-size: 14px; line-height: 1.4; white-space: pre-wrap; }
.in { background: #fff; border: 1px solid #e8e6e0; margin-right: auto; }
.out { background: #1D9E75; color: #fff; margin-left: auto; }
.time { font-size: 10px; color: #bbb; margin: 0 4px 10px; }
.reply-box { background: #fff; border-top: 1px solid #e8e6e0; padding: 1rem 1.5rem; display: flex; gap: 0.5rem; max-width: 700px; margin: 0 auto; width: 100%; }
.reply-box textarea { flex: 1; border: 1px solid #e0ddd5; border-radius: 10px; padding: 0.7rem; font-size: 14px; resize: none; font-family: inherit; }
.reply-box button { padding: 0 1.5rem; border: none; border-radius: 10px; background: #1a1a1a; color: #fff; font-weight: 600; cursor: pointer; }
</style></head><body>
<div class="header">
  <a class="back" href="/crm">← Tất cả khách</a>
  <div>
    <div class="name">{{ customer.name or 'Khách' }}</div>
    <div class="meta">Ad ID: {{ customer.ad_id or '—' }} · Ref: {{ customer.ref_code or '—' }}</div>
  </div>
  <a class="back" href="/crm/logout">Đăng xuất</a>
</div>
<div class="thread">
  {% for m in messages %}
  <div class="bubble {{ 'out' if m.direction == 'out' else 'in' }}">{{ m.body }}</div>
  <div class="time" style="text-align: {{ 'right' if m.direction == 'out' else 'left' }}">{{ m.created_at[:16].replace('T',' ') if m.created_at else '' }}</div>
  {% endfor %}
</div>
<form class="reply-box" method="POST" action="/crm/customer/{{ psid }}/reply">
  <textarea name="text" rows="2" placeholder="Nhắn tin cho khách..." required></textarea>
  <button type="submit">Gửi</button>
</form>
</body></html>"""

@app.route("/crm/login", methods=["GET", "POST"])
def crm_login():
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if CRM_PASSWORD and pw == CRM_PASSWORD:
            session["crm_logged_in"] = True
            return redirect(url_for("crm_dashboard"))
        error = "Sai mật khẩu"
    return render_template_string(CRM_LOGIN_HTML, error=error)

@app.route("/crm/logout")
def crm_logout():
    session.pop("crm_logged_in", None)
    return redirect(url_for("crm_login"))

@app.route("/crm")
@crm_login_required
def crm_dashboard():
    return render_template_string(CRM_DASHBOARD_HTML, customers=get_customers())

@app.route("/crm/customer/<psid>")
@crm_login_required
def crm_thread(psid):
    return render_template_string(
        CRM_THREAD_HTML, customer=get_customer(psid), messages=get_messages(psid), psid=psid
    )

@app.route("/crm/customer/<psid>/reply", methods=["POST"])
@crm_login_required
def crm_reply(psid):
    text = request.form.get("text", "").strip()
    if text:
        send_text(psid, text)
    return redirect(url_for("crm_thread", psid=psid))


# ── SERVE WEB ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/catalogs/<path:filename>")
def serve_catalog(filename):
    return send_from_directory("catalogs", filename)


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
