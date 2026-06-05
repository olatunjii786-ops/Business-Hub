from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta, timezone
import httpx
import logging
from pydantic import BaseModel
from database import get_db
from models import Vendor, Product
from utils import validate_init_data
from config import PAYSTACK_SECRET_KEY, ENABLE_PAYSTACK, TRIAL_DAYS

router = APIRouter(prefix="/api/vendor", tags=["vendor"])
logger = logging.getLogger(__name__)

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

@router.get("/me")
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

@router.post("/register/{telegram_id}")
async def register_vendor(telegram_id: int, data: VendorReg, db: Session = Depends(get_db)):
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
        phone_number=data.phone_number.strip(),
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

    products = db.query(Product).filter(Product.vendor_id == user['id'], Product.is_deleted == False).order_by(Product.created_at.desc()).all()
    return [{"id": p.id, "title": p.title, "price": float(p.price), "quantity": p.quantity, "is_active": p.is_active} for p in products]

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
        telegram_file_id=data.file_id
    )
    db.add(product)
    db.commit()
    return {"status": "created", "product_id": product.id}
