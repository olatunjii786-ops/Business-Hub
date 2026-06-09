from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text, BigInteger, Numeric
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime, timezone

class Vendor(Base):
    __tablename__ = "vendors"
    
    vendor_id = Column(BigInteger, primary_key=True)
    business_name = Column(String(255), unique=True, nullable=False)
    business_description = Column(Text, default="")
    logo_file_id = Column(String(255))
    phone_number = Column(String(20))
    bank_name = Column(String(100))
    account_number = Column(String(10))
    paystack_subaccount = Column(String(255))
    is_active = Column(Boolean, default=False)
    subscription_expiry = Column(DateTime(timezone=True))
    commission_waived = Column(Boolean, default=True)
    
    products = relationship("Product", back_populates="vendor")
    orders = relationship("Order", back_populates="vendor")

class Product(Base):
    __tablename__ = "products"
    
    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(BigInteger, ForeignKey("vendors.vendor_id"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text, default="")
    price = Column(Float, nullable=False)
    quantity = Column(Integer, default=1)
    sizes = Column(String(500), default="")
    telegram_file_id = Column(String(500))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    
    vendor = relationship("Vendor", back_populates="products")

class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.vendor_id"), nullable=False)
    customer_name = Column(String(200), nullable=False)
    customer_phone = Column(String(20), nullable=False)
    delivery_address = Column(Text, nullable=False)
    items = Column(Text, default='[]') # JSON string
    total_amount = Column(Float, nullable=False)
    order_code = Column(String(100), unique=True, nullable=False) # Changed from paystack_reference
    status = Column(String(20), default="pending") # pending, confirmed, delivered, cancelled
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    vendor = relationship("Vendor", back_populates="orders")
