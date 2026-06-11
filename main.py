import os
import json
import hmac
import hashlib
import httpx
import base64
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import parse_qsl
from pydantic import BaseModel
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, Column, Integer, String, Float, Text, BigInteger, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, Session

# --- CONFIGURATION & ENVIRONMENT SETUP ---
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = os.getenv("ADMIN_TELEGRAM_ID")
APP_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
# Using FLW_SECRET_HASH to match your dashboard variable
FLW_SECRET_HASH = os.getenv("FLW_SECRET_HASH", "BusinessHubSecureHash2026")

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: Environment variables TELEGRAM_BOT_TOKEN or DATABASE_URL are missing!")

BOT_USERNAME = "isaacbusinessbot"

app = FastAPI(title="Business Hub Central Engine")
templates = Jinja2Templates(directory="templates")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- DATABASE ENGINE CONFIGURATION ---
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- RELATIONAL DATA MODELS ---
class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id = Column(BigInteger, primary_key=True)
    business_name = Column(String(255), nullable=False)
    bio = Column(Text, nullable=True)
    phone_number = Column(String(20), nullable=False)
    logo_url = Column(Text, nullable=True)
    is_approved = Column(Boolean, default=True)
    bank_code = Column(String(50), nullable=True)
    account_number = Column(String(50), nullable=True)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(BigInteger, nullable=False)
    title = Column(String(200), nullable=False)
    price = Column(Float, nullable=False)
    quantity = Column(Integer, default=1)
    sizes = Column(String(255), nullable=True)
    category = Column(String(100), default="General")
    image_url = Column(Text, nullable=True)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(BigInteger, nullable=False)
    customer_id = Column(BigInteger, nullable=False)
    customer_name = Column(String(200), nullable=False)
    customer_phone = Column(String(20), nullable=False)
    delivery_address = Column(Text, nullable=False)
    items = Column(Text, default="[]")
    total_amount = Column(Float, nullable=False)
    order_code = Column(String(100), unique=True, nullable=False)
    status = Column(String(50), default="pending")

Base.metadata.create_all(bind=engine)

# --- PYNANTIC SCHEMAS ---
class CheckoutItem(BaseModel):
    product_id: int
    quantity: int
    size: str

class CheckoutRequest(BaseModel):
    customer_name: str
    customer_phone: str
    delivery_address: str
    items: List[CheckoutItem]

class PayoutUpgradePayload(BaseModel):
    bank_code: str
    account_number: str

class StatusPayload(BaseModel):
    status: str

