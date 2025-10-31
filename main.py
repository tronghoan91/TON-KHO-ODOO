# main.py
# TONKHO_ODOO_BOT – Telegram ↔ Odoo ERP Integration (Realtime, reports & suggestions)
# Author: Anh Hoàn (final) — 2025-10-31
# Notes:
# - "Có hàng" = quantity - reserved_quantity
# - Uses stock.quant read_group for aggregation by location_id / product_id
# - Deploy: Render Docker, set env variables (BOT_TOKEN, RENDER_EXTERNAL_URL, ODOO_*)

import os
import re
import csv
import io
import math
import logging
import xmlrpc.client
from aiohttp import web
from aiogram import Bot, Dispatcher, types

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ Thiếu BOT_TOKEN. Hãy khai báo biến môi trường BOT_TOKEN trên Render.")

ODOO_URL  = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB   = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "")

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "https://ton-kho-odoo.onrender.com").rstrip("/")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL  = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", "10000"))

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tonkho")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ===================== HELPERS: location classification =====================
def extract_location_code(loc_upper: str):
    """Try to extract leading code like '201/201' -> returns '201' or None"""
    if not loc_upper:
        return None
    m = re.search(r'([0-9]{2,4})\/[0-9]{2,4}', loc_upper)
    if m:
        return m.group(1)
    return None

def classify_location(loc_name_raw: str):
    """
    Classify location name into:
     - HN, HCM, THANHLY_HN, THANHLY_HCM, NHAP_HN, OTHER
    Uses multiple heuristics:
     - keyword matches (THANH LÝ, NHẬP, HCM, HÀ NỘI, HA NOI, HN)
     - code heuristics if present (e.g., '201/201' -> HN; '124/124' -> HCM)
    """
    if not loc_name_raw:
        return "OTHER"
    loc = re.sub(r'\s+', ' ', loc_name_raw.strip().upper())
    # normalize
    loc = loc.replace("TP HCM", "HCM").replace("TPHCM","HCM").replace("HA NOI","HÀ NỘI")

    # thanh lý
    if "THANH LÝ" in loc or "THANH LY" in loc or "THANH-LY" in loc or "THANHLY" in loc:
        if any(k in loc for k in ["HCM", "KHO HCM", "SHOWROOM HCM"]):
            return "THANHLY_HCM"
        if any(k in loc for k in ["HN", "HÀ NỘI", "HA NOI", "KHO HÀ NỘI", "KHO HA NOI"]):
            return "THANHLY_HN"
        # fallback via code
        code = extract_location_code(loc)
        if code:
            if code.startswith(("1","12","124")):  # heuristic for HCM-ish codes
                return "THANHLY_HCM"
            return "THANHLY_HN"
        return "THANHLY_HN"

    # nhập
    if "NHẬP" in loc or "NHAP" in loc or "INCOMING" in loc:
        if any(k in loc for k in ["HÀ NỘI","HA NOI","HN"]):
            return "NHAP_HN"
        # fallback check code
        code = extract_location_code(loc)
        if code and code.startswith(("20","2","201")):
            return "NHAP_HN"
        return "OTHER"

    # check HCM first (avoid HN substring issues)
    if any(k in loc for k in ["HCM", "KHO HCM", "SHOWROOM HCM", "CHI NHÁNH HCM"]):
        return "HCM"

    # check HN
    if any(k in loc for k in ["HÀ NỘI", "HA NOI", "HN", "KHO HÀ NỘI", "KHO HA NOI"]):
        return "HN"

    # fallback by code
    code = extract_location_code(loc)
    if code:
        # Heuristic: in your system codes like 201 -> HN, 124 -> HCM based on screenshot.
        if code.startswith(("201","20","2","31","32")):
            return "HN"
        if code.startswith(("124","12","1")):
            return "HCM"
    return "OTHER"

def safe_float(v):
    try:
        return float(v or 0)
    except:
        return 0.0

