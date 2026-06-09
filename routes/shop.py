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

router = APIRouter(tags=["shop"])
logger = logging.getLogger(__name__)

class CheckoutReq(BaseModel):
    vendor_id: int
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
async def checkout(data: CheckoutReq, db: Session = Depends(get_db)):
    vendor = db.query(Vendor).filter(Vendor.vendor_id == data.vendor_id).first()
    if not vendor:
        raise HTTPException(404, "Vendor not found")

    total = 0
    items_data = []
    for item in data.items:
        product = db.query(Product).filter(Product.id == item['product_id']).first()
        if not product or product.quantity < item['quantity']:
            raise HTTPException(400, f"Insufficient stock for {product.title if product else 'item'}")
        total += float(product.price) * item['quantity']
        items_data.append({
            "product_id": product.id,
            "title": product.title,
            "price": float(product.price),
            "quantity": item['quantity'],
            "size": item.get('size', 'One Size')
        })

    now = datetime.now(timezone.utc)
    on_trial = vendor.commission_waived and vendor.subscription_expiry > now
    commission = 0 if on_trial or not vendor.paystack_subaccount else total * COMMISSION_RATE

    reference = f"BH-{vendor.vendor_id}-{int(datetime.now().timestamp())}"
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
    db.refresh(order)

    payment_url = None
    if PAYSTACK_SECRET_KEY and PAYSTACK_SECRET_KEY.startswith('sk_'):
        try:
            async with httpx.AsyncClient() as client:
                payload = {
                    "amount": int(total * 100),
                    "email": f"customer_{reference}@businesshub.com",
                    "reference": reference,
                    "callback_url": f"{RENDER_URL}/api/paystack/callback"
                }
                if vendor.paystack_subaccount:
                    payload.update({
                        "subaccount": vendor.paystack_subaccount,
                        "transaction_charge": int(commission * 100),
                        "bearer": "subaccount"
                    })

                res = await client.post(
                    "https://api.paystack.co/transaction/initialize",
                    headers={"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"},
                    json=payload,
                    timeout=15.0
                )
                if res.status_code == 200:
                    payment_url = res.json()["data"]["authorization_url"]
        except Exception as e:
            logger.warning(f"Paystack init failed but order saved: {e}")

    return {
        "payment_url": payment_url,
        "order_id": order.id,
        "order_code": reference,
        "message": "Order created. Contact vendor to confirm payment."
    }

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
