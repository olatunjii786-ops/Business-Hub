from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
from typing import Optional
import json
import httpx
import logging
from pydantic import BaseModel
from database import get_db
from models import Vendor, Product, Order
from utils import validate_init_data
from config import PAYSTACK_SECRET_KEY, ENABLE_PAYSTACK, TRIAL_DAYS

router = APIRouter(prefix="/api/vendor", tags=["vendor"])
logger = logging.getLogger(__name__)

class VendorReg(BaseModel):
    business_name: str
    business_description: Optional[str] = ""
    phone_number: str
    bank_name: str
    account_number: str
    logo_file_id: Optional[str] = None

class ProductCreate(BaseModel):
    file_id: str
    title: str
    description: str = ""
    price: float
    quantity: int
    sizes: str = ""

@router.get("/me")
async def get_vendor_me(request: Request, db: Session = Depends(get_db)):
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
    on_trial = vendor.commission_waived and vendor.subscription_expiry and vendor.subscription_expiry > now
    days_left = max(0, (vendor.subscription_expiry - now).days) if vendor.subscription_expiry and vendor.subscription_expiry > now else 0

    return {
        "vendor_id": vendor.vendor_id,
        "business_name": vendor.business_name,
        "business_description": vendor.business_description or "",
        "logo_file_id": vendor.logo_file_id,
        "is_active": vendor.is_active,
        "subscription_expiry": vendor.subscription_expiry.isoformat() if vendor.subscription_expiry else None,
        "days_left": days_left,
        "on_trial": on_trial,
        "has_subaccount": bool(vendor.paystack_subaccount)
    }

@router.post("/register/{telegram_id}")
async def register_vendor(telegram_id: int, data: VendorReg, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user or user['id']!= telegram_id:
        raise HTTPException(403, "Cannot register for another user")
    
    logger.info(f"Registration attempt for {telegram_id}: {data.business_name}")
    
    existing = db.query(Vendor).filter(Vendor.vendor_id == telegram_id).first()
    if existing:
        raise HTTPException(400, "You already have a registered business")
    
    clean_name = data.business_name.strip()
    name_exists = db.query(Vendor).filter(func.lower(Vendor.business_name) == clean_name.lower()).first()
    if name_exists:
        raise HTTPException(400, "Business name already taken. Try another one")
    
    subaccount = None
    paystack_error = None
    
    if ENABLE_PAYSTACK:
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    "https://api.paystack.co/subaccount",
                    headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
                    json={
                        "business_name": clean_name,
                        "settlement_bank": data.bank_name,
                        "account_number": data.account_number,
                        "percentage_charge": 0
                    },
                    timeout=15.0
                )
                if res.status_code == 200:
                    subaccount = res.json()["data"]["subaccount_code"]
                else:
                    paystack_error = res.json().get("message", "Bank verification failed")
        except Exception as e:
            paystack_error = str(e)
            logger.warning(f"Paystack exception: {paystack_error}")

    vendor = Vendor(
        vendor_id=telegram_id,
        business_name=clean_name,
        business_description=data.business_description.strip()[:500],
        logo_file_id=data.logo_file_id,
        phone_number=data.phone_number.strip(),
        bank_name=data.bank_name,
        account_number=data.account_number,
        paystack_subaccount=subaccount,
        is_active=True,
        subscription_expiry=datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS),
        commission_waived=True
    )
    db.add(vendor)
    db.commit()
    
    response = {"status": "success", "trial_days": TRIAL_DAYS}
    if paystack_error:
        response["warning"] = f"Registered but payout setup pending: {paystack_error}"
    return response

@router.get("/products")
async def get_my_products(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    user_id = int(user['id'])
    logger.info(f"Vendor {user_id} requesting products")

    products = db.query(Product).filter(Product.vendor_id == user_id).order_by(Product.created_at.desc()).all()
    logger.info(f"Found {len(products)} products for vendor {user_id}")
    
    return [{
        "id": p.id,
        "title": p.title,
        "description": p.description,
        "price": float(p.price),
        "quantity": p.quantity,
        "is_active": getattr(p, 'is_active', True),
        "telegram_file_id": p.telegram_file_id
    } for p in products]

@router.post("/products/create")
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
        telegram_file_id=data.file_id,
        is_active=True
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    logger.info(f"Created product {product.id} for vendor {user['id']}")
    return {"status": "created", "product_id": product.id}

@router.delete("/products/{product_id}")
async def delete_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user['id']).first()
    if not product:
        raise HTTPException(404, "Product not found")
    
    db.delete(product)
    db.commit()
    return {"status": "deleted"}

@router.post("/products/{product_id}/toggle")
async def toggle_product(product_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    product = db.query(Product).filter(Product.id == product_id, Product.vendor_id == user['id']).first()
    if not product:
        raise HTTPException(404, "Product not found")
    
    product.is_active = not product.is_active
    db.commit()
    return {"status": "toggled", "is_active": product.is_active}

@router.get("/orders")
async def get_vendor_orders(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    orders = db.query(Order).filter(
        Order.vendor_id == user['id'],
        Order.status.in_(["paid", "delivered"])
    ).order_by(Order.created_at.desc()).limit(50).all()
    
    return [{
        "id": o.id,
        "customer_name": o.customer_name,
        "customer_phone": o.customer_phone,
        "delivery_address": o.delivery_address,
        "items": json.loads(o.items),
        "total_amount": float(o.total_amount),
        "you_keep": float(o.total_amount - o.commission),
        "created_at": o.created_at.isoformat(),
        "status": o.status
    } for o in orders]

@router.post("/orders/{order_id}/deliver")
async def mark_delivered(order_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    order = db.query(Order).filter(Order.id == order_id, Order.vendor_id == user['id']).first()
    if not order:
        raise HTTPException(404, "Order not found")
    
    if order.status == "delivered":
        raise HTTPException(400, "Already delivered")
        
    order.status = "delivered"
    db.commit()
    
    return {"status": "marked_delivered"}

# PUBLIC ENDPOINTS FOR CUSTOMER MARKETPLACE
@router.get("/public/vendors")
async def get_public_vendors(db: Session = Depends(get_db)):
    vendors = db.query(Vendor).filter(Vendor.is_active == True).all()
    return [{
        "vendor_id": v.vendor_id,
        "business_name": v.business_name,
        "business_description": v.business_description or "",
        "logo_file_id": v.logo_file_id,
        "product_count": db.query(Product).filter(Product.vendor_id == v.vendor_id, Product.is_active == True).count()
    } for v in vendors]

@router.get("/public/products")
async def get_public_products(vendor_id: Optional[int] = None, db: Session = Depends(get_db)):
    query = db.query(Product).filter(Product.is_active == True, Product.quantity > 0)
    if vendor_id:
        query = query.filter(Product.vendor_id == vendor_id)
    
    products = query.order_by(Product.created_at.desc()).limit(100).all()
    return [{
        "id": p.id,
        "vendor_id": p.vendor_id,
        "vendor_name": p.vendor.business_name,
        "title": p.title,
        "description": p.description,
        "price": float(p.price),
        "quantity": p.quantity,
        "telegram_file_id": p.telegram_file_id
    } for p in products]
