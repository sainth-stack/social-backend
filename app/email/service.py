"""Transactional email via Resend (same provider as OpsBrain-Backend)."""

from __future__ import annotations

import logging
from html import escape
from typing import Optional, Sequence, Union

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailService:
    def send_html_email(
        self,
        *,
        to: Union[str, Sequence[str]],
        subject: str,
        html: str,
    ) -> bool:
        if not settings.resend_api_key:
            logger.warning("RESEND_API_KEY is not configured; skipping email (%s)", subject)
            return False

        recipients = [to] if isinstance(to, str) else list(to)
        if not recipients:
            return False

        try:
            import resend

            resend.api_key = settings.resend_api_key
            resend.Emails.send(
                {
                    "from": settings.resend_from_email,
                    "to": recipients,
                    "subject": subject,
                    "html": html,
                }
            )
            return True
        except Exception:
            logger.exception("Failed to send email: %s", subject)
            return False

    def send_password_reset(self, *, to: str, full_name: Optional[str], reset_url: str) -> bool:
        name = escape(full_name or "there")
        url = escape(reset_url)
        html = f"""
        <div style="font-family:Inter,system-ui,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
          <h2 style="margin:0 0 12px;font-size:20px">Reset your password</h2>
          <p style="margin:0 0 16px;line-height:1.5">Hi {name},</p>
          <p style="margin:0 0 16px;line-height:1.5">
            We received a request to reset your OpsBrain AI Social Media Manager password.
            This link expires in 60 minutes.
          </p>
          <p style="margin:0 0 24px">
            <a href="{url}" style="display:inline-block;background:#4f46e5;color:#fff;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:600">
              Reset password
            </a>
          </p>
          <p style="margin:0;font-size:13px;color:#64748b;line-height:1.5">
            If you did not request this, you can ignore this email.
          </p>
        </div>
        """
        return self.send_html_email(
            to=to,
            subject="Reset your OpsBrain AI password",
            html=html,
        )

    def send_welcome(self, *, to: str, full_name: Optional[str], workspace_name: str) -> bool:
        name = escape(full_name or "there")
        workspace = escape(workspace_name)
        login_url = escape(f"{settings.frontend_url.rstrip('/')}/login")
        html = f"""
        <div style="font-family:Inter,system-ui,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#1e293b">
          <h2 style="margin:0 0 12px;font-size:20px">Welcome to OpsBrain AI</h2>
          <p style="margin:0 0 16px;line-height:1.5">Hi {name},</p>
          <p style="margin:0 0 16px;line-height:1.5">
            Your workspace <strong>{workspace}</strong> is ready on the Free plan.
            An admin can upgrade your plan when you're ready for Growth or Enterprise.
          </p>
          <p style="margin:0 0 24px">
            <a href="{login_url}" style="display:inline-block;background:#4f46e5;color:#fff;text-decoration:none;padding:12px 18px;border-radius:8px;font-weight:600">
              Open the app
            </a>
          </p>
        </div>
        """
        return self.send_html_email(
            to=to,
            subject="Welcome to OpsBrain AI Social Media Manager",
            html=html,
        )


email_service = EmailService()
