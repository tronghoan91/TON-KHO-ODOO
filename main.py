# main.py – TONKHO_ODOO_BOT (patched 2025-11-02)
# Thêm fallback product.template + timeout + thông báo lỗi rõ ràng

import os, re, io, csv, logging, xmlrpc.client
from aiohttp import web
from aiogram import Bot, Dispatcher, types
import concurrent.futures

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ Thiếu BOT_TOKEN trong môi trường Render.")

ODOO_URL  = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB   = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "")
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://ton-kho-odoo.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", "10000"))
MIN_STOCK_HN = 50
ODOO_TIMEOUT = 10  # giây

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger("tonkho")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ================= UTILITIES =================
def safe_float(v):
    try: return float(v or 0.0)
    except: return 0.0

def extract_code(txt):
    m = re.search(r'([0-9]{2,4})\/[0-9]{2,4}', txt or "")
    return m.group(1) if m else None

def classify_location(name):
    if not name: return "OTHER"
    n = re.sub(r"\s+", " ", name.upper())
    n = n.replace("TP HCM", "HCM").replace("TPHCM","HCM").replace("HA NOI","HÀ NỘI")

    if "THANH" in n and "LY" in n:
        if "HCM" in n: return "THANHLY_HCM"
        if "HN" in n or "HÀ NỘI" in n: return "THANHLY_HN"

    if "NHAP" in n or "NHẬP" in n or "INCOMING" in n:
        if "HN" in n or "HÀ NỘI" in n: return "NHAP_HN"

    if "HCM" in n or "124/" in n: return "HCM"
    if "HN" in n or "HÀ NỘI" in n or "201/" in n: return "HN"

    code = extract_code(n)
    if code:
        if code.startswith(("124","12","1")): return "HCM"
        if code.startswith(("201","20","2")): return "HN"
    return "OTHER"

# ================= ODOO CONNECT =================
def odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common", allow_none=True)
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            log.error("❌ Không thể đăng nhập Odoo.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object", allow_none=True)
        return uid, models
    except Exception as e:
        log.error("Lỗi kết nối Odoo: %s", e)
        return None, None

# ================= CORE FUNCTION =================
def _odoo_query_with_timeout(func, *args, **kwargs):
    """Đảm bảo Odoo không treo quá 10 giây"""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=ODOO_TIMEOUT)
        except concurrent.futures.TimeoutError:
            raise TimeoutError("Odoo phản hồi chậm (timeout 10s)")

def find_product_ids(uid, models, sku):
    """Tìm id sản phẩm từ cả product.product và product.template"""
    pids = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                             'product.product', 'search',
                             [[['default_code', '=', sku]]])
    if pids: 
        return pids
    # fallback: tìm trong product.template
    tmpl_ids = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                 'product.template', 'search_read',
                                 [[['default_code', '=', sku]]], {'fields': ['id','product_variant_ids']})
    if tmpl_ids and tmpl_ids[0].get("product_variant_ids"):
        return [tmpl_ids[0]["product_variant_ids"][0]]
    return []

