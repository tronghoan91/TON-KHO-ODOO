# main.py
# TONKHO_ODOO_BOT – Final stable complete
# Author: (edited) Anh Hoàn
# Version: 2025-11-01
#
# Requirements:
# - aiogram==2.23.1
# - aiohttp
#
# Env vars (Render):
# BOT_TOKEN, RENDER_EXTERNAL_URL, ODOO_URL, ODOO_DB, ODOO_USER, ODOO_PASS, PORT (optional)
#
# Features:
# - /ton <SKU> -> realtime availability (quantity - reserved) by groups, suggest transfer to HN if HN < 50
# - /tongo -> summary totals by groups
# - /thongkehn -> CSV export (all products) with columns: SKU, Name, TonHN, NhapHN, Total
# - /dexuatnhap -> CSV export (all products) with columns: SKU, Name, HN, HCM, Total, SuggestedTransferToHN
# - robust location classification including "201/201", "124/124" patterns

import os
import re
import io
import csv
import math
import logging
import xmlrpc.client
from aiohttp import web
from aiogram import Bot, Dispatcher, types

# ----------------- CONFIG -----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN missing. Set BOT_TOKEN in environment.")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
if not RENDER_EXTERNAL_URL:
    # allow older name RENDER_EXTERNAL_URL or RENDER_EXTERNAL_URL not set
    RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

ODOO_URL = os.getenv("ODOO_URL", "https://erp.nguonsongviet.vn")
ODOO_DB = os.getenv("ODOO_DB", "production")
ODOO_USER = os.getenv("ODOO_USER", "kinhdoanh09@nguonsongviet.vn")
ODOO_PASS = os.getenv("ODOO_PASS", "")

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", RENDER_EXTERNAL_URL) or os.getenv("RENDER_EXTERNAL_URL", "")
if not WEBHOOK_HOST:
    # Fallback: try to read from RENDER_EXTERNAL_URL or construct (may fail)
    WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "")
WEBHOOK_HOST = WEBHOOK_HOST.rstrip("/")
WEBHOOK_PATH = f"/tg/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}" if WEBHOOK_HOST else None

PORT = int(os.getenv("PORT", "10000"))

# business rule: minimum desired stock in HN
MIN_STOCK_HN = int(os.getenv("MIN_STOCK_HN", "50"))

# logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("tonkho_bot")

# aiogram
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(bot)

# ----------------- UTIL -----------------
def safe_float(v):
    try:
        return float(v or 0.0)
    except:
        return 0.0

def extract_location_code(loc_upper: str):
    """Extract code like '201/201' -> '201' or None"""
    if not loc_upper:
        return None
    m = re.search(r'([0-9]{2,4})\/[0-9]{2,4}', loc_upper)
    if m:
        return m.group(1)
    return None

def classify_location(loc_name_raw: str):
    """
    Classify location into groups:
      - HN, HCM, THANHLY_HN, THANHLY_HCM, NHAP_HN, OTHER
    Uses name keywords and code heuristics.
    """
    if not loc_name_raw:
        return "OTHER"
    loc = re.sub(r'\s+', ' ', str(loc_name_raw).strip().upper())
    loc = loc.replace("TP HCM", "HCM").replace("TPHCM", "HCM").replace("HA NOI", "HÀ NỘI")

    # thanh lý detection
    if any(k in loc for k in ["THANH LÝ", "THANH LY", "THANH-LY", "THANHLY"]):
        if "HCM" in loc:
            return "THANHLY_HCM"
        if any(k in loc for k in ["HÀ NỘI", "HA NOI", "HN"]):
            return "THANHLY_HN"
        code = extract_location_code(loc)
        if code and code.startswith(("124","12","1")):
            return "THANHLY_HCM"
        return "THANHLY_HN"

    # incoming detection
    if any(k in loc for k in ["NHẬP","NHAP","INCOMING"]):
        if any(k in loc for k in ["HÀ NỘI","HA NOI","HN"]):
            return "NHAP_HN"
        code = extract_location_code(loc)
        if code and code.startswith(("20","2","201")):
            return "NHAP_HN"
        return "OTHER"

    # HCM first (avoid substring HN)
    if any(k in loc for k in ["HCM","KHO HCM","SHOWROOM HCM","CHI NHÁNH HCM","124/","124 "]): 
        return "HCM"
    if any(k in loc for k in ["HÀ NỘI","HA NOI","HN","KHO HÀ NỘI","KHO HA NOI","201/","201 "]):
        return "HN"

    code = extract_location_code(loc)
    if code:
        # heuristics based on your system: 124 -> HCM, 201 -> HN
        if code.startswith(("124", "12", "1")):
            return "HCM"
        if code.startswith(("201", "20", "2")):
            return "HN"
    return "OTHER"

