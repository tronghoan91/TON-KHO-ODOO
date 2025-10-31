import os
import logging
import asyncio
import csv
import io
import xmlrpc.client
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.types import FSInputFile
from datetime import datetime

# === CẤU HÌNH CƠ BẢN ===
API_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
ODOO_URL = "https://erp.nguonsongviet.vn/odoo"
ODOO_DB = os.getenv("ODOO_DB", "nguonsongviet")
ODOO_USER = os.getenv("ODOO_USER", "admin@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "YOUR_ODOO_PASSWORD")
PORT = int(os.getenv("PORT", 10000))

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)


# === HÀM KẾT NỐI ODOO ===
def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


# === HÀM TÌM SẢN PHẨM AN TOÀN (CÓ FALLBACK TEMPLATE) ===
def find_product_ids(uid, models, sku):
    """Tìm id sản phẩm từ cả product.product và product.template (fallback an toàn)."""
    try:
        # 1️⃣ Tìm theo product.product
        pids = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                 'product.product', 'search',
                                 [[['default_code', '=', sku]]])
        if pids:
            return ("product.product", pids)

        # 2️⃣ Fallback sang product.template
        tmpl = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                 'product.template', 'search_read',
                                 [[['default_code', '=', sku]]],
                                 {'fields': ['id', 'product_variant_ids']})
        if tmpl:
            pid_list = tmpl[0].get('product_variant_ids') or []
            if pid_list:
                return ("product.product", pid_list)
            else:
                return ("product.template", [tmpl[0]['id']])
    except Exception as e:
        logging.error(f"Lỗi tìm sản phẩm {sku}: {e}")
    return (None, [])


# === TRA TỒN KHO CHI TIẾT ===
def get_stock_info(sku):
    uid, models = odoo_connect()
    model_name, pids = find_product_ids(uid, models, sku)
    if not pids:
        return f"⚠️ Không tìm thấy mã hàng *{sku}* trong Odoo."

    field = 'product_id' if model_name == 'product.product' else 'product_tmpl_id'
    domain = [[field, 'in', pids]]
    quants = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                               'stock.quant', 'search_read',
                               [domain],
                               {'fields': ['location_id', 'quantity', 'reserved_quantity']})

    if not quants:
        return f"⚠️ Không có dữ liệu tồn cho *{sku}*."

    total = 0
    hn = hcm = nhap_hn = tl_hn = tl_hcm = other = 0
    details = []

    for q in quants:
        loc = q['location_id'][1] if q['location_id'] else ''
        qty = q['quantity']
        reserved = q['reserved_quantity']
        available = qty - reserved
        total += available

        # Phân loại kho
        lname = loc.lower()
        if "201" in lname or "hà nội" in lname:
            if "nhập" in lname:
                nhap_hn += available
                group = "NHAPHN"
            elif "thanh lý" in lname:
                tl_hn += available
                group = "THANHLYHN"
            else:
                hn += available
                group = "HN"
        elif "124" in lname or "hcm" in lname or "hồ chí minh" in lname:
            if "thanh lý" in lname:
                tl_hcm += available
                group = "THANHLYHCM"
            else:
                hcm += available
                group = "HCM"
        else:
            other += available
            group = "OTHER"

        details.append(f"- {loc} | có: {available:.0f} | {group}")

    # Đề xuất nhập thêm nếu <50
    de_xuat = max(0, 50 - hn) if hn < 50 else 0

    msg = (
        f"📦 *{sku}*\n"
        f"🧮 Tổng khả dụng: {total:.0f}\n"
        f"🏢 HN: {hn:.0f} | 🏬 HCM: {hcm:.0f}\n"
        f"📥 Nhập HN: {nhap_hn:.0f} | 🛒 TL HN: {tl_hn:.0f} | TL HCM: {tl_hcm:.0f}\n"
    )
    if de_xuat > 0:
        msg += f"➡️ Đề xuất chuyển thêm {de_xuat:.0f} sp ra HN để đảm bảo tồn >=50.\n"

    msg += "\n🔍 *Chi tiết rút gọn:*\n" + "\n".join(details[:10])
    if len(details) > 10:
        msg += f"\n...(+{len(details)-10} dòng nữa)"

    return msg


# === HANDLER LỆNH /START ===
@dp.message_handler(commands=["start"])
async def start_cmd(m: types.Message):
    await m.reply(
        "🤖 Bot kiểm tra tồn kho trực tiếp từ Odoo.\n\n"
        "Các lệnh:\n"
        "• /ton <MÃ_HÀNG> — Tra tồn realtime và đề xuất chuyển ra HN (nếu <50)\n"
        "• /thongkehn — Xuất CSV thống kê tồn HN\n"
        "• /dexuatnhap — Xuất CSV đề xuất nhập HN\n"
        "Ngưỡng tồn tối thiểu HN: 50\n\n"
        "Lưu ý: Bot lấy cột *Có hàng* = Hiện có - Reserved."
    )


# === LỆNH TRA TỒN ===
@dp.message_handler(lambda m: m.text and (m.text.startswith("/ton") or m.text.strip().isalnum()))
async def ton_cmd(m: types.Message):
    sku = m.text.replace("/ton", "").strip().upper()
    if not sku:
        await m.reply("⚠️ Cú pháp: /ton <MÃ_HÀNG>")
        return
    await m.reply("⏳ Đang lấy dữ liệu từ Odoo...")
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, get_stock_info, sku)
    await m.reply(msg, parse_mode="Markdown")


# === SERVER AIOHTTP ===
async def handle_root(request):
    return web.Response(text="TONKHO_ODOO_BOT đang hoạt động.")

async def handle_webhook(request):
    data = await request.json()
    update = types.Update(**data)
    await dp.process_update(update)
    return web.Response()

def main():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_post(f"/tg/webhook/{API_TOKEN}", handle_webhook)

    async def on_startup(_):
        logging.info("🚀 TONKHO_ODOO_BOT khởi chạy (patched fallback).")
        await bot.set_webhook(f"https://ton-kho-odoo.onrender.com/tg/webhook/{API_TOKEN}")

    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
