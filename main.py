import os
import logging
import hashlib
import hmac
import asyncio
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, Column, BIGINT, VARCHAR, BOOLEAN, TIMESTAMP, NUMERIC, TEXT, ForeignKey, Integer, func, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship
from sqlalchemy.exc import OperationalError
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from pydantic import BaseModel
from urllib.parse import parse_qsl
import httpx
import json

# === 1. INIT ===
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))
RENDER_URL = os.getenv("RENDER_EXTERNAL_URL")
DATABASE_URL = os.getenv("DATABASE_URL")

app = FastAPI(title="Business Hub Engine")
templates = Jinja2Templates(directory="templates")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# === 2. DATABASE ===
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id = Column(BIGINT, primary_key=True)
    business_name = Column(VARCHAR(255), unique=True, nullable=False)
    phone_number = Column(VARCHAR(20))
    paystack_subaccount = Column(VARCHAR(255))
    is_active = Column(BOOLEAN, default=False)
    subscription_expiry = Column(TIMESTAMP(timezone=True))
    commission_waived = Column(BOOLEAN, default=True)
    products = relationship("Product", back_populates="vendor")
    orders = relationship("Order", back_populates="vendor")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"))
    title = Column(VARCHAR(255), nullable=False)
    description = Column(TEXT, default="")
    price = Column(NUMERIC(10, 2), nullable=False)
    quantity = Column(Integer, default=0)
    sizes = Column(VARCHAR(255), default="One Size")
    telegram_file_id = Column(VARCHAR(255))
    is_active = Column(BOOLEAN, default=True)
    is_deleted = Column(BOOLEAN, default=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    vendor = relationship("Vendor", back_populates="products")

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"))
    customer_name = Column(VARCHAR(255))
    customer_phone = Column(VARCHAR(20))
    delivery_address = Column(TEXT)
    items = Column(TEXT) # JSON string
    total_amount = Column(NUMERIC(10, 2))
    commission = Column(NUMERIC(10, 2))
    paystack_reference = Column(VARCHAR(255), unique=True)
    status = Column(VARCHAR(50), default="pending")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    vendor = relationship("Vendor", back_populates="orders")

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# === 3. HELPERS ===
def validate_init_data(init_data: str) -> Optional:
    try:
        vals = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_check = vals.pop('hash', None)
        data_check = '\n'.join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if h!= hash_check:
            return None
        return json.loads(vals['user'])
    except Exception:
        return None

async def require_admin(request: Request):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user or user['id']!= ADMIN_TELEGRAM_ID:
        raise HTTPException(403, "Admin only")
    return user

class VendorReg(BaseModel):
    business_name: str
    phone_number: str
    bank_name: str
    account_number: str

class ProductCreate(BaseModel):
    file_id: str
    title: str
    description: str = ""
    price: float
    quantity: int
    sizes: str

class CheckoutReq(BaseModel):
    vendor_id: int
    customer_name: str
    customer_phone: str
    delivery_address: str
    items: List

# === 4. WEBAPP ROUTES ===
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

@app.get("/webapp/add-product/{file_id}", response_class=HTMLResponse)
async def add_product_webapp(request: Request, file_id: str):
    return templates.TemplateResponse(request, "add_product.html", {"file_id": file_id})

# === 5. ADMIN API ===
@app.get("/api/admin/stats")
async def admin_stats(user = Depends(require_admin), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    total_vendors = db.query(Vendor).count()
    active_vendors = db.query(Vendor).filter(Vendor.is_active == True, Vendor.subscription_expiry > now).count()
    on_trial = db.query(Vendor).filter(Vendor.commission_waived == True, Vendor.subscription_expiry > now).count()
    total_orders = db.query(Order).filter(Order.status == "paid").count()
    total_sales = db.query(func.sum(Order.total_amount)).filter(Order.status == "paid").scalar() or 0

    return {
        "total_vendors": total_vendors,
        "active_vendors": active_vendors,
        "on_trial": on_trial,
        "total_orders": total_orders,
        "total_sales": float(total_sales)
    }

# === 6. VENDOR API ===
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

@app.post("/api/vendor/products/create")
async def create_product(request: Request, data: ProductCreate, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if not vendor or not vendor.is_active or vendor.subscription_expiry < datetime.now(timezone.utc):
        raise HTTPException(403, "Subscription inactive")

    product = Product(
        vendor_id=user['id'],
        title=data.title,
        description=data.description,
        price=data.price,
        quantity=data.quantity,
        sizes=data.sizes,
        telegram_file_id=data.file_id
    )
    db.add(product)
    db.commit()
    return {"status": "created", "product_id": product.id}

@app.post("/api/vendor/register/{telegram_id}")
async def register_vendor(telegram_id: int, data: VendorReg, db: Session = Depends(get_db)):
    existing = db.query(Vendor).filter(Vendor.vendor_id == telegram_id).first()
    if existing:
        raise HTTPException(400, "Already registered")

    # Create Paystack subaccount
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/subaccount",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={
                "business_name": data.business_name,
                "settlement_bank": data.bank_name,
                "account_number": data.account_number,
                "percentage_charge": 0
            }
        )
        if res.status_code!= 200:
            raise HTTPException(400, "Bank verification failed")
        subaccount = res.json()["data"]["subaccount_code"]

    vendor = Vendor(
        vendor_id=telegram_id,
        business_name=data.business_name,
        phone_number=data.phone_number,
        paystack_subaccount=subaccount,
        is_active=True,
        subscription_expiry=datetime.now(timezone.utc) + timedelta(days=7),
        commission_waived=True
    )
    db.add(vendor)
    db.commit()
    return {"status": "success", "trial_days": 7}

@app.post("/api/vendor/subscribe/{vendor_id}")
async def subscribe_vendor(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={
                "amount": 300000, # ₦3000 in kobo
                "email": f"vendor{vendor_id}@businesshub.com",
                "metadata": {"vendor_id": vendor_id, "type": "subscription"},
                "callback_url": f"{RENDER_URL}/api/paystack/callback"
            }
        )
        return {"authorization_url": res.json()["data"]["authorization_url"]}

# === 7. SHOP API ===
@app.get("/api/shop/{vendor_id}/products")
async def get_shop_products(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor or not vendor.is_active or vendor.subscription_expiry < datetime.now(timezone.utc):
        raise HTTPException(404, "Store unavailable")

    products = db.query(Product).filter(
        Product.vendor_id == vendor_id,
        Product.is_active == True,
        Product.is_deleted == False,
        Product.quantity > 0
    ).order_by(Product.created_at.desc()).all()

    return {
        "vendor_name": vendor.business_name,
        "products": [{
            "id": p.id,
            "title": p.title,
            "description": p.description,
            "price": float(p.price),
            "quantity": p.quantity,
            "sizes": p.sizes,
            "telegram_file_id": p.telegram_file_id
        } for p in products]
    }

@app.post("/api/checkout")
async def checkout(data: CheckoutReq, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == data.vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    total = 0
    items_data = []
    for item in data.items:
        product = db.query(Product).filter(Product.id == item['product_id']).first()
        if not product or product.quantity < item['quantity']:
            raise HTTPException(400, f"Insufficient stock for {product.title}")
        total += float(product.price) * item['quantity']
        items_data.append({
            "product_id": product.id,
            "title": product.title,
            "price": float(product.price),
            "quantity": item['quantity'],
            "size": item['size']
        })

    commission = 0 if vendor.commission_waived and vendor.subscription_expiry > datetime.now(timezone.utc) else total * 0.05

    reference = f"order_{vendor.vendor_id}_{int(datetime.now().timestamp())}"
    order = Order(
        vendor_id=data.vendor_id,
        customer_name=data.customer_name,
        customer_phone=data.customer_phone,
        delivery_address=data.delivery_address,
        items=json.dumps(items_data),
        total_amount=total,
        commission=commission,
        paystack_reference=reference,
        status="pending"
    )
    db.add(order)
    db.commit()

    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://api.paystack.co/transaction/initialize",
            headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
            json={
                "amount": int(total * 100),
                "email": f"customer_{reference}@businesshub.com",
                "reference": reference,
                "subaccount": vendor.paystack_subaccount,
                "transaction_charge": int(commission * 100),
                "bearer": "subaccount",
                "callback_url": f"{RENDER_URL}/api/paystack/callback"
            }
        )
        return {"payment_url": res.json()["data"]["authorization_url"]}

# === 8. PAYSTACK WEBHOOK ===
@app.post("/api/paystack/callback")
async def paystack_callback(request: Request, db: Session = Depends(get_db)):
    signature = request.headers.get("X-Paystack-Signature")
    body = await request.body()
    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    if computed!= signature:
        raise HTTPException(400, "Invalid signature")

    event = await request.json()
    if event["event"] == "charge.success":
        data = event["data"]
        ref = data["reference"]
        metadata = data.get("metadata", {})

        if metadata.get("type") == "subscription":
            vendor_id = metadata["vendor_id"]
            vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
            if vendor:
                vendor.is_active = True
                vendor.commission_waived = False
                vendor.subscription_expiry = datetime.now(timezone.utc) + timedelta(days=30)
                db.commit()
                await bot.send_message(vendor_id, "🎉 Subscription renewed! You keep 95% per sale for 30 days.")

        else:
            order = db.query(Order).filter(Order.paystack_reference == ref).first()
            if order and order.status == "pending":
                order.status = "paid"
                # Deduct stock
                items = json.loads(order.items)
                for item in items:
                    product = db.query(Product).filter(Product.id == item["product_id"]).first()
                    if product:
                        product.quantity -= item["quantity"]
                db.commit()

                # Notify vendor
                await bot.send_message(
                    order.vendor_id,
                    f"🎉 <b>New Order Paid!</b>\n\n"
                    f"<b>Customer:</b> {order.customer_name}\n"
                    f"<b>Phone:</b> {order.customer_phone}\n"
                    f"<b>Address:</b> {order.delivery_address}\n"
                    f"<b>Items:</b> {len(items)}\n"
                    f"<b>Total:</b> ₦{order.total_amount:,.0f}\n"
                    f"<b>You Keep:</b> ₦{order.total_amount - order.commission:,.0f}\n\n"
                    f"Prepare for delivery!",
                    parse_mode="HTML"
                )

    return {"status": "ok"}

# === 9. TELEGRAM BOT ===
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
        [InlineKeyboardButton(text="📊 Vendor Dashboard", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/vendor"))]
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
        InlineKeyboardButton(
            text="✨ Fill Product Details",
            web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/add-product/{file_id}")
        )
    ]])
    await message.answer("Got your photo! Tap below to add name, price, and details:", reply_markup=kb)

# === 10. STARTUP ===
@app.on_event("startup")
async def on_startup():
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