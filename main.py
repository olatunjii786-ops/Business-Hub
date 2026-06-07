import logging
import traceback
import httpx
from utils import validate_init_data
from models import Product  # add this to your imports at the top
from fastapi import HTTPException  # add this too
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from config import *
from database import run_migrations, SessionLocal
from utils import validate_init_data
from models import Vendor

from routes import admin, vendor, shop, custom

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Business Hub Engine")
templates = Jinja2Templates(directory="templates")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

app.include_router(admin.router)
app.include_router(vendor.router)
app.include_router(shop.router)
app.include_router(custom.router)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global error: {exc}")
    logger.error(traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": f"Server error: {str(exc)}"})

# === TELEGRAM FILE PROXY ===
@app.get("/file/{file_id}")
async def get_telegram_file(file_id: str):
    """Proxy Telegram images so we don't expose bot token in HTML"""
    try:
        async with httpx.AsyncClient() as client:
            # Step 1: Get file_path from Telegram
            res = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}", timeout=10.0)
            if res.status_code!= 200:
                logger.error(f"getFile failed: {res.text}")
                return Response(status_code=404)
            
            file_path = res.json()["result"]["file_path"]
            
            # Step 2: Download actual file
            file_res = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=10.0)
            if file_res.status_code!= 200:
                return Response(status_code=404)
                
            return Response(content=file_res.content, media_type="image/jpeg")
    except Exception as e:
        logger.error(f"File proxy error: {e}")
        return Response(status_code=500)

# === TELEGRAM HANDLERS ===
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("shop_"):
        slug = args[1].replace("shop_", "")
        with SessionLocal() as db:
            vendor = db.query(Vendor).filter(Vendor.business_name.ilike(slug.replace("_", " "))).first()
            if vendor:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🛍️ Open Shop", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/shop/{vendor.vendor_id}"))
                ]])
                await message.answer(f"Welcome to <b>{vendor.business_name}</b>!", reply_markup=kb)
                return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Register Business", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/register"))],
        [InlineKeyboardButton(text="📊 Vendor Dashboard", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/vendor"))],
        [InlineKeyboardButton(text="🛍️ Browse Stores", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/marketplace"))]
    ])
    await message.answer(
        "Welcome to <b>Business Hub</b> 🚀\n\n"
        "Sell on Telegram. Get paid instantly.\n"
        "7 days free. Keep 100% of sales.\n\n"
        "Tap below to start:",
        reply_markup=kb
    )

@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id!= ADMIN_TELEGRAM_ID:
        await message.answer("Admin only")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 Open Dashboard", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/admin"))
    ]])
    await message.answer("Admin panel:", reply_markup=kb)

@dp.message(F.photo)
async def handle_product_photo(message: types.Message):
    user_id = message.from_user.id
    with SessionLocal() as db:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == user_id).first()
        if not vendor:
            await message.answer("Register first with /start")
            return

    file_id = message.photo[-1].file_id
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✨ Fill Product Details", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/add-product/{file_id}"))
    ]])
    await message.answer("Got your photo! Tap below to add name, price, and details:", reply_markup=kb)

# === WEBAPP ROUTES ===
@app.get("/webapp/admin", response_class=HTMLResponse)
async def admin_webapp(request: Request):
    return templates.TemplateResponse(request, "admin.html")

@app.get("/webapp/vendor", response_class=HTMLResponse)
async def vendor_webapp(request: Request):
    return templates.TemplateResponse(request, "vendor.html")

@app.get("/webapp/register", response_class=HTMLResponse)
async def register_webapp(request: Request):
    return templates.TemplateResponse(request, "register.html")

@app.get("/webapp/shop/{vendor_id}", response_class=HTMLResponse)
async def shop_webapp(request: Request, vendor_id: int):
    return templates.TemplateResponse(request, "shop.html")

@app.get("/webapp/marketplace", response_class=HTMLResponse)
async def marketplace_webapp(request: Request):
    return templates.TemplateResponse(request, "customer.html")

@app.get("/webapp/add-product/{file_id}", response_class=HTMLResponse)
async def add_product_webapp(request: Request, file_id: str):
    return templates.TemplateResponse(request, "add_product.html", {"file_id": file_id})

# === STARTUP ===
@app.on_event("startup")
async def on_startup():
    run_migrations()
    webhook_url = f"{RENDER_URL}/webhook"
    await bot.set_webhook(webhook_url)
    logger.info(f"Webhook set to {webhook_url}")

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "Business Hub Engine Running"}

