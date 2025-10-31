import os
import csv
import logging
import xmlrpc.client
from io import StringIO
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.types import FSInputFile

# =========================================================
# ⚙️ CẤU HÌNH CƠ BẢN
# =========================================================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASS = os.getenv("ODOO_PASS")
PORT = int(os.getenv("PORT", 10000))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://ton-kho-odoo.onrender.com")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_URL}{WEBHOOK_PATH}"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# =========================================================
# 🧠 HÀM KẾT NỐI VỚI ODOO
# =========================================================
def get_odoo_connection():
    """Đăng nhập Odoo qua XML-RPC"""
    common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
    uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
    if not uid:
        raise Exception("Không thể xác thực với Odoo – sai tài khoản hoặc mật khẩu.")
    models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
    return uid, models

# =========================================================
# 📦 LẤY DỮ LIỆU TỒN KHO
# =========================================================
async def fetch_stock_from_odoo(product_code: str):
    """Lấy tồn kho từ bảng stock.quant"""
    try:
        uid, models = get_odoo_connection()
        records = models.execute_kw(
            ODOO_DB, uid, ODOO_PASS,
            'stock.quant', 'search_read',
            [[['product_id.default_code', '=', product_code]]],
            {'fields': ['location_id', 'quantity', 'reserved_quantity']}
        )
        if not records:
            return {"lines": []}

        lines = []
        for rec in records:
            loc = rec["location_id"][1] if isinstance(rec["location_id"], (list, tuple)) else rec["location_id"]
            lines.append({
                "location": loc,
                "qty": rec.get("quantity", 0),
                "reserved": rec.get("reserved_quantity", 0)
            })
        return {"lines": lines}
    except Exception as e:
        logging.error(f"Lỗi kết nối Odoo: {e}")
        return {"error": str(e)}

# =========================================================
# 🧮 PHÂN LOẠI KHO THEO NHÓM
# =========================================================
def classify_location(name: str):
    name = name.lower()
    if any(k in name for k in ["201", "hà nội", "hanoi", "hn"]):
        if "thanh lý" in name:
            return "THANHLYHN"
        if "nhập" in name or "import" in name:
            return "NHAPHN"
        return "HN"
    elif any(k in name for k in ["124", "hcm", "hồ chí minh", "hcmc"]):
        if "thanh lý" in name:
            return "THANHLYHCM"
        return "HCM"
    else:
        return "OTHER"

# =========================================================
# 📊 TỔNG HỢP DỮ LIỆU
# =========================================================
def summarize_stock(data):
    summary = {"HN": 0, "HCM": 0, "NHAPHN": 0, "THANHLYHN": 0, "THANHLYHCM": 0, "OTHER": 0}
    short_detail = []
    for item in data.get("lines", []):
        loc_name = item.get("location", "")
        qty = item.get("qty", 0)
        reserved = item.get("reserved", 0)
        available = qty - reserved
        group = classify_location(loc_name)
        summary[group] += available
        short_detail.append(f"- {loc_name} | có: {available} | {group}")
    return summary, "\n".join(short_detail)

# =========================================================
# 🧾 TẠO FILE CSV
# =========================================================
async def create_csv_stock(stock_list, filename):
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Mã SP", "Tồn HN", "Tồn HCM", "Tồn nhập HN", "TL HN", "TL HCM", "Tổng tồn"])
    for row in stock_list:
        writer.writerow([row["code"], row["HN"], row["HCM"], row["NHAPHN"], row["THANHLYHN"], row["THANHLYHCM"], row["total"]])
    path = f"/tmp/{filename}"
    with open(path, "w", newline='', encoding="utf-8") as f:
        f.write(output.getvalue())
    return path

# =========================================================
# 💬 COMMANDS
# =========================================================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🤖 *BOT TRA CỨU TỒN KHO ODOO*\n\n"
        "Các lệnh hỗ trợ:\n"
        "• /ton <MÃ SP> — tra tồn kho trực tiếp từ Odoo\n"
        "• /thongkehn — xuất thống kê tồn HN/HCM\n"
        "• /dexuatnhap — xuất danh sách đề xuất nhập hàng HN\n\n"
        "_Toàn bộ dữ liệu cập nhật realtime từ hệ thống Odoo_",
        parse_mode="Markdown"
    )

