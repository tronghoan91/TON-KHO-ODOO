# main.py
# TONKHO_ODOO_BOT – Telegram ↔ Odoo ERP Integration (Realtime)
# Author: Anh Hoàn – Version 2025.10.31 (Render-stable)

import os
import logging
import xmlrpc.client
from aiohttp import web
from aiogram import Bot, Dispatcher, types

# ===================== CẤU HÌNH =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ Thiếu BOT_TOKEN. Hãy khai báo trong Render Environment.")

ODOO_URL  = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB   = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "Tronghoan91@")

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://ton-kho-odoo.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", "10000"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===================== ODOO CONNECTION =====================
def odoo_connect():
    """Kết nối tới Odoo qua XML-RPC."""
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logging.error("❌ Không thể đăng nhập Odoo – kiểm tra user/pass/db.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logging.error(f"Lỗi kết nối Odoo: {e}")
        return None, None

# ===================== TRUY XUẤT TỒN KHO =====================
def get_stock_info(sku: str):
    uid, models = odoo_connect()
    if not uid:
        return "❌ Không thể kết nối đến hệ thống Odoo."

    try:
        # Tìm sản phẩm theo mã SKU
        pid = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            'product.product', 'search',
            [[['default_code', '=', sku]]]
        )
        if not pid:
            return f"❌ Không tìm thấy mã hàng *{sku}*"

        # Lấy danh sách tồn kho thực tế
        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            'stock.quant', 'search_read',
            [[['product_id', 'in', pid]]],
            {'fields': ['location_id', 'quantity', 'reserved_quantity']}
        )
        if not quants:
            return f"⚠️ Không có dữ liệu tồn cho *{sku}*"

        # Gom nhóm vị trí
        summary = {
            "HN": 0,
            "HCM": 0,
            "THANH LÝ HN": 0,
            "THANH LÝ HCM": 0,
            "NHẬP HN": 0
        }

        for q in quants:
            loc_name = (q["location_id"][1] or "").upper()
            qty = float(q["quantity"]) - float(q["reserved_quantity"])

            if "THANH LÝ" in loc_name:
                if "HN" in loc_name or "HÀ NỘI" in loc_name:
                    summary["THANH LÝ HN"] += qty
                elif "HCM" in loc_name:
                    summary["THANH LÝ HCM"] += qty
            elif "NHẬP" in loc_name or "INCOMING" in loc_name:
                if "HN" in loc_name or "HÀ NỘI" in loc_name:
                    summary["NHẬP HN"] += qty
            elif any(k in loc_name for k in ["HCM", "TP HCM", "TPHCM"]):
                summary["HCM"] += qty
            elif any(k in loc_name for k in ["HN", "HÀ NỘI"]):
                summary["HN"] += qty
            else:
                logging.debug(f"Bỏ qua vị trí không xác định: {loc_name}")

        total = sum(summary.values())

        lines = [
            f"📦 *{sku}*",
            f"Tổng khả dụng: *{total:.0f}*",
            f"1️⃣ Tồn kho HN: {summary['HN']:.0f}",
            f"2️⃣ Tồn kho HCM: {summary['HCM']:.0f}",
            f"3️⃣ Kho nhập HN: {summary['NHẬP HN']:.0f}",
            f"4️⃣ Kho thanh lý HN: {summary['THANH LÝ HN']:.0f}",
            f"5️⃣ Kho thanh lý HCM: {summary['THANH LÝ HCM']:.0f}"
        ]

        return "\n".join(lines)

    except Exception as e:
        logging.error(f"Lỗi đọc tồn kho {sku}: {e}")
        return f"❌ Lỗi đọc dữ liệu: {e}"

# ===================== HANDLERS =====================
@dp.message_handler(commands=["start", "help"])
async def help_cmd(m: types.Message):
    await m.reply(
        "🤖 Bot kiểm tra tồn kho trực tiếp từ Odoo.\n"
        "Dùng:\n`/ton <MÃ_HÀNG>` hoặc gõ mã hàng bất kỳ để tra nhanh.",
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
async def handle_webhook(request: web.Request):
    from aiogram import Bot
    try:
        data = await request.json()
        update = types.Update(**data)

        # FIX CONTEXT (bắt buộc khi dùng aiohttp custom server)
        Bot.set_current(bot)
        dp.bot = bot

        await dp.process_update(update)
    except Exception as e:
        logging.exception(f"Lỗi xử lý update: {e}")
    return web.Response(text="ok")

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"✅ Webhook set: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.close()
    logging.info("🔻 Bot stopped.")

def main():
    logging.info("🚀 TONKHO_ODOO_BOT đang khởi chạy (aiohttp server)...")
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