# --- TELEGRAM BOT UTILS ---
async def send_telegram_alert(chat_id: int, message: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        async with httpx.AsyncClient() as client:
            await client.post(url, json=payload, timeout=5.0)
    except Exception as e:
        print(f"Async Alert failure: {e}")

def validate_telegram_auth(init_data: str) -> Optional[dict]:
    if not init_data:
        return None
    try:
        vals = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_check = vals.pop('hash', None)
        data_check = '\n'.join(f"{k}={v}" for k, v in sorted(vals.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        h = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if h != hash_check:
            return None
        return json.loads(vals['user'])
    except Exception:
        return None

# --- ROUTE TEMPLATE ENDPOINTS ---
@app.get("/")
async def root_redirect():
    return RedirectResponse(url="/shop")

@app.get("/shop")
async def serve_shop(request: Request):
    return templates.TemplateResponse(request=request, name="shop.html")

# --- FIXED SECTION: Aliased /webapp/register to fix the vendor 404 ---
@app.get("/vendor")
@app.get("/webapp/register")
async def serve_vendor(request: Request):
    return templates.TemplateResponse(request=request, name="vendor.html")

# --- TELEGRAM BOT WEBHOOK ROUTER ---
@app.post("/webhook")
async def telegram_webhook_endpoint(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
        if "message" in payload:
            message = payload["message"]
            chat_id = message["chat"]["id"]
            user_text = message.get("text", "").strip()
            
            if user_text.startswith("/start"):
                parts = user_text.split(" ", 1)
                if len(parts) > 1 and parts[1].isdigit():
                    target_vendor_id = parts[1]
                    welcome_msg = (
                        f"🛍 *Welcome to Business Hub!*\n\n"
                        f"You have opened a direct merchant boutique page.\n\n"
                        f"👉 Tap the button below to view their active catalog elements!"
                    )
                    keyboard = {
                        "inline_keyboard": [[
                            {
                                "text": "🌐 Open Boutique Storefront",
                                "web_app": {"url": f"{APP_URL}/shop?startapp={target_vendor_id}"}
                            }
                        ]]
                    }
                else:
                    welcome_msg = (
                        f"👋 *Welcome to the Business Hub Ecosystem!*\n\n"
                        f"Are you a customer ready to shop top-tier products, or a vendor looking to manage your boutique automation?\n\n"
                        f"Launch your workspace window instantly using the control deck below:"
                    )
                    keyboard = {
                        "inline_keyboard": [
                            [{"text": "🛍 Open Global Marketplace", "web_app": {"url": f"{APP_URL}/shop"}}],
                            [{"text": "🛠 Open Vendor Workspace Console", "web_app": {"url": f"{APP_URL}/vendor"}}]
                        ]
                    }
                
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                async with httpx.AsyncClient() as client:
                    await client.post(url, json={
                        "chat_id": chat_id,
                        "text": welcome_msg,
                        "parse_mode": "Markdown",
                        "reply_markup": keyboard
                    }, timeout=5.0)
                    
        return {"status": "ok"}
    except Exception as e:
        print(f"Webhook error: {e}")
        return {"status": "error", "detail": str(e)}

# --- ENDPOINTS CONFIGURATION ---
@app.get("/api/vendor/me")
async def verify_vendor_session(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Session check broken")
    
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if not vendor:
        return {"registered": False}
    
    has_payout = bool(vendor.bank_code and vendor.account_number)
    
    return {
        "registered": True,
        "has_payout_setup": has_payout,
        "business_name": vendor.business_name,
        "bio": vendor.bio,
        "phone_number": vendor.phone_number,
        "logo_url": vendor.logo_url,
        "direct_link": f"https://t.me/{BOT_USERNAME}/app?startapp={vendor.vendor_id}"
    }

@app.post("/api/vendor/register")
async def register_or_edit_vendor(
    request: Request,
    business_name: str = Form(...),
    bio: str = Form(""),
    phone_number: str = Form(...),
    bank_code: Optional[str] = Form(None),
    account_number: Optional[str] = Form(None),
    logo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Security auth missing")
    
    logo_data_url = None
    if logo and logo.filename:
        file_contents = await logo.read()
        encoded = base64.b64encode(file_contents).decode("utf-8")
        logo_data_url = f"data:{logo.content_type};base64,{encoded}"

    clean_phone = "".join(c for c in phone_number if c.isdigit())
    if clean_phone.startswith("0") and len(clean_phone) == 11:
        clean_phone = "234" + clean_phone[1:]

    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if vendor:
        vendor.business_name = business_name
        vendor.bio = bio
        vendor.phone_number = clean_phone
        if logo_data_url:
            vendor.logo_url = logo_data_url
    else:
        if not bank_code or not account_number:
            raise HTTPException(status_code=400, detail="Bank settlement properties required for registration.")
            
        if len(account_number) != 10 or not account_number.isdigit():
            raise HTTPException(status_code=400, detail="Invalid NUBAN Account Number. Must be exactly 10 digits.")

        vendor = Vendor(
            vendor_id=user['id'], 
            business_name=business_name, 
            bio=bio, 
            phone_number=clean_phone, 
            logo_url=logo_data_url,
            bank_code=bank_code,
            account_number=account_number
        )
        db.add(vendor)
        
    db.commit()
    return {"success": True}

@app.post("/api/vendor/upgrade-payout")
async def upgrade_legacy_vendor_payout(
    payload: PayoutUpgradePayload,
    request: Request,
    db: Session = Depends(get_db)
):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Validation trace rejected.")
        
    vendor = db.query(Vendor).filter(Vendor.vendor_id == user['id']).first()
    if not vendor:
        raise HTTPException(status_code=404, detail="Vendor account missing.")
        
    if len(payload.account_number) != 10 or not payload.account_number.isdigit():
        raise HTTPException(status_code=400, detail="Invalid account formatting sequence.")
        
    vendor.bank_code = payload.bank_code
    vendor.account_number = payload.account_number
    db.commit()
    
    return {"success": True}

@app.post("/api/products")
async def create_new_product(
    request: Request,
    title: str = Form(...),
    price: float = Form(...),
    quantity: int = Form(...),
    sizes: str = Form(""),
    category: str = Form("General"),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid request parameters")
    
    image_data_url = None
    if image and image.filename:
        file_contents = await image.read()
        encoded = base64.b64encode(file_contents).decode("utf-8")
        image_data_url = f"data:{image.content_type};base64,{encoded}"

    new_product = Product(
        vendor_id=user['id'], 
        title=title, 
        price=price, 
        quantity=quantity, 
        sizes=sizes, 
        category=category.strip(), 
        image_url=image_data_url
    )
    db.add(new_product)
    db.commit()
    return {"success": True}

@app.post("/api/products/{product_id}/edit")
async def modify_product_entry(
    product_id: int,
    request: Request,
    title: str = Form(...),
    price: float = Form(...),
    quantity: int = Form(...),
    sizes: str = Form(""),
    category: str = Form("General"),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db)
):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Auth trace invalid")
        
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user['id']).first()
    if not product:
        raise HTTPException(status_code=404, detail="Product missing")
        
    product.title = title
    product.price = price
    product.quantity = quantity
    product.sizes = sizes
    product.category = category.strip()
    
    if image and image.filename:
        file_contents = await image.read()
        encoded = base64.b64encode(file_contents).decode("utf-8")
        image_data_url = f"data:{image.content_type};base64,{encoded}"
        product.image_url = image_data_url
        
    db.commit()
    return {"success": True}

@app.delete("/api/products/{product_id}")
async def remove_catalog_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Validation failed")
        
    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user['id']).first()
    if not product:
        raise HTTPException(status_code=404, detail="Target missing")
        
    db.delete(product)
    db.commit()
    return {"success": True}

@app.get("/api/vendor/products")
async def list_vendor_products(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Unauthorized")
    
    products = db.query(Product).filter(Product.vendor_id == user['id']).order_by(Product.id.desc()).all()
    return [{"id": p.id, "title": p.title, "price": p.price, "quantity": p.quantity, "sizes": p.sizes, "category": p.category, "image_url": p.image_url} for p in products]

@app.get("/api/marketplace/configs")
async def load_storefront_configuration(vendor_id: Optional[int] = None, db: Session = Depends(get_db)):
    if vendor_id:
        v = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
        if not v:
            return {"mode": "marketplace", "vendors": [], "products": []}
        products = db.query(Product).filter(Product.vendor_id == vendor_id).order_by(Product.id.desc()).all()
        return {
            "mode": "store",
            "vendor": {"name": v.business_name, "bio": v.bio, "logo": v.logo_url, "id": v.vendor_id},
            "products": [{"id": p.id, "title": p.title, "price": p.price, "quantity": p.quantity, "sizes": p.sizes, "category": p.category, "image_url": p.image_url} for p in products]
        }
    
    vendors = db.query(Vendor).all()
    products = db.query(Product).order_by(Product.id.desc()).limit(150).all()
    return {
        "mode": "marketplace",
        "vendors": [{"id": ven.vendor_id, "name": ven.business_name, "logo": ven.logo_url} for ven in vendors],
        "products": [{"id": p.id, "vendor_id": p.vendor_id, "title": p.title, "price": p.price, "quantity": p.quantity, "sizes": p.sizes, "category": p.category, "image_url": p.image_url} for p in products]
    }

# --- REFACTORED COHERENT PIPELINE ROUTE ---
@app.post("/api/checkout")
async def run_checkout_pipeline(req: CheckoutRequest, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Session expired")

    if not req.items:
        raise HTTPException(status_code=400, detail="Cart manifest is empty")

    first_id = req.items[0].product_id
    sample = db.query(Product).filter(Product.id == first_id).first()
    if not sample:
        raise HTTPException(status_code=404, detail="Product not listed")
    
    target_vendor_id = sample.vendor_id
    vendor_profile = db.query(Vendor).filter(Vendor.vendor_id == target_vendor_id).first()
    vendor_phone = vendor_profile.phone_number if vendor_profile else "2348000000000"

    calculated_total = 0.0
    items_summary = []

    # Read-Only Verification (Deferred Stock Adjustment)
    for item in req.items:
        prod = db.query(Product).filter(Product.id == item.product_id).first()
        if not prod or prod.quantity < item.quantity:
            raise HTTPException(status_code=400, detail=f"Stock limit exceeded for {prod.title if prod else 'Item'}")
        
        calculated_total += (prod.price * item.quantity)
        items_summary.append({"product_id": prod.id, "title": prod.title, "price": prod.price, "quantity": item.quantity, "size": item.size})

    timestamp = int(datetime.now(timezone.utc).timestamp())
    generated_code = f"BH-{target_vendor_id}-{user['id']}-{timestamp}"

    new_order = Order(
        vendor_id=target_vendor_id, customer_id=user['id'], customer_name=req.customer_name,
        customer_phone=req.customer_phone, delivery_address=req.delivery_address,
        items=json.dumps(items_summary), total_amount=calculated_total, order_code=generated_code, status="pending"
    )
    db.add(new_order)
    db.commit()

    return {"success": True, "order_code": generated_code, "total_amount": calculated_total, "vendor_phone": vendor_phone}

# --- REAL-TIME SECURE GATEWAY WEBHOOK HUB ---
@app.post("/api/v1/payments/webhook")
async def flutterwave_payment_webhook(request: Request, db: Session = Depends(get_db)):
    flw_signature = request.headers.get("verif-hash")
    if not flw_signature or flw_signature != FLW_SECRET_HASH:
        raise HTTPException(status_code=401, detail="Webhook signature check verification trace failed.")

    payload = await request.json()
    
    if payload.get("status") == "successful" or payload.get("data", {}).get("status") == "successful":
        tx_data = payload.get("data", {})
        target_order_code = tx_data.get("tx_ref")
        
        order = db.query(Order).filter(Order.order_code == target_order_code).first()
        if not order:
            return {"status": "ignored", "reason": "Order missing."}
            
        if order.status == "pending":
            try:
                # Atomically Deduct Inventory on Payment Confirmation
                loaded_items = json.loads(order.items)
                for item in loaded_items:
                    prod = db.query(Product).filter(Product.id == item["product_id"]).first()
                    if prod:
                        prod.quantity = max(0, prod.quantity - item["quantity"])
                
                order.status = "confirmed"
                db.commit()
                
                alert_message = (
                    f"💳 *PAYMENT SECURED & VERIFIED!*\n\n"
                    f"🛍 *Order Code:* `{order.order_code}`\n"
                    f"💰 *Total Value:* ₦{order.total_amount:,.2f}\n"
                    f"👤 *Customer:* {order.customer_name}\n\n"
                    f"Verification successfully completed via Flutterwave engine layers."
                )
                await send_telegram_alert(order.vendor_id, alert_message)
                
                buyer_alert = f"✅ *YOUR PAYMENT WAS CONFIRMED!*\n\nCode: `{order.order_code}`\nStatus: *CONFIRMED*"
                await send_telegram_alert(order.customer_id, buyer_alert)
                
            except Exception as e:
                db.rollback()
                print(f"Error inside processing pipeline context: {e}")
                raise HTTPException(status_code=500, detail="Internal transactional sync error.")
                
    return {"status": "processed"}

@app.get("/api/customer/orders")
async def view_customer_orders(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Unauthorized")

    orders = db.query(Order).filter(Order.customer_id == user['id']).order_by(Order.id.desc()).all()
    return [{"id": o.id, "order_code": o.order_code, "total_amount": o.total_amount, "status": o.status, "items": json.loads(o.items)} for o in orders]

@app.post("/api/customer/orders/{order_id}/cancel")
async def user_cancel_pending_order(order_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Invalid context")
        
    order = db.query(Order).filter(Order.id == order_id, Order.customer_id == user['id']).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order missing")
        
    if order.status != "pending":
        raise HTTPException(status_code=400, detail="Cannot cancel confirmed order")
        
    order.status = "cancelled"
    db.commit()
    
    cancel_alert = f"⚠️ *ORDER CANCELLED BY CUSTOMER*\n\nOrder Code: `{order.order_code}`"
    await send_telegram_alert(order.vendor_id, cancel_alert)
    return {"success": True}

@app.get("/api/vendor/orders")
async def view_incoming_vendor_orders(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Unauthorized")

    orders = db.query(Order).filter(Order.vendor_id == user['id']).order_by(Order.id.desc()).all()
    return [{"id": o.id, "order_code": o.order_code, "customer_name": o.customer_name, "customer_phone": o.customer_phone, "delivery_address": o.delivery_address, "total_amount": o.total_amount, "status": o.status, "items": json.loads(o.items)} for o in orders]

@app.post("/api/orders/{order_id}/status")
async def adjust_order_lifecycle(order_id: int, status_payload: StatusPayload, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_telegram_auth(init_data)
    if not user:
        raise HTTPException(status_code=403, detail="Credentials missing")

    order = db.query(Order).filter(Order.id == order_id, Order.vendor_id == user['id']).first()
    if not order:
         raise HTTPException(404, "Order not found")

    next_status = status_payload.status
    order.status = next_status
    db.commit()
    
    status_emoji = "✅" if next_status == "confirmed" else "🚚" if next_status == "delivered" else "❌"
    user_alert = f"{status_emoji} *YOUR ORDER HAS BEEN UPDATED!*\n\nCode: `{order.order_code}`\nNew Status: *{next_status.upper()}*"
    await send_telegram_alert(order.customer_id, user_alert)
    return {"success": True}
