from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from config import DATABASE_URL
from models import Base
import logging

logger = logging.getLogger(__name__)
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def run_migrations():
    """Auto-runs on startup. Adds missing columns safely."""
    try:
        Base.metadata.create_all(bind=engine)
        with engine.connect() as conn:
            conn.execute(text("ALTER TABLE orders ADD COLUMN IF NOT EXISTS items TEXT DEFAULT '[]';"))
            conn.execute(text("ALTER TABLE vendors ADD COLUMN IF NOT EXISTS paystack_subaccount VARCHAR(255);"))
            conn.execute(text("ALTER TABLE products ADD COLUMN IF NOT EXISTS telegram_file_id VARCHAR(255);"))
            conn.commit()
        logger.info("DB migrations completed")
    except Exception as e:
        logger.error(f"Migration error: {e}")
