"""apply_event: the only place events change world state (spec §5).

MERGE ONLY. Every write here is an INSERT into an append-only table or an
UPDATE of exactly the columns the event is about. Never write a whole row
back. Why: events are partial. A maintenance event that only says
`status = degraded` knows nothing about the tool's current recipe; if we
rebuilt the row from the event we would wipe `current_recipe_id` (and in
sensor_state, the frozen control limits) with nulls, and the system would
quietly forget what it had learned.
"""

from __future__ import annotations

import sqlite3

from config_loader import Config
from db import repo
from events.types import Event, EventType
from sim.clock import Clock, to_iso


def apply_event(conn: sqlite3.Connection, event: Event, clock: Clock, config: Config) -> None:
    p = event.payload
    ts = to_iso(event.ts)

    match event.event_type:
        case EventType.READING:
            repo.insert_reading(conn, p["reading_id"], p["sensor_id"], event.tool_id, p["value"], ts)
            repo.set_last_reading_ts(conn, p["sensor_id"], ts)

        case EventType.MAINTENANCE:
            repo.insert_maintenance(conn, p["log_id"], event.tool_id, p["description"], p.get("technician"), ts)
            # Only the tool_state fields actually present in this payload.
            fields = {k: p[k] for k in repo.TOOL_STATE_FIELDS if k in p}
            repo.merge_tool_state(conn, event.tool_id, fields)

        case EventType.RECIPE_CHANGE:
            repo.insert_recipe_change(conn, p["change_id"], event.tool_id, p["recipe_id"], ts)
            repo.merge_tool_state(conn, event.tool_id, {"current_recipe_id": p["recipe_id"]})
            # Old control limits stay in place until the new ones are ready.
            now = clock.now_iso()
            repo.start_relearning(conn, event.tool_id, now, clock.shift_iso(now, config.baseline_window_ticks))

        case EventType.PERSON_AVAILABILITY:
            repo.set_person_available(conn, p["person_id"], p["available"])

        case EventType.ALERT_ACTION | EventType.TICK:
            pass  # no world-state change (§5)
