# main.py
# TONKHO_ODOO_BOT – Telegram ↔ Odoo ERP Integration (Realtime, improved location parsing)
# Author: Anh Hoàn – Final version (2025-10-31)
# LƯU Ý: xác định "Có hàng" = quantity - reserved_quantity (tương ứng cột 'Có hàng' trên Odoo)

import os
import re
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===================== HÀM HỖ TRỢ NHẬN DIỆN KHO =====================
def classify_location(loc_name_raw: str):
    """
    Trả về 1 trong: "HN","HCM","THANHLY_HN","THANHLY_HCM","NHAP_HN","OTHER"
    Nguyên tắc:
    - Nếu có 'THANH LÝ' -> vào thanh lý tương ứng HN/HCM nếu có thông tin địa phương
    - Nếu có 'NHẬP' / 'INCOMING' + 'HN'/'HÀ NỘI' -> NHAP_HN
    - Kiểm tra từ khóa HCM trước (vì 'HN' có thể xuất hiện trong 'CHUNG HƠN'), sau đó HÀ NỘI/HN
    - Nếu không khớp, trả OTHER
    """
    if not loc_name_raw:
        return "OTHER"
    loc = re.sub(r'\s+', ' ', loc_name_raw.strip().upper())

    # chuẩn hóa một vài thuật ngữ thường gặp
    loc = loc.replace("TP HCM", "HCM").replace("TPHCM","HCM").replace("HA NOI","HÀ NỘI")
    # detect thanh lý
    if "THANH LÝ" in loc or "THANH LY" in loc or "THANH-LY" in loc:
        if any(x in loc for x in ["HCM", "KHO HCM", "SHOWROOM HCM"]):
            return "THANHLY_HCM"
        if any(x in loc for x in ["HÀ NỘI", "HA NOI", "HN", "KHO HÀ NỘI", "KHO HA NOI"]):
            return "THANHLY_HN"
        # nếu không rõ địa phương, cố gắng dùng mã vị trí (số đầu)
        code = extract_location_code(loc)
        if code and code.startswith("1"):  # heuristic: mã 2xx thường HN (tuỳ hệ thống)
            return "THANHLY_HN"
        return "THANHLY_HN"  # default đặt về HN nếu không rõ

    # detect nhập
    if "NHẬP" in loc or "NHAP" in loc or "INCOMING" in loc:
        if any(x in loc for x in ["HCM", "KHO HCM", "SHOWROOM HCM"]):
            return "OTHER"  # nhập HCM không cần đếm vào NHẬP HN; giữ OTHER
        if any(x in loc for x in ["HÀ NỘI", "HA NOI", "HN", "KHO HÀ NỘI", "KHO HA NOI"]):
            return "NHAP_HN"
        code = extract_location_code(loc)
        if code and code.startswith("2"):  # heuristic khác nếu cần
            return "NHAP_HN"
        # default: nếu chứa 'NHẬP' mà không xác định thì bỏ qua
        return "OTHER"

    # detect HCM (ưu tiên)
    if any(x in loc for x in ["HCM", "KHO HCM", "SHOWROOM HCM", "CHI NHÁNH HCM"]):
        return "HCM"

    # detect HN
    if any(x in loc for x in ["HÀ NỘI", "HA NOI", "HN", "KHO HÀ NỘI", "KHO HA NOI"]):
        return "HN"

    # fallback: tìm mã vị trí dạng "123/123" hoặc "123/456" rồi áp heuristic
    code = extract_location_code(loc)
    if code:
        # nếu mã bắt đầu bằng 1xx hoặc 2xx => nhiều hệ thống dùng 2xx cho HN, 1xx cho HCM (ví dụ)
        # KHÔNG giả sử quá cứng; ta chỉ dùng heuristic nếu không tìm thấy từ khoá
        if code.startswith(("201","20","2")):
            return "HN"
        if code.startswith(("124","12","1")):
            return "HCM"
    return "OTHER"

def extract_location_code(loc_upper: str):
    """
    Thử lấy mã vị trí ở đầu, mẫu như '201/201' hoặc '124/124'...
    Trả về mã dạng '201' hoặc '124' (chuỗi) nếu tìm được, hoặc None.
    """
    m = re.match(r'^\s*([0-9]{2,4})\/[0-9]{2,4}', loc_upper)
    if m:
        return m.group(1)
    # thử tìm token dạng '201/' ở bất kỳ vị trí
    m2 = re.search(r'([0-9]{2,4})\/[0-9]{2,4}', loc_upper)
    if m2:
        return m2.group(1)
    return None

