from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import HTTPException, status

from app.core.config import settings

logger = logging.getLogger(__name__)


class RazorpayAdapter:
    def __init__(self) -> None:
        self.key_id = (settings.razorpay_key_id or "").strip()
        self.key_secret = (settings.razorpay_key_secret or "").strip()
        self.webhook_secret = (settings.razorpay_webhook_secret or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.key_id and self.key_secret)

    def _client(self):
        if not self.configured:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Razorpay API keys are not configured on the server.",
            )
        try:
            import razorpay  # type: ignore
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="razorpay package is not installed on the server",
            ) from exc
        return razorpay.Client(auth=(self.key_id, self.key_secret))

    @staticmethod
    def _raise_razorpay_error(exc: Exception) -> None:
        detail = str(exc).strip() or "Razorpay request failed"
        status_code = status.HTTP_400_BAD_REQUEST
        if exc.__class__.__name__ in {"ServerError", "GatewayError"}:
            status_code = status.HTTP_502_BAD_GATEWAY
        raise HTTPException(status_code=status_code, detail=f"Razorpay: {detail}") from exc

    def create_order(
        self,
        *,
        amount_paise: int,
        currency: str,
        receipt: str,
        notes: dict[str, str],
    ) -> dict[str, Any]:
        client = self._client()
        try:
            order = client.order.create(
                {
                    "amount": int(amount_paise),
                    "currency": currency,
                    "receipt": receipt[:40],
                    "notes": notes,
                    "payment_capture": 1,
                }
            )
        except Exception as exc:
            self._raise_razorpay_error(exc)
        return {
            "mode": "checkout",
            "provider": "razorpay",
            "orderId": order["id"],
            "amountPaise": int(order["amount"]),
            "currency": order.get("currency") or currency,
            "keyId": self.key_id,
        }

    def verify_webhook_signature(self, body: bytes, signature: str | None) -> bool:
        if not self.webhook_secret or not signature:
            return False
        digest = hmac.new(
            self.webhook_secret.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(digest, signature)

    def verify_payment_signature(
        self, *, order_id: str, payment_id: str, signature: str
    ) -> bool:
        if not self.configured:
            return False
        client = self._client()
        try:
            client.utility.verify_payment_signature(
                {
                    "razorpay_order_id": order_id,
                    "razorpay_payment_id": payment_id,
                    "razorpay_signature": signature,
                }
            )
            return True
        except Exception:
            logger.warning("razorpay payment signature verification failed", exc_info=True)
            return False
