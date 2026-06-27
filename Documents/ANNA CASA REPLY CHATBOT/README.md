# ANNA CASA CHATBOT — SETUP GUIDE

## Files trong project
```
anna_casa_chatbot/
├── app.py                  # Main server
├── system_prompt.md        # AI personality + sales flow
├── product_knowledge.md    # Thông tin sản phẩm
└── README.md               # File này
```

## Setup từng bước

### 1. Cài dependencies
```bash
pip install flask anthropic requests
```

### 2. Set environment variables
```bash
export ANTHROPIC_API_KEY="sk-ant-..."        # Lấy từ console.anthropic.com
export META_PAGE_TOKEN="EAAxxxxx..."          # Lấy từ Meta Developer App
export META_VERIFY_TOKEN="anna_casa_2024"     # Bạn tự đặt chuỗi bất kỳ
export ESCALATE_NOTIFY_URL=""                 # Optional: Zalo/Slack webhook URL
```

### 3. Deploy server
Dùng Railway (miễn phí tier):
1. Push code lên GitHub
2. Vào railway.app → New Project → Deploy from GitHub
3. Add environment variables trong Railway dashboard
4. Railway tự tạo URL dạng: https://anna-casa.up.railway.app

### 4. Setup Meta Webhook
1. Vào developers.facebook.com
2. Chọn App của bạn → Messenger → Settings → Webhooks
3. Callback URL: https://anna-casa.up.railway.app/webhook
4. Verify Token: anna_casa_2024 (phải khớp với META_VERIFY_TOKEN)
5. Subscribe các events: messages, messaging_postbacks

### 5. Lấy Meta Page Access Token
1. developers.facebook.com → App → Messenger → Settings
2. Generate Token cho Page Anna Casa Vietnam
3. Copy vào META_PAGE_TOKEN

---

## Cập nhật thông tin sản phẩm
Chỉ cần sửa file `product_knowledge.md` — không cần đụng code.
Sau khi sửa, restart server là AI tự học thông tin mới.

## Escalate notification
Khi AI gặp câu hỏi không biết, nó sẽ:
1. Reply khách: "Dạ để em kiểm tra và phản hồi sớm nhất ạ"
2. Gửi notification đến ESCALATE_NOTIFY_URL (nếu đã cấu hình)

Để nhận notification qua Zalo:
- Dùng Zalo OA webhook URL làm ESCALATE_NOTIFY_URL
- Hoặc dùng Make.com/Zapier để forward notification

## Chi phí ước tính
- Anthropic API: ~$10-20/tháng (với ~500 conversations/ngày)
- Railway hosting: Free tier đủ dùng, hoặc $5/tháng nếu cần uptime 100%
- Tổng: ~200,000 - 500,000đ/tháng

## TODO trước khi go live
- [ ] Điền số Zalo vào system_prompt.md
- [ ] Cập nhật giá giấy dán tường và rèm vào product_knowledge.md
- [ ] Test với 10-20 conversation mẫu trước khi bật tự động
- [ ] Setup ESCALATE_NOTIFY_URL để nhận alert khi cần
- [ ] Xem lại conversation log mỗi ngày trong tuần đầu
