from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, status
from pymongo.database import Database

from app.core.mongo_utils import new_id, utcnow


def _normalize_code(code: str) -> str:
    return code.strip().upper()


def get_coupon_by_code(db: Database, code: str) -> dict | None:
    return db["billing_coupons"].find_one({"code": _normalize_code(code)})


def _coupon_applies_to_plan(coupon: dict, plan_key: str) -> bool:
    target = coupon.get("plan_key")
    if not target:
        return True
    return str(target) == str(plan_key)


def validate_coupon_for_checkout(
    db: Database,
    *,
    code: str,
    plan_key: str,
    list_price_paise: int,
) -> tuple[dict, int]:
    """Return (coupon doc, discount_paise). Raises HTTPException on invalid coupon."""
    coupon = get_coupon_by_code(db, code)
    if not coupon:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid coupon code")
    if not coupon.get("is_active", True):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This coupon is no longer active")
    if not _coupon_applies_to_plan(coupon, plan_key):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This coupon does not apply to the selected plan",
        )

    valid_until = coupon.get("valid_until")
    if valid_until and isinstance(valid_until, datetime):
        vu = valid_until if valid_until.tzinfo else valid_until.replace(tzinfo=timezone.utc)
        if vu < utcnow():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This coupon has expired")

    max_red = coupon.get("max_redemptions")
    used = int(coupon.get("redemption_count") or 0)
    if max_red is not None and used >= int(max_red):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This coupon has been fully redeemed")

    discount_type = coupon.get("discount_type") or "percent"
    discount_value = int(coupon.get("discount_value") or 0)

    if discount_type == "free":
        discount_paise = list_price_paise
    elif discount_type == "fixed_inr":
        discount_paise = min(list_price_paise, int(discount_value) * 100)
    elif discount_type == "percent":
        pct = min(100, max(0, discount_value))
        discount_paise = int(round(list_price_paise * pct / 100))
    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid coupon configuration")

    if discount_paise <= 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Coupon does not reduce the price")

    return coupon, discount_paise


def create_coupon(db: Database, data: dict, created_by: str | None) -> dict:
    code = _normalize_code(data["code"])
    if get_coupon_by_code(db, code):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Coupon code already exists")

    now = utcnow()
    doc = {
        "id": new_id(),
        "code": code,
        "plan_key": data.get("plan_key"),
        "discount_type": data["discount_type"],
        "discount_value": int(data["discount_value"]),
        "max_redemptions": data.get("max_redemptions"),
        "redemption_count": 0,
        "valid_until": data.get("valid_until"),
        "notes": data.get("notes"),
        "is_active": bool(data.get("is_active", True)),
        "created_by": created_by,
        "created_at": now,
        "updated_at": now,
    }
    db["billing_coupons"].insert_one(doc)
    return doc


def update_coupon(db: Database, coupon_id: str, data: dict) -> dict:
    coupon = db["billing_coupons"].find_one({"id": str(coupon_id)})
    if not coupon:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Coupon not found")

    updates = {k: v for k, v in data.items() if v is not None}
    if "plan_key" in updates and updates["plan_key"] == "":
        updates["plan_key"] = None
    updates["updated_at"] = utcnow()
    db["billing_coupons"].update_one({"id": str(coupon_id)}, {"$set": updates})
    return db["billing_coupons"].find_one({"id": str(coupon_id)})


def list_coupons(db: Database) -> list[dict]:
    return list(db["billing_coupons"].find().sort("created_at", -1))


def list_redemptions(db: Database, coupon_id: str | None = None) -> list[dict]:
    query: dict = {}
    if coupon_id:
        query["coupon_id"] = str(coupon_id)
    return list(db["billing_coupon_redemptions"].find(query).sort("created_at", -1))


def record_redemption(
    db: Database,
    *,
    coupon: dict,
    workspace: dict,
    user: dict,
    plan_key: str,
    amount_paise: int,
    discount_paise: int,
    payment_id: str,
) -> dict:
    now = utcnow()
    redemption = {
        "id": new_id(),
        "coupon_id": coupon["id"],
        "coupon_code": coupon["code"],
        "workspace_id": workspace["id"],
        "workspace_name": workspace.get("name"),
        "user_id": user["id"],
        "user_email": user.get("email"),
        "plan_key": plan_key,
        "amount_paise": amount_paise,
        "discount_paise": discount_paise,
        "payment_id": payment_id,
        "created_at": now,
    }
    db["billing_coupon_redemptions"].insert_one(redemption)
    db["billing_coupons"].update_one(
        {"id": coupon["id"]},
        {"$inc": {"redemption_count": 1}, "$set": {"updated_at": now}},
    )
    return redemption
