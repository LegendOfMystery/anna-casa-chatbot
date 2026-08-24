"""
Test bot logic locally — không cần Facebook webhook.
Usage:
  python3 test_bot.py text "Tôi muốn mua thảm 3x4"
  python3 test_bot.py image https://url-anh.jpg
  python3 test_bot.py image https://url-anh.jpg "Xin giá mẫu này"
"""
import sys, os

# Load env từ .env nếu có
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Fake send_text để không gửi thật lên Facebook
import unittest.mock as mock

FAKE_SENDER = "test_user_001"
printed_replies = []

def fake_send_text(recipient_id, text):
    print(f"\n{'='*50}")
    print(f"BOT → {text}")
    print('='*50)
    printed_replies.append(text)

def fake_send_image(recipient_id, url):
    print(f"\n[BOT gửi ảnh]: {url}")

with mock.patch.dict(os.environ, {
    "META_PAGE_TOKEN": os.environ.get("META_PAGE_TOKEN", "fake_token"),
    "META_PAGE_ID": os.environ.get("META_PAGE_ID", "fake_page_id"),
    "META_VERIFY_TOKEN": "fake",
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
    "GOOGLE_API_KEY": os.environ.get("GOOGLE_API_KEY", "fake"),
    "SHEET_ID": os.environ.get("SHEET_ID", "fake"),
    "ESCALATE_NOTIFY_URL": "",
}):
    import app as bot_app
    bot_app.send_text = fake_send_text
    bot_app.send_image = fake_send_image
    bot_app.send_file = lambda rid, url: print(f"\n[BOT gửi file]: {url}")

if len(sys.argv) < 3:
    print(__doc__)
    sys.exit(1)

mode = sys.argv[1]
arg  = sys.argv[2]
caption = sys.argv[3] if len(sys.argv) > 3 else ""

if mode == "text":
    print(f"\nKHÁCH: {arg}")
    bot_app.process_message(FAKE_SENDER, arg)

elif mode == "image":
    print(f"\nKHÁCH gửi ảnh: {arg}")
    if caption:
        print(f"KHÁCH kèm text: {caption}")
    bot_app.process_image(FAKE_SENDER, arg, caption)

else:
    print(f"Unknown mode: {mode}")
    print(__doc__)
