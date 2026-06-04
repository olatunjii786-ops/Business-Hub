import os
import logging
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import List, Optional
from fastapi import FastAPI, Request, Depends, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, Column, BIGINT, VARCHAR, BOOLEAN, TIMESTAMP, NUMERIC, TEXT, ForeignKey, Integer, func, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session, relationship
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import OperationalError
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from pydantic import BaseModel
import httpx

# 1. LOGGING & INIT
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Business Hub Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Replace with your TMA domain in prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. DATABASE - FIXED FOR RENDER + SUPABASE
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL env var missing")

# Use Session Pooler URL from Supabase + force SSL + timeout
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    connect_args={
        "sslmode": "require",
        "connect_timeout": 10
    }
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN env var missing")

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY")
PLATFORM_COMMISSION = 0.05

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 3. DATABASE MODELS
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

# 4. PYDANTIC SCHEMAS
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

# 5. HELPERS
async def create_paystack_subaccount(vendor: Vendor) -> str:
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
    hash = hmac.new(PAYSTACK_SECRET_KEY.encode(), payload, hashlib.sha512).hexdigest()
    return hash == signature

# 6. TELEGRAM HANDLERS WITH ERROR HANDLING
@dp.message(Command("start"))
async def command_start_handler(message: types.Message):
    try:
        user_id = message.from_user.id
        args = message.text.split()

        with SessionLocal() as db:
            # Scenario A: Customer opens store link
            if len(args) > 1 and args[1].startswith("shop_"):
                store_slug = args[1].replace("shop_", "").replace("_", " ")
                vendor = db.query(Vendor).filter(
                    func.lower(Vendor.business_name) == store_slug.lower(),
                    Vendor.is_active == True,
                    Vendor.subscription_expiry > datetime.now(timezone.utc)
                ).first()

                if vendor:
                    kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="🛒 Open Catalog",
                            web_app=WebAppInfo(url=f"https://yourfrontend.com/shop/{vendor.vendor_id}")
                        )
                    ]])
                    await message.answer(
                        f"Welcome to *{vendor.business_name}*!\n\nTap below to view our collection and order.",
                        parse_mode="Markdown", reply_markup=kb
                    )
                else:
                    await message.answer("Sorry, this store is currently closed or subscription expired.")
                return

            # Scenario B: Vendor flow
            vendor = db.query(Vendor).filter(Vendor.vendor_id == user_id).first()
            if vendor:
                if vendor.is_active and vendor.subscription_expiry and vendor.subscription_expiry > datetime.now(timezone.utc):
                    status = "🟢 Active"
                    expiry = vendor.subscription_expiry.strftime("%d %b %Y")
                else:
                    status = "🔴 Inactive - Renew Subscription"
                    expiry = "Expired"

                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚙️ Vendor Dashboard", web_app=WebAppInfo(url="https://yourfrontend.com/dashboard"))],
                    [InlineKeyboardButton(text="🔗 Get My Shop Link", callback_data=f"getlink_{vendor.vendor_id}")]
                ])
                await message.answer(
                    f"Welcome back, Boss!\n\nStore Status: {status}\nExpiry: {expiry}\n\nManage products and view orders below.",
                    reply_markup=kb
                )
            else:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🚀 Register My Business", web_app=WebAppInfo(url="https://yourfrontend.com/register"))
                ]])
                await message.answer(
                    "Welcome to *Business Hub*! 🚀\n\nAutomate your fashion store inside Telegram. Register below to activate split payments, inventory, and auto-checkouts.",
                    parse_mode="Markdown", reply_markup=kb
                )

    except OperationalError as e:
        logger.error(f"DB error on /start: {e}")
        await message.answer("⚠️ Database is starting up. Please try again in 10 seconds.")
    except Exception as e:
        logger.error(f"Unexpected error in /start: {e}")
        await message.answer("Something went wrong. Please try again.")

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
                await callback.message.answer(f"Your shop link:\n`{link}`\n\nShare this on Instagram, WhatsApp, or Facebook.", parse_mode="Markdown")
        await callback.answer()
    except Exception as e:
        logger.error(f"Error in getlink: {e}")
        await callback.answer("Error generating link", show_alert=True)

