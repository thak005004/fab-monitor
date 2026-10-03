"""Lots at risk (spec §9). Runs when an incident is NEW or UPGRADED, except dropout."""

from __future__ import annotations

import json
import sqlite3

from db import repo
from sim.clock import Clock


def mark(conn: sqlite3.Connection, incident: dict, clock: Clock) -> list[str]:
    """Mark every lot on the tool between the incident's onset and now as at_risk
    (a held lot is never downgraded) and store the list on the incident.

    The stored list only grows: an upgrade with an earlier onset adds lots,
    it never drops ones already flagged.
    """
    lot_ids = repo.lots_overlapping(conn, incident["tool_id"], clock.now_iso(), incident["onset_ts"])
    repo.mark_lots_at_risk(conn, lot_ids)
    stored = set(json.loads(incident.get("lots_at_risk") or "[]"))
    all_ids = sorted(stored | set(lot_ids))
    repo.set_incident_lots(conn, incident["incident_id"], json.dumps(all_ids))
    return all_ids


def refresh_active(conn: sqlite3.Connection, clock: Clock) -> None:
    """On each TICK: re-run the onset-to-now query for every active medium/high
    process incident, so lots that start on the tool while the incident is still
    going are marked too. No notification: the incident already reached a person."""
    for incident in repo.active_process_incidents_to_refresh(conn):
        mark(conn, dict(incident), clock)
