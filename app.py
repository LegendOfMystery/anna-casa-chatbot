import re
"""
ANNA CASA AI CHATBOT
Stack: Python + Flask + Claude API + Meta Webhook
"""

import os
import re
import json
import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic

app = Flask(__name__)

# ── CONFIG ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
ESCALATE_NOTIFY_URL = os.environ.get("ESCALATE_NOTIFY_URL", "")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── LOAD PROMPTS ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(BASE_DIR, "system_prompt.md"), "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open(os.path.join(BASE_DIR, "product_knowledge.md"), "r", encoding="utf-8") as f:
    PRODUCT_KNOWLEDGE = f.read()

FULL_SYSTEM = f"{SYSTEM_PROMPT}\n\n---\n\n{PRODUCT_KNOWLEDGE}"

# ── IN-MEMORY STORE ──────────────────────────────────────────────────────────
conversations: dict[str, list] = {}
processed_messages: set = set()
human_mode: set = set()  # sender_ids đang được human handle


def is_human_handling(sender_id: str) -> bool:
    return sender_id in human_mode


def get_history(sender_id: str) -> list:
    return conversations.get(sender_id, [])


def save_message(sender_id: str, role: str, content: str):
    if sender_id not in conversations:
        conversations[sender_id] = []
    conversations[sender_id].append({"role": role, "content": content})
    if len(conversations[sender_id]) > 20:
        conversations[sender_id] = conversations[sender_id][-20:]


# ── AI REPLY ─────────────────────────────────────────────────────────────────
def get_ai_reply(sender_id: str, user_message: str, sender_name: str = "") -> str:
    save_message(sender_id, "user", user_message)
    history = get_history(sender_id)

    context_note = f"\n[Context: Tên khách là {sender_name}]" if sender_name else ""
    messages_with_context = history.copy()
    if context_note and messages_with_context:
        messages_with_context[0] = {
            "role": messages_with_context[0]["role"],
            "content": context_note + "\n" + messages_with_context[0]["content"]
        }

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=FULL_SYSTEM,
        messages=messages_with_context
    )

    reply = response.content[0].text
    save_message(sender_id, "assistant", reply)
    return reply


# ── ESCALATE ─────────────────────────────────────────────────────────────────
def notify_human(sender_id: str, sender_name: str, message: str, ai_reply: str):
    if not ESCALATE_NOTIFY_URL:
        print(f"[ESCALATE] {sender_name} ({sender_id}): {message}")
        return
    payload = {
        "text": f"🔔 CẦN HỖ TRỢ\n"
                f"Khách: {sender_name}\n"
                f"ID: {sender_id}\n"
                f"Tin nhắn: {message}\n"
                f"AI reply: {ai_reply}"
    }
    try:
        requests.post(ESCALATE_NOTIFY_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Escalate notify failed: {e}")


# ── HÌNH ẢNH SẢN PHẨM ───────────────────────────────────────────────────────
PRODUCT_CARDS = {
    "siroc": "https://res.cloudinary.com/dxihfwscx/image/upload/v1775103698/SirocProductCard_opax1p.jpg",
}

REAL_PHOTOS = {
    "siroc": [
        "https://res.cloudinary.com/dxihfwscx/image/upload/v1775104078/anh_that_Siroc_4_gyxnoo.jpg",
        "https://res.cloudinary.com/dxihfwscx/image/upload/v1775104078/anh_that_Siroc_o9ep1e.jpg",
        "https://res.cloudinary.com/dxihfwscx/image/upload/v1775104078/anh_that_Siroc_5_oljal9.jpg",
        "https://res.cloudinary.com/dxihfwscx/image/upload/v1775104078/anh_that_Siroc_2_cjmpvx.jpg",
        "https://res.cloudinary.com/dxihfwscx/image/upload/v1775104078/anh_that_Siroc_3_xprqjp.jpg",
    ]
}

SIROC_KEYWORDS = ["siroc", "thảm siroc", "thảm bỉ", "thảm chevron"]
NO_ZALO_KEYWORDS = ["không dùng zalo", "ko dùng zalo", "không có zalo", "ko có zalo",
                    "tư vấn qua đây", "nhắn đây đi", "inbox đây đi", "qua đây đi em",
                    "không zalo", "ko zalo", "chat đây", "messenger đây",
                    "a ko dùng", "anh ko dùng", "không xài zalo", "ko xài zalo"]

REQUEST_PHOTO_KEYWORDS = [
    "gửi hình", "gửi ảnh", "cho xem hình", "cho anh hình",
    "cho chị hình", "hình thực tế", "ảnh thực tế",
    "xem hình", "xem ảnh", "hình đi", "ảnh đi",
    "hình thật", "ảnh thật", "show hình", "show ảnh",
    "hình thực", "ảnh thực", "hình chụp", "ảnh chụp",
    "có hình không", "có ảnh không"
]


def should_send_product_card(text: str, conversation_history: list) -> str | None:
    """Trả về tên sản phẩm nếu cần gửi product card, None nếu không"""
    text_lower = text.lower()
    # Chỉ gửi product card lần đầu khách hỏi — kiểm tra history chưa có card
    history_text = " ".join([m.get("content", "") for m in conversation_history]).lower()
    if any(k in text_lower for k in SIROC_KEYWORDS):
        if "product_card_sent_siroc" not in history_text:
            return "siroc"
    return None


def should_send_real_photos(text: str) -> str | None:
    """Trả về tên sản phẩm nếu cần gửi hình thực tế, None nếu không"""
    text_lower = text.lower()
    if any(k in text_lower for k in NO_ZALO_KEYWORDS):
        return "siroc"
    if any(k in text_lower for k in REQUEST_PHOTO_KEYWORDS):
        return "siroc"
    return None


# ── SEND MESSAGE ─────────────────────────────────────────────────────────────
def send_raw_message(recipient_id: str, text: str):
    """Gửi 1 tin nhắn text đơn giản"""
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": text}
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Send message failed: {e}")


def send_message(recipient_id: str, message_text: str):
    """Gửi tin nhắn — tự động tách link thành tin nhắn riêng"""
    clean_message = message_text.replace("[ESCALATE]", "").strip()

    url_pattern = r'https?://\S+'
    urls = re.findall(url_pattern, clean_message)

    if urls:
        # Tách text thành các phần: trước link, link, sau link
        parts = re.split(url_pattern, clean_message)
        
        # Gửi phần text trước link (nếu có)
        before = parts[0].strip().rstrip('—').strip()
        if before:
            send_raw_message(recipient_id, before)
        
        # Gửi từng link riêng
        for url in urls:
            send_raw_message(recipient_id, url)
        
        # Gửi phần text sau link (nếu có)
        if len(parts) > 1:
            after = parts[-1].strip().lstrip('—').strip()
            if after:
                send_raw_message(recipient_id, after)
    else:
        send_raw_message(recipient_id, clean_message)


def send_image(recipient_id: str, image_url: str):
    """Gửi hình ảnh cho khách"""
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {
                    "url": image_url,
                    "is_reusable": True
                }
            }
        }
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Send image failed: {e}")


