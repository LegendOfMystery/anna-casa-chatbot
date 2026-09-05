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

def log_message(psid: str, direction: str, body: str, mid: str = None, created_at: str = None):
    if not body:
        return
    row = {
        "psid": psid, "direction": direction, "body": body,
        "created_at": created_at or datetime.now(timezone.utc).isoformat()
    }
    if mid:
        # Có fb_mid → upsert bỏ qua nếu trùng (tránh log đúp khi backfill
        # chạy lại hoặc đè lên tin đã ghi qua webhook realtime).
        row["fb_mid"] = mid
        supabase_request(
            "POST", "messages", json_body=row,
            params={"on_conflict": "fb_mid"},
            extra_headers={"Prefer": "resolution=ignore-duplicates,return=minimal"}
        )
    else:
        supabase_request("POST", "messages", json_body=row)

def get_customers():
    return supabase_request("GET", "customers", params={"order": "last_contact_at.desc", "limit": "300"}) or []

def get_conversations(search: str = ""):
    """Danh sách hội thoại kèm preview tin nhắn cuối — đọc từ view crm_conversations."""
    params = {"order": "last_message_at.desc.nullslast", "limit": "500"}
    if search:
        params["name"] = f"ilike.*{search}*"
    data = supabase_request("GET", "crm_conversations", params=params)
    return data or []

def get_customer(psid: str):
    data = supabase_request("GET", "customers", params={"psid": f"eq.{psid}", "limit": "1"})
    return data[0] if data else {"psid": psid, "name": "", "ad_id": "", "ref_code": "", "avatar_url": "", "stage": "Mới"}

def get_messages(psid: str):
    return supabase_request("GET", "messages", params={"psid": f"eq.{psid}", "order": "created_at.asc"}) or []

_backfill_status = {"running": False, "conversations": 0, "messages": 0, "done": False, "error": ""}

def backfill_facebook_conversations(max_conversations: int = 300):
    """Kéo lịch sử hội thoại cũ từ Facebook Conversations API vào Supabase — chạy 1 lần, nền."""
    global _backfill_status
    _backfill_status = {"running": True, "conversations": 0, "messages": 0, "done": False, "error": ""}
    try:
        page_info = requests.get(f"https://graph.facebook.com/v18.0/me?access_token={META_PAGE_TOKEN}", timeout=10).json()
        page_id = page_info.get("id")
        if not page_id:
            _backfill_status.update(running=False, done=True, error=f"Không lấy được page_id: {page_info}")
            return

        url = f"https://graph.facebook.com/v18.0/{page_id}/conversations"
        params = {
            "platform": "messenger",
            "fields": "participants,messages.limit(100){message,from,created_time,id}",
            "limit": 50,
            "access_token": META_PAGE_TOKEN,
        }
        while url and _backfill_status["conversations"] < max_conversations:
            r = requests.get(url, params=params, timeout=30)
            params = None  # "next" url của Facebook đã tự kèm sẵn query string
            r.raise_for_status()
            data = r.json()
            for conv in data.get("data", []):
                _backfill_status["conversations"] += 1
                participants = conv.get("participants", {}).get("data", [])
                customer = next((p for p in participants if p.get("id") != page_id), None)
                if not customer:
                    continue
                psid = customer["id"]
                for m in conv.get("messages", {}).get("data", []):
                    body = m.get("message", "")
                    if not body:
                        continue
                    direction = "out" if m.get("from", {}).get("id") == page_id else "in"
                    log_message(psid, direction, body, mid=m.get("id"), created_at=m.get("created_time"))
                    _backfill_status["messages"] += 1
                upsert_customer(psid, name=customer.get("name", ""))
            url = data.get("paging", {}).get("next")
        _backfill_status.update(running=False, done=True)
        print(f"[BACKFILL] Xong: {_backfill_status['conversations']} hội thoại, {_backfill_status['messages']} tin nhắn")
    except Exception as e:
        _backfill_status.update(running=False, done=True, error=str(e))
        print(f"[BACKFILL] Lỗi: {e}")


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

def get_sender_profile(sender_id):
    """Trả về (name, avatar_url). Facebook có thể không trả profile_pic tùy quyền app."""
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name,profile_pic&access_token={META_PAGE_TOKEN}"
        data = requests.get(url, timeout=5).json()
        return data.get("name", ""), data.get("profile_pic", "")
    except:
        return "", ""


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
            return
        except Exception as e:
            print(f"send_file_reusable failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")
    send_file(recipient_id, file_url)


