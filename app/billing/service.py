from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from pymongo.database import Database

from app.billing import coupons as coupon_service
from app.billing.pricing import assert_upgrade_allowed, monthly_price_paise, PAID_PLAN_KEYS
from app.billing.razorpay import RazorpayAdapter
from app.core.mongo_utils import new_id, utcnow
from app.plans.catalog import get_plan_definition
from app.social.dashboard_cache import invalidate_dashboard

logger = logging.getLogger(__name__)

PAYMENT_STATUS_CREATED = "created"
PAYMENT_STATUS_PENDING = "pending"
PAYMENT_STATUS_PAID = "paid"
PAYMENT_STATUS_FAILED = "failed"

SUBSCRIPTION_PERIOD_DAYS = 30
RAZORPAY_MIN_PAISE = 100


def list_checkout_plans(db: Database) -> list[dict[str, Any]]:
    rows = []
    for plan_key in sorted(PAID_PLAN_KEYS):
        paise = monthly_price_paise(db, plan_key)
        if paise is None:
            continue
        definition = get_plan_definition(plan_key)
        rows.append(
            {
                "plan_key": plan_key,
                "name": definition.name,
                "monthly_price_inr": paise // 100,
                "monthly_price_paise": paise,
            }
        )
    return rows


def _get_payment(db: Database, payment_id: str) -> dict | None:
    return db["billing_payments"].find_one({"id": str(payment_id)})


def preview_coupon(
    db: Database,
    *,
    workspace: dict,
    plan_key: str,
    coupon_code: str,
) -> dict[str, Any]:
    assert_upgrade_allowed(workspace.get("plan") or "starter", plan_key)
    list_price = monthly_price_paise(db, plan_key)
    if list_price is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan")
    coupon, discount = coupon_service.validate_coupon_for_checkout(
        db, code=coupon_code, plan_key=plan_key, list_price_paise=list_price
    )
    amount = max(0, list_price - discount)
    return {
        "code": coupon["code"],
        "plan_key": plan_key,
        "list_price_paise": list_price,
        "discount_paise": discount,
        "amount_paise": amount,
        "currency": "INR",
    }


def create_checkout(
    db: Database,
    *,
    workspace: dict,
    user: dict,
    plan_key: str,
    coupon_code: str | None,
) -> dict[str, Any]:
    assert_upgrade_allowed(workspace.get("plan") or "starter", plan_key)
    list_price = monthly_price_paise(db, plan_key)
    if list_price is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid plan")

    discount_paise = 0
    coupon_doc = None
    if coupon_code and coupon_code.strip():
        coupon_doc, discount_paise = coupon_service.validate_coupon_for_checkout(
            db, code=coupon_code, plan_key=plan_key, list_price_paise=list_price
        )

    amount_paise = max(0, list_price - discount_paise)
    now = utcnow()
    payment_id = new_id()

    payment = {
        "id": payment_id,
        "workspace_id": workspace["id"],
        "user_id": user["id"],
        "plan_key": plan_key,
        "list_price_paise": list_price,
        "discount_paise": discount_paise,
        "amount_paise": amount_paise,
        "currency": "INR",
        "coupon_id": coupon_doc["id"] if coupon_doc else None,
        "coupon_code": coupon_doc["code"] if coupon_doc else None,
        "status": PAYMENT_STATUS_CREATED,
        "provider": "razorpay",
        "provider_order_id": None,
        "provider_payment_id": None,
        "created_at": now,
        "updated_at": now,
        "paid_at": None,
    }
    db["billing_payments"].insert_one(payment)

    if amount_paise == 0:
        _fulfill_payment(db, payment_id=payment_id, provider_payment_id="coupon_free")
        return {
            "mode": "free",
            "provider": "razorpay",
            "paymentId": payment_id,
            "planKey": plan_key,
            "orderId": None,
            "keyId": None,
            "amountPaise": 0,
            "currency": "INR",
            "listPricePaise": list_price,
            "discountPaise": discount_paise,
            "couponCode": coupon_doc["code"] if coupon_doc else None,
        }

    if amount_paise < RAZORPAY_MIN_PAISE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Payable amount must be at least ₹{RAZORPAY_MIN_PAISE / 100:.0f} after discount",
        )

    adapter = RazorpayAdapter()
    checkout = adapter.create_order(
        amount_paise=amount_paise,
        currency="INR",
        receipt=payment_id.replace("-", "")[:40],
        notes={
            "workspace_id": workspace["id"],
            "payment_id": payment_id,
            "plan_key": plan_key,
            "purpose": "social_plan",
        },
    )
    db["billing_payments"].update_one(
        {"id": payment_id},
        {
            "$set": {
                "provider_order_id": checkout["orderId"],
                "status": PAYMENT_STATUS_PENDING,
                "updated_at": utcnow(),
            }
        },
    )

    return {
        **checkout,
        "paymentId": payment_id,
        "planKey": plan_key,
        "listPricePaise": list_price,
        "discountPaise": discount_paise,
        "couponCode": coupon_doc["code"] if coupon_doc else None,
    }


