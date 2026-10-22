from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest

from app.billing import coupons
from app.billing.razorpay import RazorpayAdapter
from app.core.config import settings
from app.core.mongo_utils import new_id, utcnow

API = settings.api_v1_prefix
PRICES = {"growth": 49900, "enterprise": 99900}


@pytest.fixture(autouse=True)
def razorpay_provider(db, monkeypatch):
    provider = Mock()

    def create_order(payload):
        return {"id": f"order_{new_id()}", **payload}

    provider.order.create.side_effect = create_order
    monkeypatch.setattr(RazorpayAdapter, "_client", lambda self: provider)
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_billing")
    monkeypatch.setattr(settings, "razorpay_key_secret", "test-billing-secret")
    for plan_key, amount in PRICES.items():
        db["plan_overrides"].insert_one(
            {"plan_key": plan_key, "monthly_price_inr_paise": amount}
        )
    return provider


def _register(client, email="billing@example.com"):
    response = client.post(
        f"{API}/auth/register",
        json={"email": email, "password": "s3cret-pass", "full_name": "Billing Owner"},
    )
    assert response.status_code == 201, response.text
    session = response.json()
    return session, {"Authorization": f"Bearer {session['access_token']}"}


def _url(workspace_id):
    return f"{API}/workspaces/{workspace_id}/billing"


def _coupon(db, **overrides):
    return coupons.create_coupon(
        db,
        {
            "code": "BILLINGTEST",
            "discount_type": "free",
            "discount_value": 0,
            **overrides,
        },
        created_by=None,
    )


def _checkout(client, headers, workspace_id, plan_key="growth", **payload):
    response = client.post(
        f"{_url(workspace_id)}/checkout",
        headers=headers,
        json={"planKey": plan_key, **payload},
    )
    assert response.status_code == 200, response.text
    return response.json()


def _confirmation(checkout):
    return {
        "paymentId": checkout["paymentId"],
        "razorpayOrderId": checkout["orderId"],
        "razorpayPaymentId": "pay_test_billing",
        "razorpaySignature": "test-signature",
    }


def _assert_plan(client, db, headers, workspace_id, plan_key):
    workspace = db["workspaces"].find_one({"id": workspace_id})
    assert workspace["plan"] == plan_key
    session = client.get(f"{API}/auth/me", headers=headers)
    assert session.status_code == 200, session.text
    summary = next(row for row in session.json()["workspaces"] if row["id"] == workspace_id)
    assert summary["plan"] == plan_key
    usage = client.get(f"{API}/workspaces/{workspace_id}/social/usage", headers=headers)
    assert usage.status_code == 200, usage.text
    assert usage.json()["plan"] == plan_key


def test_checkout_plan_prices_follow_database_overrides(client, db, razorpay_provider):
    session, headers = _register(client)
    workspace_id = session["workspace"]["id"]
    db["plan_overrides"].update_one(
        {"plan_key": "growth"}, {"$set": {"monthly_price_inr": 625}}
    )
    response = client.get(f"{_url(workspace_id)}/plans", headers=headers)
    assert response.status_code == 200, response.text
    assert {row["planKey"]: row for row in response.json()} == {
        "growth": {
            "planKey": "growth", "name": "Pro", "monthlyPriceInr": 625,
            "monthlyPricePaise": 62500,
        },
        "enterprise": {
            "planKey": "enterprise", "name": "Growth", "monthlyPriceInr": 999,
            "monthlyPricePaise": 99900,
        },
    }
    checkout = _checkout(client, headers, workspace_id)
    assert checkout["listPricePaise"] == checkout["amountPaise"] == 62500
    assert razorpay_provider.order.create.call_args.args[0]["amount"] == 62500


