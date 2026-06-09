from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
import httpx
import json
import hmac
import hashlib
import logging
from pydantic import BaseModel
from typing import List
from database import get_db
from models import Vendor, Product, Order
from config import PAYSTACK_SECRET_KEY, RENDER_URL, COMMISSION_RATE
from utils import validate_init_data

router = APIRouter(tags=["shop"])
logger = logging.getLogger(__name__)

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
    # Validate Telegram auth for customer ID
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

        now = datetime.now(timezone.utc)
        on_trial = vendor.commission_waived and vendor.subscription_expiry > now
        commission = 0 if on_trial or not vendor.paystack_subaccount else order_data["total"] * COMMISSION_RATE

        reference = f"BH-{vendor_id}-{customer_telegram_id}-{int(datetime.now().timestamp())}"
        order = Order(
            vendor_id=vendor_id,
            customer_name=data.customer_name,
            customer_phone=data.customer_phone,
            delivery_address=data.delivery_address,
            items=json.dumps(order_data["items"]),
            total_amount=order_data["total"],
            commission=commission,
            paystack_reference=reference,
            status="pending"
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        created_orders.append({
            "order_id": order.id,
            "order_code": reference,
            "vendor_name": vendor.business_name,
            "total": order_data["total"]
        })

    if not created_orders:
        raise HTTPException(400, "No valid items to order")

    return {
        "orders": created_orders,
        "message": "Orders created. Contact vendor(s) to confirm payment."
    }

# NEW: Customer views their orders
@router.get("/api/customer/orders")
async def get_customer_orders(request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    # Find orders by matching paystack_reference which contains customer ID
    orders = db.query(Order).filter(
        Order.paystack_reference.like(f"%-{user['id']}-%")
    ).order_by(Order.created_at.desc()).limit(50).all()

    return [{
        "id": o.id,
        "order_code": o.paystack_reference,
        "vendor_name": o.vendor.business_name,
        "items": json.loads(o.items),
        "total_amount": float(o.total_amount),
        "status": o.status,
        "created_at": o.created_at.isoformat()
    } for o in orders]

# NEW: Customer cancels pending order
@router.post("/api/customer/orders/{order_id}/cancel")
async def cancel_order(order_id: int, request: Request, db: Session = Depends(get_db)):
    init_data = request.headers.get("X-Telegram-Init-Data")
    user = validate_init_data(init_data)
    if not user:
        raise HTTPException(403, "Invalid auth")

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(404, "Order not found")

    # Verify this customer owns the order
    if f"-{user['id']}-" not in order.paystack_reference:
        raise HTTPException(403, "Not your order")

    if order.status not in ["pending", "paid_unconfirmed"]:
        raise HTTPException(400, "Cannot cancel confirmed/delivered order")

    order.status = "cancelled"
    db.commit()
    return {"status": "cancelled"}

@router.post("/api/paystack/callback")
async def paystack_callback(request: Request, db: Session = Depends(get_db)):
    signature = request.headers.get("X-Paystack-Signature")
    body = await request.body()

    if not PAYSTACK_SECRET_KEY:
        logger.error("Paystack callback but no secret key set")
        return {"status": "error"}

    computed = hmac.new(PAYSTACK_SECRET_KEY.encode(), body, hashlib.sha512).hexdigest()
    if computed!= signature:
        logger.error("Invalid Paystack signature")
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
                from main import bot
                await bot.send_message(vendor_id, "🎉 Subscription renewed! You keep 95% per sale for 30 days.")

        else:
            order = db.query(Order).filter(Order.paystack_reference == ref).first()
            if order and order.status == "pending":
                order.status = "paid_confirmed"
                items = json.loads(order.items)
                for item in items:
                    product = db.query(Product).filter(Product.id == item["product_id"]).first()
                    if product:
                        product.quantity -= item["quantity"]
                db.commit()

                from main import bot
                await bot.send_message(
                    order.vendor_id,
                    f"🎉 <b>New Order Paid via Paystack!</b>\n\n"
                    f"<b>Order:</b> {order.paystack_reference}\n"
                    f"<b>Customer:</b> {order.customer_name}\n"
                    f"<b>Phone:</b> {order.customer_phone}\n"
                    f"<b>Address:</b> {order.delivery_address}\n"
                    f"<b>Items:</b> {len(items)}\n"
                    f"<b>Total:</b> ₦{order.total_amount:,.0f}\n"
                    f"<b>You Keep:</b> ₦{order.total_amount - order.commission:,.0f}\n\n"
                    f"Prepare for delivery!"
                )

    return {"status": "ok"}
