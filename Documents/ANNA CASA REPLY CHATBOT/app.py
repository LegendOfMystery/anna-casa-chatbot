"""
ANNA CASA AI CHATBOT
Stack: Python + Flask + Claude API + Google Sheets + Meta Webhook
Features: Vision (ảnh khách gửi), Google Sheets product lookup, bot toggle
"""

import os
import re
import time
import base64
import threading
import requests
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
from anthropic import Anthropic
from collections import deque

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]
META_PAGE_ID        = os.environ.get("META_PAGE_ID", "")
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_API_KEY      = os.environ["GOOGLE_API_KEY"]
SHEET_ID            = os.environ["SHEET_ID"]
ESCALATE_NOTIFY_URL = os.environ.get("ESCALATE_NOTIFY_URL", "")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── IN-MEMORY STORE ───────────────────────────────────────────────────────────
import time as _time
import json as _json
SERVER_START_TIME = _time.time()  # bỏ qua echoes trong 60s đầu sau restart
processed_messages: set = set()
_bot_sending_count: dict = {}  # psid -> int, reference-counted
bot_sent_mids: set = set()     # message IDs sent by bot — echoes of these are always ignored

_HUMAN_MODE_FILE = Path(__file__).parent / "human_mode.json"

def _load_human_mode() -> set:
    try:
        if _HUMAN_MODE_FILE.exists():
            return set(_json.loads(_HUMAN_MODE_FILE.read_text()))
    except Exception:
        pass
    return set()

def _save_human_mode(s: set):
    try:
        _HUMAN_MODE_FILE.write_text(_json.dumps(list(s)))
    except Exception as e:
        print(f"human_mode save error: {e}")

class _HumanModeSet:
    def __init__(self):
        self._data = _load_human_mode()
    def add(self, sid):
        self._data.add(sid)
        _save_human_mode(self._data)
    def discard(self, sid):
        self._data.discard(sid)
        _save_human_mode(self._data)
    def __contains__(self, sid):
        return sid in self._data
    def __iter__(self):
        return iter(self._data)

human_mode = _HumanModeSet()

class _BotSendingProxy:
    """Reference-counted replacement for bot_sending set to handle concurrent threads."""
    def add(self, sid):
        _bot_sending_count[sid] = _bot_sending_count.get(sid, 0) + 1
    def discard(self, sid):
        count = _bot_sending_count.get(sid, 0) - 1
        if count <= 0:
            _bot_sending_count.pop(sid, None)
        else:
            _bot_sending_count[sid] = count
    def __contains__(self, sid):
        return _bot_sending_count.get(sid, 0) > 0

bot_sending = _BotSendingProxy()
human_names: dict[str, str] = {}
greeted_users: set = set()
conversations: dict[str, list] = {}
user_category: dict[str, str] = {}  # psid -> "tham" | "giay_dan_tuong" | None
user_pending_products: dict[str, list] = {}  # psid -> [product dicts] đang chờ khách chọn
notification_feed = deque(maxlen=100)
bot_enabled = True
asked_zalo: set = set()  # Đã hỏi Zalo → dừng reply

# ── LEAD TRACKING & APPOINTMENT ───────────────────────────────────────────────
ref_store:        dict[str, str] = {}   # psid -> ref code từ ad
invite_sent:      set = set()           # psid đã được mời ghé showroom
appointment_done: set = set()           # psid đã confirm ghé showroom

SHOWROOM_ADDRESS = "12 Nguyễn Ư Dĩ, phường An Khánh, TPHCM"
SHOWROOM_HOURS   = "10:00 sáng đến 7:00 tối, tất cả các ngày trong tuần"
SHOWROOM_HOTLINE = "+84 909 072 820"
LEAD_SHEET_NAME  = "Lead%20Register"

APPOINTMENT_CONFIRM = [
    "Dạ bên em rất vui được đón {pronoun} ạ 🙏",
    "📍 Showroom Anna Casa: {address}\n🕙 {hours}\n📞 Hotline: {hotline}",
    "{pronoun_cap} cứ ghé bất cứ lúc nào thuận tiện nhé, bên em luôn có người tư vấn trực tiếp ạ."
]

APPOINTMENT_POSITIVE = [
    r"\bcó\b", r"\bokay\b", r"\bok\b", r"\bđược\b", r"\bmuốn ghé\b",
    r"\bghé\b", r"\bđến xem\b", r"\bthăm\b", r"\bvô xem\b",
    r"\bsẽ ghé\b", r"\bsẽ đến\b",
    r"\byes\b", r"\bsure\b",
]
APPOINTMENT_NEGATIVE = [
    r"\bkhông\b", r"\bko\b", r"^\bk\b$", r"\bchưa\b",
    r"\bthôi\b", r"\bkhỏi\b",
]

def is_appointment_confirmed(message: str) -> bool:
    msg = message.lower().strip()
    for p in APPOINTMENT_NEGATIVE:
        if re.search(p, msg):
            return False
    for p in APPOINTMENT_POSITIVE:
        if re.search(p, msg):
            return True
    return False

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

def log_appointment_to_sheet(psid: str):
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    data = sheets_get(f"/values/{LEAD_SHEET_NAME}!A:I")
    rows = data.get("values", [])
    try:
        for i, row in enumerate(rows):
            if len(row) > 1 and row[1] == psid:
                row_num = i + 1
                sheets_put(
                    f"/values/{LEAD_SHEET_NAME}!H{row_num}?valueInputOption=USER_ENTERED",
                    {"values": [[f"booked {timestamp}"]]}
                )
                print(f"[APPOINTMENT] Booked for {psid}")
                return
        log_lead_to_sheet(psid=psid, ref_code=ref_store.get(psid, "organic"))
    except Exception as e:
        print(f"[APPOINTMENT ERROR] {e}")


