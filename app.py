"""
ANNA CASA AI CHATBOT
Stack: Python + Flask + Claude API + Meta Webhook
"""

import os
import re
import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic

app = Flask(__name__)

# ── CONFIG ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
ESCALATE_NOTIFY_URL = os.environ.get("ESCALATE_NOTIFY_URL", "")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── LOAD PROMPTS ──────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

with open(os.path.join(BASE_DIR, "system_prompt.md"), "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open(os.path.join(BASE_DIR, "product_knowledge.md"), "r", encoding="utf-8") as f:
    PRODUCT_KNOWLEDGE = f.read()

FULL_SYSTEM = f"{SYSTEM_PROMPT}\n\n---\n\n{PRODUCT_KNOWLEDGE}"

# ── IN-MEMORY STORE ───────────────────────────────────────────────────────────
conversations: dict[str, list] = {}
processed_messages: set = set()
human_mode: set = set()


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


# ── AI REPLY ──────────────────────────────────────────────────────────────────
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


# ── ESCALATE ──────────────────────────────────────────────────────────────────
def notify_human(sender_id: str, sender_name: str, message: str, ai_reply: str):
    if not ESCALATE_NOTIFY_URL:
        print(f"[ESCALATE] {sender_name} ({sender_id}): {message}")
        return
    payload = {
        "text": f"CAN HO TRO\nKhach: {sender_name}\nID: {sender_id}\nTin nhan: {message}\nAI reply: {ai_reply}"
    }
    try:
        requests.post(ESCALATE_NOTIFY_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Escalate notify failed: {e}")


# ── HÌNH ẢNH SẢN PHẨM ────────────────────────────────────────────────────────
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

SIROC_KEYWORDS = ["siroc", "thảm siroc", "thảm bỉ"]

REQUEST_PHOTO_KEYWORDS = [
    "gửi hình", "gửi ảnh", "cho xem hình", "cho anh hình", "cho chị hình",
    "hình thực tế", "ảnh thực tế", "xem hình", "xem ảnh",
    "hình thật", "ảnh thật", "hình thực", "ảnh thực",
    "không dùng zalo", "ko dùng zalo", "không có zalo", "ko có zalo",
    "không zalo", "ko zalo", "tư vấn qua đây", "chat đây",
    "không xài zalo", "ko xài zalo"
]


def should_send_product_card(text: str, history: list) -> str | None:
    text_lower = text.lower()
    history_text = " ".join([m.get("content", "") for m in history]).lower()
    if any(k in text_lower for k in SIROC_KEYWORDS):
        if "product_card_sent_siroc" not in history_text:
            return "siroc"
    return None


def should_send_real_photos(text: str) -> str | None:
    text_lower = text.lower()
    if any(k in text_lower for k in REQUEST_PHOTO_KEYWORDS):
        return "siroc"
    return None


# ── SEND MESSAGES ─────────────────────────────────────────────────────────────
def send_raw_message(recipient_id: str, text: str):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        print(f"Send message failed: {e}")


def send_message(recipient_id: str, message_text: str):
    """Tách link thành tin nhắn riêng"""
    clean = message_text.replace("[ESCALATE]", "").strip()
    url_pattern = r'https?://\S+'
    urls = re.findall(url_pattern, clean)

    if urls:
        parts = re.split(url_pattern, clean)
        before = parts[0].strip().rstrip(',').strip()
        if before:
            send_raw_message(recipient_id, before)
        for u in urls:
            send_raw_message(recipient_id, u)
        if len(parts) > 1:
            after = parts[-1].strip().lstrip(',').strip()
            if after:
                send_raw_message(recipient_id, after)
    else:
        send_raw_message(recipient_id, clean)


def send_image(recipient_id: str, image_url: str):
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
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        print(f"Send image failed: {e}")


# ── HELPER ────────────────────────────────────────────────────────────────────
def get_sender_name(sender_id: str) -> str:
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name&access_token={META_PAGE_TOKEN}"
        return requests.get(url, timeout=5).json().get("name", "")
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

            # Echo từ sales → dừng bot
            if is_echo:
                source_type = message.get("source", {}).get("type", "")
                if source_type == "HUMAN":
                    customer_id = event.get("recipient", {}).get("id")
                    if customer_id:
                        human_mode.add(customer_id)
                        print(f"[HANDOFF] Paused for {customer_id}")
                continue

            # Deduplication
            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            if is_human_handling(sender_id):
                continue

            sender_name = get_sender_name(sender_id)
            history     = get_history(sender_id)

            product_card     = should_send_product_card(text, history)
            real_photo_product = should_send_real_photos(text)

            ai_reply = get_ai_reply(sender_id, text, sender_name)

            if "[ESCALATE]" in ai_reply:
                notify_human(sender_id, sender_name, text, ai_reply)

            if product_card and product_card in PRODUCT_CARDS:
                send_image(sender_id, PRODUCT_CARDS[product_card])
                save_message(sender_id, "assistant", f"[product_card_sent_{product_card}]")

            send_message(sender_id, ai_reply)

            if real_photo_product and real_photo_product in REAL_PHOTOS:
                for photo_url in REAL_PHOTOS[real_photo_product]:
                    send_image(sender_id, photo_url)

    return jsonify({"status": "ok"}), 200


# ── TAKEOVER PAGE ─────────────────────────────────────────────────────────────
@app.route("/takeover", methods=["GET", "POST"])
def takeover():
    if request.method == "POST":
        cid = request.form.get("customer_id", "").strip()
        action = request.form.get("action")
        if action == "bot" and cid:
            human_mode.discard(cid)
            return f"Bot da bat lai cho khach {cid}"
        return "Khong hop le"

    active = "<br>".join(human_mode) if human_mode else "Khong co"
    return f"""
    <h2>Anna Casa — Bat lai Bot</h2>
    <p>Bot tu dong dung khi sales reply. Dung trang nay de bat lai.</p>
    <form method=POST>
      Customer ID: <input name=customer_id size=40 placeholder="Paste ID khach"><br><br>
      <button name=action value=bot style="padding:8px 16px">Bat lai Bot</button>
    </form>
    <br><b>Dang o che do Human:</b><br>{active}
    """


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
