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
from flask import Flask, request, jsonify, send_from_directory
from anthropic import Anthropic
from collections import deque

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_API_KEY      = os.environ["GOOGLE_API_KEY"]
SHEET_ID            = os.environ["SHEET_ID"]
ESCALATE_NOTIFY_URL = os.environ.get("ESCALATE_NOTIFY_URL", "")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── IN-MEMORY STORE ───────────────────────────────────────────────────────────
processed_messages: set = set()
bot_sending: set = set()
human_mode: set = set()
human_names: dict[str, str] = {}
greeted_users: set = set()
conversations: dict[str, list] = {}
notification_feed = deque(maxlen=100)
bot_enabled = True
asked_zalo: set = set()  # Đã hỏi Zalo → dừng reply

# ── LEAD TRACKING & APPOINTMENT ───────────────────────────────────────────────
ref_store:        dict[str, str] = {}   # psid -> ref code từ ad
invite_sent:      set = set()           # psid đã được mời ghé showroom
appointment_done: set = set()           # psid đã confirm ghé showroom

SHOWROOM_ADDRESS = "12 Nguyễn Ư Dĩ, phường An Khánh, TP.HCM"
SHOWROOM_HOURS   = "10:00 sáng đến 7:00 tối, tất cả các ngày trong tuần"
SHOWROOM_HOTLINE = "+84 909 072 820"
LEAD_SHEET_NAME  = "Lead Register"

APPOINTMENT_CONFIRM = [
    "Dạ bên em rất vui được đón {pronoun} ạ 🙏",
    "📍 Showroom Anna Casa: {address}\n🕙 {hours}\n📞 Hotline: {hotline}",
    "{pronoun_cap} cứ ghé bất cứ lúc nào thuận tiện nhé, bên em luôn có người tư vấn trực tiếp ạ."
]

APPOINTMENT_POSITIVE = [
    r"\bcó\b", r"\bokay\b", r"\bok\b", r"\bđược\b", r"\bmuốn\b",
    r"\bghé\b", r"\bđến\b", r"\bxem\b", r"\bthăm\b", r"\bvô\b",
    r"\bvào\b", r"\bsẽ ghé\b", r"\bsẽ đến\b", r"\bnhé\b",
    r"\byes\b", r"\bsure\b", r"\bthích\b",
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

def log_lead_to_sheet(psid: str, ref_code: str, phone: str = ""):
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    row = [timestamp, psid, phone, ref_code, "new", "", ""]
    url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
        f"/values/{LEAD_SHEET_NAME}!A:G:append"
        f"?valueInputOption=USER_ENTERED&key={GOOGLE_API_KEY}"
    )
    try:
        requests.post(url, json={"values": [row]}, timeout=5)
        print(f"[LEAD] {ref_code} | {psid}")
    except Exception as e:
        print(f"[LEAD ERROR] {e}")

def log_appointment_to_sheet(psid: str):
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    read_url = (
        f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
        f"/values/{LEAD_SHEET_NAME}!A:H?key={GOOGLE_API_KEY}"
    )
    try:
        resp = requests.get(read_url, timeout=5)
        rows = resp.json().get("values", [])
        for i, row in enumerate(rows):
            if len(row) > 1 and row[1] == psid:
                row_num = i + 1
                update_url = (
                    f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}"
                    f"/values/{LEAD_SHEET_NAME}!H{row_num}"
                    f"?valueInputOption=USER_ENTERED&key={GOOGLE_API_KEY}"
                )
                requests.put(update_url, json={"values": [[f"booked {timestamp}"]]}, timeout=5)
                print(f"[APPOINTMENT] Booked for {psid}")
                return
        # Không tìm thấy PSID — ghi row mới
        log_lead_to_sheet(psid=psid, ref_code=ref_store.get(psid, "organic"))
    except Exception as e:
        print(f"[APPOINTMENT ERROR] {e}")


def is_human_handling(sender_id): return sender_id in human_mode
def get_history(sender_id): return conversations.get(sender_id, [])

def save_message(sender_id, role, content):
    if sender_id not in conversations:
        conversations[sender_id] = []
    conversations[sender_id].append({"role": role, "content": content})
    if len(conversations[sender_id]) > 20:
        conversations[sender_id] = conversations[sender_id][-20:]


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


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
sheet_cache = {"data": [], "last_updated": 0}
CACHE_TTL = 300

