"""EventType, Event, and per-type payload models (spec §4)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EventType(str, Enum):
    READING = "reading"
    MAINTENANCE = "maintenance"
    RECIPE_CHANGE = "recipe_change"
    PERSON_AVAILABILITY = "person_availability"
    ALERT_ACTION = "alert_action"
    TICK = "tick"


class Event(BaseModel):
    model_config = ConfigDict(frozen=True)

    # None only between adapter.parse() and the bus; the bus assigns the ID
    # before the event is stored or reaches the orchestrator.
    event_id: str | None = None
    event_type: EventType
    source: str
    tool_id: str | None
    payload: dict
    ts: datetime


# Payload models. Strict: unknown fields are rejected and numbers are not
# coerced from strings, so malformed input goes to dead_letter instead of
# being silently "fixed".

class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadingPayload(_Payload):
    reading_id: str = Field(pattern=r"^RD-\d+$")
    sensor_id: str = Field(min_length=1)
    value: float = Field(allow_inf_nan=False)


class MaintenancePayload(_Payload):
    log_id: str = Field(pattern=r"^M-\d+$")
    description: str = Field(min_length=1)
    technician: str | None = None
    # Partial tool_state fields. Only `status`: a recipe can change only via a
    # RECIPE_CHANGE event, which also starts relearning.
    status: Literal["healthy", "degraded", "offline"] | None = None


class RecipeChangePayload(_Payload):
    change_id: str = Field(pattern=r"^RC-\d+$")
    recipe_id: str = Field(min_length=1)


class PersonAvailabilityPayload(_Payload):
    person_id: str = Field(min_length=1)
    available: bool


class AlertActionPayload(_Payload):
    incident_id: str = Field(min_length=1)
    action: Literal["acknowledge", "confirm_hold", "dismiss", "resolve"]
    person_id: str = Field(min_length=1)
    reason: str | None = None


class TickPayload(_Payload):
    tick: int = Field(ge=0)


PAYLOAD_MODELS: dict[EventType, type[_Payload]] = {
    EventType.READING: ReadingPayload,
    EventType.MAINTENANCE: MaintenancePayload,
    EventType.RECIPE_CHANGE: RecipeChangePayload,
    EventType.PERSON_AVAILABILITY: PersonAvailabilityPayload,
    EventType.ALERT_ACTION: AlertActionPayload,
    EventType.TICK: TickPayload,
}
