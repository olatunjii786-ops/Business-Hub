from sqlalchemy import Column, BIGINT, VARCHAR, BOOLEAN, TIMESTAMP, NUMERIC, TEXT, ForeignKey, Integer, func
from sqlalchemy.orm import relationship, declarative_base

Base = declarative_base()

class Vendor(Base):
    __tablename__ = "vendors"
    vendor_id = Column(BIGINT, primary_key=True)
    business_name = Column(VARCHAR(255), unique=True, nullable=False)
    business_description = Column(TEXT, default="") # NEW
    logo_file_id = Column(VARCHAR(255)) # NEW - Telegram file_id
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
    id = Column(Integer, primary_key=True, autoincrement=True)
    vendor_id = Column(BIGINT, ForeignKey("vendors.vendor_id"))
    title = Column(VARCHAR(255), nullable=False)
    description = Column(TEXT, default="")
    price = Column(NUMERIC(10, 2), nullable=False)
    quantity = Column(Integer, default=0)
    sizes = Column(VARCHAR(255), default="One Size")
    telegram_file_id = Column(VARCHAR(255))
    is_active = Column(BOOLEAN, default=True)
    is_deleted = Column(BOOLEAN, default=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
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
    status = Column(VARCHAR(50), default="pending")  # MISSING - ADDED
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
    vendor = relationship("Vendor", back_populates="orders")