@dp.message(Command("ton"))
async def cmd_ton(message: types.Message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("⚠️ Vui lòng nhập mã sản phẩm. Ví dụ: /ton AC-281")
        return

    code = parts[1].strip().upper()
    data = await fetch_stock_from_odoo(code)
    if not data or not data.get("lines"):
        await message.reply(f"❌ Không tìm thấy dữ liệu cho {code}.")
        return

    summary, detail_text = summarize_stock(data)
    total = sum(summary.values())
    hn, hcm, nhaphn, tlhn, tlhcm = summary["HN"], summary["HCM"], summary["NHAPHN"], summary["THANHLYHN"], summary["THANHLYHCM"]
    need_move = max(0, 50 - hn) if hn < 50 else 0

    text = (
        f"📦 *{code}*\n"
        f"🧮 Tổng: {total}\n"
        f"🏢 HN: {hn} | 🏬 HCM: {hcm}\n"
        f"📥 Nhập HN: {nhaphn} | 🛒 TL HN: {tlhn} | TL HCM: {tlhcm}\n"
    )
    if need_move > 0:
        text += f"➡️ *Đề xuất chuyển thêm {need_move} sp ra HN* để đủ tồn.\n"
    text += f"\n🔍 *Chi tiết rút gọn:*\n{detail_text}"

    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("thongkehn"))
async def cmd_thongkehn(message: types.Message):
    await message.reply("⏳ Đang tổng hợp dữ liệu thống kê HN/HCM...")
    products = ["AC-281", "MK-5170", "MK-332"]  # có thể thay bằng list thực tế
    stock_list = []
    for code in products:
        data = await fetch_stock_from_odoo(code)
        if not data:
            continue
        summary, _ = summarize_stock(data)
        stock_list.append({"code": code, **summary, "total": sum(summary.values())})

    path = await create_csv_stock(stock_list, "thongkehn.csv")
    file = FSInputFile(path)
    await message.answer_document(file, caption="📈 Báo cáo thống kê tồn HN/HCM")

@dp.message(Command("dexuatnhap"))
async def cmd_dexuatnhap(message: types.Message):
    await message.reply("⏳ Đang tạo danh sách đề xuất nhập hàng HN...")
    products = ["AC-281", "MK-5170", "MK-332"]
    stock_list = []
    for code in products:
        data = await fetch_stock_from_odoo(code)
        if not data:
            continue
        summary, _ = summarize_stock(data)
        hn = summary["HN"]
        missing = max(0, 50 - hn)
        stock_list.append({"code": code, **summary, "need_move": missing, "total": sum(summary.values())})

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Mã SP", "Tồn HN", "Thiếu để đạt 50", "Tồn HCM", "Tổng tồn"])
    for row in stock_list:
        writer.writerow([row["code"], row["HN"], row["need_move"], row["HCM"], row["total"]])
    path = "/tmp/dexuatnhap.csv"
    with open(path, "w", newline='', encoding="utf-8") as f:
        f.write(output.getvalue())

    file = FSInputFile(path)
    await message.answer_document(file, caption="📥 Danh sách đề xuất nhập hàng HN")

# =========================================================
# 🌐 WEBHOOK
# =========================================================
async def handle_webhook(request: web.Request):
    update = types.Update(**await request.json())
    await dp.feed_update(bot, update)
    return web.Response()

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    logging.info(f"✅ Webhook set: {WEBHOOK_URL}")

app = web.Application()
app.router.add_post(WEBHOOK_PATH, handle_webhook)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    logging.info("🚀 TONKHO_ODOO_BOT đang khởi chạy (aiogram v3 + Odoo XML-RPC)...")
    web.run_app(app, host="0.0.0.0", port=PORT)
