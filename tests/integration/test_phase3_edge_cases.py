"""Phase 3 edge cases: identity, groups, plans with focus on uncovered branches."""

from __future__ import annotations

from datetime import datetime
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


async def test_pagination_cursor_round_trip_for_groups(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Pagination for groups: create, fetch with cursor, verify items."""
    owner = await sign_in(api, identity_provider)
    group_ids = []
    for i in range(2):
        resp = await api.post(
            "/v1/groups",
            json={
                "name": f"Group {i}",
                "default_currency": "USD",
                "default_timezone": "UTC",
            },
            headers=owner.headers,
        )
        assert resp.status_code == 201
        group_ids.append(resp.json()["id"])

    page1 = await api.get("/v1/groups?limit=1", headers=owner.headers)
    assert page1.status_code == 200
    data1 = page1.json()
    assert len(data1["items"]) == 1
    assert data1["next_cursor"] is not None
    page2 = await api.get(
        f"/v1/groups?limit=1&cursor={data1['next_cursor']}", headers=owner.headers
    )
    seen = [data1["items"][0]["id"], page2.json()["items"][0]["id"]]
    assert sorted(seen) == sorted(group_ids)


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


async def test_admin_cannot_invite_admin_to_group(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub, admin: AdminDatabase
) -> None:
    """Only owner can invite admins; admin inviting admin is forbidden."""
    owner = await sign_in(api, identity_provider, subject="owner-test")
    admin_user = await sign_in(api, identity_provider, subject="admin-test")

    # Owner creates group
    group = await api.post(
        "/v1/groups",
        json={
            "name": "Test Group",
            "default_currency": "USD",
            "default_timezone": "UTC",
        },
        headers=owner.headers,
    )
    assert group.status_code == 201
    group_id = group.json()["id"]

    # Owner promotes owner's partner to admin
    admin.execute(
        "INSERT INTO groups.group_memberships "
        "(group_id, user_id, role, state, joined_at, version, created_at, updated_at) "
        "VALUES (%s, %s, 'admin', 'active', now(), 1, now(), now())",
        group_id,
        admin_user.user_id,
    )

    # Admin tries to invite another admin (should fail)
    resp = await api.post(
        f"/v1/groups/{group_id}/invites",
        json={"role": "admin", "max_uses": None, "expires_in_hours": 24},
        headers=admin_user.headers,
    )
    assert resp.status_code == 403
    assert resp.json()["code"] == "FORBIDDEN"


async def test_group_invite_redeem_by_active_member_is_idempotent(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    """Redeeming invite when already active returns same membership, no use_count increment."""
    owner = await sign_in(api, identity_provider, subject="owner-invite")
    member = await sign_in(api, identity_provider, subject="member-invite")

    # Create group
    group = await api.post(
        "/v1/groups",
        json={
            "name": "Test Group",
            "default_currency": "USD",
            "default_timezone": "UTC",
        },
        headers=owner.headers,
    )
    group_id = group.json()["id"]

    # Create invite
    invite_resp = await api.post(
        f"/v1/groups/{group_id}/invites",
        json={"role": "member", "max_uses": None, "expires_in_hours": 24},
        headers=owner.headers,
    )
    token = invite_resp.json()["token"]

    # First redemption
    first = await api.post(
        "/v1/invites/redeem",
        json={"token": token},
        headers=member.headers,
    )
    assert first.status_code == 200
    # Verify it returned a membership/group structure
    assert "group" in first.json()

    # Get use_count after first redeem
    list_resp = await api.get(
        f"/v1/groups/{group_id}/invites",
        headers=owner.headers,
    )
    assert list_resp.status_code == 200
    initial_use_count = list_resp.json()[0]["use_count"]

    # Second redemption by same user
    second = await api.post(
        "/v1/invites/redeem",
        json={"token": token},
        headers=member.headers,
    )
    assert second.status_code == 200

    # Verify use_count did not increment
    list_resp2 = await api.get(
        f"/v1/groups/{group_id}/invites",
        headers=owner.headers,
    )
    final_use_count = list_resp2.json()[0]["use_count"]
    assert final_use_count == initial_use_count


async def test_group_invite_when_group_deleted_returns_404(
    api: httpx.AsyncClient,
    identity_provider: IdentityProviderStub,
    admin: AdminDatabase,
) -> None:
    """Group invite preview/redeem when group is in deletion_scheduled state fails."""
    owner = await sign_in(api, identity_provider, subject="owner-del")
    member = await sign_in(api, identity_provider, subject="member-del")

    # Create group and invite
    group = await api.post(
        "/v1/groups",
        json={
            "name": "Test Group",
            "default_currency": "USD",
            "default_timezone": "UTC",
        },
        headers=owner.headers,
    )
    group_id = group.json()["id"]

    invite_resp = await api.post(
        f"/v1/groups/{group_id}/invites",
        json={"role": "member", "max_uses": None, "expires_in_hours": 24},
        headers=owner.headers,
    )
    token = invite_resp.json()["token"]

    # Schedule deletion of the group
    admin.execute(
        "UPDATE groups.groups SET state = 'deletion_scheduled', "
        "deletion_scheduled_at = now() WHERE id = %s",
        group_id,
    )

    # Attempt to redeem should return 404
    resp = await api.post(
        "/v1/invites/redeem",
        json={"token": token},
        headers=member.headers,
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "INVITE_UNAVAILABLE"


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


async def test_travel_segment_arrival_before_departure_returns_422(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Travel segment with arrival before departure raises validation error."""
    owner = await sign_in(api, identity_provider)
    plan = await api.post(
        "/v1/plans",
        json={"title": "Trip", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Create travel details
    await api.put(
        f"/v1/plans/{plan_id}/travel",
        json={"destination_summary": "Paris", "notes": None},
        headers=owner.headers,
    )

    # Try to add segment with arrival before departure
    # Use fixed dates/times to avoid timezone issues
    departure = datetime(2026, 10, 10, 14, 0, 0)  # 2 PM
    arrival = datetime(2026, 10, 10, 12, 0, 0)  # 12 PM (before departure)

    resp = await api.post(
        f"/v1/plans/{plan_id}/travel/segments",
        json={
            "segment_type": "flight",
            "title": "Flight",
            "origin_label": "JFK",
            "destination_label": "CDG",
            "timing_mode": "datetime",
            "start_date": None,
            "end_date": None,
            "departure_local": departure.isoformat(),
            "departure_timezone": "America/New_York",
            "arrival_local": arrival.isoformat(),
            "arrival_timezone": "Europe/Paris",
            "sort_order": 0,
        },
        headers=owner.headers,
    )
    assert resp.status_code == 422
    error_msg = str(resp.json())
    assert "arrival" in error_msg.lower()


async def test_travel_segment_update_stale_version_returns_412(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Travel segment update with stale If-Match returns 412."""
    owner = await sign_in(api, identity_provider)
    plan = await api.post(
        "/v1/plans",
        json={"title": "Trip", "base_currency": "USD"},
        headers=owner.headers,
    )
    plan_id = plan.json()["id"]

    # Create travel details
    travel = await api.put(
        f"/v1/plans/{plan_id}/travel",
        json={"destination_summary": "Paris", "notes": None},
        headers=owner.headers,
    )
    assert travel.status_code == 200

    # Add segment
    segment = await api.post(
        f"/v1/plans/{plan_id}/travel/segments",
        json={
            "segment_type": "flight",
            "title": "Flight",
            "origin_label": "JFK",
            "destination_label": "CDG",
            "timing_mode": "date",
            "start_date": "2026-10-10",
            "end_date": None,
            "departure_local": None,
            "departure_timezone": None,
            "arrival_local": None,
            "arrival_timezone": None,
            "sort_order": 0,
        },
        headers=owner.headers,
    )
    assert segment.status_code == 201
    segment_id = segment.json()["id"]

    # Update once using PUT
    resp1 = await api.put(
        f"/v1/plans/{plan_id}/travel/segments/{segment_id}",
        json={
            "segment_type": "train",
            "title": "Train",
            "origin_label": None,
            "destination_label": None,
            "timing_mode": "date",
            "start_date": "2026-10-11",
            "end_date": None,
            "departure_local": None,
            "departure_timezone": None,
            "arrival_local": None,
            "arrival_timezone": None,
            "sort_order": 1,
        },
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert resp1.status_code == 200

    # Update with stale version
    resp2 = await api.put(
        f"/v1/plans/{plan_id}/travel/segments/{segment_id}",
        json={
            "segment_type": "bus",
            "title": "Bus",
            "origin_label": None,
            "destination_label": None,
            "timing_mode": "date",
            "start_date": "2026-10-12",
            "end_date": None,
            "departure_local": None,
            "departure_timezone": None,
            "arrival_local": None,
            "arrival_timezone": None,
            "sort_order": 2,
        },
        headers={**owner.headers, "If-Match": '"1"'},
    )
    assert resp2.status_code == 412
    assert resp2.json()["code"] == "VERSION_CONFLICT"


# ============================================================================
# Plans: Series split/cancel edge cases
# ============================================================================


async def test_series_cancel_twice_returns_409(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Cancelling an already-cancelled series returns 409 INVALID_STATE_TRANSITION."""
    owner = await sign_in(api, identity_provider)

    # Create a series with minimal validation
    series_resp = await api.post(
        "/v1/plan-series",
        json={
            "title": "Weekly Dinner",
            "kind": "dinner",
            "base_currency": "USD",
            "timezone": "UTC",
            "start_date": "2026-10-20",
            "local_start_time": "18:00",
            "duration_minutes": 60,
            "recurrence_rule": "FREQ=WEEKLY",
            "participant_user_ids": [],
            "horizon_days": 90,
        },
        headers=owner.headers,
    )
    assert series_resp.status_code == 201, series_resp.text

    series_id = series_resp.json()["series"]["id"]
    version = series_resp.json()["series"]["version"]

    # Cancel once
    resp1 = await api.post(
        f"/v1/plan-series/{series_id}/cancel",
        json={},
        headers={**owner.headers, "If-Match": f'"{version}"'},
    )
    assert resp1.status_code == 200
    version = resp1.json()["version"]

    # Cancel again (should fail)
    resp2 = await api.post(
        f"/v1/plan-series/{series_id}/cancel",
        json={},
        headers={**owner.headers, "If-Match": f'"{version}"'},
    )
    assert resp2.status_code == 409
    assert resp2.json()["code"] == "INVALID_STATE_TRANSITION"


async def test_series_split_with_from_date_in_past_returns_422(
    api: httpx.AsyncClient, identity_provider: IdentityProviderStub
) -> None:
    """Series split with from_date in the past returns 422."""
    owner = await sign_in(api, identity_provider)

    series_resp = await api.post(
        "/v1/plan-series",
        json={
            "title": "Weekly Coffee",
            "kind": "coffee",
            "base_currency": "USD",
            "timezone": "UTC",
            "start_date": "2026-10-20",
            "local_start_time": "09:00",
            "duration_minutes": 30,
            "recurrence_rule": "FREQ=WEEKLY",
            "participant_user_ids": [],
            "horizon_days": 90,
        },
        headers=owner.headers,
    )
    assert series_resp.status_code == 201, series_resp.text

    series_id = series_resp.json()["series"]["id"]
    version = series_resp.json()["series"]["version"]

    # Try to split with past date (2020-01-01 is in the past)
    resp = await api.post(
        f"/v1/plan-series/{series_id}/split",
        json={
            "from_date": "2020-01-01",
            "title": "Renamed Series",
            "local_start_time": None,
            "duration_minutes": None,
            "recurrence_rule": None,
            "timezone": None,
        },
        headers={**owner.headers, "If-Match": f'"{version}"'},
    )
    assert resp.status_code == 422
    error_msg = str(resp.json())
    assert "from_date" in error_msg.lower()


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
