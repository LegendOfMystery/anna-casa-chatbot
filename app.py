"""
ANNA CASA AI CHATBOT
Stack: Python + Flask + Claude API + Google Sheets + Meta Webhook
Logic: Chỉ reply khi khách hỏi về thảm. Dừng khi sales reply.
"""

import os
import re
import time
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
human_names: dict[str, str] = {}  # sender_id -> name
greeted_users: set = set()
conversations: dict[str, list] = {}
notification_feed = deque(maxlen=100)

bot_enabled = True  # Global toggle — sales bật/tắt từ web


def is_human_handling(sender_id): return sender_id in human_mode
def get_history(sender_id): return conversations.get(sender_id, [])

def save_message(sender_id, role, content):
    if sender_id not in conversations:
        conversations[sender_id] = []
    conversations[sender_id].append({"role": role, "content": content})
    if len(conversations[sender_id]) > 20:
        conversations[sender_id] = conversations[sender_id][-20:]


# ── KEYWORDS ──────────────────────────────────────────────────────────────────
RUG_KEYWORDS = [
    "thảm", "tham", "carpet", "rug", "siroc", "thảm bỉ", "thảm len",
    "thảm tròn", "thảm vuông", "thảm phòng khách", "thảm phòng ngủ",
    "thảm trải sàn", "kích thước thảm", "giá thảm", "mua thảm",
    "thảm màu", "thảm họa tiết", "thảm bền", "chất liệu thảm"
]

ESCALATE_TRIGGERS = [
    r'\b0[0-9]{9}\b',           # phone number
    r'\b\+84[0-9]{9}\b',        # phone with +84
    r'hoàn tiền', r'hoàn trả',
    r'hủy đơn', r'huỷ đơn',
    r'khiếu nại', r'phàn nàn',
    r'giảm giá', r'discount',
]

def is_rug_question(text: str) -> bool:
    text_lower = text.lower()
    return any(k in text_lower for k in RUG_KEYWORDS)

def needs_escalate(text: str) -> bool:
    text_lower = text.lower()
    return any(re.search(p, text_lower) for p in ESCALATE_TRIGGERS)


# ── GOOGLE SHEETS ─────────────────────────────────────────────────────────────
sheet_cache = {"data": [], "last_updated": 0}
CACHE_TTL = 300  # 5 phút

def fetch_rug_products() -> list[dict]:
    now = time.time()
    if now - sheet_cache["last_updated"] < CACHE_TTL and sheet_cache["data"]:
        return sheet_cache["data"]

    try:
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/A:K?key={GOOGLE_API_KEY}"
        res = requests.get(url, timeout=10)
        data = res.json()
        rows = data.get("values", [])
        if not rows:
            return sheet_cache["data"]

        headers = rows[0]
        products = []
        for row in rows[1:]:
            row_padded = row + [""] * (len(headers) - len(row))
            p = dict(zip(headers, row_padded))
            # Chỉ lấy sản phẩm thuộc danh mục Thảm
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
        line = f"- {p.get('Tên sản phẩm','')} | Danh mục: {p.get('Danh mục','')} | Giá: {p.get('Giá','')} | Kích thước: {p.get('Kích thước','')} | Chất liệu: {p.get('Chất liệu','')} | Màu/Họa tiết: {p.get('Màu / Họa tiết','')} | Xuất xứ: {p.get('Xuất xứ','')} | Bảo hành: {p.get('Bảo hành','')} | Link: {p.get('Link sản phẩm','')}"
        lines.append(line)
    return "\n".join(lines)


# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
SYSTEM_BASE = """Bạn là Trâm, chuyên viên tư vấn tại Anna Casa Vietnam — thương hiệu nội thất Quiet Luxury.

NHIỆM VỤ: Chỉ tư vấn về thảm. Không tư vấn sản phẩm khác.

THÔNG TIN SHOWROOM:
- Địa chỉ: 12 Nguyễn Ư Dĩ, Thảo Điền, Q2, TP.HCM
- Giờ mở cửa: 10h sáng đến 7h tối
- Ship toàn quốc

CÁCH TRẢ LỜI:
- Mỗi tin nhắn CHỈ 1-2 câu ngắn, như nhắn tin thật
- Xưng "em", gọi khách "anh/chị"
- Không dùng emoji, không bullet point, không dấu gạch ngang dài
- Không hỏi lại những gì khách đã nói rõ
- Cuối tin chỉ hỏi tối đa 1 câu nếu cần thêm thông tin
- Khi khách hỏi size: tư vấn dựa trên diện tích phòng nếu biết
- Khi khách hỏi giá: báo thẳng từ dữ liệu sản phẩm
- Khi khách hỏi hình: "Dạ em gửi anh/chị xem thêm tại: [link sản phẩm]"

KHI NÀO ESCALATE:
Nếu khách để lại số điện thoại, yêu cầu hoàn tiền, hủy đơn, hoặc giảm giá:
- Reply: "Dạ để em chuyển cho bộ phận phụ trách hỗ trợ anh/chị ngay ạ."
- Thêm [ESCALATE] vào cuối (không hiện cho khách)

TUYỆT ĐỐI KHÔNG:
- Bịa thông tin không có trong dữ liệu sản phẩm
- Tư vấn sản phẩm không phải thảm
- Hỏi lại những gì khách đã nói
- Dùng dấu "/" trong câu trả lời — thay bằng "và" hoặc "hoặc"
- Viết "anh/chị" — luôn viết "anh chị" không có dấu gạch chéo

Dữ liệu sản phẩm thảm hiện có:
{product_data}"""


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


