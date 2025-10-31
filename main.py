# main.py
# TONKHO_ODOO_BOT – Telegram ↔ Odoo ERP Integration
# Sửa để chạy trên Render (Docker) với aiogram 2.x + webhook
# Author (edited): Anh Hoàn (với chỉnh sửa)

import logging
import os
import xmlrpc.client
from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook
import asyncio

# ---------------------------
# Cấu hình: ưu tiên biến môi trường
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.warning("⚠️ BOT_TOKEN không được đặt. Bot sẽ không hoạt động nếu token không hợp lệ.")

# Odoo - nên set qua biến môi trường trên Render (không commit credential)
ODOO_URL = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "Tronghoan91@")  # KHÔNG NÊN để mặc định trong repo

# Webhook / Host (Render cung cấp HTTPS). RECOMMENDED: set RENDER_EXTERNAL_URL env var in Render dashboard.
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL")  # ví dụ: "https://ten-app.onrender.com"
if not WEBHOOK_HOST:
    logging.warning("⚠️ REENDER_EXTERNAL_URL không được đặt. Hãy đặt biến môi trường RENDER_EXTERNAL_URL bằng URL app của bạn.")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}" if BOT_TOKEN else "/tg/webhook/undefined"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

# ---------------------------
# Setup bot + dispatcher
# ---------------------------
bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else Bot(token="")  # nếu token rỗng: sẽ gặp lỗi khi gọi API -> log sẽ hiển thị
dp = Dispatcher(bot)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(message)s")

# ---------------------------
# Kết nối Odoo
# ---------------------------
def get_odoo_connection():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logging.error("❌ Không thể đăng nhập Odoo – kiểm tra ODOO_USER/ODOO_PASS/ODOO_DB.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
        return uid, models
    except Exception as e:
        logging.exception(f"❌ Lỗi kết nối Odoo: {e}")
        return None, None

# ---------------------------
# Lấy tồn theo SKU
# ---------------------------
def get_stock_qty(sku: str):
    uid, models = get_odoo_connection()
    if not uid or not models:
        return "❌ Không thể kết nối tới Odoo để lấy dữ liệu tồn."

    try:
        product_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                        'product.product', 'search',
                                        [[['default_code', '=', sku]]])
        if not product_ids:
            return f"❌ Không tìm thấy mã hàng *{sku}*"

        quants = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'search_read',
                                   [[['product_id', 'in', product_ids]]],
                                   {'fields': ['location_id', 'quantity', 'reserved_quantity']})
        if not quants:
            return f"⚠️ Không có dữ liệu tồn cho sản phẩm *{sku}*"

        total_available = sum((q.get('quantity') or 0) - (q.get('reserved_quantity') or 0) for q in quants)
        detail_lines = "\n".join(
            f"- {q['location_id'][1]}: {(q.get('quantity') or 0) - (q.get('reserved_quantity') or 0):.0f}"
            for q in quants if (q.get('quantity') or 0) - (q.get('reserved_quantity') or 0) != 0
        )

        return (f"📦 *{sku}*\n"
                f"Tồn khả dụng: *{total_available:.0f}*\n"
                f"{detail_lines if detail_lines else '(Không có chi tiết theo kho)'}")
    except Exception as e:
        logging.exception(f"Lỗi khi đọc tồn cho {sku}: {e}")
        return f"❌ Lỗi đọc dữ liệu tồn: {e}"

# ---------------------------
# Handlers Telegram
# ---------------------------
@dp.message_handler(commands=['start', 'help'])
async def start_cmd(message: types.Message):
    await message.answer("🤖 Bot kiểm tra tồn kho Odoo.\nDùng cú pháp:\n`/TON MÃ_HÀNG`\nVí dụ: `/TON AC-281`", parse_mode="Markdown")

@dp.message_handler(commands=['TON'])
async def ton_cmd(message: types.Message):
    sku = message.text.replace('/TON', '').strip().upper()
    if not sku:
        await message.reply("⚠️ Vui lòng nhập mã hàng sau lệnh.\nVí dụ: `/TON AC-281`", parse_mode="Markdown")
        return
    result = get_stock_qty(sku)
    # Nếu result là chuỗi Markdown, gửi trả về
    await message.reply(result, parse_mode="Markdown")

# ---------------------------
# Webhook startup/shutdown
# ---------------------------
async def on_startup(dp):
    if not WEBHOOK_URL:
        logging.error("❌ WEBHOOK_URL không hợp lệ. Không thể set webhook.")
        return
    try:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"✅ Webhook đã được thiết lập tại {WEBHOOK_URL}")
    except Exception as e:
        logging.exception(f"❌ Lỗi khi set webhook: {e}")

async def on_shutdown(dp):
    logging.warning("🔻 Đang tắt bot...")
    try:
        await bot.delete_webhook()
    except Exception as e:
        logging.exception(f"Lỗi khi xóa webhook: {e}")
    await bot.close()

# ---------------------------
# Entrypoint
# ---------------------------
def main():
    # Kiểm tra config sớm
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN chưa được cấu hình. Hủy khởi chạy.")
        return
    if not WEBHOOK_HOST:
        logging.error("RENDER_EXTERNAL_URL chưa được cấu hình. Hủy khởi chạy.")
        return

    logging.info("🚀 TONKHO_ODOO_BOT đang khởi chạy (webhook mode)...")
    # start_webhook sẽ tự tạo aiohttp server nội bộ
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8080))
    )

if __name__ == "__main__":
    main()