@dp.message(Command("admin"))
async def master_admin_handler(message: types.Message):
    MY_TELEGRAM_ID = 6379620342
    if message.from_user.id!= MY_TELEGRAM_ID:
        return

    try:
        with SessionLocal() as db:
            total_vendors = db.query(Vendor).count()
            active_vendors = db.query(Vendor).filter(
                Vendor.is_active == True,
                Vendor.subscription_expiry > datetime.now(timezone.utc)
            ).count()
            revenue = db.query(func.sum(Order.your_commission)).filter(Order.payment_status == "success").scalar() or 0

            await message.answer(
                f"📊 **MASTER CONTROL PANEL**\n\n"
                f"👥 Total Vendors: {total_vendors}\n"
                f"🟢 Active Stores: {active_vendors}\n"
                f"💰 Your Total Commissions: ₦{revenue:,.2f}",
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Admin panel error: {e}")
        await message.answer("Error loading dashboard")

# 7. FASTAPI ROUTES
@app.get("/health")
async def healthcheck():
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "db": "connected"}
    except Exception as e:
        return {"status": "error", "db": str(e)}

@app.post("/api/vendor/register/{telegram_id}")
async def register_vendor(telegram_id: int, data: VendorRegister, db: Session = Depends(get_db)):
    existing = db.query(Vendor).filter(Vendor.vendor_id == telegram_id).first()
    if existing:
        raise HTTPException(400, "Vendor already registered")

    vendor = Vendor(vendor_id=telegram_id, **data.dict())
    db.add(vendor)
    db.commit()

    try:
        subaccount_code = await create_paystack_subaccount(vendor)
        vendor.paystack_subaccount = subaccount_code
        db.commit()
    except Exception as e:
        logger.error(f"Subaccount failed: {e}")

    return {"status": "registered", "next_step": "pay_subscription"}

@app.post("/api/vendor/subscribe/{telegram_id}")
async def init_subscription(telegram_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == telegram_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    url = "https://api.paystack.co/transaction/initialize"
    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    payload = {
        "email": f"vendor{telegram_id}@businesshub.ng",
        "amount": 200000,
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
    if not vendor or not vendor.is_active:
        raise HTTPException(403, "Subscription inactive")

    db_product = Product(vendor_id=vendor_id, **product.dict())
    db.add(db_product)
    db.commit()
    return {"status": "added", "product_id": db_product.id}

@app.post("/api/checkout")
async def customer_checkout(data: CheckoutRequest, db: Session = Depends(get_db)):
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

# 8. WEBHOOKS
@app.on_event("startup")
async def on_startup():
    RENDER_URL = os.getenv("RENDER_EXTERNAL_URL", "https://business-hub-9rce.onrender.com")
    webhook_url = f"{RENDER_URL}/telegram-webhook"
    await bot.delete_webhook(drop_pending_updates=True)
    result = await bot.set_webhook(url=webhook_url, allowed_updates=["message", "callback_query"])
    logger.info(f"Webhook set: {result} | URL: {webhook_url}")
    webhook_info = await bot.get_webhook_info()
    logger.info(f"Webhook info: {webhook_info}")

@app.on_event("shutdown")
async def on_shutdown():
    await bot.session.close()

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    await dp.feed_webhook_update(bot, update)
    return {"ok": True}

@app.get("/set_webhook_debug")
async def debug_webhook():
    info = await bot.get_webhook_info()
    return {
        "url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_date": info.last_error_date,
        "last_error_message": info.last_error_message
    }

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
                db.commit()
                await bot.send_message(v_id, "🎯 Payment Verified! Your store is live for 30 days. Share your link to start selling!")

        elif metadata.get("type") == "customer_order":
            order_id = int(metadata.get("order_id"))
            order = db.query(Order).filter(Order.id == order_id).first()
            if order:
                order.payment_status = "success"
                db.commit()
                items_text = "\n".join([f"- {i['qty']}x {i['title']} ({i['size']})" for i in order.product_details])
                await bot.send_message(
                    order.vendor_id,
                    f"🚨 *New Paid Order #{order.id}*\n\n{items_text}\n\n"
                    f"💰 Amount: ₦{order.vendor_payout:,.2f} (after 5% cut)\n"
                    f"👤 {order.customer_name}\n📞 {order.customer_phone}\n📍 {order.delivery_address}",
                    parse_mode="Markdown"
                )
    return {"status": "ok"}