def send_uploaded_attachment(recipient_id, file_storage):
    """Gửi file/ảnh nhân viên tải trực tiếp từ máy lên (CRM) — multipart, không cần host link."""
    import json as _json2
    mimetype = file_storage.mimetype or ""
    att_type = "image" if mimetype.startswith("image/") else "file"
    url = f"https://graph.facebook.com/v18.0/me/messages"
    data = {
        "recipient": _json2.dumps({"id": recipient_id}),
        "message": _json2.dumps({"attachment": {"type": att_type, "payload": {"is_reusable": True}}}),
        "access_token": META_PAGE_TOKEN,
    }
    files = {"filedata": (file_storage.filename, file_storage.stream, mimetype or "application/octet-stream")}
    try:
        r = requests.post(url, data=data, files=files, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"send_uploaded_attachment failed: {e} | body={r.text[:500] if 'r' in dir() else ''}")


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
def process_message(sender_id, text, message_id=None):
    try:
        sender_name, avatar_url = get_sender_profile(sender_id)
        first_name = sender_name.split()[-1] if sender_name else ""
        pronoun = detect_gender(sender_name)
        print(f"[MSG] name='{sender_name}' pronoun='{pronoun}'")

        threading.Thread(target=upsert_customer, args=(sender_id,), kwargs={"name": sender_name, "avatar_url": avatar_url}, daemon=True).start()
        threading.Thread(target=log_message, args=(sender_id, "in", text), kwargs={"mid": message_id}, daemon=True).start()

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
def process_image(sender_id, image_url, caption="", message_id=None):
    try:
        sender_name, avatar_url = get_sender_profile(sender_id)
        first_name = sender_name.split()[-1] if sender_name else ""
        pronoun = detect_gender(sender_name)

        threading.Thread(target=upsert_customer, args=(sender_id,), kwargs={"name": sender_name, "avatar_url": avatar_url}, daemon=True).start()
        threading.Thread(
            target=log_message,
            args=(sender_id, "in", f"[Khách gửi hình] {caption}" if caption else "[Khách gửi hình]"),
            kwargs={"mid": message_id},
            daemon=True
        ).start()

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
                # Echo = mọi tin Page gửi ra (bot gửi HOẶC nhân viên trả lời tay
                # trong Facebook Inbox) — log lại để CRM đồng bộ đầy đủ 2 chiều.
                customer_psid = event.get("recipient", {}).get("id")
                if customer_psid:
                    echo_body = text
                    if not echo_body and attachments:
                        att_type = attachments[0].get("type", "file")
                        att_url = attachments[0].get("payload", {}).get("url", "")
                        label = "Hình ảnh" if att_type == "image" else "File"
                        echo_body = f"[{label}] {att_url}" if att_url else f"[{label}]"
                    if echo_body:
                        threading.Thread(target=upsert_customer, args=(customer_psid,), daemon=True).start()
                        threading.Thread(
                            target=log_message, args=(customer_psid, "out", echo_body),
                            kwargs={"mid": message_id}, daemon=True
                        ).start()
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
                                kwargs={"message_id": message_id},
                                daemon=True
                            ).start()
                if not has_image:
                    # Video/Reel/sticker — bot không xem được, xử lý theo caption (nếu có)
                    threading.Thread(
                        target=process_message,
                        args=(sender_id, text or ""),
                        kwargs={"message_id": message_id},
                        daemon=True
                    ).start()
                continue

            # Xử lý text
            if not text:
                continue

            threading.Thread(
                target=process_message,
                args=(sender_id, text),
                kwargs={"message_id": message_id},
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

CRM_INBOX_HTML = """<!DOCTYPE html>
<html lang="vi"><head><meta charset="UTF-8">
<title>{{ customer.name if customer else 'Anna Casa CRM' }}</title>
<style>""" + _CRM_STYLE + """
html, body { height: 100%; overflow: hidden; }
.app { display: flex; height: 100vh; }

/* ── Sidebar ── */
.sidebar { width: 340px; flex-shrink: 0; background: #fff; border-right: 1px solid #e8e6e0; display: flex; flex-direction: column; }
.sidebar-head { padding: 1rem 1.2rem 0.75rem; border-bottom: 1px solid #f0ede8; }
.logo { font-size: 13px; font-weight: 600; letter-spacing: 0.1em; color: #888; text-transform: uppercase; margin-bottom: 0.75rem; display: flex; justify-content: space-between; align-items: center; }
.logo a { color: #888; text-decoration: none; font-size: 11px; font-weight: 500; text-transform: none; letter-spacing: 0; }
.search-box { width: 100%; padding: 0.55rem 0.8rem; border: 1px solid #e0ddd5; border-radius: 8px; font-size: 13px; margin-bottom: 0.6rem; }
.sync-row { display: flex; align-items: center; gap: 0.5rem; }
.sync-btn { padding: 0.35rem 0.7rem; border: 1px solid #e0ddd5; border-radius: 7px; background: #fafaf8; font-size: 12px; font-weight: 600; cursor: pointer; }
.sync-btn:hover { background: #f0ede8; }
.sync-status { font-size: 11px; color: #999; }
.conv-list { flex: 1; overflow-y: auto; }
.conv-item { display: flex; gap: 0.7rem; padding: 0.75rem 1.2rem; text-decoration: none; color: inherit; border-bottom: 1px solid #f5f4f0; }
.conv-item:hover { background: #faf9f6; }
.conv-item.active { background: #eef7f3; }
.avatar { width: 38px; height: 38px; border-radius: 50%; background: #e0ddd5; color: #6b6b6b; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 14px; flex-shrink: 0; overflow: hidden; position: relative; }
.avatar img { width: 100%; height: 100%; object-fit: cover; border-radius: 50%; position: absolute; top: 0; left: 0; }
.avatar.lg { width: 44px; height: 44px; font-size: 16px; }
.conv-main { min-width: 0; flex: 1; }
.conv-top { display: flex; justify-content: space-between; gap: 0.5rem; }
.conv-name { font-size: 13.5px; font-weight: 600; color: #1a1a1a; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.conv-time { font-size: 11px; color: #aaa; flex-shrink: 0; }
.conv-preview { font-size: 12.5px; color: #888; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: 2px; }
.conv-empty { padding: 2rem 1.2rem; color: #aaa; font-size: 13px; text-align: center; }

/* ── Main pane ── */
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.main-empty { flex: 1; display: flex; align-items: center; justify-content: center; color: #bbb; font-size: 14px; }
.thread-header { background: #fff; border-bottom: 1px solid #e8e6e0; padding: 0.9rem 1.5rem; display: flex; justify-content: space-between; align-items: center; gap: 1rem; }
.thread-header-left { display: flex; align-items: center; gap: 0.8rem; min-width: 0; }
.thread-header .name { font-weight: 600; font-size: 15px; }
.thread-header .meta { font-size: 12px; color: #aaa; margin-top: 1px; }
.badges { display: flex; gap: 0.4rem; margin-top: 4px; flex-wrap: wrap; }
.badge { font-size: 10.5px; font-weight: 600; padding: 2px 8px; border-radius: 6px; background: #f0ede8; color: #666; }
.badge.ad { background: #e8f3ff; color: #2e6fb8; }
.stage-select { font-size: 12.5px; padding: 0.4rem 0.6rem; border: 1px solid #e0ddd5; border-radius: 8px; background: #fff; font-weight: 600; color: #1a1a1a; }
.thread { flex: 1; padding: 1.5rem; overflow-y: auto; position: relative; }
.thread.drag-over::after { content: 'Thả file vào đây để gửi'; position: absolute; inset: 10px; border: 2px dashed #1D9E75; border-radius: 12px; background: rgba(29,158,117,0.06); display: flex; align-items: center; justify-content: center; font-size: 14px; font-weight: 600; color: #1D9E75; }
.bubble { max-width: 65%; padding: 0.65rem 1rem; border-radius: 14px; margin-bottom: 0.2rem; font-size: 14px; line-height: 1.4; white-space: pre-wrap; word-wrap: break-word; }
.in { background: #fff; border: 1px solid #e8e6e0; margin-right: auto; }
.out { background: #1D9E75; color: #fff; margin-left: auto; }
.time { font-size: 10px; color: #bbb; margin: 0 4px 10px; }
.reply-box { background: #fff; border-top: 1px solid #e8e6e0; padding: 1rem 1.5rem; display: flex; flex-direction: column; gap: 0.5rem; }
.reply-row { display: flex; gap: 0.5rem; align-items: flex-end; }
.attach-btn { width: 40px; height: 40px; flex-shrink: 0; border: 1px solid #e0ddd5; border-radius: 10px; background: #fafaf8; cursor: pointer; font-size: 16px; display: flex; align-items: center; justify-content: center; }
.attach-btn:hover { background: #f0ede8; }
.reply-box textarea { flex: 1; border: 1px solid #e0ddd5; border-radius: 10px; padding: 0.7rem; font-size: 14px; resize: none; font-family: inherit; }
.reply-box button.send { padding: 0 1.5rem; border: none; border-radius: 10px; background: #1a1a1a; color: #fff; font-weight: 600; cursor: pointer; }
.attach-preview { display: none; align-items: center; gap: 0.5rem; font-size: 12.5px; background: #f0ede8; padding: 0.4rem 0.7rem; border-radius: 8px; width: fit-content; }
.attach-preview button { border: none; background: none; cursor: pointer; color: #888; font-size: 14px; }
</style></head><body>
<div class="app">
  <div class="sidebar">
    <div class="sidebar-head">
      <div class="logo">Anna Casa CRM<a href="/crm/logout">Đăng xuất</a></div>
      <form method="GET" action="/crm">
        <input class="search-box" type="text" name="q" value="{{ search }}" placeholder="Tìm khách theo tên...">
      </form>
      <div class="sync-row">
        <form method="POST" action="/crm/backfill" onsubmit="return confirm('Kéo lịch sử hội thoại cũ từ Facebook về? Có thể mất vài phút.')">
          <button class="sync-btn" type="submit">↻ Đồng bộ hội thoại cũ</button>
        </form>
        <span class="sync-status" id="sync-status"></span>
      </div>
    </div>
    <div class="conv-list">
      {% if conversations %}
        {% for c in conversations %}
        <a class="conv-item {{ 'active' if c.psid == active_psid else '' }}" href="/crm/customer/{{ c.psid }}">
          <div class="avatar">
            {% if c.avatar_url %}<img src="{{ c.avatar_url }}" onerror="this.remove()">{% endif %}
            <span>{{ (c.name or '?')[0]|upper }}</span>
          </div>
          <div class="conv-main">
            <div class="conv-top">
              <span class="conv-name">{{ c.name or 'Khách' }}</span>
              <span class="conv-time">{{ (c.last_message_at or c.last_contact_at or '')[5:10] }}</span>
            </div>
            <div class="conv-preview">{{ '↩ ' if c.last_message_direction == 'out' else '' }}{{ c.last_message_body or '' }}</div>
          </div>
        </a>
        {% endfor %}
      {% else %}
        <div class="conv-empty">Chưa có hội thoại nào</div>
      {% endif %}
    </div>
  </div>

  <div class="main">
    {% if customer %}
    <div class="thread-header">
      <div class="thread-header-left">
        <div class="avatar lg">
          {% if customer.avatar_url %}<img src="{{ customer.avatar_url }}" onerror="this.remove()">{% endif %}
          <span>{{ (customer.name or '?')[0]|upper }}</span>
        </div>
        <div>
          <div class="name">{{ customer.name or 'Khách' }}</div>
          <div class="meta">Ref: {{ customer.ref_code or '—' }}</div>
          <div class="badges">
            {% if customer.ad_id %}<span class="badge ad">Ad ID: {{ customer.ad_id }}</span><span class="badge ad">Messenger Ads</span>{% endif %}
          </div>
        </div>
      </div>
      <form method="POST" action="/crm/customer/{{ active_psid }}/stage" onchange="this.submit()">
        <select class="stage-select" name="stage">
          {% for s in stages %}
          <option value="{{ s }}" {{ 'selected' if customer.stage == s else '' }}>{{ s }}</option>
          {% endfor %}
        </select>
      </form>
    </div>
    <div class="thread" id="thread">
      {% for m in messages %}
      <div class="bubble {{ 'out' if m.direction == 'out' else 'in' }}">{{ m.body }}</div>
      <div class="time" style="text-align: {{ 'right' if m.direction == 'out' else 'left' }}">{{ m.created_at[:16].replace('T',' ') if m.created_at else '' }}</div>
      {% endfor %}
    </div>
    <form class="reply-box" method="POST" action="/crm/customer/{{ active_psid }}/reply" enctype="multipart/form-data" id="reply-form">
      <div class="attach-preview" id="attach-preview">
        <span id="attach-name"></span>
        <button type="button" onclick="clearAttachment()">✕</button>
      </div>
      <div class="reply-row">
        <input type="file" name="attachment" id="attachment-input" style="display:none">
        <button type="button" class="attach-btn" onclick="document.getElementById('attachment-input').click()" title="Đính kèm file/ảnh">📎</button>
        <textarea name="text" rows="1" placeholder="Nhắn tin cho khách... (kéo thả file vào để gửi)"></textarea>
        <button class="send" type="submit">Gửi</button>
      </div>
    </form>
    <script>
      const threadEl = document.getElementById('thread');
      threadEl.scrollTop = threadEl.scrollHeight;
      const fileInput = document.getElementById('attachment-input');
      const preview = document.getElementById('attach-preview');
      function showAttachPreview(file) {
        if (file) {
          preview.style.display = 'flex';
          document.getElementById('attach-name').textContent = file.name;
        } else {
          preview.style.display = 'none';
        }
      }
      function clearAttachment() {
        fileInput.value = '';
        showAttachPreview(null);
      }
      fileInput.addEventListener('change', () => showAttachPreview(fileInput.files[0]));
      threadEl.addEventListener('dragover', e => { e.preventDefault(); threadEl.classList.add('drag-over'); });
      threadEl.addEventListener('dragleave', () => threadEl.classList.remove('drag-over'));
      threadEl.addEventListener('drop', e => {
        e.preventDefault();
        threadEl.classList.remove('drag-over');
        if (e.dataTransfer.files.length) {
          fileInput.files = e.dataTransfer.files;
          showAttachPreview(fileInput.files[0]);
        }
      });
    </script>
    {% else %}
    <div class="main-empty">Chọn 1 hội thoại bên trái để xem</div>
    {% endif %}
  </div>
</div>
<script>
async function pollBackfill() {
  try {
    const res = await fetch('/crm/backfill/status');
    const data = await res.json();
    const el = document.getElementById('sync-status');
    if (data.running) {
      el.textContent = `Đang đồng bộ... ${data.conversations} hội thoại, ${data.messages} tin nhắn`;
      setTimeout(pollBackfill, 3000);
    } else if (data.done && data.conversations > 0) {
      el.textContent = data.error ? `Lỗi: ${data.error}` : `Xong ${data.conversations} hội thoại — reload để xem`;
    }
  } catch (e) {}
}
pollBackfill();
</script>
</body></html>"""

@app.route("/crm/login", methods=["GET", "POST"])
def crm_login():
    error = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        if CRM_PASSWORD and pw == CRM_PASSWORD:
            session["crm_logged_in"] = True
            return redirect(url_for("crm_inbox"))
        error = "Sai mật khẩu"
    return render_template_string(CRM_LOGIN_HTML, error=error)

@app.route("/crm/logout")
def crm_logout():
    session.pop("crm_logged_in", None)
    return redirect(url_for("crm_login"))

@app.route("/crm", defaults={"psid": None})
@app.route("/crm/customer/<psid>")
@crm_login_required
def crm_inbox(psid):
    search = request.args.get("q", "").strip()
    return render_template_string(
        CRM_INBOX_HTML,
        conversations=get_conversations(search),
        search=search,
        active_psid=psid,
        customer=get_customer(psid) if psid else None,
        messages=get_messages(psid) if psid else [],
        stages=CRM_STAGES,
    )

@app.route("/crm/backfill", methods=["POST"])
@crm_login_required
def crm_backfill():
    if not _backfill_status["running"]:
        threading.Thread(target=backfill_facebook_conversations, daemon=True).start()
    return redirect(url_for("crm_inbox"))

@app.route("/crm/backfill/status")
@crm_login_required
def crm_backfill_status():
    return jsonify(_backfill_status)

@app.route("/crm/customer/<psid>/reply", methods=["POST"])
@crm_login_required
def crm_reply(psid):
    text = request.form.get("text", "").strip()
    if text:
        send_text(psid, text)
    attachment = request.files.get("attachment")
    if attachment and attachment.filename:
        send_uploaded_attachment(psid, attachment)
    return redirect(url_for("crm_inbox", psid=psid))

CRM_STAGES = ["Mới", "Đang tư vấn", "Đã báo giá", "Đã chốt", "Không tiềm năng"]

@app.route("/crm/customer/<psid>/stage", methods=["POST"])
@crm_login_required
def crm_set_stage(psid):
    stage = request.form.get("stage", "")
    if stage in CRM_STAGES:
        upsert_customer(psid, stage=stage)
    return redirect(url_for("crm_inbox", psid=psid))


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
