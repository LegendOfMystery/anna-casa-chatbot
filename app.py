"""
ANNA CASA AI CHATBOT
Stack: Python + Flask + Meta Webhook
Flow: Siroc ad trigger → fixed 2-step reply → stop
"""

import os
import time
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# ── CONFIG ───────────────────────────────────────────────────────────────────
META_PAGE_TOKEN   = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]

# ── IN-MEMORY STORE ───────────────────────────────────────────────────────────
processed_messages: set = set()
bot_sending: set = set()
replied_users: set = set()  # Đã reply rồi → không reply thêm nữa

# ── KEYWORDS ──────────────────────────────────────────────────────────────────
SIROC_AD_TRIGGERS = [
    "tư vấn thảm siroc từ bỉ",
    "tư vấn thảm siroc",
    "tu van tham siroc tu bi",
    "tu van tham siroc",
]

# ── NỘI DUNG CỐ ĐỊNH ─────────────────────────────────────────────────────────
SIROC_GREETING = (
    "Dạ em chào anh chị, em là Trâm chuyên viên tại Anna Casa sẽ hỗ trợ mình nha."
)

SIROC_INTRO = (
    "Dạ Thảm Siroc bên Anna Casa là dòng thảm Bỉ dùng công nghệ Heat Set, "
    "sợi được xử lý nhiệt trước khi dệt nên bền hơn, ít bám bụi và "
    "dễ vệ sinh hơn thảm thông thường ạ."
)

SIROC_PRODUCT_CARD = (
    "https://res.cloudinary.com/dxihfwscx/image/upload/v1775103698/SirocProductCard_opax1p.jpg"
)

# ── SEND HELPERS ──────────────────────────────────────────────────────────────
def send_text(recipient_id: str, text: str):
    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {"recipient": {"id": recipient_id}, "message": {"text": text}}
    try:
        requests.post(url, json=payload, timeout=10).raise_for_status()
    except Exception as e:
        print(f"send_text failed: {e}")


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
        print(f"send_image failed: {e}")


# ── SIROC AD FLOW ─────────────────────────────────────────────────────────────
def process_siroc_ad(sender_id: str):
    try:
        time.sleep(5)  # chờ trước khi bắt đầu

        bot_sending.add(sender_id)

        # Bước 1 — Chào
        send_text(sender_id, SIROC_GREETING)
        time.sleep(2)  # 2s delay

        # Bước 2 — Gửi ảnh
        send_image(sender_id, SIROC_PRODUCT_CARD)
        time.sleep(5)  # 5s delay

        # Bước 3 — Giới thiệu
        send_text(sender_id, SIROC_INTRO)

        replied_users.add(sender_id)
        print(f"[SIROC_AD] Done for {sender_id}")

        time.sleep(10)
        bot_sending.discard(sender_id)

    except Exception as e:
        print(f"process_siroc_ad error: {e}")
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

            # Bỏ qua echo từ bot
            if is_echo:
                continue

            # Deduplication
            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            text_lower = text.lower().strip()

            # Chỉ xử lý nếu là Siroc ad trigger VÀ chưa reply user này
            if any(t in text_lower for t in SIROC_AD_TRIGGERS):
                if sender_id not in replied_users:
                    print(f"[SIROC_AD] Triggered for {sender_id}")
                    threading.Thread(
                        target=process_siroc_ad,
                        args=(sender_id,),
                        daemon=True
                    ).start()
                else:
                    print(f"[SKIP] Already replied to {sender_id}")
            else:
                # Mọi tin nhắn khác → bỏ qua hoàn toàn
                print(f"[SKIP] Non-trigger message from {sender_id}: {text[:50]}")

    return jsonify({"status": "ok"}), 200


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
