from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone
import json
from pydantic import BaseModel
from typing import List
from database import get_db
from models import Vendor, Product, Order
from utils import validate_init_data

router = APIRouter(tags=["shop"])

class CheckoutReq(BaseModel):
    customer_name: str
    customer_phone: str
    delivery_address: str
    items: List

@router.get("/api/shop/{vendor_id}/products")
async def get_shop_products(vendor_id: int, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
    if not vendor or not vendor.is_active or vendor.subscription_expiry < datetime.now(timezone.utc):
        raise HTTPException(404, "Store unavailable")

    products = db.query(Product).filter(
        Product.vendor_id == vendor_id,
        Product.is_active == True,
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

@router.post("/api/checkout")
async def checkout(request: Request, data: CheckoutReq, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Please open this from Telegram")

    customer_telegram_id = user['id']

    # Group items by vendor
    vendor_orders = {}
    for item in data.items:
        product = db.query(Product).filter(Product.id == item['product_id']).first()
        if not product:
            raise HTTPException(400, f"Product {item['product_id']} not found")
        if product.quantity < item['quantity']:
            raise HTTPException(400, f"Insufficient stock for {product.title}")

        vid = product.vendor_id
        if vid not in vendor_orders:
            vendor_orders[vid] = {"items": [], "total": 0}
        vendor_orders[vid]["items"].append({
            "product_id": product.id,
            "title": product.title,
            "price": float(product.price),
            "quantity": item['quantity'],
            "size": item.get('size', 'One Size')
        })
        vendor_orders[vid]["total"] += float(product.price) * item['quantity']

    # Create one order per vendor
    created_orders = []
    for vendor_id, order_data in vendor_orders.items():
        vendor = db.query(Vendor).filter(Vendor.vendor_id == vendor_id).first()
        if not vendor:
            continue

        order_code = f"BH-{vendor_id}-{customer_telegram_id}-{int(datetime.now().timestamp())}"
        order = Order(
            vendor_id=vendor_id,
            customer_name=data.customer_name,
            customer_phone=data.customer_phone,
            delivery_address=data.delivery_address,
            items=json.dumps(order_data["items"]),
            total_amount=order_data["total"],
            order_code=order_code,
            status="pending"
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        created_orders.append({
            "order_id": order.id,
            "order_code": order_code,
            "vendor_name": vendor.business_name,
            "total": order_data["total"]
        })

    if not created_orders:
        raise HTTPException(400, "No valid items to order")

    return {
        "orders": created_orders,
        "message": "Orders created. Contact vendor(s) to arrange payment."
    }

@router.get("/api/customer/orders")
async def get_customer_orders(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    orders = db.query(Order).filter(
        Order.order_code.like(f"%-{user['id']}-%")
    ).order_by(Order.created_at.desc()).limit(50).all()

    return [{
        "id": o.id,
        "order_code": o.order_code,
        "vendor_name": o.vendor.business_name,
        "items": json.loads(o.items),
        "total_amount": float(o.total_amount),
        "status": o.status,
        "created_at": o.created_at.isoformat()
    } for o in orders]

@router.post("/api/customer/orders/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "Order not found")

    if f"-{user['id']}-" not in order.order_code:
        raise HTTPException(403, "Not your order")

    if order.status not in ["pending"]:
        raise HTTPException(400, "Cannot cancel confirmed/delivered order")

    order.status = "cancelled"
    db.commit()
    return {"status": "cancelled"}
