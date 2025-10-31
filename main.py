import os
import logging
import asyncio
import csv
import io
import xmlrpc.client
from aiohttp import web
from aiogram import Bot, Dispatcher, types
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
dp = Dispatcher()  # ✅ Chuẩn cho Aiogram v3

# === KẾT NỐI ODOO ===
def odoo_connect():
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models


# === TÌM SẢN PHẨM (CÓ FALLBACK TEMPLATE) ===
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

    total = hn = hcm = nhap_hn = tl_hn = tl_hcm = other = 0
    details = []

    for q in quants:
        loc = q['location_id'][1] if q['location_id'] else ''
        qty = q['quantity']
        reserved = q['reserved_quantity']
        available = qty - reserved
        total += available

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


# === XUẤT CSV THỐNG KÊ & ĐỀ XUẤT ===
def export_csv_data(type_export="thongke"):
    uid, models = odoo_connect()
    quants = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                               'stock.quant', 'search_read',
                               [[]],
                               {'fields': ['product_id', 'location_id', 'quantity', 'reserved_quantity']})
    data = {}
    for q in quants:
        pid = q['product_id'][1] if q['product_id'] else ''
        loc = q['location_id'][1] if q['location_id'] else ''
        qty = q['quantity']
        reserved = q['reserved_quantity']
        available = qty - reserved
        lname = loc.lower()

        if pid not in data:
            data[pid] = {"HN": 0, "HCM": 0, "NHAPHN": 0, "TLHN": 0, "TLHCM": 0, "TONG": 0}

        if "201" in lname or "hà nội" in lname:
            if "nhập" in lname:
                data[pid]["NHAPHN"] += available
            elif "thanh lý" in lname:
                data[pid]["TLHN"] += available
            else:
                data[pid]["HN"] += available
        elif "124" in lname or "hcm" in lname or "hồ chí minh" in lname:
            if "thanh lý" in lname:
                data[pid]["TLHCM"] += available
            else:
                data[pid]["HCM"] += available

        data[pid]["TONG"] += available

    output = io.StringIO()
    writer = csv.writer(output)
    if type_export == "thongke":
        writer.writerow(["Mã SP", "Tồn HN", "Tồn HCM", "Tổng tồn", "Thanh lý HN", "Thanh lý HCM", "Kho nhập HN"])
        for pid, v in data.items():
            writer.writerow([pid, v["HN"], v["HCM"], v["TONG"], v["TLHN"], v["TLHCM"], v["NHAPHN"]])
    else:  # dexuatnhap
        writer.writerow(["Mã SP", "Tồn HN", "Thiếu để đạt 50", "Tồn HCM", "Tổng tồn"])
        for pid, v in data.items():
            de_xuat = max(0, 50 - v["HN"])
            writer.writerow([pid, v["HN"], de_xuat, v["HCM"], v["TONG"]])

    output.seek(0)
    return output.getvalue()


# === HANDLER /START ===
@dp.message(commands=["start"])
async def start_cmd(message: types.Message):
    await message.answer(
        "🤖 Bot kiểm tra tồn kho trực tiếp từ Odoo.\n\n"
        "Lệnh khả dụng:\n"
        "• /ton <MÃ_HÀNG> — Tra tồn realtime, đề xuất chuyển ra HN (nếu <50)\n"
        "• /thongkehn — Xuất CSV thống kê tồn HN/HCM\n"
        "• /dexuatnhap — Xuất CSV đề xuất nhập hàng HN\n\n"
        "Tất cả dữ liệu lấy trực tiếp từ Odoo."
    )


# === LỆNH TRA TỒN ===
@dp.message()
async def ton_cmd(message: types.Message):
    text = message.text.strip().upper()
    if not text or text.startswith("/"):
        return
    await message.answer("⏳ Đang lấy dữ liệu từ Odoo...")
    loop = asyncio.get_event_loop()
    msg = await loop.run_in_executor(None, get_stock_info, text)
    await message.answer(msg, parse_mode="Markdown")


# === LỆNH XUẤT CSV ===
@dp.message(commands=["thongkehn"])
async def thongkehn_cmd(message: types.Message):
    await message.answer("⏳ Đang tổng hợp dữ liệu thống kê tồn HN/HCM...")
    loop = asyncio.get_event_loop()
    csv_data = await loop.run_in_executor(None, export_csv_data, "thongke")
    await message.answer_document(("thongkehn.csv", csv_data.encode("utf-8")))

@dp.message(commands=["dexuatnhap"])
async def dexuatnhap_cmd(message: types.Message):
    await message.answer("⏳ Đang tạo danh sách đề xuất nhập hàng HN...")
    loop = asyncio.get_event_loop()
    csv_data = await loop.run_in_executor(None, export_csv_data, "dexuatnhap")
    await message.answer_document(("dexuatnhap.csv", csv_data.encode("utf-8")))


# === SERVER AIOHTTP ===
async def handle_root(request):
    return web.Response(text="TONKHO_ODOO_BOT đang hoạt động.")

async def handle_webhook(request):
    from aiogram import Bot as AiogramBot
    data = await request.json()
    update = types.Update(**data)
    AiogramBot.set_current(bot)
    await dp.feed_update(bot, update)
    return web.Response()

async def on_startup(app):
    logging.info("🚀 TONKHO_ODOO_BOT khởi chạy (FULL v3).")
    await bot.set_webhook(f"https://ton-kho-odoo.onrender.com/tg/webhook/{API_TOKEN}")

def main():
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_post(f"/tg/webhook/{API_TOKEN}", handle_webhook)
    app.on_startup.append(on_startup)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
