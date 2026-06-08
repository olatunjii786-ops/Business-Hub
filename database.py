from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base
from config import DATABASE_URL
import logging

logger = logging.getLogger(__name__)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()  # Only define Base here

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def run_migrations():
    try:
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            # Orders
            conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS items TEXT DEFAULT '[]';"))
            conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS status VARCHAR(50) DEFAULT 'pending';"))
            conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS customer_id BIGINT;"))
            conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS paystack_reference VARCHAR(255);"))
            
            # Vendors
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS business_description TEXT DEFAULT '';"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS logo_file_id VARCHAR(255);"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS phone_number VARCHAR(20);"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS bank_name VARCHAR(100);"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS account_number VARCHAR(10);"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS paystack_subaccount VARCHAR(255);"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT FALSE;"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS subscription_expiry TIMESTAMPTZ;"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS commission_waived BOOLEAN DEFAULT TRUE;"))
            
            # Products
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT DEFAULT '';"))
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS quantity INTEGER DEFAULT 1;"))
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS sizes VARCHAR(500) DEFAULT '';"))
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS telegram_file_id VARCHAR(255);"))
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;"))
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();"))
            
            conn.commit()
        logger.info("DB migrations completed")
    except Exception as e:
        logger.error(f"Migration error: {e}")