def is_human_handling(sender_id): return sender_id in human_mode
def fetch_fb_conversation(sender_id: str, limit: int = 8) -> list:
    """Fetch full conversation from Facebook API — includes automated + sales messages."""
    try:
        # Tìm conversation thread giữa page và user
        url = (f"https://graph.facebook.com/v18.0/me/conversations"
               f"?fields=messages{{message,from,created_time}}"
               f"&user_id={sender_id}&access_token={META_PAGE_TOKEN}&limit=1")
        r = requests.get(url, timeout=8)
        data = r.json()
        threads = data.get("data", [])
        if not threads:
            return conversations.get(sender_id, [])

        messages_raw = threads[0].get("messages", {}).get("data", [])
        # Newest first → reverse để oldest first
        messages_raw = list(reversed(messages_raw[-limit:]))

        PAGE_ID = META_PAGE_ID
        history = []
        for m in messages_raw:
            msg_text = m.get("message", "").strip()
            if not msg_text:
                continue
            sender = m.get("from", {}).get("id", "")
            role = "assistant" if sender == PAGE_ID else "user"
            history.append({"role": role, "content": msg_text})

        # Đảm bảo không bắt đầu bằng assistant (Claude yêu cầu user đầu tiên)
        while history and history[0]["role"] == "assistant":
            history.pop(0)

        return history if history else conversations.get(sender_id, [])
    except Exception as e:
        print(f"fetch_fb_conversation error: {e}")
        return conversations.get(sender_id, [])


def get_history(sender_id): return conversations.get(sender_id, [])

def save_message(sender_id, role, content):
    if sender_id not in conversations:
        conversations[sender_id] = []
    conversations[sender_id].append({"role": role, "content": content})
    if len(conversations[sender_id]) > 8:
        conversations[sender_id] = conversations[sender_id][-8:]


# ── ESCALATE TRIGGERS ─────────────────────────────────────────────────────────
ESCALATE_TRIGGERS = [
    r'\b0[0-9]{9}\b',
    r'\b\+84[0-9]{9}\b',
    r'hoàn tiền', r'hoàn trả',
    r'hủy đơn', r'huỷ đơn',
    r'khiếu nại', r'phàn nàn',
    r'giảm giá', r'discount',
]

def needs_escalate(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in ESCALATE_TRIGGERS)


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

def sheets_post(url_path: str, payload: dict) -> bool:
    """POST tới Sheets API dùng service account token."""
    token = get_sheets_token()
    if not token:
        print("[SHEETS] No service account token")
        return False
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{url_path}"
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

