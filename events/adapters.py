"""One adapter per source (spec §4). parse(raw) -> Event, or raises ParseError.

A raw record is an envelope:
  {"event_type", "source", "tool_id", "ts", "payload"}
Adapters also check IDs against reference data (sensors, tools, people), so
a reading for an unknown sensor is quarantined instead of failing a DB insert.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pydantic import ValidationError

from db import repo
from events.types import PAYLOAD_MODELS, Event, EventType
from sim.clock import parse_iso

ENVELOPE_KEYS = {"event_type", "source", "tool_id", "ts", "payload"}


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class Reference:
    """Fixed reference data the adapters validate against."""

    sensor_tools: dict[str, str]
    tools: frozenset[str]
    people: frozenset[str]

    @classmethod
    def load(cls, conn: sqlite3.Connection) -> Reference:
        return cls(repo.sensor_tools(conn), frozenset(repo.tool_ids(conn)), frozenset(repo.person_ids(conn)))


class Adapter:
    name: str
    event_type: EventType
    tool_scoped: bool  # True: tool_id must be a known tool. False: tool_id must be null.

    def __init__(self, ref: Reference) -> None:
        self.ref = ref

    def parse(self, raw: dict) -> Event:
        if not isinstance(raw, dict):
            raise ParseError(f"record must be an object, got {type(raw).__name__}")
        keys = set(raw)
        if keys != ENVELOPE_KEYS:
            raise ParseError(f"envelope keys: missing {sorted(ENVELOPE_KEYS - keys)}, unexpected {sorted(keys - ENVELOPE_KEYS)}")
        if raw["source"] != self.name:
            raise ParseError(f"source {raw['source']!r} sent to the {self.name!r} adapter")
        if raw["event_type"] != self.event_type.value:
            raise ParseError(f"event_type {raw['event_type']!r} not accepted by the {self.name!r} adapter")
        try:
            ts = parse_iso(raw["ts"])
        except (TypeError, ValueError) as e:
            raise ParseError(f"bad ts: {e}") from None

        tool_id = raw["tool_id"]
        if self.tool_scoped:
            if tool_id not in self.ref.tools:
                raise ParseError(f"unknown tool_id {tool_id!r}")
        elif tool_id is not None:
            raise ParseError(f"{self.event_type.value} events must have tool_id null")

        try:
            model = PAYLOAD_MODELS[self.event_type].model_validate(raw["payload"])
        except ValidationError as e:
            raise ParseError(f"bad payload: {_summarize(e)}") from None
        # exclude_unset: "fields present in the payload" stays meaningful for
        # merge-only updates (an absent field is not the same as null).
        payload = model.model_dump(exclude_unset=True)
        self.check_references(tool_id, payload)
        return Event(event_type=self.event_type, source=self.name, tool_id=tool_id, payload=payload, ts=ts)

    def check_references(self, tool_id: str | None, payload: dict) -> None:
        pass


class SensorAdapter(Adapter):
    name, event_type, tool_scoped = "sensor", EventType.READING, True

    def check_references(self, tool_id, payload):
        owner = self.ref.sensor_tools.get(payload["sensor_id"])
        if owner is None:
            raise ParseError(f"unknown sensor_id {payload['sensor_id']!r}")
        if owner != tool_id:
            raise ParseError(f"sensor {payload['sensor_id']} belongs to {owner}, not {tool_id}")


class MaintenanceAdapter(Adapter):
    name, event_type, tool_scoped = "maintenance", EventType.MAINTENANCE, True


class RecipeAdapter(Adapter):
    name, event_type, tool_scoped = "recipe", EventType.RECIPE_CHANGE, True


class PeopleAdapter(Adapter):
    name, event_type, tool_scoped = "people", EventType.PERSON_AVAILABILITY, False

    def check_references(self, tool_id, payload):
        if payload["person_id"] not in self.ref.people:
            raise ParseError(f"unknown person_id {payload['person_id']!r}")


class DashboardAdapter(Adapter):
    name, event_type, tool_scoped = "dashboard", EventType.ALERT_ACTION, False

    def check_references(self, tool_id, payload):
        if payload["person_id"] not in self.ref.people:
            raise ParseError(f"unknown person_id {payload['person_id']!r}")


class ClockAdapter(Adapter):
    name, event_type, tool_scoped = "clock", EventType.TICK, False


ADAPTER_CLASSES = (SensorAdapter, MaintenanceAdapter, RecipeAdapter, PeopleAdapter, DashboardAdapter, ClockAdapter)


def default_adapters(conn: sqlite3.Connection) -> dict[str, Adapter]:
    ref = Reference.load(conn)
    return {cls.name: cls(ref) for cls in ADAPTER_CLASSES}


def _summarize(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, err['loc'])) or '<payload>'}: {err['msg']}" for err in e.errors())
