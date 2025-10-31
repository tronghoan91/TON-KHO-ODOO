# main.py
# TONKHO_ODOO_BOT – Final Stable Version (Render, Fixed Min Stock HN = 50)
# Author: Anh Hoàn
# Version: 2025-11-01

import os
import re
import csv
import io
import logging
import xmlrpc.client
from aiohttp import web
from aiogram import Bot, Dispatcher, types

# ===================== CẤU HÌNH =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ Thiếu BOT_TOKEN. Hãy khai báo biến môi trường BOT_TOKEN trong Render.")

ODOO_URL  = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB   = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "")

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://ton-kho-odoo.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", "10000"))

# Ngưỡng tồn kho tối thiểu tại HN
MIN_STOCK_HN = 50

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tonkho")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===================== HÀM HỖ TRỢ =====================
def safe_float(v):
    try:
        return float(v or 0.0)
    except:
        return 0.0

def extract_location_code(loc_upper: str):
    """Ví dụ: '201/201 KHO HÀ NỘI' -> trả '201'"""
    if not loc_upper:
        return None
    m = re.search(r'([0-9]{2,4})\/[0-9]{2,4}', loc_upper)
    if m:
        return m.group(1)
    return None

def classify_location(loc_name_raw: str):
    """Xác định nhóm kho"""
    if not loc_name_raw:
        return "OTHER"
    loc = re.sub(r'\s+', ' ', str(loc_name_raw).strip().upper())
    loc = loc.replace("TP HCM", "HCM").replace("TPHCM", "HCM").replace("HA NOI", "HÀ NỘI")

    if any(k in loc for k in ["THANH LÝ", "THANH LY", "THANH-LY", "THANHLY"]):
        if "HCM" in loc:
            return "THANHLY_HCM"
        if any(k in loc for k in ["HÀ NỘI", "HA NOI", "HN"]):
            return "THANHLY_HN"

    if any(k in loc for k in ["NHẬP", "NHAP", "INCOMING"]):
        if any(k in loc for k in ["HÀ NỘI", "HA NOI", "HN"]):
            return "NHAP_HN"

    if any(k in loc for k in ["HCM", "KHO HCM", "SHOWROOM HCM", "CHI NHÁNH HCM"]):
        return "HCM"
    if any(k in loc for k in ["HÀ NỘI", "HA NOI", "HN", "KHO HÀ NỘI", "KHO HA NOI"]):
        return "HN"

    code = extract_location_code(loc)
    if code:
        if code.startswith(("124", "12", "1")):
            return "HCM"
        if code.startswith(("201", "20", "2")):
            return "HN"

    return "OTHER"

