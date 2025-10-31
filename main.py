# main.py
# TONKHO_ODOO_BOT – Telegram ↔ Odoo ERP Integration (Real-time, grouped by warehouse)
# Author: Anh Hoàn

import os, logging, xmlrpc.client
from aiogram import Bot, Dispatcher, types
from aiogram.utils.executor import start_webhook

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ODOO_URL  = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB   = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "Tronghoan91@")

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://tonkho-odoo.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===================== ODOO CONNECTION =====================
def odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logging.error("❌ Không thể đăng nhập Odoo.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logging.error(f"Lỗi kết nối Odoo: {e}")
        return None, None

# ===================== GET STOCK BY SKU =====================
def get_stock_info(sku: str):
    uid, models = odoo_connect()
    if not uid:
        return "❌ Không thể kết nối đến Odoo."

    try:
        # Tìm sản phẩm
        pid = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                'product.product', 'search',
                                [[['default_code', '=', sku]]])
        if not pid:
            return f"❌ Không tìm thấy mã hàng *{sku}*"

        quants = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'search_read',
                                   [[['product_id', 'in', pid]]],
                                   {'fields': ['location_id', 'quantity', 'reserved_quantity']})

        if not quants:
            return f"⚠️ Không có dữ liệu tồn cho *{sku}*"

        # Gom nhóm theo vị trí
        summary = {
            "HN": 0,
            "HCM": 0,
            "THANH LÝ HN": 0,
            "THANH LÝ HCM": 0,
            "NHẬP HN": 0
        }

        for q in quants:
            loc = (q["location_id"][1] or "").upper()
            qty = float(q["quantity"]) - float(q["reserved_quantity"])

            if "THANH LÝ" in loc:
                if "HN" in loc or "HÀ NỘI" in loc:
                    summary["THANH LÝ HN"] += qty
                elif "HCM" in loc:
                    summary["THANH LÝ HCM"] += qty
                else:
                    # nếu không xác định rõ, cộng chung HN
                    summary["THANH LÝ HN"] += qty
            elif "NHẬP" in loc or "INCOMING" in loc:
                if "HN" in loc or "HÀ NỘI" in loc:
                    summary["NHẬP HN"] += qty
            elif "HCM" in loc or "TPHCM" in loc or "TP HCM" in loc:
                summary["HCM"] += qty
            elif "HN" in loc or "HÀ NỘI" in loc:
                summary["HN"] += qty
            else:
                # Không rõ kho => bỏ qua hoặc log
                logging.debug(f"Bỏ qua vị trí không nhận diện: {loc}")

        total = sum(summary.values())

        lines = [
            f"📦 *{sku}*",
            f"1️⃣ Tồn kho HN: {summary['HN']:.0f}",
            f"2️⃣ Tồn kho HCM: {summary['HCM']:.0f}",
            f"3️⃣ Kho nhập HN: {summary['NHẬP HN']:.0f}",
            f"4️⃣ Kho thanh lý HN: {summary['THANH LÝ HN']:.0f}",
            f"5️⃣ Kho thanh lý HCM: {summary['THANH LÝ HCM']:.0f}",
            f"— Tổng khả dụng: *{total:.0f}*"
        ]

        return "\n".join(lines)

    except Exception as e:
        logging.error(f"Lỗi đọc tồn {sku}: {e}")
        return f"❌ Lỗi đọc dữ liệu: {e}"

# ===================== HANDLERS =====================
@dp.message_handler(commands=["start", "help"])
async def help_cmd(m: types.Message):
    await m.reply(
        "🤖 Bot kiểm tra tồn kho trực tiếp từ Odoo.\n"
        "Dùng lệnh:\n`/ton <MÃ_HÀNG>` hoặc gõ mã hàng bất kỳ.",
        parse_mode="Markdown"
    )

@dp.message_handler(commands=["ton"])
async def ton_cmd(m: types.Message):
    sku = m.text.replace("/ton", "").strip().upper()
    if not sku:
        return await m.reply("Dùng: `/ton MÃ_HÀNG`", parse_mode="Markdown")
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

@dp.message_handler()
async def any_text(m: types.Message):
    sku = m.text.strip().upper()
    if not sku or " " in sku:
        return
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

# ===================== WEBHOOK SERVER =====================
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