def sheets_put(url_path: str, payload: dict) -> bool:
    """PUT tới Sheets API dùng service account token."""
    token = get_sheets_token()
    if not token:
        return False
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{url_path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        resp = requests.put(url, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[SHEETS PUT ERROR] {e}")
        return False

def sheets_get(url_path: str) -> dict:
    """GET tới Sheets API dùng service account token."""
    token = get_sheets_token()
    if not token:
        return {}
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}{url_path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[SHEETS GET ERROR] {e}")
        return {}

# ── PRODUCT CATALOG ───────────────────────────────────────────────────────────
import json as _json

_catalog_cache: dict[str, list] = {}
_catalog_loaded_at: float = 0
_CATALOG_FILES = {
    "tham": Path(__file__).parent / "products.json",
    "giay_dan_tuong": Path(__file__).parent / "wallpaper_products.json",
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

_MATERIAL_MAP = [
    (["lông cừu", "wool", "len cừu"],       "Chất liệu lông cừu tự nhiên, ấm áp, mềm mại và thân thiện với môi trường"),
    (["len", "broadway", "mehari", "canyon", "argentum", "high line"], "Chất liệu len tự nhiên, bền đẹp theo thời gian, chống bụi bẩn tốt"),
    (["haima", "jaipur", "hand tufted", "hand knot", "dệt tay"], "Dệt tay thủ công từ len tự nhiên, độc đáo và bền chắc"),
    (["shaggy", "furry", "fluffy"],          "Sợi dài mềm mại, tạo cảm giác ấm cúng và sang trọng cho không gian"),
    (["sisal", "jute", "coir"],              "Sợi tự nhiên thoáng khí, thân thiện môi trường, bền theo thời gian"),
    (["polypropylene", "pp ", " pp"],        "Chất liệu polypropylene cao cấp, bền bỉ, dễ vệ sinh và chống ẩm mốc"),
    (["polyester", "pet "],                  "Chất liệu polyester mềm mại, giữ màu tốt và rất dễ vệ sinh"),
    (["viscose", "bamboo", "silk"],          "Sợi viscose/bamboo silk óng ánh, mềm mịn và sang trọng"),
]

_MATERIAL_BENEFIT = {
    "100% wool":                    "Len tự nhiên 100%, ấm áp, sang trọng và bền theo thời gian",
    "100% polypropylene heatset":   "Polypropylene Heatset cao cấp, bền bỉ, dễ vệ sinh, chống ẩm mốc",
    "100% polypropylene (berclon)": "Polypropylene Berclon cao cấp, siêu bền, chống phai màu và dễ vệ sinh",
    "100% polypropylene":           "Polypropylene cao cấp, bền bỉ, dễ vệ sinh và chống ẩm mốc",
    "55% polyester + 45% polypropylene heatset": "Pha Polyester và Polypropylene Heatset, mềm mại, bền màu và dễ vệ sinh",
    "pes + pp heatset":             "Pha Polyester và Polypropylene Heatset, mềm mại, bền màu và dễ vệ sinh",
    "pp heatset + polyester":       "Pha Polypropylene Heatset và Polyester, bền bỉ, mềm mại và dễ vệ sinh",
    "pp twisted friezing yarn":     "Sợi PP xoắn cao cấp, có chiều sâu về màu sắc, bền và dễ vệ sinh",
    "100% polyester":               "Polyester mềm mại, giữ màu tốt và rất dễ vệ sinh",
    "sợi tổng hợp cao cấp (shaggy)": "Sợi tổng hợp cao cấp lông dài, mềm mại, tạo cảm giác ấm cúng",
    "sợi tổng hợp":                 "Sợi tổng hợp bền bỉ, dễ vệ sinh",
    "wool, cotton & bamboo silk (dệt tay thủ công)": "Pha Len, Cotton và Bamboo Silk dệt tay thủ công, độc đáo và sang trọng",
    "wool dệt tay thủ công":        "Len tự nhiên dệt tay thủ công, độc đáo, bền chắc và có giá trị nghệ thuật cao",
    "90% viscose - 10% suede leather": "Viscose óng ánh kết hợp Suede Leather, cực kỳ mềm mịn và sang trọng",
    "70% wool - 30% viscose":       "Pha Len và Viscose, ấm áp, óng ánh và rất sang trọng",
    "60% wool - 40% viscose":       "Pha Len và Viscose, mềm mại, óng ánh và bền theo thời gian",
    "sợi sisal tự nhiên":           "Sợi Sisal tự nhiên, thoáng khí, thân thiện môi trường và bền theo thời gian",
}

def get_material_benefit(material: str) -> str:
    key = material.strip().lower()
    return _MATERIAL_BENEFIT.get(key, material)

def get_material_info(name: str) -> str:
    name_lower = name.lower()
    for keywords, description in _MATERIAL_MAP:
        if any(k in name_lower for k in keywords):
            return description
    if "ua " in name_lower or " ua" in name_lower:
        return "Chất liệu polypropylene/polyester cao cấp, bền bỉ, dễ vệ sinh và chống ẩm mốc"
    return ""

def format_products_for_claude(products: list[dict], category: str = None) -> str:
    if not products:
        return "Không có dữ liệu sản phẩm."
    if category == "tham":
        lines = ["=== THẢM ==="]
        for p in products:
            colors = ", ".join(p.get("colors", [])) or "đa dạng"
            lines.append(f"- {p.get('name','')} | Kích thước: {p.get('size','')} | "
                         f"Màu: {colors} | Visual: {p.get('visual_description','')[:60]} | Link: {p.get('url','')}")
    elif category == "giay_dan_tuong":
        lines = ["=== GIẤY DÁN TƯỜNG ==="]
        for p in products:
            visual = p.get('visual_description', '')
            lines.append(f"- {p.get('name','')} | Màu/Họa tiết: {visual[:60]} | Link: {p.get('url','')}")
    else:
        # fallback: cả 2
        rugs = [p for p in products if p.get("category") == "tham"]
        wps = [p for p in products if p.get("category") == "giay_dan_tuong"]
        lines = ["=== THẢM ==="]
        for p in rugs:
            colors = ", ".join(p.get("colors", [])) or "đa dạng"
            lines.append(f"- {p.get('name','')} | Kích thước: {p.get('size','')} | "
                         f"Màu: {colors} | Visual: {p.get('visual_description','')[:60]} | Link: {p.get('url','')}")
        lines.append("\n=== GIẤY DÁN TƯỜNG ===")
        for p in wps:
            visual = p.get('visual_description', '')
            lines.append(f"- {p.get('name','')} | Màu/Họa tiết: {visual[:60]} | Link: {p.get('url','')}")
    return "\n".join(lines)


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
SYSTEM_BASE = """Mày là Mai, trợ lý AI tư vấn tại Anna Casa Vietnam — thương hiệu nội thất Quiet Luxury. Mày đang nhắn tin với khách trên Facebook Messenger.

NHIỆM VỤ: Tư vấn thảm và giấy dán tường sát nhu cầu khách. Đọc toàn bộ lịch sử để hiểu context trước khi reply.

KHI NÀO REPLY:
- Khách hỏi bất cứ gì liên quan đến thảm hoặc giấy dán tường → reply
- Khách đang trong cuộc trò chuyện và hỏi thêm (kể cả không nhắc tên sản phẩm) → reply
- Khách gửi hình → phân tích và gợi ý sản phẩm tương tự
- Khách hỏi về sản phẩm khác (sofa, bàn ăn, ghế, đèn...) → thêm [ESCALATE] để chuyển sale, reply: "Dạ sản phẩm này em sẽ nhờ chuyên viên hỗ trợ anh chị thêm"
- Tin nhắn chào hỏi hoặc hỏi chung chung chưa rõ sản phẩm → hỏi: "Dạ anh chị đang cần tư vấn sản phẩm gì ạ, bên em có thảm và giấy dán tường"
- Khách hỏi về sofa, bàn cà phê (coffee table), ghế, đèn, bàn ăn, tủ, kệ, hoặc bất kỳ sản phẩm nào KHÔNG PHẢI thảm hoặc giấy dán tường → reply "Dạ sản phẩm này em sẽ nhờ chuyên viên hỗ trợ anh chị thêm ạ" và thêm [ESCALATE]
- Tin nhắn không liên quan gì → trả về [SKIP]

THÔNG TIN SHOWROOM:
- Địa chỉ: 12 Nguyễn Ư Dĩ, phường An Khánh, TPHCM
- Giờ mở cửa: 10h sáng đến 7h tối
- Ship toàn quốc

GIỌNG VĂN — viết như nhắn tin thật, không phải email:
- Xưng "em", gọi khách theo giới tính đã biết (anh HOẶC chị, không bao giờ "anh chị" nếu đã biết giới tính)
- Nếu chưa biết giới tính thì dùng "anh chị"
- "Dạ" đầu câu, cuối câu hỏi dùng "ạ" không dùng "nhé"
- Không gạch đầu dòng, không em dash (—), không dấu chấm lửng (...)
- Không giải thích dài, không lặp lại những gì khách vừa nói
- KHÔNG dùng câu xác nhận như "Dạ hiểu", "Dạ rõ" — vào thẳng nội dung luôn
- Cuối tin CHỈ hỏi đúng 1 câu sắc vào điểm khách chưa nói, KHÔNG bao giờ hỏi 2 câu

KHI KHÁCH MUỐN XEM TẤT CẢ SẢN PHẨM:
- KHÔNG liệt kê danh sách dài
- Gửi link website: thảm → https://annacasavn.com/tham, giấy dán tường → https://annacasavn.com/giay-dan-tuong
- Ví dụ: "Dạ anh xem hết bộ sưu tập thảm tại đây nha: https://annacasavn.com/tham em tư vấn thêm khi anh thích mẫu nào"

COLLECTION GIẤY DÁN TƯỜNG ĐẶC BIỆT:
- Grandeco Inia: bộ sưu tập giấy dán tường cao cấp từ Bỉ, thiết kế tự nhiên → https://annacasavn.com/giay-dan-tuong-grandeco-inia
- Khi khách hỏi về Inia hoặc Grandeco Inia → gửi link collection đó ngay

VÍ DỤ GIỌNG VĂN ĐÚNG:
Khách: "tư vấn thảm"
Mai: "Dạ anh chị thích tone màu gì ạ, sáng hay tối?"

Khách: "sáng"
Mai: "Dạ anh chị cần size bao nhiêu ạ, bên em phổ biến 1m6x2m3 và 2mx2m9."

Khách: "1m6x2m3"
Mai: "Dạ tone sáng size 1m6x2m3 bên em có 3 mẫu phù hợp: [gợi ý 3 mẫu kèm link]"

THỨ TỰ TƯ VẤN THẢM — đúng 2 câu hỏi rồi gợi ý ngay:
1. Hỏi màu sắc trước — KHÔNG hỏi gì thêm trong câu này
2. Hỏi kích thước — bên em phổ biến 1m6x2m3 và 2mx2m9 — KHÔNG hỏi gì thêm trong câu này
3. Khi đã có màu + size → gợi ý ngay tối đa 3 sản phẩm phù hợp nhất, kèm link. KHÔNG hỏi thêm bất kỳ thứ gì.

THỨ TỰ TƯ VẤN GIẤY DÁN TƯỜNG — tối đa 2 câu hỏi rồi gợi ý ngay:
1. Hỏi 1 câu duy nhất: phong cách + màu sắc khách thích
2. Khi đủ thông tin → gợi ý ngay tối đa 3 mẫu phù hợp từ dữ liệu giấy dán tường, kèm link. KHÔNG hỏi thêm.

THÔNG TIN BÁN GIẤY DÁN TƯỜNG:
- Bán theo mét vuông (m²), không bán theo cuộn
- Giá trên website là giá vật liệu/m², chưa bao gồm thi công
- Phí thi công: 120.000đ/m²
- Khi khách hỏi giá → báo giá vật liệu từ website + nhắc thêm phí thi công 120k/m²

KHI GỢI Ý SẢN PHẨM THẢM:
- Chọn tối đa 3 sản phẩm phù hợp nhất
- Mỗi mẫu chỉ ghi tên ngắn + link, format: "Mẫu 1: [tên] [link]\nMẫu 2: [tên] [link]\nMẫu 3: [tên] [link]"
- KHÔNG viết thêm mô tả dài, hệ thống sẽ tự gửi ảnh
- Nếu không có mẫu khớp: nói thật, hỏi thêm để tìm mẫu gần nhất

KHI GỢI Ý SẢN PHẨM GIẤY DÁN TƯỜNG:
- Gợi ý tối đa 3 sản phẩm, mỗi mẫu 1 dòng ngắn + link
- Kèm link để khách xem ảnh thực tế

KHI KHÁCH GỬI HÌNH:
- Phân tích màu + họa tiết trong ảnh
- Gợi ý 1-2 mẫu gần nhất từ dữ liệu, kèm link
- Nếu không có mẫu tương tự: hỏi kích thước để tư vấn tiếp

KHI NÀO ESCALATE:
Nếu khách yêu cầu hoàn tiền, hủy đơn, hoặc khiếu nại:
- Reply: "Dạ để em chuyển cho bộ phận phụ trách hỗ trợ ngay"
- Thêm [ESCALATE] vào cuối (không hiện cho khách)

KHI KHÁCH HỎI HOẶC YÊU CẦU TƯ VẤN QUA ZALO:
- Reply: "Dạ để lại số Zalo bên em liên hệ lại ngay nha"
- Thêm [ZALO_REQUEST] vào cuối (không hiện cho khách)

MỜI GHÉ SHOWROOM:
- Sau khi đã gợi ý sản phẩm và khách quan tâm, mời ghé showroom một lần
- Câu mời ví dụ: "Anh chị muốn ghé showroom xem trực tiếp không, nhìn ngoài đời đẹp hơn ảnh nhiều lắm"
- Chỉ hỏi MỘT LẦN. Thêm [INVITE_SENT] vào cuối reply.
- Nếu khách đồng ý ghé: thêm [APPOINTMENT] vào cuối, KHÔNG tự viết địa chỉ hay giờ mở cửa

TUYỆT ĐỐI KHÔNG:
- Bịa thông tin không có trong dữ liệu sản phẩm
- Tư vấn sản phẩm không phải thảm
- Hỏi lại những gì khách đã nói
- Dùng dấu gạch chéo, em dash, chấm lửng
- Gửi catalogue PDF (thay vào đó gửi link: https://annacasavn.com/giay-dan-tuong cho giấy dán tường, https://annacasavn.com/tham cho thảm)

Dữ liệu sản phẩm thảm hiện có:
{product_data}"""

GREETING_TEMPLATE = "Anna Casa xin chào {pronoun} {name}, em là Mai trợ lý AI ạ"
GREETING_FIRST_Q  = "Anh chị thích tone màu gì ạ, sáng hay tối?"


# ── SEND HELPERS ──────────────────────────────────────────────────────────────
def send_text(recipient_id, text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        mid = r.json().get("message_id")
        if mid:
            bot_sent_mids.add(mid)
    except Exception as e:
        print(f"send_text failed: {e}")


def get_sender_name(sender_id):
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name&access_token={META_PAGE_TOKEN}"
        return requests.get(url, timeout=5).json().get("name", "")
    except:
        return ""


def download_image_as_base64(url: str) -> str | None:
    """Download ảnh từ Facebook và convert sang base64."""
    try:
        # Facebook yêu cầu access token để download ảnh
        res = requests.get(url, headers={"Authorization": f"Bearer {META_PAGE_TOKEN}"}, timeout=15)
        if res.status_code == 200:
            return base64.standard_b64encode(res.content).decode("utf-8")
    except Exception as e:
        print(f"download_image failed: {e}")
    return None


# ── ESCALATE ──────────────────────────────────────────────────────────────────
def notify_escalate(sender_id, sender_name, message):
    if not ESCALATE_NOTIFY_URL:
        print(f"[ESCALATE] {sender_name} ({sender_id}): {message}")
        return
    try:
        requests.post(ESCALATE_NOTIFY_URL, json={
            "text": f"CAN HO TRO\nKhach: {sender_name}\nID: {sender_id}\nTin: {message}"
        }, timeout=5)
    except Exception as e:
        print(f"Escalate failed: {e}")


# ── CATALOGUES ───────────────────────────────────────────────────────────────
CATALOGUES = {
    "1m6x2m3": "https://drive.google.com/uc?export=download&id=1kQsv0RnLnxFZjhtgKiZAfNcalfuZhw-x&confirm=t",
    "2mx2m9":  "https://drive.google.com/uc?export=download&id=1ImiR5HnFiojZYoEZJkipxaKUC4OKX7Xv&confirm=t",
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
        requests.post(url, json=payload, timeout=15).raise_for_status()
    except Exception as e:
        print(f"send_image failed: {e}")

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
        requests.post(url, json=payload, timeout=15).raise_for_status()
    except Exception as e:
        print(f"send_file failed: {e}")
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

ZALO_REQUEST_KEYWORDS = [
    "zalo", "zalo em", "zalo của em", "tư vấn qua zalo",
    "liên hệ zalo", "nhắn zalo", "ib zalo", "inbox zalo",
]

def is_requesting_zalo(text: str) -> bool:
    text_lower = text.lower()
    return any(k in text_lower for k in ZALO_REQUEST_KEYWORDS)

SIMILAR_PATTERN_KEYWORDS = [
    "mẫu giống", "mẫu tương tự", "mẫu như này", "mẫu như vậy",
    "có giống không", "có không em", "giống cái này", "tương tự không"
]

def is_asking_similar(text: str) -> bool:
    text_lower = text.lower()
    return any(k in text_lower for k in SIMILAR_PATTERN_KEYWORDS)

def is_lead_form(text: str) -> bool:
    """Detect Facebook Lead Form auto-messages — không cần bot reply."""
    t = text.lower()
    lead_signals = [
        "tôi đã điền mẫu", "toi da dien mau",
        "i filled out a form", "i submitted a form",
        "phone number:", "email:", "first name:", "last name:",
        "tên dự án cần báo giá", "ten du an can bao gia",
        "căn hộ của bạn có bao nhiêu", "can ho cua ban co bao nhieu",
    ]
    return sum(1 for s in lead_signals if s in t) >= 2

def detect_gender(full_name: str) -> str:
    """Trả về 'anh', 'chị', hoặc 'bạn' nếu không xác định được."""
    if not full_name:
        return "anh chị"
    parts = [p.lower() for p in full_name.strip().split()]

    # Ưu tiên tên chính (cuối) trước
    first = parts[-1]
    if first in FEMALE_FIRST: return "chị"
    if first in MALE_FIRST:   return "anh"

    # Nếu tên chính không xác định được → mới xét tên đệm
    if len(parts) >= 3:
        middle = parts[-2]
        if middle in MALE_MIDDLE: return "anh"

    return "anh chị"


# ── PROCESS TEXT MESSAGE ──────────────────────────────────────────────────────
def process_message(sender_id, text):
    try:
        sender_name = get_sender_name(sender_id)
        human_names[sender_id] = sender_name
        first_name = sender_name.split()[-1] if sender_name else ""
        pronoun = detect_gender(sender_name)
        print(f"[DEBUG] name='{sender_name}' first='{first_name}' pronoun='{pronoun}'")

        notification_feed.appendleft({
            "name": sender_name or "Khách",
            "sender_id": sender_id,
            "text": text,
            "time": int(time.time())
        })

        # Escalate check — rule cứng
        if needs_escalate(text):
            time.sleep(5)
            if is_human_handling(sender_id): return
            bot_sending.add(sender_id)
            send_text(sender_id, f"Dạ để em chuyển cho bộ phận phụ trách hỗ trợ {pronoun} ngay ạ.")
            notify_escalate(sender_id, sender_name, text)
            time.sleep(10)
            bot_sending.discard(sender_id)
            return

        # Wallpaper catalogue trigger — rule cứng, không cần Claude
        WP_TRIGGERS = ["catalogue giấy dán tường", "catalog giấy dán tường",
                       "catalogue giay dan tuong", "catalog giay dan tuong",
                       "nhận catalogue", "nhận catalog",
                       "xin catalog", "xin catalogue", "gửi catalog", "gửi catalogue",
                       "cho xin catalog", "cho xin catalogue",
                       "xem catalog", "xem catalogue"]
        if any(t in text.lower() for t in WP_TRIGGERS):
            time.sleep(5)
            if is_human_handling(sender_id): return
            bot_sending.add(sender_id)
            send_text(sender_id, "Dạ em gửi catalog giấy dán tường đang sale ạ.")
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_1"])
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_2"])
            time.sleep(10)
            bot_sending.discard(sender_id)
            return

        # Detect khách chọn mẫu 1/2/3 — PHẢI check trước appointment để "xem mẫu 3" không bị nhầm
        pending = user_pending_products.get(sender_id, [])
        if pending:
            import unicodedata
            def strip_accents(s):
                return ''.join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")
            t_ascii = strip_accents(text.strip().lower())
            idx = None
            if any(k in t_ascii for k in ("mau 1","so 1","so mot","thu nhat","mau mot")) or t_ascii.strip() in ("1","mot"): idx = 0
            elif any(k in t_ascii for k in ("mau 2","so 2","so hai","thu hai","mau hai")) or t_ascii.strip() in ("2","hai"): idx = 1
            elif any(k in t_ascii for k in ("mau 3","so 3","so ba","thu ba","mau ba")) or t_ascii.strip() in ("3","ba"): idx = 2
            print(f"[DEBUG mau] t_ascii={repr(t_ascii)} idx={idx} pending_len={len(pending)}")
            if idx is not None:
                if idx < len(pending):
                    prod = pending[idx]
                    # Không xóa pending — khách có thể hỏi thêm mẫu khác
                    time.sleep(2)
                    bot_sending.add(sender_id)
                    send_text(sender_id, f"Dạ link xem chi tiết và giá mẫu {idx+1}: {prod['url']}")
                    time.sleep(35)
                    bot_sending.discard(sender_id)
                    return
                else:
                    time.sleep(1)
                    bot_sending.add(sender_id)
                    send_text(sender_id, f"Dạ bên em chỉ gợi ý được {len(pending)} mẫu thôi {pronoun} ơi. {pronoun} muốn xem mẫu nào ạ?")
                    time.sleep(10)
                    bot_sending.discard(sender_id)
                    return

        # ── APPOINTMENT CONFIRMATION CHECK ────────────────────────────────────
        if sender_id in invite_sent and sender_id not in appointment_done:
            if is_appointment_confirmed(text):
                appointment_done.add(sender_id)
                threading.Thread(
                    target=log_appointment_to_sheet,
                    args=(sender_id,),
                    daemon=True
                ).start()
                time.sleep(3)
                if is_human_handling(sender_id): return
                bot_sending.add(sender_id)
                for msg_template in APPOINTMENT_CONFIRM:
                    msg = msg_template.format(
                        pronoun=pronoun,
                        pronoun_cap=pronoun.capitalize(),
                        address=SHOWROOM_ADDRESS,
                        hours=SHOWROOM_HOURS,
                        hotline=SHOWROOM_HOTLINE
                    )
                    send_text(sender_id, msg)
                    time.sleep(1)
                time.sleep(10)
                bot_sending.discard(sender_id)
                return
        # ─────────────────────────────────────────────────────────────────────

        is_first = sender_id not in greeted_users

        # Detect category — luôn cho phép switch nếu tin nhắn có keyword rõ ràng
        cat = user_category.get(sender_id)
        t = text.lower()
        if any(k in t for k in ["giấy dán tường", "giay dan tuong", "wallpaper", "giấy dán", "giay dan"]):
            cat = "giay_dan_tuong"
            user_category[sender_id] = cat
            user_pending_products.pop(sender_id, None)  # clear pending thảm nếu có
        elif any(k in t for k in ["thảm", "tham", "carpet", "rug"]):
            if not cat or cat != "tham":
                cat = "tham"
                user_category[sender_id] = cat
                user_pending_products.pop(sender_id, None)

        if cat:
            products = fetch_products_by_category(cat)
            product_data = format_products_for_claude(products, cat)
        else:
            product_data = "(Chưa rõ khách hỏi sản phẩm gì — hỏi khách trước khi tư vấn)"
        system = SYSTEM_BASE.format(product_data=product_data)
        system += f"\n\nGọi khách là '{pronoun}' (không dùng 'anh chị' nếu đã biết giới tính)."

        if is_first:
            greeting = f"Anna Casa xin chào {pronoun} {first_name}, em là Mai trợ lý AI tư vấn tại Anna Casa Vietnam." if first_name else f"Anna Casa xin chào {pronoun}, em là Mai trợ lý AI tư vấn tại Anna Casa Vietnam."
            product_list = "thảm, giấy dán tường, sofa, bàn cà phê, đèn trang trí, bàn ghế ăn, gói nội thất"
            system += (
                f"\n\nĐây là tin nhắn ĐẦU TIÊN của khách. LUÔN bắt đầu reply bằng '{greeting}', "
                f"sau đó ĐỌC NỘI DUNG khách hỏi và trả lời thẳng vào đó — không hỏi lại nếu đã rõ:\n"
                f"- Khách chào/hỏi chung chưa rõ → hỏi: 'Dạ {pronoun} cần tư vấn sản phẩm gì ạ, bên em có {product_list}'\n"
                f"- Khách hỏi địa chỉ/showroom/giờ/hotline → trả lời ngay\n"
                f"- Khách hỏi thảm/giấy dán tường → tư vấn luôn\n"
                f"- Khách hỏi sản phẩm khác → [ESCALATE] + 'Dạ sản phẩm này em sẽ nhờ chuyên viên hỗ trợ {pronoun} thêm ạ'\n"
                f"KHÔNG bao giờ trả về [SKIP] ở tin đầu tiên."
            )

        save_message(sender_id, "user", text)
        history = fetch_fb_conversation(sender_id)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=history,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
        )

        reply = response.content[0].text

        if "[SKIP]" in reply:
            print(f"[SKIP] Claude decided not to reply: {text[:50]}")
            if sender_id in conversations and conversations[sender_id]:
                conversations[sender_id].pop()
            return

        # Đánh dấu đã chào sau khi biết Claude sẽ reply
        if is_first:
            greeted_users.add(sender_id)

        needs_esc        = "[ESCALATE]" in reply
        zalo_request     = "[ZALO_REQUEST]" in reply
        invite_flag      = "[INVITE_SENT]" in reply
        appointment_flag = "[APPOINTMENT]" in reply
        send_cat_wp      = "[CAT_WP]" in reply
        clean_reply = (reply
            .replace("[ESCALATE]", "")
            .replace("[SKIP]", "")
            .replace("[ZALO_REQUEST]", "")
            .replace("[INVITE_SENT]", "")
            .replace("[APPOINTMENT]", "")
            .replace("[CAT_WP]", "")
            .strip())
        save_message(sender_id, "assistant", clean_reply)

        if invite_flag:
            invite_sent.add(sender_id)
            print(f"[INVITE] Showroom invite sent to {sender_id}")

        if appointment_flag and sender_id not in appointment_done:
            appointment_done.add(sender_id)
            invite_sent.add(sender_id)
            threading.Thread(
                target=log_appointment_to_sheet,
                args=(sender_id,),
                daemon=True
            ).start()

        if needs_esc:
            notify_escalate(sender_id, sender_name, text)

        if zalo_request:
            asked_zalo.add(sender_id)
            print(f"[ZALO] {sender_id} requested Zalo contact")

        bot_sending.add(sender_id)
        time.sleep(5)
        if is_human_handling(sender_id):
            bot_sending.discard(sender_id)
            return

        # Gửi ảnh thảm nếu reply có link sản phẩm thảm — không gửi text reply
        if cat == "tham":
            all_rugs = {p["url"]: p for p in fetch_products_by_category("tham")}
            found_urls = re.findall(r'https://annacasavn\.com/tham[^\s\)\"]+', clean_reply)
            matched = [all_rugs[u] for u in found_urls[:3] if u in all_rugs and all_rugs[u].get("img")]
            if matched:
                user_pending_products[sender_id] = matched
                for i, prod in enumerate(matched, 1):
                    raw_material = prod.get("material", "")
                    material_desc = get_material_benefit(raw_material) if raw_material else get_material_info(prod["name"])
                    origin = prod.get("origin", "")
                    label = f"Mẫu {i}: {prod['name']}"
                    if material_desc:
                        mat_line = f"Dạ mẫu này {material_desc}"
                        if origin:
                            mat_line += f", nhập khẩu từ {origin}"
                        label += f"\n{mat_line} ạ"
                    time.sleep(1)
                    send_text(sender_id, label)
                    time.sleep(1)
                    send_image(sender_id, prod["img"])
                time.sleep(1)
                send_text(sender_id, f"Dạ {pronoun} thích mẫu nào ạ?")
                # Bỏ qua text reply từ Claude khi đã gửi ảnh
            else:
                # Không có ảnh → gửi text bình thường
                if is_first:
                    parts = re.split(r'(?<=nha\.)\s+|(?<=nha,)\s+', clean_reply, maxsplit=1)
                    if len(parts) == 2:
                        send_text(sender_id, parts[0].strip())
                        time.sleep(1)
                        send_text(sender_id, parts[1].strip())
                    else:
                        send_text(sender_id, clean_reply)
                else:
                    send_text(sender_id, clean_reply)
        else:
            # Tin đầu tiên → tách câu chào thành tin riêng
            if is_first:
                parts = re.split(r'(?<=nha\.)\s+|(?<=nha,)\s+', clean_reply, maxsplit=1)
                if len(parts) == 2:
                    send_text(sender_id, parts[0].strip())
                    time.sleep(1)
                    send_text(sender_id, parts[1].strip())
                else:
                    send_text(sender_id, clean_reply)
            else:
                send_text(sender_id, clean_reply)

        # Gửi confirm appointment nếu Claude detect khách đồng ý ngay trong reply
        if appointment_flag:
            time.sleep(1)
            for msg_template in APPOINTMENT_CONFIRM:
                msg = msg_template.format(
                    pronoun=pronoun,
                    pronoun_cap=pronoun.capitalize(),
                    address=SHOWROOM_ADDRESS,
                    hours=SHOWROOM_HOURS,
                    hotline=SHOWROOM_HOTLINE
                )
                send_text(sender_id, msg)
                time.sleep(1)
        if send_cat_wp:
            time.sleep(1)
            send_text(sender_id, "Dạ em gửi catalog giấy dán tường đang sale ạ.")
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_1"])
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_2"])

        time.sleep(35)
        bot_sending.discard(sender_id)

    except Exception as e:
        print(f"process_message error: {e}")
        bot_sending.discard(sender_id)


# ── PROCESS IMAGE MESSAGE ─────────────────────────────────────────────────────
def process_image(sender_id, image_url, caption=""):
    try:
        sender_name = get_sender_name(sender_id)
        human_names[sender_id] = sender_name
        first_name = sender_name.split()[-1] if sender_name else ""

        notification_feed.appendleft({
            "name": sender_name or "Khách",
            "sender_id": sender_id,
            "text": "[Gửi hình]",
            "time": int(time.time())
        })

        # Tin đầu tiên với ảnh — chào trước
        is_first = sender_id not in greeted_users
        if is_first:
            greeted_users.add(sender_id)
            greeting = GREETING_TEMPLATE.format(name=first_name)
            time.sleep(5)
            if is_human_handling(sender_id): return
            bot_sending.add(sender_id)
            send_text(sender_id, greeting)
            time.sleep(1)
            bot_sending.discard(sender_id)

        # Download ảnh
        img_b64 = download_image_as_base64(image_url)
        if not img_b64:
            send_text(sender_id, "Dạ em không xem được hình, anh chị gửi lại thử nha.")
            return

        products = fetch_rug_products()
        product_data = format_products_for_claude(products)
        system = SYSTEM_BASE.format(product_data=product_data)

        # Gọi Claude với ảnh
        vision_message = {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": img_b64
                    }
                },
                {
                    "type": "text",
                    "text": f"Đây là ảnh tĩnh khách gửi (có thể là screenshot từ Reels/video — bỏ qua mọi UI overlay như nút play, thanh điều hướng, giao diện app). Tập trung phân tích NỘI DUNG thật sự trong ảnh: phòng ốc, sản phẩm nội thất, màu sắc, họa tiết.{f' Khách nhắn kèm: \"{caption}\".' if caption else ''} Nếu khách hỏi về sản phẩm không phải thảm (sofa, bàn, đèn...) → [ESCALATE] + 'Dạ sản phẩm này em sẽ nhờ chuyên viên hỗ trợ anh chị thêm ạ'. Nếu thấy thảm hoặc khách hỏi về thảm → phân tích màu sắc và họa tiết, tìm mẫu tương tự và gợi ý."
                }
            ]
        }

        history = fetch_fb_conversation(sender_id) + [vision_message]

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=history,
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"}
        )

        reply = response.content[0].text
        clean_reply = reply.replace("[ESCALATE]", "").replace("[SKIP]", "").strip()

        # Lưu vào history dạng text
        save_message(sender_id, "user", "[Khách gửi hình]")
        save_message(sender_id, "assistant", clean_reply)

        time.sleep(3)
        if is_human_handling(sender_id): return

        bot_sending.add(sender_id)
        send_text(sender_id, clean_reply)
        time.sleep(10)
        bot_sending.discard(sender_id)

    except Exception as e:
        print(f"process_image error: {e}")
        bot_sending.discard(sender_id)


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

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id  = event.get("sender", {}).get("id")
            message    = event.get("message", {})
            text       = message.get("text", "")
            message_id = message.get("mid", "")
            is_echo    = message.get("is_echo", False)
            attachments = message.get("attachments", [])

            if not sender_id:
                continue

            # ── REF TRACKING (Click-to-Messenger ad) ─────────────────────────
            # Lấy ref từ postback.payload (mỗi ad set payload khác nhau)
            postback = event.get("postback", {})
            postback_payload = postback.get("payload", "").strip()
            # Fallback: thử referral object nếu có
            referral = event.get("referral") or postback.get("referral") or {}
            ref = referral.get("ref", "").strip() or postback_payload

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
                # Bỏ qua mọi echo trong 60s đầu sau server restart (tránh HANDOFF giả từ echo cũ)
                if _time.time() - SERVER_START_TIME < 60:
                    continue
                # Bỏ qua echo của messages bot đã gửi (kể cả echo trễ/trùng)
                if message_id and message_id in bot_sent_mids:
                    continue
                # Bỏ qua Facebook automated messages (Instant Reply, away message...)
                _echo_text = message.get("text", "")
                _et = _echo_text.lower()
                _AUTOMATED_FRAGMENTS = (
                    "cảm ơn anh chị đã nhắn tin",
                    "cam on anh chi da nhan tin",
                    "chuyên viên anna casa sẽ phản hồi",
                    "chuyen vien anna casa se phan hoi",
                    "anna casa chuyên nội thất nhập khẩu",
                    "anna casa chuyen noi that nhap khau",
                    "chúng tôi có thể giúp gì cho bạn",
                    "chung toi co the giup gi cho ban",
                    "chúng tôi có thể hỗ trợ gì cho bạn",
                    "chung toi co the ho tro gi cho ban",
                )
                if any(f in _et for f in _AUTOMATED_FRAGMENTS):
                    continue
                customer_id = event.get("recipient", {}).get("id")
                if not customer_id: continue
                if customer_id in bot_sending: continue
                human_mode.add(customer_id)
                print(f"[HANDOFF] Sales replied for {customer_id}")
                continue

            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            if not bot_enabled:
                continue

            if is_human_handling(sender_id):
                continue

            if text and is_lead_form(text):
                print(f"[SKIP] Lead form message from {sender_id}")
                continue

            if is_requesting_zalo(text):
                asked_zalo.add(sender_id)
                print(f"[ZALO] Customer requested Zalo for {sender_id}")
                continue

            if sender_id in asked_zalo:
                print(f"[SKIP] Customer already requested Zalo for {sender_id}")
                continue

            # Xử lý ảnh / video / share
            if attachments:
                has_image = False
                for att in attachments:
                    if att.get("type") == "image":
                        has_image = True
                        image_url = att.get("payload", {}).get("url", "")
                        if image_url and not is_asking_similar(text):
                            threading.Thread(
                                target=process_image,
                                args=(sender_id, image_url, text or ""),
                                daemon=True
                            ).start()
                if not has_image:
                    # Video, Reel, sticker, share — không phân tích được
                    threading.Thread(
                        target=process_message,
                        args=(sender_id, text or "[Khách gửi video/Reel/file — không xem được nội dung. Hỏi khách muốn tư vấn sản phẩm gì hoặc nhờ gửi ảnh tĩnh.]"),
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
    human_list = [{"id": sid, "name": human_names.get(sid, sid)} for sid in human_mode]
    return jsonify({"bot_enabled": bot_enabled, "human_mode": human_list})


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    global bot_enabled
    bot_enabled = not bot_enabled
    print(f"[TOGGLE] Bot {'enabled' if bot_enabled else 'disabled'}")
    return jsonify({"bot_enabled": bot_enabled})


@app.route("/api/reactivate", methods=["POST"])
def api_reactivate():
    data = request.get_json()
    cid = data.get("customer_id", "").strip()
    if cid:
        human_mode.discard(cid)
        asked_zalo.discard(cid)
        invite_sent.discard(cid)
        appointment_done.discard(cid)
    return jsonify({"ok": True})


# ── SERVE WEB ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
