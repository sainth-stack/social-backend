"""LinkedIn OAuth 2.0 — member profile + company Pages the user administers."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import quote, urlencode

import httpx
from fastapi import HTTPException, status

from app.core.config import settings
from app.social.models import SocialAccountType, SocialPlatform
from app.social.oauth.base import OAuthAccountProfile, OAuthHandler, OAuthTokenResult

logger = logging.getLogger(__name__)

# Always-safe member scopes (Share on LinkedIn + Sign In with LinkedIn).
LINKEDIN_MEMBER_SCOPES = [
    "openid",
    "profile",
    "email",
    "w_member_social",
]

# Company Page scopes — only when Community Management API is approved on the app.
# Requesting these without approval breaks OAuth: unauthorized_scope_error.
LINKEDIN_ORG_SCOPES = [
    "rw_organization_admin",
    "w_organization_social",
    "r_organization_social",
]

_API_HEADERS = {
    "X-Restli-Protocol-Version": "2.0.0",
    "LinkedIn-Version": "202401",
}


class LinkedInOAuth(OAuthHandler):
    platform = SocialPlatform.LINKEDIN

    def __init__(self) -> None:
        self.client_id = settings.linkedin_client_id
        self.client_secret = settings.linkedin_client_secret
        self.redirect_uri = settings.linkedin_redirect_uri

    def _require_credentials(self) -> None:
        if not self.client_id or not self.client_secret:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "LinkedIn OAuth is not configured. Set LINKEDIN_CLIENT_ID and "
                    "LINKEDIN_CLIENT_SECRET in the backend environment."
                ),
            )

    def _scopes(self) -> list[str]:
        scopes = list(LINKEDIN_MEMBER_SCOPES)
        if settings.linkedin_organization_scopes:
            scopes.extend(LINKEDIN_ORG_SCOPES)
        return scopes

    def build_authorization_url(self, state: str) -> str:
        self._require_credentials()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "scope": " ".join(self._scopes()),
        }
        return f"https://www.linkedin.com/oauth/v2/authorization?{urlencode(params)}"

    def exchange_code(self, code: str, *, state: str | None = None) -> OAuthTokenResult:
        self._require_credentials()
        token_data = self._exchange_code(code)
        access_token = token_data["access_token"]
        expires_in = int(token_data.get("expires_in") or 3600)
        token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        refresh_token = token_data.get("refresh_token")

        accounts: list[OAuthAccountProfile] = []

        # 1) Personal profile (always)
        profile = self._get_profile(access_token)
        person_id = str(profile.get("sub") or profile.get("id") or "").strip()
        if person_id:
            person_urn = f"urn:li:person:{person_id}"
            accounts.append(
                OAuthAccountProfile(
                    platform_account_id=f"person:{person_id}",
                    account_name=profile.get("name")
                    or f"{profile.get('given_name', '')} {profile.get('family_name', '')}".strip()
                    or "LinkedIn Profile",
                    account_type=SocialAccountType.PROFILE,
                    account_picture_url=profile.get("picture"),
                    follower_count=self._network_size(access_token, person_urn),
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expires_at=token_expires_at,
                )
            )

        # 2) Company Pages the member administers
        for org in self._list_administered_organizations(access_token):
            workspace_id = org["id"]
            org_urn = f"urn:li:workspace:{workspace_id}"
            accounts.append(
                OAuthAccountProfile(
                    platform_account_id=f"workspace:{workspace_id}",
                    account_name=org.get("name") or f"LinkedIn Page {workspace_id}",
                    account_type=SocialAccountType.PAGE,
                    account_picture_url=org.get("picture"),
                    follower_count=self._network_size(
                        access_token,
                        org_urn,
                        edge_type="CompanyFollowedByMember",
                    ),
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expires_at=token_expires_at,
                )
            )

        if not accounts:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="LinkedIn profile id missing",
            )

        return OAuthTokenResult(accounts=accounts)

    def sync_account_stats(self, platform_account_id: str, access_token: str) -> dict:
        kind, entity_id = _split_account_id(platform_account_id)
        if kind == "workspace":
            org = self._get_organization(access_token, entity_id)
            urn = f"urn:li:workspace:{entity_id}"
            return {
                "account_name": org.get("name") or f"LinkedIn Page {entity_id}",
                "account_picture_url": org.get("picture"),
                "follower_count": self._network_size(
                    access_token, urn, edge_type="CompanyFollowedByMember"
                ),
            }

        profile = self._get_profile(access_token)
        person_id = entity_id or str(profile.get("sub") or profile.get("id") or "")
        urn = f"urn:li:person:{person_id}" if person_id else ""
        return {
            "account_name": profile.get("name")
            or f"{profile.get('given_name', '')} {profile.get('family_name', '')}".strip(),
            "account_picture_url": profile.get("picture"),
            "follower_count": self._network_size(access_token, urn) if urn else 0,
        }

    # ── Internals ─────────────────────────────────────────────────────────────

    def _exchange_code(self, code: str) -> dict[str, Any]:
        url = "https://www.linkedin.com/oauth/v2/accessToken"
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, data=data)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPError as exc:
            logger.error("LinkedIn token exchange failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"LinkedIn authorization failed: {exc}",
            ) from exc

    def _auth_headers(self, access_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {access_token}",
            **_API_HEADERS,
        }

    def _get_profile(self, access_token: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    "https://api.linkedin.com/v2/userinfo",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if response.status_code == 200:
                    return response.json()
        except httpx.HTTPError:
            pass

        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                "https://api.linkedin.com/v2/me",
                headers=self._auth_headers(access_token),
                params={
                    "projection": (
                        "(id,localizedFirstName,localizedLastName,"
                        "profilePicture(displayImage~:playableStreams))"
                    )
                },
            )
            response.raise_for_status()
            data = response.json()
        name = (
            f"{data.get('localizedFirstName', '')} {data.get('localizedLastName', '')}"
        ).strip()
        picture = _extract_profile_picture(data)
        return {"id": data.get("id"), "name": name or "LinkedIn User", "picture": picture}

    def _list_administered_organizations(
        self, access_token: str
    ) -> list[dict[str, Any]]:
        """Return company Pages where the member is an approved admin."""
        elements: list[dict[str, Any]] = []
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    "https://api.linkedin.com/v2/organizationAcls",
                    headers=self._auth_headers(access_token),
                    params={
                        "q": "roleAssignee",
                        "role": "ADMINISTRATOR",
                        "state": "APPROVED",
                        "projection": (
                            "(elements*(workspace,role,state,"
                            "workspace~(id,localizedName,logoV2(original~:playableStreams))))"
                        ),
                    },
                )
                if response.status_code >= 400:
                    # Scopes / Community Management API not approved yet.
                    logger.warning(
                        "LinkedIn organizationAcls failed (%s): %s",
                        response.status_code,
                        response.text[:300],
                    )
                    return []
                elements = list((response.json() or {}).get("elements") or [])
        except httpx.HTTPError as exc:
            logger.warning("LinkedIn organizationAcls request failed: %s", exc)
            return []

        orgs: list[dict[str, Any]] = []
        seen: set[str] = set()
        for element in elements:
            org_urn = str(element.get("workspace") or "")
            workspace_id = _urn_id(org_urn, "workspace")
            if not workspace_id or workspace_id in seen:
                continue
            seen.add(workspace_id)

            embedded = element.get("workspace~") or {}
            name = embedded.get("localizedName")
            picture = _extract_org_logo(embedded)
            if not name:
                details = self._get_organization(access_token, workspace_id)
                name = details.get("name")
                picture = picture or details.get("picture")

            orgs.append({"id": workspace_id, "name": name, "picture": picture})
        return orgs

    def _get_organization(self, access_token: str, workspace_id: str) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"https://api.linkedin.com/v2/workspaces/{workspace_id}",
                    headers=self._auth_headers(access_token),
                    params={
                        "projection": (
                            "(id,localizedName,logoV2(original~:playableStreams))"
                        )
                    },
                )
                if response.status_code >= 400:
                    logger.warning(
                        "LinkedIn workspace %s fetch failed: %s",
                        workspace_id,
                        response.text[:200],
                    )
                    return {"id": workspace_id, "name": f"LinkedIn Page {workspace_id}"}
                data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("LinkedIn workspace fetch error: %s", exc)
            return {"id": workspace_id, "name": f"LinkedIn Page {workspace_id}"}

        return {
            "id": str(data.get("id") or workspace_id),
            "name": data.get("localizedName") or f"LinkedIn Page {workspace_id}",
            "picture": _extract_org_logo(data),
        }

    def _network_size(
        self,
        access_token: str,
        urn: str,
        *,
        edge_type: str = "FirstDegreeConnection",
    ) -> int:
        """Follower / connection count. Returns 0 when the scope is unavailable."""
        if not urn:
            return 0
        try:
            encoded = quote(urn, safe="")
            with httpx.Client(timeout=30.0) as client:
                response = client.get(
                    f"https://api.linkedin.com/v2/networkSizes/{encoded}",
                    headers=self._auth_headers(access_token),
                    params={"edgeType": edge_type},
                )
                if response.status_code >= 400:
                    logger.info(
                        "LinkedIn networkSizes unavailable for %s (%s)",
                        urn,
                        response.status_code,
                    )
                    return 0
                data = response.json() or {}
                return int(data.get("firstDegreeSize") or data.get("count") or 0)
        except Exception as exc:
            logger.info("LinkedIn networkSizes failed for %s: %s", urn, exc)
            return 0


def _split_account_id(platform_account_id: str) -> tuple[str, str]:
    """Return (kind, id) for person:ID / workspace:ID / legacy bare person id."""
    value = (platform_account_id or "").strip()
    if value.startswith("workspace:"):
        return "workspace", value.split(":", 1)[1]
    if value.startswith("person:"):
        return "person", value.split(":", 1)[1]
    if value.startswith("urn:li:workspace:"):
        return "workspace", value.rsplit(":", 1)[-1]
    if value.startswith("urn:li:person:"):
        return "person", value.rsplit(":", 1)[-1]
    return "person", value


def _urn_id(urn: str, entity: str) -> Optional[str]:
    prefix = f"urn:li:{entity}:"
    if urn.startswith(prefix):
        return urn[len(prefix) :]
    return None


def _extract_profile_picture(data: dict[str, Any]) -> Optional[str]:
    try:
        display = (data.get("profilePicture") or {}).get("displayImage~") or {}
        elements = display.get("elements") or []
        if not elements:
            return None
        identifiers = (elements[-1].get("identifiers") or [])
        if identifiers:
            return identifiers[0].get("identifier")
    except Exception:
        return None
    return None


def _extract_org_logo(data: dict[str, Any]) -> Optional[str]:
    try:
        logo = (data.get("logoV2") or {}).get("original~") or {}
        elements = logo.get("elements") or []
        if not elements:
            return None
        identifiers = (elements[-1].get("identifiers") or [])
        if identifiers:
            return identifiers[0].get("identifier")
    except Exception:
        return None
    return None