# ----------------- ODOO CONNECT -----------------
def odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_PASS, {})
        if not uid:
            logger.error("Odoo authentication failed - check credentials.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logger.exception("Odoo connection error: %s", e)
        return None, None

# ----------------- CORE: per-SKU accurate (read_group by location_id) -----------------
def get_stock_info(sku: str):
    """
    Return a text message for SKU including:
      - total availability (sum of groups)
      - group totals (HN,HCM,NHAP_HN,THANHLY_HN,THANHLY_HCM)
      - suggestion to transfer to HN if HN < MIN_STOCK_HN
      - simplified per-location details (top locations, max 10)
    """
    uid, models = odoo_connect()
    if not uid:
        return "❌ Không thể kết nối tới Odoo."

    try:
        # find product(s)
        pids = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                 'product.product', 'search',
                                 [[['default_code', '=', sku]]])
        if not pids:
            return f"❌ Không tìm thấy mã hàng *{sku}*"

        # read_group aggregated by location_id for these products
        domain = [['product_id','in', pids]]
        groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'read_group',
                                   [domain, ['location_id','quantity','reserved_quantity'], ['location_id']],
                                   {'lazy': False})

        # map location ids to names
        loc_ids = [g['location_id'][0] for g in groups if g.get('location_id')]
        loc_map = {}
        if loc_ids:
            loc_records = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                           'stock.location', 'read',
                                           [loc_ids, ['id','name']])
            loc_map = {r['id']: r.get('name','') for r in loc_records}

        # aggregate into groups and collect simplified details
        summary = {"HN":0.0, "HCM":0.0, "THANHLY_HN":0.0, "THANHLY_HCM":0.0, "NHAP_HN":0.0, "OTHER":0.0}
        details = []  # (loc_name, available, cls)

        for g in groups:
            loc = g.get('location_id')
            if not loc:
                continue
            loc_id = loc[0]
            loc_name = loc_map.get(loc_id) or loc[1]
            qty = safe_float(g.get('quantity',0))
            reserved = safe_float(g.get('reserved_quantity',0))
            available = qty - reserved
            # tiny rounding
            if abs(available) < 1e-9:
                available = 0.0
            cls = classify_location(loc_name)
            summary[cls] = summary.get(cls, 0.0) + available
            details.append((loc_name, available, cls))

        # compute totals and suggestion
        total = sum(summary[k] for k in ("HN","HCM","THANHLY_HN","THANHLY_HCM","NHAP_HN"))
        hn = summary["HN"]
        hcm = summary["HCM"]

        # suggestion: fixed minimum model
        transfer_needed = 0
        if hn < MIN_STOCK_HN:
            transfer_needed = int(round(MIN_STOCK_HN - hn))
            if transfer_needed < 0:
                transfer_needed = 0

        # build message
        lines = [
            f"📦 *{sku}*",
            f"📊 Tổng khả dụng: *{total:.0f}*",
            f"1️⃣ Tồn kho HN: {hn:.0f}",
            f"2️⃣ Tồn kho HCM: {hcm:.0f}",
            f"3️⃣ Kho nhập HN: {summary['NHAP_HN']:.0f}",
            f"4️⃣ Kho thanh lý HN: {summary['THANHLY_HN']:.0f}",
            f"5️⃣ Kho thanh lý HCM: {summary['THANHLY_HCM']:.0f}",
        ]

        if transfer_needed > 0:
            lines.append(f"\n💡 Đề xuất chuyển thêm *{transfer_needed} sp* ra HN để đạt tối thiểu {MIN_STOCK_HN}.")
        else:
            lines.append("\n✅ Tồn HN đạt mức tối thiểu, không cần chuyển thêm hàng.")

        # simplified details - show only non-zero avail, top 10 by avail
        shown = [d for d in details if abs(d[1]) >= 1.0]
        if shown:
            shown.sort(key=lambda x: -x[1])
            lines.append("")
            lines.append("🔍 Chi tiết theo vị trí:")
            for lname, avail, cls in shown[:10]:
                # print short: name: qty (GROUP)
                lines.append(f"- {lname}: {int(round(avail))} ({cls})")
            # if more OTHER exist, summarize
            other_sum = sum(a for _, a, c in shown if c == "OTHER")
            if abs(other_sum) >= 1.0 and not any("OTHER" in line for line in lines[-10:]):
                lines.append(f"- Nhóm khác: {int(round(other_sum))} (OTHER)")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("get_stock_info error: %s", e)
        return f"❌ Lỗi khi đọc tồn: {e}"