@pytest.mark.parametrize("plan_key", ["growth", "enterprise"])
def test_free_coupon_checkout_fulfills_plan_and_confirm_is_idempotent(
    client, db, fake_redis, razorpay_provider, plan_key
):
    session, headers = _register(client)
    workspace_id = session["workspace"]["id"]
    coupon = _coupon(db, plan_key=plan_key, max_redemptions=1)
    dashboard_url = f"{API}/workspaces/{workspace_id}/social/dashboard/home"
    assert client.get(dashboard_url, headers=headers).status_code == 200
    version = int(fake_redis.get(f"dash:ver:{workspace_id}") or 0)
    payload = {"planKey": plan_key, "couponCode": "  billingtest  "}
    preview = client.post(
        f"{_url(workspace_id)}/coupons/validate", headers=headers, json=payload
    )
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "code": coupon["code"], "planKey": plan_key,
        "listPricePaise": PRICES[plan_key], "discountPaise": PRICES[plan_key],
        "amountPaise": 0, "currency": "INR",
    }
    assert db["billing_payments"].count_documents({}) == 0
    assert db["billing_coupon_redemptions"].count_documents({}) == 0
    checkout = _checkout(client, headers, workspace_id, **payload)
    assert checkout == {
        "mode": "free", "provider": "razorpay", "paymentId": checkout["paymentId"],
        "planKey": plan_key, "orderId": None, "keyId": None, "amountPaise": 0,
        "currency": "INR", "listPricePaise": PRICES[plan_key],
        "discountPaise": PRICES[plan_key], "couponCode": coupon["code"],
    }
    payment = db["billing_payments"].find_one({"id": checkout["paymentId"]})
    workspace = db["workspaces"].find_one({"id": workspace_id})
    assert payment["status"] == "paid"
    assert payment["provider_payment_id"] == "coupon_free"
    assert workspace["plan_period_end"] - payment["paid_at"] == timedelta(days=30)
    assert int(fake_redis.get(f"dash:ver:{workspace_id}")) == version + 1
    redemption = db["billing_coupon_redemptions"].find_one({"payment_id": payment["id"]})
    assert redemption["coupon_id"] == coupon["id"]
    assert redemption["workspace_id"] == workspace_id
    assert redemption["user_id"] == session["user"]["id"]
    assert redemption["plan_key"] == plan_key
    assert redemption["amount_paise"] == 0
    assert redemption["discount_paise"] == PRICES[plan_key]
    for _ in range(2):
        response = client.post(
            f"{_url(workspace_id)}/confirm", headers=headers,
            json={"paymentId": payment["id"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "paid"
        assert response.json()["paidAt"] is not None
        assert response.json()["couponCode"] == coupon["code"]
    assert db["billing_payments"].find_one({"id": payment["id"]}) == payment
    assert db["workspaces"].find_one({"id": workspace_id}) == workspace
    assert db["billing_coupon_redemptions"].count_documents({}) == 1
    assert db["billing_coupons"].find_one({"id": coupon["id"]})["redemption_count"] == 1
    assert int(fake_redis.get(f"dash:ver:{workspace_id}")) == version + 1
    _assert_plan(client, db, headers, workspace_id, plan_key)
    history = client.get(f"{_url(workspace_id)}/payments", headers=headers)
    assert history.status_code == 200, history.text
    assert history.json() == [response.json()]
    razorpay_provider.order.create.assert_not_called()
    razorpay_provider.utility.verify_payment_signature.assert_not_called()


@pytest.mark.parametrize("plan_key", ["growth", "enterprise"])
@pytest.mark.parametrize("discount_type", [None, "percent", "fixed_inr"])
def test_paid_checkout_and_confirm_fulfill_once(
    client, db, fake_redis, razorpay_provider, plan_key, discount_type
):
    session, headers = _register(client)
    workspace_id = session["workspace"]["id"]
    payload = {}
    discount = 0
    if discount_type:
        coupon = _coupon(db, discount_type=discount_type, discount_value=20, plan_key=plan_key)
        payload["couponCode"] = coupon["code"]
        discount = PRICES[plan_key] // 5 if discount_type == "percent" else 2000
    checkout = _checkout(client, headers, workspace_id, plan_key, **payload)
    amount = PRICES[plan_key] - discount
    assert checkout["mode"] == "checkout"
    assert checkout["provider"] == "razorpay"
    assert checkout["keyId"] == "rzp_test_billing"
    assert checkout["amountPaise"] == amount
    assert checkout["listPricePaise"] == PRICES[plan_key]
    assert checkout["discountPaise"] == discount
    assert checkout["currency"] == "INR"
    assert checkout["couponCode"] == payload.get("couponCode")
    payment_id = checkout["paymentId"]
    razorpay_provider.order.create.assert_called_once_with({
        "amount": amount, "currency": "INR", "receipt": payment_id.replace("-", "")[:40],
        "notes": {
            "workspace_id": workspace_id, "payment_id": payment_id,
            "plan_key": plan_key, "purpose": "social_plan",
        },
        "payment_capture": 1,
    })
    pending = db["billing_payments"].find_one({"id": payment_id})
    assert pending["status"] == "pending"
    assert pending["paid_at"] is None
    assert pending["provider_order_id"] == checkout["orderId"]
    assert pending["user_id"] == session["user"]["id"]
    assert db["billing_coupon_redemptions"].count_documents({}) == 0
    _assert_plan(client, db, headers, workspace_id, "starter")
    response = client.post(
        f"{_url(workspace_id)}/confirm", headers=headers, json=_confirmation(checkout)
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == payment_id
    assert response.json()["workspaceId"] == workspace_id
    assert response.json()["planKey"] == plan_key
    assert response.json()["amountPaise"] == amount
    assert response.json()["status"] == "paid"
    razorpay_provider.utility.verify_payment_signature.assert_called_once_with({
        "razorpay_order_id": checkout["orderId"],
        "razorpay_payment_id": "pay_test_billing", "razorpay_signature": "test-signature",
    })
    paid = db["billing_payments"].find_one({"id": payment_id})
    workspace = db["workspaces"].find_one({"id": workspace_id})
    assert paid["provider_payment_id"] == "pay_test_billing"
    assert workspace["plan_period_end"] - paid["paid_at"] == timedelta(days=30)
    version = fake_redis.get(f"dash:ver:{workspace_id}")
    repeated = client.post(
        f"{_url(workspace_id)}/confirm", headers=headers, json=_confirmation(checkout)
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == response.json()
    assert db["billing_payments"].find_one({"id": payment_id}) == paid
    assert db["workspaces"].find_one({"id": workspace_id}) == workspace
    assert fake_redis.get(f"dash:ver:{workspace_id}") == version
    assert razorpay_provider.utility.verify_payment_signature.call_count == 1
    if discount_type:
        redemption = db["billing_coupon_redemptions"].find_one({"payment_id": payment_id})
        assert redemption["amount_paise"] == amount
        assert redemption["discount_paise"] == discount
        assert redemption["workspace_id"] == workspace_id
        assert db["billing_coupons"].find_one({"id": coupon["id"]})["redemption_count"] == 1
    assert db["billing_coupon_redemptions"].count_documents({}) == int(bool(discount_type))
    _assert_plan(client, db, headers, workspace_id, plan_key)


@pytest.mark.parametrize(
    "overrides,detail",
    [
        (None, "Invalid coupon code"),
        ({"is_active": False}, "This coupon is no longer active"),
        ({"plan_key": "enterprise"}, "This coupon does not apply to the selected plan"),
        ({"valid_until": utcnow() - timedelta(days=1)}, "This coupon has expired"),
        ({"max_redemptions": 1}, "This coupon has been fully redeemed"),
        ({"discount_type": "percent", "discount_value": 0}, "Coupon does not reduce the price"),
    ],
)
def test_invalid_coupon_does_not_create_payment_or_charge(
    client, db, razorpay_provider, overrides, detail
):
    session, headers = _register(client)
    workspace_id = session["workspace"]["id"]
    if overrides is not None:
        coupon = _coupon(db, **overrides)
        if overrides.get("max_redemptions"):
            db["billing_coupons"].update_one(
                {"id": coupon["id"]}, {"$set": {"redemption_count": 1}}
            )
    workspace = db["workspaces"].find_one({"id": workspace_id})
    before_coupons = list(db["billing_coupons"].find())
    for endpoint in ("coupons/validate", "checkout"):
        response = client.post(
            f"{_url(workspace_id)}/{endpoint}", headers=headers,
            json={"planKey": "growth", "couponCode": "BILLINGTEST"},
        )
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == detail
    assert db["billing_payments"].count_documents({}) == 0
    assert db["billing_coupon_redemptions"].count_documents({}) == 0
    assert list(db["billing_coupons"].find()) == before_coupons
    assert db["workspaces"].find_one({"id": workspace_id}) == workspace
    razorpay_provider.order.create.assert_not_called()


@pytest.mark.parametrize("failure", ["missing_details", "order_mismatch", "invalid_signature"])
def test_paid_confirmation_rejects_invalid_details_without_fulfilling(
    client, db, razorpay_provider, failure
):
    session, headers = _register(client)
    workspace_id = session["workspace"]["id"]
    coupon = _coupon(db, discount_type="percent", discount_value=20)
    checkout = _checkout(client, headers, workspace_id, couponCode=coupon["code"])
    payload = _confirmation(checkout)
    expected_status = "pending"
    if failure == "missing_details":
        payload = {"paymentId": checkout["paymentId"]}
        detail = "Missing Razorpay payment details"
    elif failure == "order_mismatch":
        payload["razorpayOrderId"] = "order_wrong"
        detail = "Order mismatch"
    else:
        razorpay_provider.utility.verify_payment_signature.side_effect = ValueError("Invalid signature")
        detail = "Payment verification failed"
        expected_status = "failed"
    workspace = db["workspaces"].find_one({"id": workspace_id})
    response = client.post(f"{_url(workspace_id)}/confirm", headers=headers, json=payload)
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == detail
    payment = db["billing_payments"].find_one({"id": checkout["paymentId"]})
    assert payment["status"] == expected_status
    assert payment["paid_at"] is None
    assert db["workspaces"].find_one({"id": workspace_id}) == workspace
    assert db["billing_coupon_redemptions"].count_documents({}) == 0
    assert db["billing_coupons"].find_one({"id": coupon["id"]})["redemption_count"] == 0
    if failure != "invalid_signature":
        razorpay_provider.utility.verify_payment_signature.assert_not_called()


@pytest.mark.parametrize("level", ["viewer", "editor", "publisher", "admin"])
def test_billing_requires_social_admin_for_checkout_and_confirm(
    client, db, razorpay_provider, level
):
    owner, owner_headers = _register(client, "owner@example.com")
    member, member_headers = _register(client, "member@example.com")
    workspace_id = owner["workspace"]["id"]
    db["workspace_members"].insert_one({
        "id": new_id(), "workspace_id": workspace_id, "user_id": member["user"]["id"],
        "role": "member", "social_level": level, "created_at": utcnow(),
    })
    coupon = _coupon(db)
    for endpoint in ("plans", "payments"):
        assert client.get(f"{_url(workspace_id)}/{endpoint}", headers=member_headers).status_code == 200
    preview = client.post(
        f"{_url(workspace_id)}/coupons/validate", headers=member_headers,
        json={"planKey": "growth", "couponCode": coupon["code"]},
    )
    assert preview.status_code == 200, preview.text
    checkout = _checkout(client, owner_headers, workspace_id)
    before = list(db["billing_payments"].find())
    response = client.post(
        f"{_url(workspace_id)}/checkout", headers=member_headers, json={"planKey": "growth"}
    )
    confirm = client.post(
        f"{_url(workspace_id)}/confirm", headers=member_headers, json=_confirmation(checkout)
    )
    if level == "admin":
        assert response.status_code == 200, response.text
        assert confirm.status_code == 200, confirm.text
        assert db["billing_payments"].find_one({"id": response.json()["paymentId"]})["user_id"] == member["user"]["id"]
        _assert_plan(client, db, member_headers, workspace_id, "growth")
    else:
        for denied in (response, confirm):
            assert denied.status_code == 403, denied.text
            assert denied.json()["detail"] == "Requires social access level 'admin' or higher"
        assert list(db["billing_payments"].find()) == before
        assert db["workspaces"].find_one({"id": workspace_id})["plan"] == "starter"
        razorpay_provider.utility.verify_payment_signature.assert_not_called()


def test_billing_workspace_isolation_and_payment_ownership(client, db, razorpay_provider):
    first, first_headers = _register(client, "first@example.com")
    second, second_headers = _register(client, "second@example.com")
    first_id, second_id = first["workspace"]["id"], second["workspace"]["id"]
    checkout = _checkout(client, first_headers, first_id)
    for method, endpoint, payload in (
        ("get", "plans", None), ("get", "payments", None),
        ("post", "coupons/validate", {"planKey": "growth", "couponCode": "BILLINGTEST"}),
        ("post", "checkout", {"planKey": "growth"}),
        ("post", "confirm", _confirmation(checkout)),
    ):
        kwargs = {"headers": second_headers}
        if payload is not None:
            kwargs["json"] = payload
        response = getattr(client, method)(f"{_url(first_id)}/{endpoint}", **kwargs)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Access denied"
    for payment_id in (checkout["paymentId"], new_id()):
        response = client.post(
            f"{_url(second_id)}/confirm", headers=second_headers,
            json={**_confirmation(checkout), "paymentId": payment_id},
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Payment not found"
    assert db["billing_payments"].count_documents({}) == 1
    assert db["billing_payments"].find_one({"id": checkout["paymentId"]})["status"] == "pending"
    assert all(row["plan"] == "starter" for row in db["workspaces"].find())
    razorpay_provider.utility.verify_payment_signature.assert_not_called()


def test_payment_history_is_workspace_scoped_sorted_and_limited(client, db):
    first, first_headers = _register(client, "first@example.com")
    second, second_headers = _register(client, "second@example.com")
    first_id, second_id = first["workspace"]["id"], second["workspace"]["id"]
    payment_ids = []
    start = utcnow() - timedelta(days=1)
    for index in range(22):
        checkout = _checkout(client, first_headers, first_id)
        payment_ids.append(checkout["paymentId"])
        db["billing_payments"].update_one(
            {"id": checkout["paymentId"]}, {"$set": {"created_at": start + timedelta(seconds=index)}}
        )
    second_checkout = _checkout(client, second_headers, second_id)
    for workspace_id, headers, expected in (
        (first_id, first_headers, list(reversed(payment_ids))[:20]),
        (second_id, second_headers, [second_checkout["paymentId"]]),
    ):
        response = client.get(f"{_url(workspace_id)}/payments", headers=headers)
        assert response.status_code == 200, response.text
        rows = response.json()
        assert [row["id"] for row in rows] == expected
        assert all(row["workspaceId"] == workspace_id for row in rows)
        assert all(row["status"] == "pending" and row["paidAt"] is None for row in rows)
        assert all("provider_order_id" not in row and "user_id" not in row for row in rows)


@pytest.mark.parametrize(
    "current_plan,target_plan,detail",
    [
        ("starter", "starter", "Only Pro and Growth plans are available for self-serve checkout."),
        ("starter", "unknown", "Only Pro and Growth plans are available for self-serve checkout."),
        ("growth", "growth", "Select a higher plan than your current subscription."),
        ("enterprise", "growth", "Select a higher plan than your current subscription."),
    ],
)
def test_checkout_rejects_invalid_or_non_upgrade_plans_without_charge(
    client, db, razorpay_provider, current_plan, target_plan, detail
):
    session, headers = _register(client)
    workspace_id = session["workspace"]["id"]
    db["workspaces"].update_one({"id": workspace_id}, {"$set": {"plan": current_plan}})
    response = client.post(
        f"{_url(workspace_id)}/checkout", headers=headers, json={"planKey": target_plan}
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == detail
    assert db["billing_payments"].count_documents({}) == 0
    assert db["workspaces"].find_one({"id": workspace_id})["plan"] == current_plan
    razorpay_provider.order.create.assert_not_called()