# ===================== ODOO CONNECTION =====================
def odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logger.error("Odoo authenticate failed. Check credentials.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logger.exception("Odoo connection error: %s", e)
        return None, None

# ===================== CORE: per-SKU realtime =====================
def get_stock_info(sku: str):
    uid, models = odoo_connect()
    if not uid:
        return "❌ Không thể kết nối Odoo."

    try:
        pid = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                'product.product', 'search',
                                [[['default_code','=', sku]]])
        if not pid:
            return f"❌ Không tìm thấy mã hàng *{sku}*"

        # get quants for this product
        quants = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'search_read',
                                   [[['product_id','in', pid]]],
                                   {'fields': ['location_id','quantity','reserved_quantity']})
        if not quants:
            return f"⚠️ Không có dữ liệu tồn cho *{sku}*"

        summary = {"HN":0.0,"HCM":0.0,"THANHLY_HN":0.0,"THANHLY_HCM":0.0,"NHAP_HN":0.0,"OTHER":0.0}
        # collect per-location details optionally if needed
        for q in quants:
            loc = (q.get('location_id') and q['location_id'][1]) or ""
            loc_u = str(loc).upper()
            qty = safe_float(q.get('quantity',0)) - safe_float(q.get('reserved_quantity',0))
            cls = classify_location(loc_u)
            if cls in summary:
                summary[cls] += qty
            else:
                summary["OTHER"] += qty

        total = sum(summary[k] for k in ("HN","HCM","THANHLY_HN","THANHLY_HCM","NHAP_HN"))
        lines = [
            f"📦 *{sku}*",
            f"📊 Tổng khả dụng (nhóm chính): *{total:.0f}*",
            f"1️⃣ Tồn kho HN: {summary['HN']:.0f}",
            f"2️⃣ Tồn kho HCM: {summary['HCM']:.0f}",
            f"3️⃣ Kho nhập HN: {summary['NHAP_HN']:.0f}",
            f"4️⃣ Kho thanh lý HN: {summary['THANHLY_HN']:.0f}",
            f"5️⃣ Kho thanh lý HCM: {summary['THANHLY_HCM']:.0f}"
        ]
        if abs(summary["OTHER"]) > 0.5:
            lines.append(f"ℹ️ Kho khác không phân loại: {summary['OTHER']:.0f}")
        return "\n".join(lines)
    except Exception as e:
        logger.exception("Error get_stock_info: %s", e)
        return f"❌ Lỗi khi đọc tồn: {e}"

# ===================== AGGREGATIONS & REPORTS =====================
def aggregate_totals_by_location_group():
    """
    Return dict of totals for groups (HN,HCM,THANHLY_HN,THANHLY_HCM,NHAP_HN) aggregated across all locations.
    Uses read_group on stock.quant grouped by location_id.
    """
    uid, models = odoo_connect()
    if not uid:
        return None, "Không kết nối Odoo"

    try:
        # retrieve all locations for mapping id->name
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                  'stock.location', 'search_read',
                                  [[], ['id','name']])
        loc_map = {l['id']: l['name'] for l in locs}

        # aggregate quants grouped by location_id
        groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'read_group',
                                   [[], ['quantity','reserved_quantity','location_id'], ['location_id']],
                                   {'lazy': False})
        summary = {"HN":0.0,"HCM":0.0,"THANHLY_HN":0.0,"THANHLY_HCM":0.0,"NHAP_HN":0.0,"OTHER":0.0}
        for g in groups:
            loc = g.get('location_id')
            if not loc:
                continue
            loc_id = loc[0]; loc_name = loc_map.get(loc_id, loc[1] if isinstance(loc, (list,tuple)) else str(loc))
            loc_u = str(loc_name).upper()
            qty = safe_float(g.get('quantity',0)) - safe_float(g.get('reserved_quantity',0))
            cls = classify_location(loc_u)
            if cls in summary:
                summary[cls] += qty
            else:
                summary["OTHER"] += qty
        return summary, None
    except Exception as e:
        logger.exception("aggregate error: %s", e)
        return None, str(e)

