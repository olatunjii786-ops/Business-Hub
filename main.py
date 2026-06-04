import os
import logging
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, BIGINT, VARCHAR, BOOLEAN, TIMESTAMP, NUMERIC, TEXT, ForeignKey, Integer, func, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from pydantic import BaseModel
from urllib.parse import parse_qsl
import httpx
import json

# 1. LOGGING & INIT
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Business Hub Engine")
templates = Jinja2Templates(directory="templates")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. CONFIG FROM RENDER ENV VARS
DATABASE_URL = os.getenv("DATABASE_URL")
BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")

if not DATABASE_URL or not BOT_TOKEN:
    raise ValueError("DATABASE_URL and BOT_TOKEN are required in Render env vars")

PLATFORM_COMMISSION = 0.05

# 3. DATABASE
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={"sslmode": "require", "connect_timeout": 10}
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 4. DATABASE MODELS - WITH 7-DAY TRIAL
class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id = Column(BIGINT, primary_key=True)
    business_name = Column(VARCHAR(255), unique=True, nullable=False)
    phone_number = Column(VARCHAR(50))
    bank_name = Column(VARCHAR(100))
    account_number = Column(VARCHAR(50))
    paystack_subaccount = Column(VARCHAR(100))
    is_active = Column(BOOLEAN, default=False)
    subscription_expiry = Column(TIMESTAMP(timezone=True))
    commission_waived = Column(BOOLEAN, default=False)
    trial_used = Column(BOOLEAN, default=False)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    products = relationship("Product", back_populates="vendor")
    orders = relationship("Order", back_populates="vendor")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"), nullable=False)
    title = Column(VARCHAR(255), nullable=False)
    description = Column(TEXT)
    price = Column(NUMERIC(12, 2), nullable=False)
    sizes = Column(VARCHAR(255))
    quantity = Column(Integer, default=1)
    telegram_file_id = Column(TEXT)
    supabase_image_url = Column(TEXT)
    is_active = Column(BOOLEAN, default=True)
    is_deleted = Column(BOOLEAN, default=False)
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    vendor = relationship("Vendor", back_populates="products")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"), nullable=False)
    customer_telegram_id = Column(BIGINT)
    customer_name = Column(VARCHAR(255))
    customer_phone = Column(VARCHAR(50))
    delivery_address = Column(TEXT)
    product_details = Column(JSONB)
    subtotal_amount = Column(NUMERIC(12, 2))
    total_amount = Column(NUMERIC(12, 2))
    your_commission = Column(NUMERIC(12, 2))
    vendor_payout = Column(NUMERIC(12, 2))
    paystack_reference = Column(VARCHAR(255), unique=True)
    payment_status = Column(VARCHAR(50), default='pending')
    order_status = Column(VARCHAR(50), default='paid')
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))
    vendor = relationship("Vendor", back_populates="orders")

# 5. PYDANTIC SCHEMAS
class VendorRegister(BaseModel):
    business_name: str
    phone_number: str
    bank_name: str
    account_number: str

class ProductCreate(BaseModel):
    title: str
    description: Optional[str] = None
    price: float
    sizes: str
    quantity: int
    telegram_file_id: str

class CartItem(BaseModel):
    product_id: int
    quantity: int
    size: str

class CheckoutRequest(BaseModel):
    vendor_id: int
    customer_name: str
    customer_phone: str
    delivery_address: str
    items: List[CartItem]

# 6. HELPERS
def validate_init_data(init_data: str):
    try:
        parsed = dict(parse_qsl(init_data))
        hash_check = parsed.pop('hash')
        data_check = '\n'.join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        hash_calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if hash_calc!= hash_check:
            return None
        return json.loads(parsed['user'])
    except:
        return None

def require_admin(request: Request):
    if ADMIN_TELEGRAM_ID == 0:
        raise HTTPException(500, "ADMIN_TELEGRAM_ID not set in env")
    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data:
        raise HTTPException(403, "Missing auth")
    user = validate_init_data(init_data)
    if not user or user['id']!= ADMIN_TELEGRAM_ID:
        raise HTTPException(403, "Admin only")
    return user

