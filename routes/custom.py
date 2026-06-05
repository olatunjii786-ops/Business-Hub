from fastapi import APIRouter, Depends
from database import get_db
from sqlalchemy.orm import Session
from models import Vendor

router = APIRouter(prefix="/api/custom", tags=["custom"])

@router.get("/ping")
async def ping():
    return {"status": "custom router working"}

# Add your new endpoints here. Example:
# @router.post("/send-broadcast")
# async def broadcast():
# return {"sent": True}
