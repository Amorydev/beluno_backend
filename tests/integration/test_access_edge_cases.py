"""Edge cases for identity and plans that the main suites do not reach."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from beluno.testkit.api_client import sign_in
from beluno.testkit.database import AdminDatabase
from beluno.testkit.identity import IdentityProviderStub

pytestmark = pytest.mark.integration


# ============================================================================
# HTTP: Cursor and If-Match validation
# ============================================================================


async def test_invalid_cursor_format_returns_422(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Malformed cursor (not base64) raises validation error."""
    owner = await sign_in(api, identity_provider, name="Owner")
    resp = await api.get("/v1/plans?cursor=!!!invalid!!!", headers=owner.headers)
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_FAILED"


async def test_invalid_if_match_format_returns_422(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """If-Match header without quotes or version raises validation error."""
    owner = await sign_in(api, identity_provider)
    plan = await api.post(
        "/v1/plans",
        json={"title": "Test", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]
    resp = await api.patch(
        f"/v1/plans/{plan_id}",
        json={"title": "Updated"},
        headers={**owner.headers, "If-Match": "no-quotes"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "VALIDATION_FAILED"


async def test_pagination_cursor_round_trip_for_plans(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Pagination: create multiple plans, fetch with cursor, verify round-trip."""
    owner = await sign_in(api, identity_provider)
    # Create 3 plans
    plan_ids = []
    for i in range(3):
        resp = await api.post(
            "/v1/plans",
            json={"title": f"Plan {i}", "base_currency": "USD"},
            headers=owner.headers,
        )
        assert resp.status_code == 201
        plan_ids.append(resp.json()["id"])

    # Fetch first page (limit 1)
    page1 = await api.get("/v1/plans?limit=1", headers=owner.headers)
    assert page1.status_code == 200
    data1 = page1.json()
    assert len(data1["items"]) == 1
    assert "next_cursor" in data1

    # Fetch second page using cursor
    page2 = await api.get(f"/v1/plans?limit=1&cursor={data1['next_cursor']}", headers=owner.headers)
    assert page2.status_code == 200
    data2 = page2.json()
    assert len(data2["items"]) == 1
    assert data1["items"][0]["id"] != data2["items"][0]["id"]


# ============================================================================
# Identity: Session and email challenge edge cases
# ============================================================================


async def test_refresh_with_unknown_token_returns_401(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Refresh token that doesn't exist returns 401."""
    unknown_token = "unknown_token_not_in_db"
    resp = await api.post(
        "/v1/auth/refresh",
        json={"refresh_token": unknown_token},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTHENTICATION_REQUIRED"


async def test_session_cannot_refresh_after_revocation(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    """Session can only refresh once; after revocation, further refresh fails."""
    user = await sign_in(api, identity_provider, subject="revoke-test")
    refresh_token = user.refresh_token
    assert refresh_token is not None

    # Manually revoke the session (must include revoked_reason)
    admin.execute(
        "UPDATE iam.sessions SET revoked_at = now(), revoked_reason = 'logout' WHERE id = %s",
        user.session_id,
    )

    # Attempt refresh should fail
    resp = await api.post(
        "/v1/auth/refresh",
        json={"refresh_token": refresh_token},
    )
    assert resp.status_code == 401
    assert resp.json()["code"] == "AUTHENTICATION_REQUIRED"


# ============================================================================
# Groups: Invite edge cases
# ============================================================================


# ============================================================================
# Plans: Participant role transitions
# ============================================================================


async def test_owner_transfer_to_non_registered_participant_fails(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Owner cannot transfer ownership to a placeholder participant."""
    owner = await sign_in(api, identity_provider)

    # Create plan
    plan = await api.post(
        "/v1/plans",
        json={"title": "Test", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Add a placeholder participant
    resp = await api.post(
        f"/v1/plans/{plan_id}/participants",
        json={"placeholder_name": "Guest"},
        headers=owner.headers,
    )
    assert resp.status_code == 201
    placeholder_id = resp.json()["id"]

    # Try to transfer ownership to placeholder (should fail)
    plan_version = (await api.get(f"/v1/plans/{plan_id}", headers=owner.headers)).json()["version"]
    resp = await api.post(
        f"/v1/plans/{plan_id}/ownership-transfer",
        json={"participant_id": str(placeholder_id)},
        headers={**owner.headers, "If-Match": f'"{plan_version}"'},
    )
    # Should fail with 409 (INVALID_STATE) or 422 (VALIDATION_FAILED)
    assert resp.status_code in (409, 422)
    assert resp.json()["code"] in ("INVALID_STATE", "VALIDATION_FAILED")


async def test_guest_cannot_be_promoted_to_admin(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    """Guest participants cannot be promoted to admin or any registered role."""
    owner = await sign_in(api, identity_provider, subject="owner-guest")

    # Create plan
    plan = await api.post(
        "/v1/plans",
        json={"title": "Test", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Add a guest placeholder
    guest_id = str(uuid4())
    admin.execute(
        "INSERT INTO plans.plan_participants "
        "(id, plan_id, identity_kind, user_id, display_name, role, access_state, "
        "rsvp_status, version, created_at, updated_at) "
        "VALUES (%s, %s, 'placeholder', NULL, 'Guest', 'member', 'active', 'invited', 1, "
        "now(), now())",
        guest_id,
        plan_id,
    )

    # Try to promote guest to admin (should fail)
    resp = await api.patch(
        f"/v1/plans/{plan_id}/participants/{guest_id}",
        json={"role": "admin"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_stale_if_match_returns_412(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """If-Match with wrong version returns 412 VERSION_CONFLICT."""
    owner = await sign_in(api, identity_provider)
    plan = await api.post(
        "/v1/plans",
        json={"title": "Test", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Update once to bump version to 2
    resp1 = await api.patch(
        f"/v1/plans/{plan_id}",
        json={"title": "Updated"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert resp1.status_code == 200
    assert resp1.json()["version"] == 2

    # Try to update with stale version
    resp2 = await api.patch(
        f"/v1/plans/{plan_id}",
        json={"title": "StaleUpdate"},
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert resp2.status_code == 412
    assert resp2.json()["code"] == "VERSION_CONFLICT"


async def test_archived_plan_rejects_participant_add(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    """Archived plan rejects participant add."""
    owner = await sign_in(api, identity_provider)
    other_user = await sign_in(api, identity_provider)

    # Create plan
    plan = await api.post(
        "/v1/plans",
        json={"title": "Test", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Archive the plan
    admin.execute(
        "UPDATE plans.plans SET state = 'archived' WHERE id = %s",
        plan_id,
    )

    # Try to add participant (should fail with INVALID_STATE_TRANSITION)
    resp = await api.post(
        f"/v1/plans/{plan_id}/participants",
        json={"user_id": str(other_user.user_id)},
        headers=owner.headers,
    )
    # Should be 409 or 422 (INVALID_STATE_TRANSITION or similar)
    assert resp.status_code in (409, 422, 403)


async def test_join_request_rejection_transitions_to_removed(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    """Rejecting a join request moves participant to REMOVED state."""
    owner = await sign_in(api, identity_provider)
    requester = await sign_in(api, identity_provider)

    # Create plan
    plan = await api.post(
        "/v1/plans",
        json={"title": "Test Plan", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Manually add requester in pending_approval state via database
    participant_id = str(uuid4())
    admin.execute(
        "INSERT INTO plans.plan_participants "
        "(id, plan_id, identity_kind, user_id, display_name, role, access_state, "
        "rsvp_status, version, created_at, updated_at) "
        "VALUES (%s, %s, 'user', %s, 'Requester', 'member', 'pending_approval', "
        "'invited', 1, now(), now())",
        participant_id,
        plan_id,
        requester.user_id,
    )

    # Owner rejects request
    resp = await api.post(
        f"/v1/plans/{plan_id}/participants/{participant_id}/review",
        json={"approve": False},
        headers=owner.headers,
    )
    assert resp.status_code == 200
    result = resp.json()
    assert result["access_state"] == "removed"


# ============================================================================
# Plans: Travel segment validation
# ============================================================================


# ============================================================================
# Plans: Series split/cancel edge cases
# ============================================================================


# ============================================================================
# Plans: Invites and guest claims
# ============================================================================


async def test_plan_invite_preview_rate_limiting_includes_retry_after(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Invite preview rate limit (429) includes Retry-After header."""
    owner = await sign_in(api, identity_provider)

    # Create plan and invite
    plan = await api.post(
        "/v1/plans",
        json={"title": "Private", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Create invite
    invite_resp = await api.post(
        f"/v1/plans/{plan_id}/invites",
        json={"role": "member", "max_uses": 3, "expires_in_hours": 24},
        headers=owner.headers,
    )
    token = invite_resp.json()["token"]

    # The per-client preview limit is 60 per 10 minutes; the 61st request is refused.
    statuses = [
        (await api.post("/v1/invites/preview", json={"token": token})).status_code
        for _ in range(61)
    ]
    assert statuses[:60] == [200] * 60
    limited = await api.post("/v1/invites/preview", json={"token": token})
    assert limited.status_code == 429
    assert int(limited.headers["Retry-After"]) >= 1
    assert limited.json()["code"] == "RATE_LIMITED"