# ----------------- AGGREGATIONS / REPORT BUILDERS -----------------
def aggregate_totals_by_location_group():
    """
    Return summary dict or (None, error)
    """
    uid, models = odoo_connect()
    if not uid:
        return None, "Không kết nối Odoo"
    try:
        groups = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                   'stock.quant', 'read_group',
                                   [[], ['location_id','quantity','reserved_quantity'], ['location_id']],
                                   {'lazy': False})
        loc_ids = [g['location_id'][0] for g in groups if g.get('location_id')]
        loc_map = {}
        if loc_ids:
            loc_records = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                           'stock.location', 'read',
                                           [loc_ids, ['id','name']])
            loc_map = {r['id']: r.get('name','') for r in loc_records}
        summary = {"HN":0.0, "HCM":0.0, "THANHLY_HN":0.0, "THANHLY_HCM":0.0, "NHAP_HN":0.0, "OTHER":0.0}
        for g in groups:
            loc = g.get('location_id')
            if not loc:
                continue
            lid = loc[0]
            lname = loc_map.get(lid) or loc[1]
            qty = safe_float(g.get('quantity',0))
            reserved = safe_float(g.get('reserved_quantity',0))
            avail = qty - reserved
            cls = classify_location(lname)
            summary[cls] = summary.get(cls, 0.0) + avail
        return summary, None
    except Exception as e:
        logger.exception("aggregate_totals_by_location_group error: %s", e)
        return None, str(e)

def build_thongkehn_csv():
    """
    Build CSV buffer (StringIO) containing ALL products with columns:
      SKU, Name, TonHN, NhapHN, Total
    Strategy:
      - Use read_group to get aggregated per-product totals over location groups (HN and NHAP_HN)
      - Use read_group for total across all locations for product totals
      - Then fetch product metadata by chunks
    """
    uid, models = odoo_connect()
    if not uid:
        return None, "Không kết nối Odoo"
    try:
        # get all locations and classify which IDs belong to HN and NHAP_HN
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                  'stock.location', 'search_read',
                                  [[], ['id','name']], {'limit': 0})
        loc_map = {l['id']: l['name'] for l in locs}
        hn_loc_ids = [lid for lid,name in loc_map.items() if classify_location(name) == "HN"]
        nhap_hn_loc_ids = [lid for lid,name in loc_map.items() if classify_location(name) == "NHAP_HN"]

        # aggregated per product for HN
        hn_map = {}
        if hn_loc_ids:
            hn_rows = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                       'stock.quant', 'read_group',
                                       [[['location_id','in', hn_loc_ids]], ['product_id','quantity','reserved_quantity'], ['product_id']],
                                       {'lazy': False})
            for r in hn_rows:
                pid = r.get('product_id') and r['product_id'][0]
                if not pid: continue
                hn_map[pid] = hn_map.get(pid, 0.0) + (safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0)))
        # aggregated per product for NHAP_HN
        nhap_map = {}
        if nhap_hn_loc_ids:
            nhap_rows = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                         'stock.quant', 'read_group',
                                         [[['location_id','in', nhap_hn_loc_ids]], ['product_id','quantity','reserved_quantity'], ['product_id']],
                                         {'lazy': False})
            for r in nhap_rows:
                pid = r.get('product_id') and r['product_id'][0]
                if not pid: continue
                nhap_map[pid] = nhap_map.get(pid, 0.0) + (safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0)))

        # total per product across all locations
        total_rows = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                       'stock.quant', 'read_group',
                                       [[], ['product_id','quantity','reserved_quantity'], ['product_id']],
                                       {'lazy': False})
        total_map = {}
        for r in total_rows:
            pid = r.get('product_id') and r['product_id'][0]
            if not pid: continue
            total_map[pid] = total_map.get(pid, 0.0) + (safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0)))

        # all product ids present
        product_ids = list({*total_map.keys(), *hn_map.keys(), *nhap_map.keys()})
        if not product_ids:
            return None, "Không có sản phẩm để xuất báo cáo."

        # fetch product meta in chunks
        products = []
        CHUNK = 300
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
        writer.writerow(["SKU","TenSP","TonHN","NhapHN","TongTon"])
        for pid in product_ids:
            sku = prod_map.get(pid,{}).get('sku', str(pid))
            name = prod_map.get(pid,{}).get('name','')
            tonhn = int(round(hn_map.get(pid,0)))
            nhaphn = int(round(nhap_map.get(pid,0)))
            tong = int(round(total_map.get(pid,0)))
            writer.writerow([sku, name, tonhn, nhaphn, tong])
        buf.seek(0)
        return buf, None
    except Exception as e:
        logger.exception("build_thongkehn_csv error: %s", e)
        return None, str(e)

