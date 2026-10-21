from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from pymongo.database import Database

from app.auth.deps import get_current_user, require_platform_admin
from app.billing import coupons as coupon_service
from app.billing import service as billing_service
from app.billing.razorpay import RazorpayAdapter
from app.billing.schemas import (
    AdminCouponCreateRequest,
    AdminCouponOut,
    AdminCouponRedemptionOut,
    AdminCouponUpdateRequest,
    BillingPlanPriceOut,
    CheckoutCreateRequest,
    CheckoutSessionOut,
    ConfirmPaymentRequest,
    CouponPreviewOut,
    CouponValidateRequest,
    PaymentOut,
)
from app.core.database import get_db
from app.workspaces.deps import require_social_level, require_workspace_access
from app.workspaces.models import SocialLevel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces/{workspace_id}/billing", tags=["billing"])

webhooks_router = APIRouter(prefix="/billing", tags=["billing-webhooks"])

admin_coupons_router = APIRouter(
    prefix="/admin/coupons",
    tags=["admin-coupons"],
    dependencies=[Depends(require_platform_admin)],
)


@router.get("/plans", response_model=list[BillingPlanPriceOut])
def list_billing_plans(
    workspace: dict = Depends(require_workspace_access),
    db: Database = Depends(get_db),
) -> list[BillingPlanPriceOut]:
    return [BillingPlanPriceOut(**row) for row in billing_service.list_checkout_plans(db)]


@router.post("/coupons/validate", response_model=CouponPreviewOut)
def validate_coupon(
    payload: CouponValidateRequest,
    workspace: dict = Depends(require_workspace_access),
    db: Database = Depends(get_db),
) -> CouponPreviewOut:
    return CouponPreviewOut(**billing_service.preview_coupon(
        db,
        workspace=workspace,
        plan_key=payload.plan_key,
        coupon_code=payload.coupon_code,
    ))


@router.post("/checkout", response_model=CheckoutSessionOut)
def create_checkout(
    payload: CheckoutCreateRequest,
    workspace: dict = Depends(require_workspace_access),
    _member=Depends(require_social_level(SocialLevel.ADMIN)),
    current_user: dict = Depends(get_current_user),
    db: Database = Depends(get_db),
) -> CheckoutSessionOut:
    session = billing_service.create_checkout(
        db,
        workspace=workspace,
        user=current_user,
        plan_key=payload.plan_key,
        coupon_code=payload.coupon_code,
    )
    return CheckoutSessionOut(
        mode=session["mode"],
        payment_id=session["paymentId"],
        plan_key=session["planKey"],
        order_id=session.get("orderId"),
        key_id=session.get("keyId"),
        amount_paise=session["amountPaise"],
        currency=session.get("currency", "INR"),
        list_price_paise=session["listPricePaise"],
        discount_paise=session["discountPaise"],
        coupon_code=session.get("couponCode"),
    )


@router.post("/confirm", response_model=PaymentOut)
def confirm_checkout(
    payload: ConfirmPaymentRequest,
    workspace_id: str,
    workspace: dict = Depends(require_workspace_access),
    _member=Depends(require_social_level(SocialLevel.ADMIN)),
    db: Database = Depends(get_db),
) -> PaymentOut:
    payment = billing_service.confirm_payment(
        db,
        workspace_id=workspace_id,
        payment_id=payload.payment_id,
        razorpay_order_id=payload.razorpay_order_id,
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_signature=payload.razorpay_signature,
    )
    return _payment_out(payment)


@router.get("/payments", response_model=list[PaymentOut])
def list_payments(
    workspace_id: str,
    workspace: dict = Depends(require_workspace_access),
    db: Database = Depends(get_db),
) -> list[PaymentOut]:
    rows = billing_service.list_workspace_payments(db, workspace_id)
    return [_payment_out(p) for p in rows]


def _payment_out(payment: dict) -> PaymentOut:
    return PaymentOut(
        id=payment["id"],
        workspace_id=payment["workspace_id"],
        plan_key=payment["plan_key"],
        status=payment["status"],
        amount_paise=int(payment["amount_paise"]),
        currency=payment.get("currency", "INR"),
        coupon_code=payment.get("coupon_code"),
        created_at=payment["created_at"],
        paid_at=payment.get("paid_at"),
    )


@webhooks_router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, db: Database = Depends(get_db)) -> dict:
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature")
    adapter = RazorpayAdapter()
    if not adapter.verify_webhook_signature(body, signature):
        logger.warning("razorpay webhook signature rejected")
        return {"ok": False}
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return {"ok": False}
    billing_service.handle_razorpay_webhook(db, payload)
    return {"ok": True}


@admin_coupons_router.get("", response_model=list[AdminCouponOut])
def admin_list_coupons(db: Database = Depends(get_db)) -> list[AdminCouponOut]:
    return [_coupon_out(c) for c in coupon_service.list_coupons(db)]


@admin_coupons_router.post("", response_model=AdminCouponOut, status_code=201)
def admin_create_coupon(
    payload: AdminCouponCreateRequest,
    db: Database = Depends(get_db),
    current_user: dict = Depends(get_current_user),
) -> AdminCouponOut:
    doc = coupon_service.create_coupon(
        db,
        {
            "code": payload.code,
            "plan_key": payload.plan_key,
            "discount_type": payload.discount_type,
            "discount_value": payload.discount_value,
            "max_redemptions": payload.max_redemptions,
            "valid_until": payload.valid_until,
            "notes": payload.notes,
            "is_active": payload.is_active,
        },
        created_by=current_user["id"],
    )
    return _coupon_out(doc)


@admin_coupons_router.patch("/{coupon_id}", response_model=AdminCouponOut)
def admin_update_coupon(
    coupon_id: str,
    payload: AdminCouponUpdateRequest,
    db: Database = Depends(get_db),
) -> AdminCouponOut:
    doc = coupon_service.update_coupon(
        db,
        coupon_id,
        payload.model_dump(exclude_unset=True),
    )
    return _coupon_out(doc)


@admin_coupons_router.get("/redemptions", response_model=list[AdminCouponRedemptionOut])
def admin_list_redemptions(
    couponId: str | None = None,
    db: Database = Depends(get_db),
) -> list[AdminCouponRedemptionOut]:
    rows = coupon_service.list_redemptions(db, coupon_id=couponId)
    return [
        AdminCouponRedemptionOut(
            id=r["id"],
            coupon_id=r["coupon_id"],
            coupon_code=r["coupon_code"],
            workspace_id=r["workspace_id"],
            workspace_name=r.get("workspace_name"),
            user_id=r["user_id"],
            user_email=r.get("user_email"),
            plan_key=r["plan_key"],
            amount_paise=int(r["amount_paise"]),
            discount_paise=int(r.get("discount_paise") or 0),
            created_at=r["created_at"],
        )
        for r in rows
    ]


def _coupon_out(c: dict) -> AdminCouponOut:
    return AdminCouponOut(
        id=c["id"],
        code=c["code"],
        plan_key=c.get("plan_key"),
        discount_type=c.get("discount_type", "percent"),
        discount_value=int(c.get("discount_value") or 0),
        max_redemptions=c.get("max_redemptions"),
        redemption_count=int(c.get("redemption_count") or 0),
        valid_until=c.get("valid_until"),
        notes=c.get("notes"),
        is_active=bool(c.get("is_active", True)),
        created_at=c["created_at"],
        updated_at=c["updated_at"],
    )
