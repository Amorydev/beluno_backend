"""Problem reports ("Report a problem") and the record check shown before sending."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from beluno.contracts.common import strip_optional_text

ProblemCategory = Literal["balance_wrong", "sync_issue", "other"]
LinkedEntityType = Literal[
    "expense",
    "settlement",
    "budget",
    "place",
    "itinerary_item",
    "poll",
    "booking",
    "task",
    "packing_item",
]
# The person's own words: no control characters, not blank, at most 1000 characters.
Observation = Annotated[
    str, AfterValidator(strip_optional_text), StringConstraints(max_length=1000)
]
AppVersion = Annotated[str, StringConstraints(max_length=64, pattern=r"^[0-9A-Za-z.+\-_ ()]*$")]


class LinkedRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: LinkedEntityType
    entity_id: UUID


class ClientState(BaseModel):
    """What the app knows about its own sync; numbers and times only."""

    model_config = ConfigDict(extra="forbid")

    app_version: AppVersion | None = None
    pending_operations: int | None = Field(default=None, ge=0, le=1_000_000)
    last_synced_at: datetime | None = None


class ProblemReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: ProblemCategory
    plan_id: UUID | None = None
    linked: LinkedRecord | None = None
    message: Observation
    attach_diagnostics: bool = Field(
        default=True,
        description="Attach a diagnostic snapshot (IDs, states, versions; never expense text, "
        "notes, or codes) and get its code",
    )
    client: ClientState | None = None

    @model_validator(mode="after")
    def _linked_needs_plan(self) -> Self:
        if self.linked is not None and self.plan_id is None:
            raise ValueError("a linked record needs its plan_id")
        return self


class ProblemReportResponse(BaseModel):
    id: UUID
    diagnostic_code: str | None = Field(description="Quote it to support, e.g. BLN-7F3K-29QD")
    created_at: datetime


class RecordCheckResponse(BaseModel):
    """Shown on the report screen before sending ("audit OK")."""

    plan_id: UUID
    ledger_ok: bool = Field(description="Reconciling the plan's ledger found no disagreement")
    record_found: bool | None = Field(description="The linked record is in this plan (if asked)")
    record_version: int | None
