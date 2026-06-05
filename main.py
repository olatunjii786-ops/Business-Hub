import logging
import traceback
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse # Added HTMLResponse
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

# Import all route modules - add new ones here
from routes import admin, vendor, shop, custom

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Business Hub Engine")
templates = Jinja2Templates(directory="templates")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Register all routers - your new endpoints auto-load
app.include_router(admin.router)
app.include_router(vendor.router)
app.include_router(shop.router)
app.include_router(custom.router)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Global error: {exc}")
    logger.error(traceback.format_exc())
    return JSONResponse(status_code=500, content={"detail": f"Server error: {str(exc)}"})

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
        InlineKeyboardButton(text="✨ Fill Product Details", web_app=WebAppInfo(url=f"{RENDER_URL}/webapp/add-product/{file_id}"))
    ]])
    await message.answer("Got your photo! Tap below to add name, price, and details:", reply_markup=kb)

# === WEBAPP ROUTES - Keep these in main.py ===
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

# === STARTUP ===
@app.on_event("startup")
async def on_startup():
    run_migrations() # Auto-fixes DB schema - no shell needed
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