def fetch_rug_products() -> list[dict]:
    now = time.time()
    if now - sheet_cache["last_updated"] < CACHE_TTL and sheet_cache["data"]:
        return sheet_cache["data"]
    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/A:K?key={GOOGLE_API_KEY}"
        res = requests.get(url, timeout=10)
        rows = res.json().get("values", [])
        if not rows:
            return sheet_cache["data"]
        headers = rows[0]
        products = []
        for row in rows[1:]:
            row_padded = row + [""] * (len(headers) - len(row))
            p = dict(zip(headers, row_padded))
            if "thảm" in str(p.get("Danh mục", "")).lower() or \
               "thảm" in str(p.get("Tên sản phẩm", "")).lower():
                products.append(p)
        sheet_cache["data"] = products
        sheet_cache["last_updated"] = now
        print(f"[SHEETS] Loaded {len(products)} rug products")
        return products
    except Exception as e:
        print(f"[SHEETS] Error: {e}")
        return sheet_cache["data"]


def format_products_for_claude(products: list[dict]) -> str:
    if not products:
        return "Không có dữ liệu sản phẩm."
    lines = []
    for p in products:
        line = (f"- {p.get('Tên sản phẩm','')} | Danh mục: {p.get('Danh mục','')} | "
                f"Giá: {p.get('Giá','')} | Kích thước: {p.get('Kích thước','')} | "
                f"Chất liệu: {p.get('Chất liệu','')} | Màu/Họa tiết: {p.get('Màu / Họa tiết','')} | "
                f"Xuất xứ: {p.get('Xuất xứ','')} | Bảo hành: {p.get('Bảo hành','')} | "
                f"Link: {p.get('Link sản phẩm','')}")
        lines.append(line)
    return "\n".join(lines)


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
SYSTEM_BASE = """Bạn là Trâm, chuyên viên tư vấn tại Anna Casa Vietnam — thương hiệu nội thất Quiet Luxury.

NHIỆM VỤ: Tư vấn về thảm. Đọc toàn bộ lịch sử cuộc trò chuyện để hiểu context trước khi reply.

KHI NÀO REPLY:
- Khách hỏi bất cứ gì liên quan đến thảm → reply
- Khách đang trong cuộc trò chuyện về thảm và hỏi thêm (kể cả không nhắc từ "thảm") → reply
- Khách gửi hình → phân tích và gợi ý thảm tương tự
- Khách hỏi về sản phẩm khác mà chưa từng hỏi thảm → trả về [SKIP]
- Tin nhắn chào hỏi chung chung không rõ ý định → trả về [SKIP]

THÔNG TIN SHOWROOM:
- Địa chỉ: 12 Nguyễn Ư Dĩ, Thảo Điền, Q2, TP.HCM
- Giờ mở cửa: 10h sáng đến 7h tối
- Ship toàn quốc

CÁCH TRẢ LỜI:
- Mỗi tin nhắn CHỈ 1-2 câu ngắn, như nhắn tin thật
- Xưng "em", gọi khách "anh chị"
- Không dùng emoji, không bullet point, không dấu gạch ngang dài
- Không hỏi lại những gì khách đã nói rõ
- Cuối tin CHỈ hỏi đúng 1 câu — KHÔNG BAO GIỜ hỏi 2 câu trong 1 tin
- Khi khách hỏi giá: báo thẳng từ dữ liệu sản phẩm
- Khi gợi ý sản phẩm: kèm link sản phẩm luôn
- Không dùng dấu "/" ở bất kỳ đâu

THỨ TỰ HỎI KHI TƯ VẤN — hỏi từng câu một theo thứ tự:
1. Hỏi kích thước (nếu chưa biết): "Anh chị cần kích thước thảm như nào ạ? Bên em phổ biến dòng 1m6x2m3 và 2mx2m9."
2. Khi khách chọn size 1m6x2m3 → chỉ reply đúng 1 câu "Dạ size 1m6x2m3 bên em có ạ." rồi thêm [CATALOGUE_1M6] — không nói thêm gì nữa, hệ thống tự xử lý
3. Khi khách chọn size 2mx2m9 → chỉ reply đúng 1 câu "Dạ size 2mx2m9 bên em có ạ." rồi thêm [CATALOGUE_2MX] — không nói thêm gì nữa, hệ thống tự xử lý
4. KHÔNG hỏi màu sắc, không hỏi Zalo, không nói gì thêm sau khi chọn size — hệ thống tự gửi PDF và hỏi Zalo

KHI GỬI CATALOGUE:
- Khách hỏi size 1m6x2m3 hoặc chọn size đó → thêm [CATALOGUE_1M6] vào cuối reply
- Khách hỏi size 2mx2m9 hoặc chọn size đó → thêm [CATALOGUE_2MX] vào cuối reply
- Khách nhắn "nhận catalogue giấy dán tường" hoặc tương tự → thêm [CATALOGUE_WP] vào cuối reply
- Chỉ gửi 1 lần per loại, không gửi lại nếu đã gửi rồi

KHI KHÁCH GỬI HÌNH VÀ HỎI CÓ MẪU TƯƠNG TỰ KHÔNG:
- Nếu khách hỏi "có mẫu giống này không", "mẫu tương tự", "mẫu như này" → trả về [SKIP]
- Không phân tích ảnh, không gợi ý sản phẩm tương tự trong trường hợp này
- Phân tích màu sắc và họa tiết trong ảnh
- Tìm trong dữ liệu sản phẩm những mẫu thảm có màu và họa tiết tương tự
- Gợi ý 1-2 sản phẩm gần nhất kèm link
- Nếu không có mẫu tương tự: "Dạ mẫu này bên em chưa có, anh chị cho em biết kích thước cần dùng để em tư vấn mẫu gần nhất nha."

KHI NÀO ESCALATE:
Nếu khách để lại số điện thoại, yêu cầu hoàn tiền, hủy đơn, hoặc giảm giá:
- Reply: "Dạ để em chuyển cho bộ phận phụ trách hỗ trợ anh chị ngay ạ."
- Thêm [ESCALATE] vào cuối (không hiện cho khách)

MỜI GHÉ SHOWROOM:
- Sau khi tư vấn đủ (đã trả lời ít nhất 2-3 câu hỏi về thảm, hoặc khách đã hỏi giá, kích thước, chất liệu), mời khách ghé showroom một lần.
- Câu mời mẫu: "Anh chị có muốn ghé showroom bên em xem trực tiếp không ạ? Nhìn thảm ngoài đời đẹp hơn ảnh nhiều ạ."
- Chỉ hỏi MỘT LẦN. Không hỏi lại nếu khách chưa trả lời hoặc đổi chủ đề.
- Sau khi hỏi, thêm [INVITE_SENT] vào cuối reply (không hiện cho khách).
- Nếu khách đồng ý ghé: thêm [APPOINTMENT] vào cuối reply, KHÔNG tự viết địa chỉ hay giờ mở cửa.

TUYỆT ĐỐI KHÔNG:
- Bịa thông tin không có trong dữ liệu sản phẩm
- Tư vấn sản phẩm không phải thảm
- Hỏi lại những gì khách đã nói
- Dùng dấu "/" ở bất kỳ đâu

Dữ liệu sản phẩm thảm hiện có:
{product_data}"""