def build_dexuatnhap_csv():
    """
    Build CSV containing: SKU, Name, HN, HCM, Total, SuggestedToTransferToHN
    Suggestion = max(0, MIN_STOCK_HN - HN)
    Output includes ALL products present in stock.quants (could be large).
    """
    uid, models = odoo_connect()
    if not uid:
        return None, "Không kết nối Odoo"
    try:
        # map locations
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                  'stock.location', 'search_read',
                                  [[], ['id','name']], {'limit': 0})
        loc_map = {l['id']: l['name'] for l in locs}
        hn_loc_ids = [lid for lid,name in loc_map.items() if classify_location(name) == "HN"]
        hcm_loc_ids = [lid for lid,name in loc_map.items() if classify_location(name) == "HCM"]

        # aggregate per product for HN and HCM and total
        def agg_by_loc_ids(loc_ids):
            out = {}
            if not loc_ids:
                return out
            rows = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                    'stock.quant', 'read_group',
                                    [[['location_id','in', loc_ids]], ['product_id','quantity','reserved_quantity'], ['product_id']],
                                    {'lazy': False})
            for r in rows:
                pid = r.get('product_id') and r['product_id'][0]
                if not pid: continue
                out[pid] = out.get(pid, 0.0) + (safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0)))
            return out

        hn_map = agg_by_loc_ids(hn_loc_ids)
        hcm_map = agg_by_loc_ids(hcm_loc_ids)

        total_rows = models.execute_kw(ODOO_DB, uid, ODOO_PASS,
                                       'stock.quant', 'read_group',
                                       [[], ['product_id','quantity','reserved_quantity'], ['product_id']],
                                       {'lazy': False})
        total_map = {}
        for r in total_rows:
            pid = r.get('product_id') and r['product_id'][0]
            if not pid: continue
            total_map[pid] = total_map.get(pid, 0.0) + (safe_float(r.get('quantity',0)) - safe_float(r.get('reserved_quantity',0)))

        product_ids = list({*total_map.keys(), *hn_map.keys(), *hcm_map.keys()})
        if not product_ids:
            return None, "Không có sản phẩm."

        # fetch product meta in chunks
        products = []
        CHUNK = 300
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
        writer.writerow(["SKU","TenSP","HN","HCM","TongTon","SuggestedTransferToHN"])
        for pid in product_ids:
            hn = int(round(hn_map.get(pid,0)))
            hcm = int(round(hcm_map.get(pid,0)))
            tong = int(round(total_map.get(pid,0)))
            suggested = max(0, MIN_STOCK_HN - hn)
            sku = prod_map.get(pid,{}).get('sku', str(pid))
            name = prod_map.get(pid,{}).get('name','')
            writer.writerow([sku, name, hn, hcm, tong, suggested])
        buf.seek(0)
        return buf, None
    except Exception as e:
        logger.exception("build_dexuatnhap_csv error: %s", e)
        return None, str(e)