async def create_paystack_subaccount(vendor: Vendor) -> str:
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(500, "PAYSTACK_SECRET_KEY not set. Add it in Render env vars.")
    url = "https://api.paystack.co/subaccount"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}", "Content-Type": "application/json"}
    payload = {
        "business_name": vendor.business_name,
        "settlement_bank": vendor.bank_name,
        "account_number": vendor.account_number,
        "percentage_charge": PLATFORM_COMMISSION * 100
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        data = r.json()
        if data.get("status"):
            return data["data"]["subaccount_code"]
        raise HTTPException(400, f"Paystack subaccount failed: {data.get('message')}")

def verify_paystack_signature(payload: bytes, signature: str) -> bool:
    if not PAYSTACK_SECRET_KEY:
        return False
    hash = hmac.new(PAYSTACK_SECRET_KEY.encode(), payload, hashlib.sha512).hexdigest()
    return hash == signature

# 7. TELEGRAM HANDLERS
@dp.message(Command("start"))
async def command_start_handler(message: types.Message):
    try:
        user_id = message.from_user.id
        args = message.text.split()
        with SessionLocal() as db:
            if len(args) > 1 and args[1].startswith("shop_"):
                store_slug = args[1].replace("shop_", "").replace("_", " ")
                vendor = db.query(Vendor).filter(
                    func.lower(Vendor.business_name) == store_slug.lower(),
                    Vendor.is_active == True,
                    Vendor.subscription_expiry > datetime.now(timezone.utc)
                ).first()
                if vendor:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="🛒 Open Catalog", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/shop/{vendor.vendor_id}"))
                    ]])
                    await message.answer(f"Welcome to *{vendor.business_name}*!\n\nTap below to view our collection.", parse_mode="Markdown", reply_markup=kb)
                else:
                    await message.answer("Sorry, this store is currently closed or subscription expired.")
                return

            vendor = db.query(Vendor).filter(Vendor.vendor_id == user_id).first()
            if vendor:
                now = datetime.now(timezone.utc)
                on_trial = vendor.commission_waived and vendor.subscription_expiry > now
                if vendor.is_active and vendor.subscription_expiry > now:
                    status = "🟢 Active" + (" (7-DAY FREE)" if on_trial else "")
                    expiry = vendor.subscription_expiry.strftime("%d %b %Y")
                else:
                    status = "🔴 Inactive - Renew Subscription"
                    expiry = "Expired"
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚙️ Vendor Dashboard", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/vendor"))],
                    [InlineKeyboardButton(text="🔗 Get My Shop Link", callback_data=f"getlink_{vendor.vendor_id}")]
                ])
                await message.answer(f"Welcome back, Boss!\n\nStore Status: {status}\nExpiry: {expiry}", reply_markup=kb)
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🚀 Register My Business", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/register"))
                ]])
                await message.answer("Welcome to *Business Hub*! 🚀\n\nRegister to get 7 DAYS FREE - keep 100% of sales.", parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logger.error(f"Error in /start: {e}")
        await message.answer("Something went wrong. Try again.")

@dp.callback_query(lambda c: c.data.startswith("getlink_"))
async def send_shop_link(callback: types.CallbackQuery):
    try:
        vendor_id = int(callback.data.split("_")[1])
        with SessionLocal() as db:
            vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
            if vendor:
                slug = vendor.business_name.lower().replace(" ", "_")
                bot_info = await bot.get_me()
                link = f"https://t.me/{bot_info.username}?start=shop_{slug}"
                await callback.message.answer(f"Your shop link:\n`{link}`\n\nShare this!", parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in getlink: {e}")
        await callback.answer("Error generating link", show_alert=True)

@dp.message(Command("admin"))
async def master_admin_handler(message: types.Message):
    if message.from_user.id!= ADMIN_TELEGRAM_ID:
        return
    markup = types.InlineKeyboardMarkup(inline_keyboard=[[
        types.InlineKeyboardButton(text="📊 Open Dashboard", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/admin"))
    ]])
    await message.answer("BusinessHub Admin Panel", reply_markup=markup)

# 8. FASTAPI ROUTES
@app.get("/health")
async def healthcheck():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}

# === ADMIN WEBAPP ===
@app.get("/webapp/admin", response_class=HTMLResponse)
async def admin_webapp(request: Request):
    return templates.TemplateResponse(request, "admin.html")

# === VENDOR WEBAPP === ← START PASTE HERE
@app.get("/webapp/vendor", response_class=HTMLResponse)
async def vendor_webapp(request: Request):
    return templates.TemplateResponse(request, "vendor.html")

@app.get("/webapp/register", response_class=HTMLResponse)
async def register_webapp(request: Request):
    return templates.TemplateResponse(request, "register.html")

@app.get("/webapp/shop/{vendor_id}", response_class=HTMLResponse)
async def shop_webapp(request: Request, vendor_id: int):
    return templates.TemplateResponse(request, "shop.html")

@app.get("/webapp/add-product/{file_id}", response_class=HTMLResponse)
async def add_product_webapp(request: Request, file_id: str):
    return templates.TemplateResponse(request, "add_product.html", {"file_id": file_id})

@app.post("/api/vendor/products/create")
async def create_product_with_form(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")
    
    data = await request.json()
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if not vendor or not vendor.is_active or vendor.subscription_expiry < datetime.now(timezone.utc):
        raise HTTPException(403, "Subscription inactive")
    
    product = Product(
        vendor_id=user['id'],
        title=data['title'],
        description=data.get('description', ''),
        price=float(data['price']),
        quantity=int(data['quantity']),
        sizes=data['sizes'],
        telegram_file_id=data['file_id']
    )
    db.add(product)
    db.commit()
    return {"status": "created", "product_id": product.id}

# Replace your old photo handler with this
@dp.message(lambda m: m.photo and m.from_user)
async def handle_product_photo(message: types.Message):
    user_id = message.from_user.id
    with SessionLocal() as db:
        vendor = db.query(Vendor).filter(Vendor.vendor_id == user_id).first()
        if not vendor:
            await message.answer("Register first with /start")
            return
    
    file_id = message.photo[-1].file_id
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="✨ Fill Product Details", 
            web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/add-product/{file_id}")
        )
    ]])
    await message.answer("Got your photo! Tap below to add name, price, and details:", reply_markup=kb)