# ===================== KẾT NỐI ODOO =====================
def odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logger.error("❌ Không thể đăng nhập Odoo – kiểm tra tài khoản.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logger.error(f"Lỗi kết nối Odoo: {e}")
        return None, None

# ===================== TRA TỒN KHO =====================
def get_stock_info(sku: str):
    uid, models = odoo_connect()
    if not uid:
        return "❌ Không thể kết nối đến hệ thống Odoo."

    try:
        pids = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                 'product.product', 'search',
                                 [[['default_code', '=', sku]]])
        if not pids:
            return f"❌ Không tìm thấy mã hàng *{sku}*"

        groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'read_group',
                                   [[['product_id', 'in', pids]],
                                    ['location_id', 'quantity', 'reserved_quantity'],
                                    ['location_id']],
                                   {'lazy': False})

        loc_ids = [g['location_id'][0] for g in groups if g.get('location_id')]
        loc_map = {}
        if loc_ids:
            loc_records = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                           'stock.location', 'read',
                                           [loc_ids, ['id', 'name']])
            loc_map = {r['id']: r.get('name', '') for r in loc_records}

        summary = {"HN": 0.0, "HCM": 0.0, "THANHLY_HN": 0.0,
                   "THANHLY_HCM": 0.0, "NHAP_HN": 0.0, "OTHER": 0.0}
        simplified_details = []

        for g in groups:
            loc = g.get('location_id')
            if not loc:
                continue
            loc_id = loc[0]
            loc_name = loc_map.get(loc_id) or loc[1]
            qty = safe_float(g.get('quantity', 0))
            reserved = safe_float(g.get('reserved_quantity', 0))
            available = qty - reserved
            cls = classify_location(loc_name)
            summary[cls] = summary.get(cls, 0) + available
            simplified_details.append((loc_name, available, cls))

        total = sum(summary.values())
        hn, hcm = summary["HN"], summary["HCM"]

        # ====== ĐỀ XUẤT CHUYỂN HÀNG ======
        chuyen = 0
        if hn < MIN_STOCK_HN:
            chuyen = round(MIN_STOCK_HN - hn)

        lines = [
            f"📦 *{sku}*",
            f"📊 Tổng khả dụng: *{total:.0f}*",
            f"1️⃣ Tồn kho HN: {hn:.0f}",
            f"2️⃣ Tồn kho HCM: {hcm:.0f}",
            f"3️⃣ Kho nhập HN: {summary['NHAP_HN']:.0f}",
            f"4️⃣ Kho thanh lý HN: {summary['THANHLY_HN']:.0f}",
            f"5️⃣ Kho thanh lý HCM: {summary['THANHLY_HCM']:.0f}",
        ]

        # Đề xuất
        if chuyen > 0:
            lines.append(f"\n💡 Đề xuất chuyển thêm *{chuyen} sp* ra HN để đạt mức tồn tối thiểu {MIN_STOCK_HN}.")
        else:
            lines.append("\n✅ Tồn HN đạt mức tối thiểu, không cần chuyển thêm hàng.")

        # Rút gọn hiển thị
        shown = [d for d in simplified_details if abs(d[1]) > 0.5]
        if shown:
            lines.append("")
            lines.append("🔍 Chi tiết theo vị trí:")
            for lname, avail, cls in sorted(shown, key=lambda x: -x[1])[:10]:
                lines.append(f"- {lname}: {int(round(avail))} ({cls})")

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"Lỗi đọc tồn {sku}: {e}")
        return f"❌ Lỗi đọc dữ liệu: {e}"

# ===================== TELEGRAM HANDLERS =====================
@dp.message_handler(commands=["start", "help"])
async def start_cmd(m: types.Message):
    msg = (
        "🤖 BOT KIỂM TRA TỒN KHO (Odoo Realtime)\n\n"
        "Các lệnh khả dụng:\n"
        "• /ton <MÃ_HÀNG> — Tra tồn kho realtime và đề xuất chuyển ra HN.\n"
        "• /tongo — Tổng hợp tồn toàn hệ thống (HN, HCM, nhập, thanh lý).\n"
        "• /thongkehn — Xuất file thống kê tồn tại HN.\n"
        "• /dexuatnhap — Xuất file đề xuất nhập hàng cho HN.\n\n"
        f"Ngưỡng tối thiểu tồn kho HN hiện tại: {MIN_STOCK_HN} sản phẩm.\n"
        "Tính theo cột 'Có hàng' = Số lượng - Reserved trong Odoo."
    )
    await m.reply(msg)

@dp.message_handler(commands=["ton"])
async def ton_cmd(m: types.Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("Dùng: /ton <MÃ_HÀNG>")
    sku = parts[1].strip().upper()
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

@dp.message_handler()
async def any_text(m: types.Message):
    t = m.text.strip().upper()
    if not t or " " in t:
        return
    res = get_stock_info(t)
    await m.reply(res, parse_mode="Markdown")

# ===================== WEBHOOK =====================
async def handle_webhook(request: web.Request):
    from aiogram import Bot as AiogramBot
    try:
        data = await request.json()
        update = types.Update(**data)
        AiogramBot.set_current(bot)
        dp.bot = bot
        await dp.process_update(update)
    except Exception as e:
        logger.exception(f"Lỗi xử lý update: {e}")
    return web.Response(text="ok")

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    logger.info(f"✅ Webhook set: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.close()
    logger.info("🔻 Bot stopped.")

def main():
    logger.info("🚀 TONKHO_ODOO_BOT đang khởi chạy...")
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
