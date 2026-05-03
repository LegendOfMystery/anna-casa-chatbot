"""
ANNA CASA — Notification Server
Nhận webhook Facebook, serve web thông báo cho sales team
"""

import os
import time
import requests
from flask import Flask, request, jsonify, send_from_directory
from collections import deque

app = Flask(__name__)

META_PAGE_TOKEN   = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN = os.environ["META_VERIFY_TOKEN"]

# Lưu tối đa 50 tin nhắn gần nhất trong RAM
messages = deque(maxlen=50)
processed_messages = set()


def get_sender_name(sender_id: str) -> str:
    try:
        url = f"https://graph.facebook.com/{sender_id}?fields=name&access_token={META_PAGE_TOKEN}"
        return requests.get(url, timeout=5).json().get("name", "Khách")
    except:
        return "Khách"


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

            if not sender_id or not text or is_echo:
                continue

            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            name = get_sender_name(sender_id)
            messages.appendleft({
                "id": message_id,
                "name": name,
                "sender_id": sender_id,
                "text": text,
                "time": int(time.time())
            })
            print(f"[MSG] {name}: {text}")

    return jsonify({"status": "ok"}), 200


# ── API CHO WEB POLL ──────────────────────────────────────────────────────────
@app.route("/api/messages")
def api_messages():
    since = request.args.get("since", 0, type=int)
    new_msgs = [m for m in messages if m["time"] > since]
    return jsonify(new_msgs)


# ── SERVE WEB ─────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ── RUN ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
