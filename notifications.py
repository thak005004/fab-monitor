"""Notifications, escalation, reassignment and persistence (spec §12).

Delivery is a per-person inbox (the notifications table); a webhook would
plug in at send(). Only medium and high incidents ever notify anyone.

Reasons: new, upgraded, reactivated, persistent, escalated, reassigned.
"""

from __future__ import annotations

import sqlite3

from agents.triage import TriageDecision, pick_owner
from config_loader import ACTIONABLE_SEVERITIES, Config
from db import repo
from sim.clock import Clock

REASONS = ("new", "upgraded", "reactivated", "persistent", "escalated", "reassigned")


def send(conn: sqlite3.Connection, clock: Clock, incident: dict, decision: TriageDecision, reason: str) -> str | None:
    """Notify the triage owner. Returns the notification ID, or None for a low incident."""
    if reason not in REASONS:
        raise ValueError(f"unknown notification reason {reason!r}")
    if decision.severity not in ACTIONABLE_SEVERITIES:
        return None  # low = watch list, never notified
    return _insert(conn, clock, incident["incident_id"], decision.owner_id, reason,
                   incident["escalation_level"], decision.message)


def persistence_due(conn: sqlite3.Connection, incident: dict, clock: Clock, config: Config) -> bool:
    """An acknowledged medium/high incident still triggering persistence_ticks after
    its latest notification gets one "persistent" notice. (Unacknowledged ones
    don't need it: escalation already re-notifies them.)"""
    if incident["status"] not in ("acknowledged", "hold_confirmed"):
        return False
    if incident["severity"] not in ACTIONABLE_SEVERITIES:
        return False
    latest = repo.latest_notification(conn, incident["incident_id"])
    if latest is None or repo.has_notification_with_reason(conn, incident["incident_id"], "persistent"):
        return False
    return clock.ticks_between(latest["sent_at"], clock.now_iso()) >= config.persistence_ticks


def escalate_overdue(conn: sqlite3.Connection, clock: Clock, config: Config) -> list[str]:
    """On TICK: an open (unacknowledged) medium/high incident whose latest
    notification is at least escalation_ticks old goes to the next qualified,
    available, not-yet-notified person, one escalation level up; if nobody is
    left, to on-call. On-call is the end of the chain: once on-call has been
    notified, escalation stops (it would otherwise re-page on-call forever).

    High incidents always escalate. A medium incident escalates only if it has
    had at least one trigger since its latest notification: a medium false alarm
    that has gone quiet is not worth paging the next person about."""
    now = clock.now_iso()
    sent = []
    for inc in repo.open_notified_incidents(conn):
        latest = repo.latest_notification(conn, inc["incident_id"])
        if clock.ticks_between(latest["sent_at"], now) < config.escalation_ticks:
            continue
        # updated_at moves on every trigger (and, for an open incident, nothing else).
        if inc["severity"] == "medium" and not inc["updated_at"] > latest["sent_at"]:
            continue
        notified = repo.notified_people(conn, inc["incident_id"])
        if config.oncall_person_id in notified:
            continue
        person, on_call = pick_owner(repo.qualified_available_people(conn, inc["tool_id"]), notified, config)
        level = inc["escalation_level"] + 1
        repo.set_incident_owner(conn, inc["incident_id"], person, level)
        message = (f"{'[ON-CALL] ' if on_call else ''}[{inc['severity'].upper()}] escalated (level {level}): "
                   f"{inc['sensor_id']} on {inc['tool_id']} ({inc['incident_id']}) has not been acknowledged "
                   f"for {config.escalation_ticks}+ ticks. Rule: {inc['rule_fired']}.\n"
                   f"Previous message:\n{latest['message']}")
        sent.append(_insert(conn, clock, inc["incident_id"], person, "escalated", level, message))
    return sent


def reassign_from(conn: sqlite3.Connection, clock: Clock, config: Config, person_id: str) -> list[str]:
    """On PERSON_AVAILABILITY(available=false): every open, unacknowledged incident
    owned by that person goes to the next qualified, available person who hasn't
    been notified about it yet (on-call if nobody is left)."""
    sent = []
    for inc in repo.open_incidents_owned_by(conn, person_id):
        notified = repo.notified_people(conn, inc["incident_id"])
        person, on_call = pick_owner(repo.qualified_available_people(conn, inc["tool_id"]), notified, config)
        repo.set_incident_owner(conn, inc["incident_id"], person, inc["escalation_level"])
        latest = repo.latest_notification(conn, inc["incident_id"])
        message = (f"{'[ON-CALL] ' if on_call else ''}[{inc['severity'].upper()}] reassigned to you: "
                   f"{person_id} is unavailable. {inc['sensor_id']} on {inc['tool_id']} ({inc['incident_id']}).\n"
                   f"Previous message:\n{latest['message'] if latest else '-'}")
        sent.append(_insert(conn, clock, inc["incident_id"], person, "reassigned", inc["escalation_level"], message))
    return sent


def _insert(conn, clock: Clock, incident_id: str, person_id: str, reason: str, level: int, message: str) -> str:
    notification_id = f"N-{repo.count_notifications(conn) + 1:05d}"
    repo.insert_notification(conn, notification_id, incident_id, person_id, reason, level, message, clock.now_iso())
    return notification_id
