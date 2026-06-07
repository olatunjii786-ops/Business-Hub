from sqlalchemy import Column, BIGINT, VARCHAR, BOOLEAN, TIMESTAMP, NUMERIC, TEXT, ForeignKey, Integer, func, String, DateTime
from sqlalchemy.orm import relationship, declarative_base
from datetime import datetime

Base = declarative_base()

class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id = Column(BIGINT, primary_key=True)
    business_name = Column(VARCHAR(255), unique=True, nullable=False)
    business_description = Column(TEXT, default="")
    logo_file_id = Column(VARCHAR(255))
    phone_number = Column(VARCHAR(20))
    bank_name = Column(VARCHAR(100))
    account_number = Column(VARCHAR(10))
    paystack_subaccount = Column(VARCHAR(255))
    is_active = Column(BOOLEAN, default=False)
    subscription_expiry = Column(TIMESTAMP(timezone=True))
    commission_waived = Column(BOOLEAN, default=True)
    products = relationship("Product", back_populates="vendor")
    orders = relationship("Order", back_populates="vendor")

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"))  # Changed BigInteger -> BIGINT
    title = Column(String, nullable=False)
    price = Column(NUMERIC(10, 2), nullable=False)
    quantity = Column(Integer, default=1)
    is_active = Column(BOOLEAN, default=True)  # Changed Boolean -> BOOLEAN for consistency
    telegram_file_id = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)  # Now DateTime is imported
    vendor = relationship("Vendor", back_populates="products")
    
class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"))
    customer_name = Column(VARCHAR(255))
    customer_phone = Column(VARCHAR(20))
    delivery_address = Column(TEXT)
    items = Column(TEXT, default='[]')
    total_amount = Column(NUMERIC(10, 2))
    commission = Column(NUMERIC(10, 2))
    paystack_reference = Column(VARCHAR(255), unique=True)
    status = Column(VARCHAR(50), default="pending")
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    vendor = relationship("Vendor", back_populates="orders")
