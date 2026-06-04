import os
from datetime import datetime, timedelta, timezone
from fastapi import FastAPI, Request, Depends, HTTPException
from sqlalchemy import create_engine, Column, BIGINT, VARCHAR, BOOLEAN, TIMESTAMP, NUMERIC, TEXT, ForeignKey, Integer
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
import httpx

# 1. INITIALIZE ENGINES & CONFIGS
app = FastAPI(title="Business Hub Engine")

DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "placeholder_test_key")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 2. DATABASE MODELS
class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id = Column(BIGINT, primary_key=True)
    business_name = Column(VARCHAR(255))
    phone_number = Column(VARCHAR(50))
    bank_name = Column(VARCHAR(100))
    account_number = Column(VARCHAR(50))
    paystack_subaccount = Column(VARCHAR(100))
    is_active = Column(BOOLEAN, default=False)
    subscription_expiry = Column(TIMESTAMP(timezone=True))

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"))
    title = Column(VARCHAR(255))
    price = Column(NUMERIC(12, 2))
    sizes = Column(VARCHAR(255))
    quantity = Column(Integer, default=1)
    telegram_file_id = Column(TEXT)
    is_deleted = Column(BOOLEAN, default=False)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"))
    customer_name = Column(VARCHAR(255))
    customer_phone = Column(VARCHAR(50))
    delivery_address = Column(TEXT)
    product_details = Column(TEXT)
    total_amount = Column(NUMERIC(12, 2))
    your_commission = Column(NUMERIC(12, 2))
    paystack_reference = Column(VARCHAR(255), unique=True)
    payment_status = Column(VARCHAR(50), default='pending')
    created_at = Column(TIMESTAMP(timezone=True), default=lambda: datetime.now(timezone.utc))

# 3. TELEGRAM BOT HANDLERS

@dp.message(Command("start"))
async def command_start_handler(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    db = SessionLocal()
    
    try:
        # Scenario A: Customer opens a store link
        if len(args) > 1 and args[1].startswith("shop_"):
            store_name = args[1].replace("shop_", "").replace("_", " ")
            vendor = db.query(Vendor).filter(Vendor.business_name.ilike(store_name), Vendor.is_active == True).first()
            
            if vendor:
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="🛒 Open Catalog", web_app=WebAppInfo(url=f"https://yourfrontend.com/shop/{vendor.vendor_id}"))
                ]])
                await message.answer(f"Welcome to *{vendor.business_name}*! Tap below to view our active collections and place your order.", parse_mode="Markdown", reply_markup=kb)
            else:
                await message.answer("Sorry, this store is currently closed or unavailable.")
            return

        # Scenario B: Vendor opens the bot
        vendor = db.query(Vendor).filter(Vendor.vendor_id == user_id).first()
        if vendor:
            status_text = "🟢 Active" if vendor.is_active else "🔴 Inactive / Expired"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⚙️ Vendor Dashboard", web_app=WebAppInfo(url=f"https://yourfrontend.com/dashboard"))],
                [InlineKeyboardButton(text="🔗 Copy My Shop Link", callback_data="copy_link")]
            ])
            await message.answer(f"Welcome back, Boss!\nStore Status: {status_text}\n\nUse the dashboard below to manage stock or update clothes.", reply_markup=kb)
        else:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚀 Register My Business", web_app=WebAppInfo(url="https://yourfrontend.com/register"))
            ]])
            await message.answer("Welcome to *Business Hub*! Introduce your business to automated checkouts. Set up your store below to automate your inventory, orders, and local split payments.", parse_mode="Markdown", reply_markup=kb)
    finally:
        db.close()

# 4. FASTAPI DEPLOYMENT ROUTING & TELEGRAM AUTOMATION

@app.on_event("startup")
async def on_startup():
    # ⚠️ CHANGE THIS to your exact Render Web Service URL string
    MY_LIVE_RENDER_URL = "https://business-hub-backend.onrender.com" 
    
    webhook_url = f"{MY_LIVE_RENDER_URL}/telegram-webhook"
    try:
        await bot.set_webhook(url=webhook_url)
        print(f"🚀 WEBHOOK CONNECTED TO TELEGRAM: {webhook_url}")
    except Exception as e:
        print(f"❌ WEBHOOK CONNECTION FAILED: {e}")

@app.post("/telegram-webhook")
async def telegram_webhook(request: Request):
    update = types.Update.model_validate(await request.json(), context={"bot": bot})
    await dp.feed_update(bot, update=update)
    return {"status": "ok"}

@app.post("/api/v1/payments/webhook")
async def paystack_webhook(request: Request, db: Session = Depends(get_db)):
    payload = await request.json()
    event = payload.get("event")
    data = payload.get("data")
    
    if event == "charge.success":
        reference = data.get("reference")
        metadata = data.get("metadata", {})
        
        if metadata.get("type") == "vendor_subscription":
            v_id = metadata.get("vendor_id")
            vendor = db.query(Vendor).filter(Vendor.vendor_id == v_id).first()
            if vendor:
                vendor.is_active = True
                vendor.subscription_expiry = datetime.now(timezone.utc) + timedelta(days=30)
                db.commit()
                await bot.send_message(chat_id=v_id, text="🎯 Payment Verified! Your Business Hub account has been fully activated for 30 days. You can now share your link!")
                
        elif metadata.get("type") == "customer_order":
            order = db.query(Order).filter(Order.paystack_reference == reference).first()
            if order:
                order.payment_status = "success"
                db.commit()
                await bot.send_message(chat_id=order.vendor_id, text=f"🚨 *New Paid Order!*\n\nItems: {order.product_details}\nAmount: ₦{order.total_amount}\nCustomer: {order.customer_name}\nAddress: {order.delivery_address}", parse_mode="Markdown")

    return {"status": "accepted"}

# 5. PRIVATE MASTER ADMIN COMMAND
@dp.message(Command("admin"))
async def master_admin_handler(message: types.Message):
    MY_TELEGRAM_ID = 6379620342  
    if message.from_user.id != MY_TELEGRAM_ID:
        await message.answer("Unauthorized.")
        return
        
    db = SessionLocal()
    try:
        total_vendors = db.query(Vendor).count()
        active_vendors = db.query(Vendor).filter(Vendor.is_active == True).count()
        
        suc_orders = db.query(Order).filter(Order.payment_status == "success").all()
        total_commissions = sum(order.your_commission for order in suc_orders)
        
        metrics_dashboard = (
            "📊 **MASTER CONTROL PANEL**\n\n"
            f"👥 Total Registered Vendors: {total_vendors}\n"
            f"🟢 Total Active Stores: {active_vendors}\n"
            f"💰 Your Total Commissions: ₦{total_commissions:,.2f}\n"
        )
        await message.answer(metrics_dashboard, parse_mode="Markdown")
    finally:
        db.close()
