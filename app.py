"""
ANNA CASA AI CHATBOT
Stack: Python + Flask + Claude API + Meta Webhook
Flow:
  - Mọi tin nhắn từ khách → Claude xử lý
  - Sales reply → bot dừng hẳn
"""

import os
import time
import threading
import requests
from flask import Flask, request, jsonify
from anthropic import Anthropic

app = Flask(__name__)

# ── CONFIG ───────────────────────────────────────────────────────────────────
META_PAGE_TOKEN     = os.environ["META_PAGE_TOKEN"]
META_VERIFY_TOKEN   = os.environ["META_VERIFY_TOKEN"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
ESCALATE_NOTIFY_URL = os.environ.get("ESCALATE_NOTIFY_URL", "")

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── SYSTEM PROMPT ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """Bạn là chuyên viên AI tư vấn tại Anna Casa Vietnam.
QUAN TRỌNG: Mỗi tin nhắn của bạn CHỈ được 1-2 câu. Tuyệt đối không viết dài. Nhắn tin như người thật, không như email.

THÔNG TIN THẢM SIROC:
- Xuất xứ: Bỉ, công nghệ Heat Set (sợi xử lý nhiệt trước khi dệt)
- Ưu điểm: bền hơn, ít bám bụi, dễ vệ sinh hơn thảm thông thường
- Giá: 2.200.000đ/m²
- Size: cắt theo yêu cầu, một chiều không quá 4 mét
- Bảo hành: 24 tháng

THÔNG TIN GIẤY DÁN TƯỜNG:
- Không báo giá qua chat
- Gửi link cho khách xem: annacasavn.com/giay-dan-tuong
- Bán theo m²
- Nếu khách muốn tư vấn chi tiết hơn → xin số Zalo hoặc mời showroom

THÔNG TIN CHUNG:
- Showroom: 12 Nguyễn Ư Dĩ, Thảo Điền, Q2, TP.HCM
- Giờ mở cửa: 10h sáng đến 7h tối
- Ship toàn quốc

KHI KHÁCH VỪA NHẮN TIN LẦN ĐẦU:
Luôn tự giới thiệu trước: "Mình là chuyên viên AI tư vấn tại Anna Casa."
Sau đó mới trả lời nội dung khách hỏi.
Chỉ giới thiệu 1 lần duy nhất — nếu khách đã nhắn tin trước đó thì không giới thiệu lại.

CÁCH TRẢ LỜI:
- Xưng "bạn" với khách, tự xưng là "mình" và giới thiệu với khách là mình là chuyên viên ảo hoặc không xưng
- Mỗi reply CHỈ 1-2 câu ngắn, như nhắn tin thật — không viết dài
- Không chào hỏi dài dòng, không tổng kết cuối tin nhắn
- Không dùng emoji, không bullet point, không dấu gạch ngang dài
- Đọc kỹ tin nhắn khách — trả lời đúng ý, không hỏi lại những gì khách đã nói rõ
- Cuối mỗi tin chỉ hỏi tối đa 1 câu nếu cần thêm thông tin
- Nếu khách hỏi size thảm Siroc: xác nhận hợp lệ nếu không chiều nào quá 4 mét, tính giá = chiều dài x chiều rộng x 2.200.000đ
- Nếu khách hỏi giấy dán tường: gửi link annacasavn.com/giay-dan-tuong

KHI NÀO ESCALATE:
Nếu khách đưa số Zalo và muốn tư vấn qua Zalo, yêu cầu hoàn tiền, hoặc huỷ đơn:
- Reply: "Để mình chuyển cho bộ phận phụ trách hỗ trợ bạn ngay nha."
- Thêm [ESCALATE] vào cuối response (không hiện cho khách thấy)"""

# ── IN-MEMORY STORE ───────────────────────────────────────────────────────────
processed_messages: set = set()
bot_sending: set = set()
human_mode: set = set()
conversations: dict[str, list] = {}


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


SIROC_PRODUCT_CARD = (
    "https://res.cloudinary.com/dxihfwscx/image/upload/v1775103698/SirocProductCard_opax1p.jpg"
)

# ── ESCALATE ──────────────────────────────────────────────────────────────────
def notify_human(sender_id: str, message: str):
    if not ESCALATE_NOTIFY_URL:
        print(f"[ESCALATE] {sender_id}: {message}")
        return
    payload = {
        "text": f"CAN HO TRO\nID: {sender_id}\nTin nhan: {message}"
    }
    try:
        requests.post(ESCALATE_NOTIFY_URL, json=payload, timeout=5)
    except Exception as e:
        print(f"Escalate notify failed: {e}")


# ── CLAUDE REPLY ──────────────────────────────────────────────────────────────
def process_message(sender_id: str, text: str):
    try:
        save_message(sender_id, "user", text)
        history = get_history(sender_id)

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            system=SYSTEM_PROMPT,
            messages=history
        )

        reply = response.content[0].text
        needs_escalate = "[ESCALATE]" in reply
        clean_reply = reply.replace("[ESCALATE]", "").strip()

        save_message(sender_id, "assistant", clean_reply)

        if needs_escalate:
            notify_human(sender_id, text)

        # Chờ 5s — tự nhiên hơn
        time.sleep(5)

        if is_human_handling(sender_id):
            print(f"[HANDOFF] Cancelled after delay for {sender_id}")
            return

        bot_sending.add(sender_id)

        # Gửi product card Siroc lần đầu nếu khách hỏi về Siroc
        history_text = " ".join([m.get("content", "") for m in history]).lower()
        text_lower = text.lower()
        if ("siroc" in text_lower or "thảm bỉ" in text_lower) and "product_card_sent" not in history_text:
            send_image(sender_id, SIROC_PRODUCT_CARD)
            save_message(sender_id, "assistant", "[product_card_sent]")
            time.sleep(1)

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
                if not customer_id:
                    continue
                if customer_id in bot_sending:
                    print(f"[ECHO] Bot echo, skip for {customer_id}")
                    continue
                # Sales đã reply → dừng bot
                human_mode.add(customer_id)
                print(f"[HANDOFF] Sales replied, paused for {customer_id}")
                continue

            # Deduplication
            if message_id and message_id in processed_messages:
                continue
            if message_id:
                processed_messages.add(message_id)

            if is_human_handling(sender_id):
                print(f"[SKIP] Human mode for {sender_id}")
                continue

            print(f"[CLAUDE] Handling message from {sender_id}: {text[:50]}")
            threading.Thread(
                target=process_message,
                args=(sender_id, text),
                daemon=True
            ).start()

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
