# main.py
# TONKHO_ODOO_BOT – Telegram ↔ Odoo ERP Integration (Real-time)
# Author: Anh Hoàn – Bản tối ưu cho Render Docker (2025.10)

import os, logging, xmlrpc.client, asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook

# ===================== CẤU HÌNH =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")  # Telegram bot token
ODOO_URL  = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB   = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "Tronghoan91@")  # => nên đưa vào ENV
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://tonkho-odoo.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===================== ODOO HELPER =====================
def _odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logging.error("❌ Đăng nhập Odoo thất bại.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logging.error(f"❌ Kết nối Odoo lỗi: {e}")
        return None, None

def _fetch_stock(sku: str):
    uid, models = _odoo_connect()
    if not uid: return "❌ Không thể kết nối Odoo."
    try:
        # tìm sản phẩm theo SKU
        pid = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                'product.product', 'search',
                                [[['default_code', '=', sku]]])
        if not pid:
            return f"❌ Không tìm thấy mã {sku}."
        quants = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'search_read',
                                   [[['product_id', 'in', pid]]],
                                   {'fields': ['location_id', 'quantity', 'reserved_quantity']})
        if not quants:
            return f"⚠️ Không có dữ liệu tồn cho {sku}."

        # gom nhóm theo kho
        groups = {}
        for q in quants:
            loc = q['location_id'][1]
            qty = float(q['quantity']) - float(q['reserved_quantity'])
            for key in ["HN", "HCM", "THANH LÝ", "NHẬP"]:
                if key in loc.upper():
                    groups[key] = groups.get(key, 0) + qty
        total = sum(q['quantity'] - q['reserved_quantity'] for q in quants)

        lines = [f"📦 *{sku}*"]
        lines.append(f"Tồn khả dụng tổng: *{total:.0f}*")
        for key in ["HN", "HCM", "THANH LÝ HN", "THANH LÝ HCM", "NHẬP HN"]:
            for k, v in groups.items():
                if key.replace(" ", "") in k.replace(" ", ""):
                    lines.append(f"- {key}: {v:.0f}")
        if len(lines) == 2:
            for k,v in groups.items(): lines.append(f"- {k}: {v:.0f}")
        return "\n".join(lines)
    except Exception as e:
        logging.error(f"Lỗi đọc tồn {sku}: {e}")
        return f"❌ Lỗi đọc dữ liệu: {e}"

# ===================== HANDLERS =====================
@dp.message_handler(commands=["start","help"])
async def help_cmd(m: types.Message):
    await m.reply("🤖 Bot kiểm tra tồn kho trực tiếp Odoo.\nDùng:\n`/ton <SKU>` hoặc gõ mã hàng bất kỳ.", parse_mode="Markdown")

@dp.message_handler(commands=["ton"])
async def ton_cmd(m: types.Message):
    sku = m.text.replace("/ton","").strip().upper()
    if not sku:
        return await m.reply("Dùng: `/ton MÃ_HÀNG`", parse_mode="Markdown")
    res = _fetch_stock(sku)
    await m.reply(res, parse_mode="Markdown")

@dp.message_handler()
async def any_text(m: types.Message):
    sku = m.text.strip().upper()
    if not sku or " " in sku:
        return
    res = _fetch_stock(sku)
    await m.reply(res, parse_mode="Markdown")

# ===================== WEBHOOK SETUP =====================
async def on_startup(dp):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"✅ Webhook set: {WEBHOOK_URL}")

async def on_shutdown(dp):
    await bot.delete_webhook()
    await bot.close()
    logging.info("🔻 Bot stopped.")

def main():
    logging.info("🚀 Starting TONKHO_ODOO_BOT...")
    start_webhook(
        dispatcher=dp,
        webhook_path=WEBHOOK_PATH,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        skip_updates=True,
        host="0.0.0.0",
        port=PORT
    )

if __name__ == "__main__":
    main()