@app.get("/api/marketplace/products")
async def get_marketplace_products():
    """Public endpoint for customers to browse all products"""
    try:
        with SessionLocal() as db:
            products = db.query(Product).join(Vendor).filter(
                Vendor.is_active == True
            ).all()
            
            result = []
            for p in products:
                result.append({
                    "id": p.id,
                    "title": p.title,
                    "price": float(p.price),
                    "quantity": getattr(p, 'quantity', 1),  # defaults to 1 if column missing
                    "telegram_file_id": getattr(p, 'telegram_file_id', None),
                    "vendor_id": p.vendor.vendor_id,
                    "vendor_name": p.vendor.business_name
                })
            return result
    except Exception as e:
        logger.error(f"Marketplace products error: {e}")
        return []
        
@app.post("/api/orders/create")
async def create_order(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data or not validate_init_data(init_data, BOT_TOKEN):
        raise HTTPException(401)

    user = parse_user(init_data)
    data = await request.json()

    conn = get_db()
    cur = conn.cursor()

    # Group items by vendor
    vendors = {}
    total = 0
    for item in data['items']:
        cur.execute("SELECT vendor_id, price, title FROM products WHERE id =?", (item['product_id'],))
        p = cur.fetchone()
        if not p:
            continue
        vid = p['vendor_id']
        if vid not in vendors:
            vendors[vid] = {'items': [], 'total': 0}
        item_total = item['price'] * item['quantity']
        vendors[vid]['items'].append({
            'title': p['title'],
            'qty': item['quantity'],
            'price': item['price'],
            'total': item_total
        })
        vendors[vid]['total'] += item_total
        total += item_total

    # Create one order per vendor
    for vid, vdata in vendors.items():
        cur.execute("""
            INSERT INTO orders (vendor_id, customer_id, customer_name, customer_phone,
                              delivery_address, total_amount, items_json, status, created_at)
            VALUES (?,?,?,?,?,?,?, 'pending', datetime('now'))
        """, (vid, user['id'], data['customer_name'], data['customer_phone'],
              data['delivery_address'], vdata['total'], json.dumps(vdata['items'])))

        order_id = cur.lastrowid

        # Notify vendor via bot
        cur.execute("SELECT telegram_id, business_name FROM vendors WHERE id =?", (vid,))
        vendor = cur.fetchone()
        if vendor:
            items_text = "\n".join([f"• {i['qty']}x {i['title']} — ₦{i['total']:,}" for i in vdata['items']])
            msg = f"""🎉 New Order #{order_id}

Customer: {data['customer_name']}
Phone: {data['customer_phone']}
Address: {data['delivery_address']}

Items:
{items_text}

Total: ₦{vdata['total']:,}

Customer will pay you directly. Confirm payment then mark as delivered in your dashboard."""

            try:
                await bot.send_message(vendor['telegram_id'], msg)
            except Exception as e:
                print(f"Failed to notify vendor: {e}")

    conn.commit()
    conn.close()
    return {"success": True}
    
@app.get("/api/vendor/products")
async def get_vendor_products(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(401, "Unauthorized")
    
    logger.info(f"Vendor {user['id']} requesting products")
    
    with SessionLocal() as db:
        products = db.query(Product).filter(Product.vendor_id == user['id']).all()
        logger.info(f"Found {len(products)} products for vendor {user['id']}")
        
        return [
            {
                "id": p.id,
                "title": p.title,
                "price": float(p.price),
                "quantity": p.quantity,
                "is_active": p.is_active,
                "telegram_file_id": p.telegram_file_id
            }
            for p in products
        ]

@app.get("/api/vendor/me")
async def get_vendor_me(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(401)
    
    with SessionLocal() as db:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
        if not vendor:
            raise HTTPException(404, "Vendor not found")
        
        from datetime import datetime
        days_left = 0
        if vendor.subscription_expiry:
            days_left = (vendor.subscription_expiry - datetime.now(vendor.subscription_expiry.tzinfo)).days
        
        return {
            "vendor_id": vendor.vendor_id,
            "business_name": vendor.business_name,
            "days_left": max(0, days_left),
            "on_trial": vendor.commission_waived
        }
        
@app.delete("/api/products/{product_id}")
async def delete_product(product_id: int, request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(401)
    
    with SessionLocal() as db:
        product = db.query(Product).filter(Product.id == product_id).first()
        if not product:
            raise HTTPException(404)
        if product.vendor_id != user['id']:
            raise HTTPException(403)
        
        db.delete(product)
        db.commit()
    return {"success": True}
