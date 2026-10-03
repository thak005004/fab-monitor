"""In-process event bus (spec §4). In production this would be a message broker.

publish(raw, adapter):
  1. adapter.parse(raw). ParseError -> dead_letter, return. Never raises.
  2. Assign event_id, insert into events.
  3. orchestrator.on_event(event).

Events are processed one at a time, in order, synchronously.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Protocol

from db import repo
from db.connection import transaction
from events.adapters import Adapter, ParseError
from events.types import Event
from sim.clock import Clock, to_iso


class EventHandler(Protocol):
    def on_event(self, event: Event) -> None: ...


class Bus:
    def __init__(
        self,
        conn: sqlite3.Connection,
        orchestrator: EventHandler,
        clock: Clock,
        adapters: dict[str, Adapter],
    ) -> None:
        self.conn = conn
        self.orchestrator = orchestrator
        self.clock = clock
        self.adapters = adapters
        # IDs are sequential and gap-free, so the next one follows from the row count.
        self._event_seq = repo.count_rows(conn, "events")
        self._dl_seq = repo.count_rows(conn, "dead_letter")

    def publish(self, raw: dict, adapter: Adapter) -> Event | None:
        """Returns the stored event, or None if the record was quarantined."""
        try:
            event = adapter.parse(raw)
        except ParseError as e:
            self._dead_letter(raw, adapter.name, str(e))
            return None

        self._event_seq += 1
        event = event.model_copy(update={"event_id": f"EV-{self._event_seq:07d}"})

        # A record can pass parsing and still violate a DB constraint (e.g. a
        # duplicate reading_id). Wrap the event in a savepoint so that one bad
        # record is rolled back and quarantined without losing the rest of the
        # tick. Other exceptions are bugs and propagate.
        self.conn.execute("SAVEPOINT bus_event")
        try:
            repo.insert_event(
                self.conn, event.event_id, event.event_type.value, event.source, event.tool_id,
                json.dumps(event.payload, sort_keys=True), to_iso(event.ts),
            )
            self.orchestrator.on_event(event)
        except sqlite3.IntegrityError as e:
            self.conn.execute("ROLLBACK TO bus_event")
            self.conn.execute("RELEASE bus_event")
            self._event_seq -= 1
            self._dead_letter(raw, adapter.name, f"integrity error: {e}")
            return None
        self.conn.execute("RELEASE bus_event")
        return event

    def publish_tick(self, records: list[dict]) -> list[Event]:
        """Publish one tick's records in order, all in one transaction."""
        accepted = []
        with transaction(self.conn):
            for raw in records:
                source = raw.get("source") if isinstance(raw, dict) else None
                adapter = self.adapters.get(source) if isinstance(source, str) else None
                if adapter is None:
                    self._dead_letter(raw, source if isinstance(source, str) else None, f"no adapter for source {source!r}")
                    continue
                event = self.publish(raw, adapter)
                if event is not None:
                    accepted.append(event)
        return accepted

    def _dead_letter(self, raw, source: str | None, reason: str) -> None:
        try:
            raw_text = json.dumps(raw, sort_keys=True, default=repr)
        except (TypeError, ValueError):  # e.g. keys of mixed types; still keep the evidence
            raw_text = repr(raw)
        self._dl_seq += 1
        repo.insert_dead_letter(self.conn, f"DL-{self._dl_seq:06d}", source, raw_text, reason, self.clock.now_iso())