def build_thongkehn_csv():
    """
    Build CSV with columns: SKU, TenSP, TonHN, NhapHN, TongTon
    Uses read_group to aggregate by product over selected location ids (HN and NHAP_HN).
    """
    uid, models = odoo_connect()
    if not uid:
        return None, "Không kết nối Odoo"

    try:
        # get locations list first (id->name)
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                  'stock.location', 'search_read',
                                  [[], ['id','name']])
        loc_map = {l['id']: l['name'] for l in locs}
        # classify locations into HN group and NHAP_HN group
        hn_loc_ids = [lid for lid, name in loc_map.items() if classify_location(name) == "HN"]
        nhap_hn_loc_ids = [lid for lid, name in loc_map.items() if classify_location(name) == "NHAP_HN"]

        # aggregate per product for HN
        hn_domain = [['location_id','in', hn_loc_ids]] if hn_loc_ids else [['id','=',0]]
        hn_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                      'stock.quant', 'read_group',
                                      [hn_domain, ['product_id','quantity','reserved_quantity'], ['product_id']],
                                      {'lazy': False})

        # aggregate per product for NHAP_HN
        nhap_domain = [['location_id','in', nhap_hn_loc_ids]] if nhap_hn_loc_ids else [['id','=',0]]
        nhap_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                        'stock.quant', 'read_group',
                                        [nhap_domain, ['product_id','quantity','reserved_quantity'], ['product_id']],
                                        {'lazy': False})

        # aggregate total per product across all locations
        total_groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                         'stock.quant', 'read_group',
                                         [[], ['product_id','quantity','reserved_quantity'], ['product_id']],
                                         {'lazy': False})

        # build maps product_id -> totals
        def build_map(group_rows):
            out = {}
            for r in group_rows:
                pid = r.get('product_id') and r['product_id'][0]
                if not pid:
                    continue
                qty = safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0))
                out[pid] = out.get(pid, 0.0) + qty
            return out

        hn_map = build_map(hn_groups)
        nhap_map = build_map(nhap_groups)
        total_map = build_map(total_groups)

        # we need product names for product ids present in any map
        product_ids = list({*hn_map.keys(), *nhap_map.keys(), *total_map.keys()})
        if not product_ids:
            return None, "Không có SKU trong HN"

        # chunk product_ids for search_read (safety)
        products = []
        CHUNK = 200
        for i in range(0, len(product_ids), CHUNK):
            chunk = product_ids[i:i+CHUNK]
            prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                      'product.product', 'read',
                                      [chunk, ['id','default_code','name']])
            products.extend(prods)
        prod_map = {p['id']:{'sku': p.get('default_code') or str(p['id']), 'name': p.get('name','')} for p in products}

        # build CSV in memory
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["SKU","TenSP","TonHN","NhapHN","TongTon"])
        for pid in product_ids:
            sku = prod_map.get(pid,{}).get('sku', str(pid))
            name = prod_map.get(pid,{}).get('name','')
            tonhn = int(round(hn_map.get(pid,0)))
            nhaphn = int(round(nhap_map.get(pid,0)))
            tong = int(round(total_map.get(pid,0)))
            # only include those with HN>0 or nhap>0 maybe? But requirement: "thống kê tồn tại kho HN" -> include if tonhn>0 or nhaphn>0
            if tonhn==0 and nhaphn==0 and tong==0:
                continue
            writer.writerow([sku, name, tonhn, nhaphn, tong])
        buf.seek(0)
        return buf, None
    except Exception as e:
        logger.exception("build_thongkehn_csv error: %s", e)
        return None, str(e)