# ----------------- TELEGRAM HANDLERS -----------------
@dp.message_handler(commands=["start","help"])
async def cmd_start(m: types.Message):
    txt = (
        "🤖 BOT KIỂM TRA TỒN KHO (Odoo Realtime)\n\n"
        "Các lệnh:\n"
        "• /ton <MÃ_HÀNG> — Tra tồn realtime và đề xuất chuyển ra HN nếu HN < MIN (50).\n"
        "• /tongo — Tổng tồn theo nhóm (HN, HCM, Nhập HN, Thanh lý).\n"
        "• /thongkehn — Xuất CSV toàn bộ sản phẩm: SKU, Tên, TonHN, NhapHN, TongTon.\n"
        "• /dexuatnhap — Xuất CSV đề xuất nhập/chuyển cho HN (cột SuggestedTransferToHN).\n\n"
        f"Ngưỡng tối thiểu tồn HN: {MIN_STOCK_HN} sản phẩm.\n"
        "Lưu ý: 'Có hàng' = Hiện có - Reserved."
    )
    await m.reply(txt)

@dp.message_handler(commands=["ton"])
async def cmd_ton(m: types.Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        return await m.reply("Dùng: /ton <MÃ_HÀNG>")
    sku = parts[1].strip().upper()
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

@dp.message_handler(commands=["tongo"])
async def cmd_tongo(m: types.Message):
    await m.reply("Đang tổng hợp dữ liệu... Xin chờ.")
    summary, err = aggregate_totals_by_location_group()
    if err:
        return await m.reply(f"❌ Lỗi: {err}")
    total = sum(summary[k] for k in ("HN","HCM","THANHLY_HN","THANHLY_HCM","NHAP_HN"))
    lines = [
        f"📊 Tổng tồn (nhóm chính): *{total:.0f}*",
        f"1️⃣ HN: {summary['HN']:.0f}",
        f"2️⃣ HCM: {summary['HCM']:.0f}",
        f"3️⃣ Nhập HN: {summary['NHAP_HN']:.0f}",
        f"4️⃣ Thanh lý HN: {summary['THANHLY_HN']:.0f}",
        f"5️⃣ Thanh lý HCM: {summary['THANHLY_HCM']:.0f}"
    ]
    if abs(summary.get("OTHER",0.0)) >= 1.0:
        lines.append(f"ℹ️ Kho khác: {summary['OTHER']:.0f}")
    await m.reply("\n".join(lines), parse_mode="Markdown")

@dp.message_handler(commands=["thongkehn"])
async def cmd_thongkehn(m: types.Message):
    await m.reply("Đang tạo file thống kê (toàn bộ sản phẩm)... Xin chờ — việc này có thể mất chút thời gian.")
    buf, err = build_thongkehn_csv()
    if err:
        return await m.reply(f"❌ Lỗi: {err}")
    data = buf.getvalue().encode('utf-8-sig')
    await bot.send_document(chat_id=m.chat.id,
                            document=types.InputFile(io.BytesIO(data), filename="thongke_hn.csv"),
                            caption="Thống kê tồn kho HN (TOÀN BỘ sản phẩm)")

@dp.message_handler(commands=["dexuatnhap"])
async def cmd_dexuatnhap(m: types.Message):
    await m.reply("Đang tạo file đề xuất nhập/chuyển cho HN... Xin chờ.")
    buf, err = build_dexuatnhap_csv()
    if err:
        return await m.reply(f"❌ Lỗi: {err}")
    data = buf.getvalue().encode('utf-8-sig')
    await bot.send_document(chat_id=m.chat.id,
                            document=types.InputFile(io.BytesIO(data), filename="dexuatnhap_hn.csv"),
                            caption=f"Đề xuất chuyển/nhập ra HN (MIN {MIN_STOCK_HN})")

@dp.message_handler()
async def any_text(m: types.Message):
    t = m.text.strip()
    if not t or " " in t:
        return
    sku = t.strip().upper()
    res = get_stock_info(sku)
    await m.reply(res, parse_mode="Markdown")

# ----------------- WEBHOOK SERVER -----------------
async def handle_webhook(request: web.Request):
    from aiogram import Bot as AiogramBot
    try:
        data = await request.json()
        update = types.Update(**data)
        # set context so handlers using m.reply() work
        AiogramBot.set_current(bot)
        dp.bot = bot
        await dp.process_update(update)
    except Exception as e:
        logger.exception("Webhook processing error: %s", e)
    return web.Response(text="ok")

async def on_startup(app):
    if WEBHOOK_URL:
        try:
            await bot.set_webhook(WEBHOOK_URL)
            logger.info("✅ Webhook set: %s", WEBHOOK_URL)
        except Exception as e:
            logger.exception("set_webhook error: %s", e)
    else:
        logger.warning("WEBHOOK_URL not configured; webhook won't be set.")

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