# ── HELPER: LẤY TÊN KHÁCH ────────────────────────────────────────────────────
def get_sender_name(sender_id: str) -> str:
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name&access_token={META_PAGE_TOKEN}"
        res = requests.get(url, timeout=5)
        return res.json().get("name", "")
    except:
        return ""


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

            # Nếu sales reply trong inbox → tự động dừng bot cho khách đó
            # Chỉ dừng khi echo KHÔNG phải từ chính bot (app_id khác)
            if is_echo:
                # Echo từ chính bot sẽ có source.type = "NON_HUMAN" hoặc không có source
                source = event.get("message", {}).get("source", {})
                source_type = source.get("type", "")
                # Nếu source type là HUMAN thì mới là sales reply thật
                if source_type == "HUMAN":
                    customer_id = event.get("recipient", {}).get("id")
                    if customer_id:
                        human_mode.add(customer_id)
                        print(f"[HANDOFF] Bot paused for customer {customer_id}")
                continue

            # Deduplication
            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            # Nếu đang human handle thì bỏ qua
            if is_human_handling(sender_id):
                continue

            # Lấy tên khách
            sender_name = get_sender_name(sender_id)

            # Kiểm tra có cần gửi product card không
            product_card = should_send_product_card(text, get_history(sender_id))

            # Kiểm tra có cần gửi hình thực tế không (khách từ chối Zalo)
            real_photo_product = should_send_real_photos(text)

            # Lấy AI reply
            ai_reply = get_ai_reply(sender_id, text, sender_name)

            # Escalate nếu cần
            if "[ESCALATE]" in ai_reply:
                notify_human(sender_id, sender_name, text, ai_reply)

            # Gửi product card trước nếu cần
            if product_card and product_card in PRODUCT_CARDS:
                send_image(sender_id, PRODUCT_CARDS[product_card])
                # Đánh dấu đã gửi để không gửi lại
                save_message(sender_id, "assistant", f"[product_card_sent_{product_card}]")

            # Gửi text reply
            send_message(sender_id, ai_reply)

            # Gửi hình thực tế sau text nếu khách từ chối Zalo
            if real_photo_product and real_photo_product in REAL_PHOTOS:
                for photo_url in REAL_PHOTOS[real_photo_product]:
                    send_image(sender_id, photo_url)

    return jsonify({"status": "ok"}), 200


# ── TAKEOVER CONTROL PAGE ─────────────────────────────────────────────────────
@app.route("/takeover", methods=["GET", "POST"])
def takeover():
    """Trang để sales bật lại bot sau khi xử lý xong"""
    if request.method == "POST":
        action = request.form.get("action")
        cid = request.form.get("customer_id", "").strip()
        if action == "bot" and cid:
            human_mode.discard(cid)
            return f"✅ Bot đã bật lại cho khách {cid}"
        return "❌ Không hợp lệ"

    active = "<br>".join(human_mode) if human_mode else "Không có"
    return f"""
    <h2>Anna Casa — Bật lại Bot</h2>
    <p>Bot tự động dừng khi sales reply. Dùng trang này để bật lại bot cho khách.</p>
    <form method=POST>
      Customer ID: <input name=customer_id size=40 placeholder="Paste ID khách vào đây"><br><br>
      <button name=action value=bot style="padding:8px 16px">▶ Bật lại Bot</button>
    </form>
    <br><b>Đang ở chế độ Human (bot đang dừng):</b><br>{active}
    """


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