def confirm_payment(
    db: Database,
    *,
    workspace_id: str,
    payment_id: str,
    razorpay_order_id: str | None,
    razorpay_payment_id: str | None,
    razorpay_signature: str | None,
) -> dict:
    payment = _get_payment(db, payment_id)
    if not payment or payment["workspace_id"] != str(workspace_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    if payment["status"] == PAYMENT_STATUS_PAID:
        return payment

    amount = int(payment["amount_paise"])
    if amount == 0:
        return _fulfill_payment(db, payment_id=payment_id, provider_payment_id="coupon_free")

    if not razorpay_order_id or not razorpay_payment_id or not razorpay_signature:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Razorpay payment details")

    expected_order = payment.get("provider_order_id")
    if expected_order and expected_order != razorpay_order_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Order mismatch")

    adapter = RazorpayAdapter()
    if not adapter.verify_payment_signature(
        order_id=razorpay_order_id,
        payment_id=razorpay_payment_id,
        signature=razorpay_signature,
    ):
        db["billing_payments"].update_one(
            {"id": payment_id},
            {"$set": {"status": PAYMENT_STATUS_FAILED, "updated_at": utcnow()}},
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Payment verification failed")

    return _fulfill_payment(
        db,
        payment_id=payment_id,
        provider_payment_id=razorpay_payment_id,
        provider_order_id=razorpay_order_id,
    )


def _fulfill_payment(
    db: Database,
    *,
    payment_id: str,
    provider_payment_id: str,
    provider_order_id: str | None = None,
) -> dict:
    payment = _get_payment(db, payment_id)
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found")
    if payment["status"] == PAYMENT_STATUS_PAID:
        return payment

    now = utcnow()
    period_end = now + timedelta(days=SUBSCRIPTION_PERIOD_DAYS)

    db["billing_payments"].update_one(
        {"id": payment_id},
        {
            "$set": {
                "status": PAYMENT_STATUS_PAID,
                "provider_payment_id": provider_payment_id,
                "provider_order_id": provider_order_id or payment.get("provider_order_id"),
                "paid_at": now,
                "updated_at": now,
            }
        },
    )

    workspace_id = payment["workspace_id"]
    plan_key = payment["plan_key"]
    db["workspaces"].update_one(
        {"id": workspace_id},
        {
            "$set": {
                "plan": plan_key,
                "plan_period_end": period_end,
                "updated_at": now,
            }
        },
    )
    invalidate_dashboard(str(workspace_id))

    coupon_id = payment.get("coupon_id")
    if coupon_id:
        coupon = db["billing_coupons"].find_one({"id": coupon_id})
        workspace = db["workspaces"].find_one({"id": workspace_id}) or {"id": workspace_id}
        user = db["users"].find_one({"id": payment["user_id"]}) or {"id": payment["user_id"]}
        if coupon:
            coupon_service.record_redemption(
                db,
                coupon=coupon,
                workspace=workspace,
                user=user,
                plan_key=plan_key,
                amount_paise=int(payment["amount_paise"]),
                discount_paise=int(payment.get("discount_paise") or 0),
                payment_id=payment_id,
            )

    logger.info(
        "workspace %s upgraded to %s via payment %s",
        workspace_id,
        plan_key,
        payment_id,
    )
    return db["billing_payments"].find_one({"id": payment_id}) or payment


def handle_razorpay_webhook(db: Database, payload: dict) -> None:
    event = payload.get("event") or ""
    entity: dict[str, Any] = {}
    if event.startswith("payment."):
        entity = payload.get("payload", {}).get("payment", {}).get("entity") or {}
    elif event.startswith("order."):
        entity = payload.get("payload", {}).get("order", {}).get("entity") or {}

    if event not in {"payment.captured", "order.paid"}:
        return

    notes = entity.get("notes") or {}
    payment_id = notes.get("payment_id")
    if not payment_id:
        order_id = entity.get("order_id") or entity.get("id")
        if order_id:
            payment = db["billing_payments"].find_one({"provider_order_id": order_id})
            payment_id = payment["id"] if payment else None
    if not payment_id:
        logger.warning("razorpay webhook missing payment_id notes event=%s", event)
        return

    payment = _get_payment(db, payment_id)
    if not payment or payment["status"] == PAYMENT_STATUS_PAID:
        return

    provider_payment_id = entity.get("id") if event.startswith("payment.") else entity.get("payment_id")
    _fulfill_payment(
        db,
        payment_id=payment_id,
        provider_payment_id=str(provider_payment_id or "webhook"),
        provider_order_id=entity.get("order_id") or payment.get("provider_order_id"),
    )


def list_workspace_payments(db: Database, workspace_id: str, limit: int = 20) -> list[dict]:
    return list(
        db["billing_payments"]
        .find({"workspace_id": str(workspace_id)})
        .sort("created_at", -1)
        .limit(limit)
    )
