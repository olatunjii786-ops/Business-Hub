from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timezone
from database import get_db
from models import Vendor, Order
from utils import validate_init_data
from config import ADMIN_TELEGRAM_ID

router = APIRouter(prefix="/api/admin", tags=["admin"])

@router.get("/stats")
async def admin_stats(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    if not init_data:
        raise HTTPException(403, "Missing authentication")
    
    user = validate_init_data(init_data)
    if not user or user['id']!= ADMIN_TELEGRAM_ID:
        raise HTTPException(403, "Admin only")
    
    now = datetime.now(timezone.utc)
    total_vendors = db.query(Vendor).count()
    active_vendors = db.query(Vendor).filter(Vendor.is_active == True, Vendor.subscription_expiry > now).count()
    on_trial = db.query(Vendor).filter(Vendor.commission_waived == True, Vendor.subscription_expiry > now).count()
    total_orders = db.query(func.count(Order.id)).filter(Order.status == "paid").scalar() or 0
    total_sales = db.query(func.sum(Order.total_amount)).filter(Order.status == "paid").scalar() or 0

    return {
        "total_vendors": total_vendors,
        "active_vendors": active_vendors,
        "on_trial": on_trial,
        "total_orders": int(total_orders),
        "total_sales": float(total_sales)
      }