# ── ESCALATE ──────────────────────────────────────────────────────────────────
def notify_escalate(sender_id, sender_name, message):
    if not ESCALATE_NOTIFY_URL:
        print(f"[ESCALATE] {sender_name} ({sender_id}): {message}")
        return
    try:
        requests.post(ESCALATE_NOTIFY_URL, json={
            "text": f"⚠️ CẦN HỖ TRỢ\nKhách: {sender_name}\nID: {sender_id}\nTin: {message}"
        }, timeout=5)
    except Exception as e:
        print(f"Escalate failed: {e}")


# ── PROCESS MESSAGE ───────────────────────────────────────────────────────────
def process_message(sender_id, text):
    try:
        sender_name = get_sender_name(sender_id)
        human_names[sender_id] = sender_name
        first_name = sender_name.split()[-1] if sender_name else ""

        # Thêm vào notification feed
        notification_feed.appendleft({
            "name": sender_name or "Khách",
            "sender_id": sender_id,
            "text": text,
            "time": int(time.time())
        })

        # Chỉ xử lý nếu là câu hỏi về thảm
        if not is_rug_question(text):
            print(f"[SKIP] Not a rug question from {sender_id}: {text[:50]}")
            return

        # Escalate ngay nếu cần
        if needs_escalate(text):
            time.sleep(5)
            if is_human_handling(sender_id): return
            bot_sending.add(sender_id)
            send_text(sender_id, "Dạ để em chuyển cho bộ phận phụ trách hỗ trợ anh/chị ngay ạ.")
            notify_escalate(sender_id, sender_name, text)
            time.sleep(10)
            bot_sending.discard(sender_id)
            return

        # Fetch product data
        products = fetch_rug_products()
        product_data = format_products_for_claude(products)
        system = SYSTEM_BASE.format(product_data=product_data)

        # Lời chào lần đầu
        is_first = sender_id not in greeted_users
        if is_first:
            greeted_users.add(sender_id)
            greeting_note = f"\n\nĐây là tin nhắn ĐẦU TIÊN của khách. Bắt đầu bằng: 'Anna Casa xin chào anh chị {first_name}, em là Trâm sẽ hỗ trợ mình nha.' Sau đó trả lời câu hỏi của khách trong cùng 1 tin nhắn."
            system += greeting_note

        # Claude
        save_message(sender_id, "user", text)
        history = get_history(sender_id)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=history
        )

        reply = response.content[0].text
        needs_esc = "[ESCALATE]" in reply
        clean_reply = reply.replace("[ESCALATE]", "").strip()
        save_message(sender_id, "assistant", clean_reply)

        if needs_esc:
            notify_escalate(sender_id, sender_name, text)

        # Chờ 5s rồi reply
        time.sleep(5)
        if is_human_handling(sender_id):
            print(f"[HANDOFF] Cancelled for {sender_id}")
            return

        bot_sending.add(sender_id)

        # Nếu là tin đầu tiên → tách câu chào thành tin riêng
        if is_first:
            # Tìm câu chào (kết thúc bằng "nha." hoặc "nha,")
            parts = re.split(r'(?<=nha\.)\s+|(?<=nha,)\s+', clean_reply, maxsplit=1)
            if len(parts) == 2:
                send_text(sender_id, parts[0].strip())
                time.sleep(1)
                send_text(sender_id, parts[1].strip())
            else:
                send_text(sender_id, clean_reply)
        else:
            send_text(sender_id, clean_reply)

        time.sleep(10)
        bot_sending.discard(sender_id)

    except Exception as e:
        print(f"process_message error: {e}")
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

            if not sender_id or not text:
                continue

            if is_echo:
                customer_id = event.get("recipient", {}).get("id")
                if not customer_id: continue
                if customer_id in bot_sending:
                    continue
                human_mode.add(customer_id)
                print(f"[HANDOFF] Sales replied for {customer_id}")
                continue

            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            if not bot_enabled:
                print(f"[SKIP] Bot globally disabled")
                continue

            if is_human_handling(sender_id):
                print(f"[SKIP] Human mode for {sender_id}")
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
    global bot_enabled
    human_list = [
        {"id": sid, "name": human_names.get(sid, sid)}
        for sid in human_mode
    ]
    return jsonify({
        "bot_enabled": bot_enabled,
        "human_mode": human_list
    })


@app.route("/api/toggle", methods=["POST"])
def api_toggle():
    global bot_enabled
    bot_enabled = not bot_enabled
    print(f"[TOGGLE] Bot {'enabled' if bot_enabled else 'disabled'} globally")
    return jsonify({"bot_enabled": bot_enabled})


@app.route("/api/reactivate", methods=["POST"])
def api_reactivate():
    data = request.get_json()
    cid = data.get("customer_id", "").strip()
    if cid:
        human_mode.discard(cid)
        greeted_users.discard(cid)
        print(f"[REACTIVATE] Bot reactivated for {cid}")
    return jsonify({"ok": True})


# ── SERVE WEB ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
