"""
ANNA CASA AI CHATBOT
Stack: Python + Flask + Claude API + Meta Webhook
"""

import os
import json
import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic

app = Flask(__name__)

# ── CONFIG ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]      # Facebook page access token
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]    # bạn tự đặt, dùng lúc verify webhook
ESCALATE_NOTIFY_URL = os.environ.get("ESCALATE_NOTIFY_URL", "")  # Zalo/Slack webhook để notify

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── LOAD PROMPTS ─────────────────────────────────────────────────────────────
with open("system_prompt.md", "r", encoding="utf-8") as f:
    SYSTEM_PROMPT = f.read()

with open("product_knowledge.md", "r", encoding="utf-8") as f:
    PRODUCT_KNOWLEDGE = f.read()

FULL_SYSTEM = f"{SYSTEM_PROMPT}\n\n---\n\n{PRODUCT_KNOWLEDGE}"

# ── IN-MEMORY CONVERSATION STORE ─────────────────────────────────────────────
# Production: thay bằng Redis hoặc SQLite
conversations: dict[str, list] = {}

def get_history(sender_id: str) -> list:
    return conversations.get(sender_id, [])

def save_message(sender_id: str, role: str, content: str):
    if sender_id not in conversations:
        conversations[sender_id] = []
    conversations[sender_id].append({"role": role, "content": content})
    # Giữ tối đa 20 tin nhắn gần nhất để tránh token quá dài
    if len(conversations[sender_id]) > 20:
        conversations[sender_id] = conversations[sender_id][-20:]

# ── AI REPLY ─────────────────────────────────────────────────────────────────
def get_ai_reply(sender_id: str, user_message: str, sender_name: str = "") -> str:
    save_message(sender_id, "user", user_message)
    history = get_history(sender_id)

    # Inject tên khách vào context nếu có
    context_note = f"\n[Context: Tên khách là {sender_name}]" if sender_name else ""
    messages_with_context = history.copy()
    if context_note and messages_with_context:
        messages_with_context[0] = {
            "role": messages_with_context[0]["role"],
            "content": context_note + "\n" + messages_with_context[0]["content"]
        }

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        system=FULL_SYSTEM,
        messages=messages_with_context
    )

    reply = response.content[0].text
    save_message(sender_id, "assistant", reply)
    return reply

# ── ESCALATE ─────────────────────────────────────────────────────────────────
def notify_human(sender_id: str, sender_name: str, message: str, ai_reply: str):
    """Gửi notification cho human khi AI escalate"""
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

# ── SEND MESSAGE ─────────────────────────────────────────────────────────────
def send_message(recipient_id: str, message_text: str, platform: str = "messenger"):
    """Gửi tin nhắn về Facebook/Instagram"""
    # Xóa tag [ESCALATE] trước khi gửi cho khách
    clean_message = message_text.replace("[ESCALATE]", "").strip()

    url = f"https://graph.facebook.com/v18.0/me/messages?access_token={META_PAGE_TOKEN}"
    payload = {
        "recipient": {"id": recipient_id},
        "message": {"text": clean_message}
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"Send message failed: {e}")

# ── WEBHOOK VERIFY ────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Meta gọi endpoint này lần đầu để verify"""
    mode      = request.args.get("hub.mode")
    token     = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == META_VERIFY_TOKEN:
        return challenge, 200
    return "Forbidden", 403

# ── WEBHOOK RECEIVE ───────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def receive_webhook():
    """Nhận tin nhắn từ Facebook/Instagram"""
    data = request.get_json()
    if not data:
        return jsonify({"status": "no data"}), 200

    for entry in data.get("entry", []):
        for event in entry.get("messaging", []):
            sender_id = event.get("sender", {}).get("id")
            message   = event.get("message", {})
            text      = message.get("text", "")

            if not sender_id or not text:
                continue

            # Lấy tên khách từ Meta Graph API
            sender_name = get_sender_name(sender_id)

            # Lấy AI reply
            ai_reply = get_ai_reply(sender_id, text, sender_name)

            # Kiểm tra có cần escalate không
            if "[ESCALATE]" in ai_reply:
                notify_human(sender_id, sender_name, text, ai_reply)

            # Gửi reply cho khách
            send_message(sender_id, ai_reply)

    return jsonify({"status": "ok"}), 200

# ── HELPER: LẤY TÊN KHÁCH ────────────────────────────────────────────────────
def get_sender_name(sender_id: str) -> str:
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name&access_token={META_PAGE_TOKEN}"
        res = requests.get(url, timeout=5)
        return res.json().get("name", "")
    except:
        return ""

# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
app.run(host="0.0.0.0", port=port, debug=False)