def build_dexuatnhap_csv(min_percent=20):
    """
    For each product, compute HN and HCM totals and suggest import qty = max(0, ceil(min_percent% * HCM - HN))
    Output CSV with SKU, Name, HN, HCM, SuggestedImport (only if >0)
    """
    uid, models = odoo_connect()
    if not uid:
        return None, "Không kết nối Odoo"

    try:
        # get location mapping
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                  'stock.location', 'search_read',
                                  [[], ['id','name']])
        loc_map = {l['id']: l['name'] for l in locs}
        hn_loc_ids = [lid for lid,name in loc_map.items() if classify_location(name) == "HN"]
        hcm_loc_ids = [lid for lid,name in loc_map.items() if classify_location(name) == "HCM"]

        # get per-product totals for HN and HCM and overall total
        def agg_by_locations(loc_ids):
            if not loc_ids:
                return {}
            domain = [['location_id','in', loc_ids]]
            rows = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                     'stock.quant', 'read_group',
                                     [domain, ['product_id','quantity','reserved_quantity'], ['product_id']],
                                     {'lazy': False})
            out = {}
            for r in rows:
                pid = r.get('product_id') and r['product_id'][0]
                if not pid: continue
                out[pid] = out.get(pid, 0.0) + (safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0)))
            return out

        hn_map = agg_by_locations(hn_loc_ids)
        hcm_map = agg_by_locations(hcm_loc_ids)
        # optional total_map if needed
        total_rows = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                      'stock.quant', 'read_group',
                                      [[], ['product_id','quantity','reserved_quantity'], ['product_id']],
                                      {'lazy': False})
        total_map = {}
        for r in total_rows:
            pid = r.get('product_id') and r['product_id'][0]
            if not pid: continue
            total_map[pid] = total_map.get(pid,0.0) + (safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0)))

        # assemble product ids to check
        product_ids = list({*hn_map.keys(), *hcm_map.keys(), *total_map.keys()})
        if not product_ids:
            return None, "Không có dữ liệu sản phẩm."

        # get product names in chunks
        products = []
        CHUNK=200
        for i in range(0, len(product_ids), CHUNK):
            chunk = product_ids[i:i+CHUNK]
            prods = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                      'product.product', 'read',
                                      [chunk, ['id','default_code','name']])
            products.extend(prods)
        prod_map = {p['id']:{'sku': p.get('default_code') or str(p['id']), 'name': p.get('name','')} for p in products}

        # build CSV
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["SKU","TenSP","HN","HCM","TongTon","SuggestedImport"])
        for pid in product_ids:
            hn = int(round(hn_map.get(pid,0)))
            hcm = int(round(hcm_map.get(pid,0)))
            tong = int(round(total_map.get(pid,0)))
            desired = math.ceil(hcm * (min_percent/100.0))
            suggested = max(0, desired - hn)
            if suggested > 0:
                sku = prod_map.get(pid,{}).get('sku', str(pid))
                name = prod_map.get(pid,{}).get('name','')
                writer.writerow([sku, name, hn, hcm, tong, suggested])
        buf.seek(0)
        return buf, None
    except Exception as e:
        logger.exception("build_dexuatnhap_csv error: %s", e)
        return None, str(e)

# ===================== TELEGRAM HANDLERS =====================
@dp.message_handler(commands=["start","help"])
async def cmd_start(m: types.Message):
    txt = (
        "🤖 Bot kiểm tra tồn kho trực tiếp từ Odoo.\n\n"
        "Các lệnh:\n"
        "• /ton <SKU> — Tra tồn kho realtime cho mã hàng (trả HN/HCM/nhập HN/thanh lý).\n"
        "• /tongo — Tổng tồn theo nhóm kho (HN, HCM, thanh lý, nhập HN) — tóm tắt.\n"
        "• /thongkehn — Xuất CSV thống kê SKU có tồn tại kho HN (cột: SKU, Tên, Tồn HN, Nhập HN, Tổng).\n"
        "• /dexuatnhap [minPercent] — Đề xuất nhập HN nếu HN < minPercent% của HCM (mặc định minPercent=20). Trả CSV.\n\n"
        "Lưu ý: Bot lấy 'Có hàng' = Hiện có - Reserved (tương ứng cột 'Có hàng' trên Odoo)."
    )
    await m.reply(txt)