def get_stock_info(sku: str):
    uid, models = odoo_connect()
    if not uid: return "❌ Không thể kết nối Odoo."

    try:
        # tìm id sản phẩm
        pids = _odoo_query_with_timeout(find_product_ids, uid, models, sku)
        if not pids:
            return f"⚠️ Không tìm thấy mã hàng *{sku}* trong Odoo (chưa có biến thể tồn kho)."

        # đọc group tồn
        domain = [['product_id','in', pids]]
        groups = _odoo_query_with_timeout(
            models.execute_kw, ODOO_DB, uid, ODOO_PASS,
            'stock.quant', 'read_group',
            [domain, ['location_id','quantity','reserved_quantity'], ['location_id']],
            {'lazy': False}
        )
        if not groups:
            return f"⚠️ Mã hàng *{sku}* không có tồn kho khả dụng."

        loc_ids = [g['location_id'][0] for g in groups if g.get('location_id')]
        loc_map = {}
        if loc_ids:
            recs = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                     'stock.location', 'read',
                                     [loc_ids, ['id','name']])
            loc_map = {r['id']: r.get('name','') for r in recs}

        summary = {"HN":0.0,"HCM":0.0,"THANHLY_HN":0.0,"THANHLY_HCM":0.0,"NHAP_HN":0.0,"OTHER":0.0}
        details = []
        for g in groups:
            loc = g.get("location_id")
            if not loc: continue
            lid = loc[0]; lname = loc_map.get(lid) or loc[1]
            qty = safe_float(g.get("quantity",0))
            res = safe_float(g.get("reserved_quantity",0))
            avail = qty - res
            cls = classify_location(lname)
            summary[cls] = summary.get(cls,0.0) + avail
            details.append((lname, avail, cls))

        total = sum(summary.values())
        hn, hcm = summary["HN"], summary["HCM"]
        chuyen = max(0, MIN_STOCK_HN - hn)

        lines = [
            f"📦 *{sku}*",
            f"📊 Tổng khả dụng: *{total:.0f}*",
            f"1️⃣ Tồn kho HN: {hn:.0f}",
            f"2️⃣ Tồn kho HCM: {hcm:.0f}",
            f"3️⃣ Kho nhập HN: {summary['NHAP_HN']:.0f}",
            f"4️⃣ Kho thanh lý HN: {summary['THANHLY_HN']:.0f}",
            f"5️⃣ Kho thanh lý HCM: {summary['THANHLY_HCM']:.0f}",
        ]
        if chuyen>0: lines.append(f"\n💡 Đề xuất chuyển thêm *{chuyen} sp* ra HN để đạt 50.")
        else: lines.append("\n✅ Tồn HN đạt mức tối thiểu, không cần chuyển thêm.")

        shown = [d for d in details if abs(d[1])>0.5]
        if shown:
            lines.append("\n🔍 Chi tiết theo vị trí:")
            for lname,a,cls in sorted(shown,key=lambda x:-x[1])[:10]:
                lines.append(f"- {lname}: {int(round(a))} ({cls})")
        return "\n".join(lines)

    except TimeoutError as te:
        return f"⚠️ Hệ thống Odoo phản hồi chậm: {te}"
    except Exception as e:
        log.error("Lỗi đọc tồn %s: %s", sku, e)
        return f"❌ Lỗi khi đọc dữ liệu: {e}"

# ================= TELEGRAM =================
@dp.message_handler(commands=["start","help"])
async def start_cmd(m: types.Message):
    txt = (
        "🤖 Bot kiểm tra tồn kho trực tiếp từ Odoo\n\n"
        "• /ton <MÃ_HÀNG> — Tra tồn realtime và đề xuất chuyển ra HN (nếu <50)\n"
        "• /thongkehn — Xuất CSV thống kê tồn HN\n"
        "• /dexuatnhap — Xuất CSV đề xuất nhập HN\n"
        f"Ngưỡng tồn tối thiểu HN: {MIN_STOCK_HN}"
    )
    await m.reply(txt)

@dp.message_handler(commands=["ton"])
async def ton_cmd(m: types.Message):
    parts = m.text.split(maxsplit=1)
    if len(parts)<2: return await m.reply("Dùng: /ton <MÃ_HÀNG>")
    sku = parts[1].strip().upper()
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

@dp.message_handler()
async def any_text(m: types.Message):
    t = m.text.strip().upper()
    if not t or " " in t: return
    res = get_stock_info(t)
    await m.reply(res, parse_mode="Markdown")

# ================= WEBHOOK =================
async def handle_webhook(request):
    from aiogram import Bot as AiogramBot
    try:
        data = await request.json()
        update = types.Update(**data)
        AiogramBot.set_current(bot)
        dp.bot = bot
        await dp.process_update(update)
    except Exception as e:
        log.exception("Webhook update error: %s", e)
    return web.Response(text="ok")

async def on_startup(app):
    await bot.set_webhook(WEBHOOK_URL)
    log.info(f"✅ Webhook set: {WEBHOOK_URL}")

async def on_shutdown(app):
    await bot.delete_webhook()
    await bot.close()
    log.info("🔻 Bot stopped.")

def main():
    log.info("🚀 TONKHO_ODOO_BOT khởi chạy (patched fallback).")
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