# ===================== ODOO CONNECT =====================
def odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logging.error("❌ Không thể đăng nhập Odoo – kiểm tra user/pass/db.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logging.exception(f"Lỗi kết nối Odoo: {e}")
        return None, None

# ===================== LẤY TỒN => GOM THEO KHO =====================
def get_stock_info(sku: str):
    uid, models = odoo_connect()
    if not uid:
        return "❌ Không thể kết nối đến hệ thống Odoo."

    try:
        # tìm product theo SKU
        pid = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            'product.product', 'search',
            [[['default_code', '=', sku]]]
        )
        if not pid:
            return f"❌ Không tìm thấy mã hàng *{sku}*"

        # Lấy tất cả stock.quant liên quan (quantity = 'Hiện có', reserved_quantity -> trừ ra để thành 'Có hàng')
        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            'stock.quant', 'search_read',
            [[['product_id', 'in', pid]]],
            {'fields': ['location_id', 'quantity', 'reserved_quantity']}
        )
        if not quants:
            return f"⚠️ Không có dữ liệu tồn cho *{sku}*"

        # tổng các nhóm cần thiết
        summary = {
            "HN": 0.0,
            "HCM": 0.0,
            "THANHLY_HN": 0.0,
            "THANHLY_HCM": 0.0,
            "NHAP_HN": 0.0,
            "OTHER": 0.0
        }

        # Duyệt quants, lấy "có hàng" = quantity - reserved_quantity
        for q in quants:
            loc = (q.get('location_id') and q['location_id'][1]) or ""
            loc_upper = str(loc).upper()
            qty = (float(q.get('quantity') or 0) - float(q.get('reserved_quantity') or 0))
            # normalize tiny -0.0
            if abs(qty) < 1e-9:
                qty = 0.0

            cls = classify_location(loc_upper)
            if cls == "HN":
                summary["HN"] += qty
            elif cls == "HCM":
                summary["HCM"] += qty
            elif cls == "THANHLY_HN":
                summary["THANHLY_HN"] += qty
            elif cls == "THANHLY_HCM":
                summary["THANHLY_HCM"] += qty
            elif cls == "NHAP_HN":
                summary["NHAP_HN"] += qty
            else:
                summary["OTHER"] += qty
                logging.debug(f"OTHER loc -> '{loc_upper}' qty={qty}")

        total = sum([v for k, v in summary.items() if k != "OTHER"])  # chỉ tính các nhóm quan trọng

        # format trả về: show rõ mã vị trí nếu muốn? hiện trả số tổng cho từng nhóm.
        lines = [
            f"📦 *{sku}*",
            f"📊 Tổng khả dụng (tính theo các nhóm chính): *{total:.0f}*",
            f"1️⃣ Tồn kho HN: {summary['HN']:.0f}",
            f"2️⃣ Tồn kho HCM: {summary['HCM']:.0f}",
            f"3️⃣ Kho nhập HN: {summary['NHAP_HN']:.0f}",
            f"4️⃣ Kho thanh lý HN: {summary['THANHLY_HN']:.0f}",
            f"5️⃣ Kho thanh lý HCM: {summary['THANHLY_HCM']:.0f}"
        ]

        # thêm debug ngắn nếu có OTHER (vị trí không xác định)
        if summary["OTHER"] != 0:
            lines.append(f"ℹ️ (Kho khác không phân loại: {summary['OTHER']:.0f})")

        return "\n".join(lines)

    except Exception as e:
        logging.exception(f"Lỗi đọc tồn cho {sku}: {e}")
        return f"❌ Lỗi đọc dữ liệu: {e}"

# ===================== HANDLER TELEGRAM =====================
@dp.message_handler(commands=["start", "help"])
async def help_cmd(m: types.Message):
    await m.reply(
        "🤖 Bot kiểm tra tồn kho trực tiếp từ Odoo.\n"
        "Dùng:\n`/ton <MÃ_HÀNG>` hoặc gõ mã hàng bất kỳ để tra nhanh.\n"
        "Lưu ý: Bot lấy 'Có hàng' = Hiện có - Reserved (tương ứng cột 'Có hàng' trên Odoo).",
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
    from aiogram import Bot as AiogramBot
    try:
        data = await request.json()
        update = types.Update(**data)

        # fix context để các handler dùng m.reply() được (bắt buộc)
        AiogramBot.set_current(bot)
        dp.bot = bot

        await dp.process_update(update)
    except Exception as e:
        logging.exception(f"Lỗi xử lý update: {e}")
    return web.Response(text="ok")

async def on_startup(app):
    # set webhook — nếu WEBHOOK_URL không hợp lệ thì log sẽ hiển thị
    try:
        await bot.set_webhook(WEBHOOK_URL)
        logging.info(f"✅ Webhook set: {WEBHOOK_URL}")
    except Exception as e:
        logging.exception(f"Lỗi set_webhook: {e}")

async def on_shutdown(app):
    try:
        await bot.delete_webhook()
    except Exception:
        pass
    try:
        await bot.close()
    except Exception:
        pass
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