GREETING_TEMPLATE = "Anna Casa xin chào anh chị {name}, em là Trâm sẽ hỗ trợ mình nha."
GREETING_FIRST_Q  = "Anh chị cần kích thước thảm như nào ạ? Bên em phổ biến dòng 1m6x2m3 và 2mx2m9."


# ── SEND HELPERS ──────────────────────────────────────────────────────────────
def send_text(recipient_id, text):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
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

SIMILAR_PATTERN_KEYWORDS = [
    "mẫu giống", "mẫu tương tự", "mẫu như này", "mẫu như vậy",
    "có giống không", "có không em", "giống cái này", "tương tự không"
]

def is_asking_similar(text: str) -> bool:
    text_lower = text.lower()
    return any(k in text_lower for k in SIMILAR_PATTERN_KEYWORDS)

def detect_gender(full_name: str) -> str:
    """Trả về 'anh', 'chị', hoặc 'anh chị' nếu không xác định được."""
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
                       "nhận catalogue", "nhận catalog"]
        if any(t in text.lower() for t in WP_TRIGGERS):
            time.sleep(5)
            if is_human_handling(sender_id): return
            bot_sending.add(sender_id)
            send_text(sender_id, "Dạ em gửi catalog giấy dán tường đang sale ạ.")
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_1"])
            time.sleep(1)
            send_file(sender_id, CATALOGUES["wallpaper_2"])
            time.sleep(1)
            send_text(sender_id, f"Dạ {pronoun} cho em Zalo để em tư vấn thêm ạ.")
            asked_zalo.add(sender_id)
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

        # Claude xử lý tất cả — kể cả tin đầu tiên
        products = fetch_rug_products()
        product_data = format_products_for_claude(products)
        system = SYSTEM_BASE.format(product_data=product_data)
        system += f"\n\nGọi khách là '{pronoun}' (không dùng 'anh chị' nếu đã biết giới tính)."

        if is_first:
            greeting = f"Anna Casa xin chào {pronoun} {first_name}, em là Trâm sẽ hỗ trợ mình nha."
            system += f"\n\nĐây là tin nhắn ĐẦU TIÊN. Nếu khách hỏi về thảm: bắt đầu bằng '{greeting}' rồi xuống dòng hỏi '{GREETING_FIRST_Q}'. Nếu không liên quan đến thảm: trả về [SKIP]."

        save_message(sender_id, "user", text)
        history = get_history(sender_id)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system,
            messages=history
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

        needs_esc       = "[ESCALATE]" in reply
        send_cat_1m6    = "[CATALOGUE_1M6]" in reply
        send_cat_2mx    = "[CATALOGUE_2MX]" in reply
        send_cat_wp     = "[CATALOGUE_WP]" in reply
        invite_flag     = "[INVITE_SENT]" in reply
        appointment_flag = "[APPOINTMENT]" in reply
        clean_reply = (reply
            .replace("[ESCALATE]", "")
            .replace("[SKIP]", "")
            .replace("[CATALOGUE_1M6]", "")
            .replace("[CATALOGUE_2MX]", "")
            .replace("[CATALOGUE_WP]", "")
            .replace("[INVITE_SENT]", "")
            .replace("[APPOINTMENT]", "")
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

        time.sleep(5)
        if is_human_handling(sender_id): return

        bot_sending.add(sender_id)

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

        # Gửi catalogue PDF nếu có
        if send_cat_1m6:
            time.sleep(1)
            send_text(sender_id, "Dạ em gửi catalog thảm ạ.")
            time.sleep(1)
            send_file(sender_id, CATALOGUES["1m6x2m3"])
            time.sleep(1)
            send_text(sender_id, f"Dạ {pronoun} cho em Zalo để em gửi mẫu ạ.")
            asked_zalo.add(sender_id)
        if send_cat_2mx:
            time.sleep(1)
            send_text(sender_id, "Dạ em gửi catalog thảm ạ.")
            time.sleep(1)
            send_file(sender_id, CATALOGUES["2mx2m9"])
            time.sleep(1)
            send_text(sender_id, f"Dạ {pronoun} cho em Zalo để em gửi mẫu ạ.")
            asked_zalo.add(sender_id)

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
            time.sleep(1)
            send_text(sender_id, f"Dạ {pronoun} cho em Zalo để em tư vấn thêm ạ.")
            asked_zalo.add(sender_id)

        time.sleep(10)
        bot_sending.discard(sender_id)

    except Exception as e:
        print(f"process_message error: {e}")
        bot_sending.discard(sender_id)


# ── PROCESS IMAGE MESSAGE ─────────────────────────────────────────────────────
def process_image(sender_id, image_url):
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
                    "text": "Khách gửi hình này. Phân tích màu sắc và họa tiết, sau đó tìm trong dữ liệu sản phẩm những mẫu thảm tương tự và gợi ý cho khách."
                }
            ]
        }

        history = get_history(sender_id) + [vision_message]

        response = client.messages.create(
            model="claude-sonnet-4-20250514",  # Dùng Sonnet cho vision
            max_tokens=150,
            system=system,
            messages=history
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
                threading.Thread(
                    target=log_lead_to_sheet,
                    args=(sender_id, ref),
                    daemon=True
                ).start()
                print(f"[LEAD] Logged ref={ref} for {sender_id}")
            # ─────────────────────────────────────────────────────────────────

            if is_echo:
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

            if sender_id in asked_zalo:
                print(f"[SKIP] Already asked Zalo for {sender_id}")
                continue

            # Xử lý ảnh
            if attachments:
                for att in attachments:
                    if att.get("type") == "image":
                        image_url = att.get("payload", {}).get("url", "")
                        if image_url and not is_asking_similar(text):
                            threading.Thread(
                                target=process_image,
                                args=(sender_id, image_url),
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
        greeted_users.discard(cid)
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