# === VENDOR API ENDPOINTS ===
@app.get("/api/vendor/me")
async def get_my_vendor(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data:
        raise HTTPException(403, "Missing auth")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")
    
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if not vendor:
        raise HTTPException(404, "Not registered")
    
    now = datetime.now(timezone.utc)
    on_trial = vendor.commission_waived and vendor.subscription_expiry > now
    days_left = max(0, (vendor.subscription_expiry - now).days) if vendor.subscription_expiry > now else 0
    
    return {
        "vendor_id": vendor.vendor_id,
        "business_name": vendor.business_name,
        "is_active": vendor.is_active,
        "subscription_expiry": vendor.subscription_expiry.isoformat() if vendor.subscription_expiry else None,
        "days_left": days_left,
        "on_trial": on_trial,
        "has_subaccount": bool(vendor.paystack_subaccount)
    }

@app.get("/api/vendor/products")
async def get_my_products(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")
    
    products = db.query(Product).filter(
        Product.vendor_id == user['id'],
        Product.is_deleted == False
    ).order_by(Product.created_at.desc()).all()
    
    return [{"id": p.id, "title": p.title, "price": float(p.price), "quantity": p.quantity, "is_active": p.is_active} for p in products]
# === END PASTE HERE ===

@app.get("/api/admin/stats")
async def admin_stats(user = Depends(require_admin), db: Session = Depends(get_db)):
    
    vendors = db.query(Vendor).all()
    total = len(vendors)
    now = datetime.now(timezone.utc)
    trial = sum(1 for v in vendors if v.commission_waived and v.subscription_expiry > now)
    active = sum(1 for v in vendors if v.is_active and v.subscription_expiry > now)
    revenue = db.query(func.sum(Order.your_commission)).filter(Order.payment_status == "success").scalar() or 0
    return {"total_vendors": total, "active_vendors": active, "trial_vendors": trial, "your_revenue": float(revenue)}

@app.get("/api/admin/vendors")
async def admin_vendors(user = Depends(require_admin), db: Session = Depends(get_db)):
    vendors = db.query(Vendor).order_by(Vendor.created_at.desc()).all()
    now = datetime.now(timezone.utc)
    result = []
    for v in vendors:
        expiry = v.subscription_expiry
        days_left = max(0, (expiry - now).days) if expiry and expiry > now else 0
        on_trial = v.commission_waived and expiry > now
        result.append({
            "id": v.vendor_id,
            "business_name": v.business_name,
            "phone": v.phone_number,
            "is_active": v.is_active,
            "days_left": days_left,
            "on_trial": on_trial
        })
    return result

@app.post("/api/admin/vendors/{vendor_id}/approve")
async def approve_vendor(vendor_id: int, user = Depends(require_admin), db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if vendor:
        vendor.is_active = True
        db.commit()
    return {"status": "approved"}

# === VENDOR REGISTRATION - 7 DAY FREE TRIAL ===
@app.post("/api/vendor/register/{telegram_id}")
async def register_vendor(telegram_id: int, data: VendorRegister, db: Session = Depends(get_db)):
    existing = db.query(Vendor).filter(Vendor.vendor_id == telegram_id).first()
    if existing:
        raise HTTPException(400, "Vendor already registered")
    
    vendor = Vendor(
        vendor_id=telegram_id,
        **data.dict(),
        is_active=True,
        subscription_expiry=datetime.now(timezone.utc) + timedelta(days=7),
        commission_waived=True,
        trial_used=True
    )
    db.add(vendor)
    db.commit()
    
    if PAYSTACK_SECRET_KEY:
        try:
            subaccount_code = await create_paystack_subaccount(vendor)
            vendor.paystack_subaccount = subaccount_code
            db.commit()
        except Exception as e:
            logger.error(f"Subaccount failed: {e}")
    
    return {"status": "registered", "trial_days": 7, "message": "7-day free trial activated"}

@app.post("/api/vendor/subscribe/{telegram_id}")
async def init_subscription(telegram_id: int, db: Session = Depends(get_db)):
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(500, "PAYSTACK_SECRET_KEY not set")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == telegram_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")
    url = "https://api.paystack.co/transaction/initialize"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    payload = {
        "email": f"vendor{telegram_id}@businesshub.ng",
        "amount": 300000,
        "metadata": {"type": "vendor_subscription", "vendor_id": telegram_id}
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        return r.json()["data"]

@app.get("/api/shop/{vendor_id}/products")
async def get_vendor_products(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(
        Vendor.vendor_id == vendor_id,
        Vendor.is_active == True,
        Vendor.subscription_expiry > datetime.now(timezone.utc)
    ).first()
    if not vendor:
        raise HTTPException(404, "Store inactive")
    products = db.query(Product).filter(
        Product.vendor_id == vendor_id,
        Product.is_active == True,
        Product.is_deleted == False,
        Product.quantity > 0
    ).all()
    return {"vendor_name": vendor.business_name, "products": products}

@app.post("/api/vendor/{vendor_id}/products")
async def add_product(vendor_id: int, product: ProductCreate, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor or not vendor.is_active or vendor.subscription_expiry < datetime.now(timezone.utc):
        raise HTTPException(403, "Subscription inactive")
    db_product = Product(vendor_id=vendor_id, **product.dict())
    db.add(db_product)
    db.commit()
    return {"status": "added", "product_id": db_product.id}

# === CHECKOUT - 0% COMMISSION DURING TRIAL ===
@app.post("/api/checkout")
async def customer_checkout(data: CheckoutRequest, db: Session = Depends(get_db)):
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(500, "PAYSTACK_SECRET_KEY not set")
    vendor = db.query(Vendor).filter(Vendor.vendor_id == data.vendor_id).first()
    if not vendor or not vendor.paystack_subaccount:
        raise HTTPException(400, "Vendor cannot accept payments yet")
    
    subtotal = 0
    items_json = []
    for item in data.items:
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product or product.quantity < item.quantity:
            raise HTTPException(400, f"Product out of stock")
        subtotal += float(product.price) * item.quantity
        items_json.append({"product_id": item.product_id, "title": product.title, "qty": item.quantity, "size": item.size})
        product.quantity -= item.quantity
    
    now = datetime.now(timezone.utc)
    on_trial = vendor.commission_waived and vendor.subscription_expiry > now
    
    if on_trial:
        commission = 0
        vendor_payout = subtotal
    else:
        commission = round(subtotal * PLATFORM_COMMISSION, 2)
        vendor_payout = round(subtotal - commission, 2)
    
    total = subtotal
    order = Order(
        vendor_id=data.vendor_id,
        customer_name=data.customer_name,
        customer_phone=data.customer_phone,
        delivery_address=data.delivery_address,
        product_details=items_json,
        subtotal_amount=subtotal,
        total_amount=total,
        your_commission=commission,
        vendor_payout=vendor_payout
    )
    db.add(order)
    db.commit()
    
    url = "https://api.paystack.co/transaction/initialize"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    payload = {
        "email": f"customer{order.id}@businesshub.ng",
        "amount": int(total * 100),
        "subaccount": vendor.paystack_subaccount,
        "bearer": "subaccount",
        "metadata": {"type": "customer_order", "order_id": order.id}
    }
    async with httpx.AsyncClient() as client:
        r = await client.post(url, json=payload, headers=headers)
        pay_data = r.json()["data"]
        order.paystack_reference = pay_data["reference"]
        db.commit()
    return {"payment_url": pay_data["authorization_url"], "order_id": order.id}

# 9. WEBHOOKS
@app.on_event("startup")
async def on_startup():
    if not RENDER_URL:
        logger.warning("RENDER_EXTERNAL_URL not set. Webhook not configured.")
        return
    webhook_url = f"{RENDER_URL}/telegram-webhook"
    await bot.delete_webhook(drop_pending_updates=True)
    result = await bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"])
    logger.info(f"Webhook set: {result} | URL: {webhook_url}")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.post("/api/v1/payments/webhook")
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(None), db: Session = Depends(get_db)):
    body = await request.body()
    if not verify_paystack_signature(body, x_paystack_signature):
        raise HTTPException(400, "Invalid signature")
    payload = await request.json()
    event = payload.get("event")
    data = payload.get("data")
    if event == "charge.success":
        metadata = data.get("metadata", {})
        if metadata.get("type") == "vendor_subscription":
            v_id = int(metadata.get("vendor_id"))
            vendor = db.query(Vendor).filter(Vendor.vendor_id == v_id).first()
            if vendor:
                vendor.is_active = True
                vendor.subscription_expiry = datetime.now(timezone.utc) + timedelta(days=30)
                vendor.commission_waived = False
                db.commit()
                await bot.send_message(v_id, "🎯 Payment Verified! Your store is live for 30 days. You now keep 95% per sale.")
        elif metadata.get("type") == "customer_order":
            order_id = int(metadata.get("order_id"))
            order = db.query(Order).filter(Order.id == order_id).first()
            if order:
                order.payment_status = "success"
                db.commit()
                vendor = db.query(Vendor).filter(Vendor.vendor_id == order.vendor_id).first()
                on_trial = vendor.commission_waived and vendor.subscription_expiry > datetime.now(timezone.utc)
                payout_msg = f"💰 Amount: ₦{order.vendor_payout:,.2f} (100% - Trial Bonus)" if on_trial else f"💰 Amount: ₦{order.vendor_payout:,.2f} (after 5% cut)"
                items_text = "\n".join([f"- {i['qty']}x {i['title']} ({i['size']})" for i in order.product_details])
                await bot.send_message(
                    order.vendor_id,
                    f"🚨 *New Paid Order #{order.id}*\n\n{items_text}\n\n{payout_msg}\n👤 {order.customer_name}\n📞 {order.customer_phone}\n📍 {order.delivery_address}",
                    parse_mode="Markdown"
                )
    return {"status": "ok"}
