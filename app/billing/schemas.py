from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

CamelModel = ConfigDict(populate_by_name=True, alias_generator=to_camel)


class BillingPlanPriceOut(BaseModel):
    model_config = CamelModel

    plan_key: str
    name: str
    monthly_price_inr: int
    monthly_price_paise: int


class CouponValidateRequest(BaseModel):
    model_config = CamelModel

    plan_key: str
    coupon_code: str = Field(min_length=1, max_length=64)


class CouponPreviewOut(BaseModel):
    model_config = CamelModel

    code: str
    plan_key: str
    list_price_paise: int
    discount_paise: int
    amount_paise: int
    currency: str = "INR"


class CheckoutCreateRequest(BaseModel):
    model_config = CamelModel

    plan_key: str
    coupon_code: Optional[str] = None


class CheckoutSessionOut(BaseModel):
    model_config = CamelModel

    mode: Literal["checkout", "free"]
    provider: str = "razorpay"
    payment_id: str
    plan_key: str
    order_id: Optional[str] = None
    key_id: Optional[str] = None
    amount_paise: int
    currency: str = "INR"
    list_price_paise: int
    discount_paise: int
    coupon_code: Optional[str] = None


class ConfirmPaymentRequest(BaseModel):
    model_config = CamelModel

    payment_id: str
    razorpay_order_id: Optional[str] = None
    razorpay_payment_id: Optional[str] = None
    razorpay_signature: Optional[str] = None


class PaymentOut(BaseModel):
    model_config = CamelModel

    id: str
    workspace_id: str
    plan_key: str
    status: str
    amount_paise: int
    currency: str
    coupon_code: Optional[str] = None
    created_at: datetime
    paid_at: Optional[datetime] = None


class AdminCouponCreateRequest(BaseModel):
    model_config = CamelModel

    code: str = Field(min_length=3, max_length=32)
    plan_key: Optional[str] = None
    discount_type: Literal["percent", "fixed_inr", "free"] = "percent"
    discount_value: int = Field(ge=0)
    max_redemptions: Optional[int] = Field(default=None, ge=1)
    valid_until: Optional[datetime] = None
    notes: Optional[str] = None
    is_active: bool = True


class AdminCouponUpdateRequest(BaseModel):
    model_config = CamelModel

    plan_key: Optional[str] = None
    discount_type: Optional[Literal["percent", "fixed_inr", "free"]] = None
    discount_value: Optional[int] = Field(default=None, ge=0)
    max_redemptions: Optional[int] = Field(default=None, ge=1)
    valid_until: Optional[datetime] = None
    notes: Optional[str] = None
    is_active: Optional[bool] = None


class AdminCouponOut(BaseModel):
    model_config = CamelModel

    id: str
    code: str
    plan_key: Optional[str]
    discount_type: str
    discount_value: int
    max_redemptions: Optional[int]
    redemption_count: int
    valid_until: Optional[datetime]
    notes: Optional[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime


class AdminCouponRedemptionOut(BaseModel):
    model_config = CamelModel

    id: str
    coupon_id: str
    coupon_code: str
    workspace_id: str
    workspace_name: Optional[str]
    user_id: str
    user_email: Optional[str]
    plan_key: str
    amount_paise: int
    discount_paise: int
    created_at: datetime