@dp.message_handler(commands=["ton"])
async def cmd_ton(m: types.Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("Dùng: /ton <SKU>")
    sku = parts[1].strip().upper()
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

@dp.message_handler(commands=["tongo"])
async def cmd_tongo(m: types.Message):
    await m.reply("Đang tổng hợp dữ liệu... Xin chờ (có thể lâu nếu DB lớn).")
    summary, err = aggregate_totals_by_location_group()
    if err:
        return await m.reply(f"❌ Lỗi: {err}")
    total = sum(summary[k] for k in ("HN","HCM","THANHLY_HN","THANHLY_HCM","NHAP_HN"))
    text = [
        f"📊 Tổng tồn (nhóm chính): *{total:.0f}*",
        f"1️⃣ HN: {summary['HN']:.0f}",
        f"2️⃣ HCM: {summary['HCM']:.0f}",
        f"3️⃣ Nhập HN: {summary['NHAP_HN']:.0f}",
        f"4️⃣ Thanh lý HN: {summary['THANHLY_HN']:.0f}",
        f"5️⃣ Thanh lý HCM: {summary['THANHLY_HCM']:.0f}"
    ]
    if abs(summary.get("OTHER",0)) > 0.5:
        text.append(f"ℹ️ Kho khác không phân loại: {summary['OTHER']:.0f}")
    await m.reply("\n".join(text), parse_mode="Markdown")

@dp.message_handler(commands=["thongkehn"])
async def cmd_thongkehn(m: types.Message):
    await m.reply("Đang tạo báo cáo THỐNG KÊ HN... Xin chờ.")
    buf, err = build_thongkehn_csv()
    if err:
        return await m.reply(f"❌ Lỗi: {err}")
    # send as file
    await bot.send_document(chat_id=m.chat.id,
                            document=types.InputFile(io.BytesIO(buf.getvalue().encode('utf-8-sig')), filename="thongke_hn.csv"),
                            caption="Thống kê tồn kho HN (SKU, Tên, TonHN, NhapHN, TongTon)")

@dp.message_handler(commands=["dexuatnhap"])
async def cmd_dexuatnhap(m: types.Message):
    parts = m.text.split()
    min_percent = 20
    if len(parts) >= 2:
        try:
            min_percent = int(parts[1])
        except:
            pass
    await m.reply(f"Đang tính đề xuất nhập HN (minPercent={min_percent}%)... Xin chờ.")
    buf, err = build_dexuatnhap_csv(min_percent=min_percent)
    if err:
        return await m.reply(f"❌ Lỗi: {err}")
    # check if empty
    if buf.getvalue().strip().splitlines().__len__() <= 1:
        return await m.reply("Không tìm thấy SKU cần đề xuất nhập theo ngưỡng hiện tại.")
    await bot.send_document(chat_id=m.chat.id,
                            document=types.InputFile(io.BytesIO(buf.getvalue().encode('utf-8-sig')), filename=f"dexuatnhap_hn_{min_percent}pct.csv"),
                            caption=f"Đề xuất nhập HN (minPercent={min_percent}%)")

@dp.message_handler()
async def any_text(m: types.Message):
    # treat plain SKU queries
    t = m.text.strip()
    if not t or " " in t or len(t) < 2:
        return
    sku = t.strip().upper()
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

# ===================== WEBHOOK SERVER =====================
async def handle_webhook(request: web.Request):
    from aiogram import Bot as AiogramBot
    try:
        data = await request.json()
        update = types.Update(**data)
        # set current bot context so handlers using m.reply() work
        AiogramBot.set_current(bot)
        dp.bot = bot
        await dp.process_update(update)
    except Exception as e:
        logger.exception("Webhook processing error: %s", e)
    return web.Response(text="ok")

async def on_startup(app):
    try:
        await bot.set_webhook(WEBHOOK_URL)
        logger.info("✅ Webhook set: %s", WEBHOOK_URL)
    except Exception as e:
        logger.exception("set_webhook error: %s", e)

async def on_shutdown(app):
    try:
        await bot.delete_webhook()
    except:
        pass
    try:
        await bot.close()
    except:
        pass
    logger.info("🔻 Bot stopped.")

def main():
    logger.info("🚀 TONKHO_ODOO_BOT starting (aiohttp server)...")
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="ok"))
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
