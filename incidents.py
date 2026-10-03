"""Incidents (spec §8). One incident = one problem on one sensor, per category.

Dedupe is per sensor AND per category, so a data problem (the sensor went
silent) is never hidden inside an open process incident, or vice versa.

An incident is "active" while open, acknowledged, or hold_confirmed. Only an
open incident can expire. Expired is set by the system; resolved only by a person.
Low-severity incidents are a watch list (see orchestrator.handle_trigger).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from config_loader import SEVERITY_RANK, Config
from db import repo
from sim.clock import Clock

if TYPE_CHECKING:
    from agents.monitor import Trigger

log = logging.getLogger(__name__)


# trending is listed for when trending_log_only is off; while it is on,
# trending never reaches open_or_update.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "process": ("beyond_spec", "beyond_3sigma", "sustained_run", "capability_degraded", "trending"),
    "data": ("dropout",),
}
RULE_CATEGORY = {rule: cat for cat, rules in CATEGORIES.items() for rule in rules}


class Change(str, Enum):
    NEW = "new"
    UPGRADED = "upgraded"
    REACTIVATED = "reactivated"  # triggered again after reactivation_quiet_ticks of silence
    NONE = "none"


# action -> (statuses it may start from, status it moves to)
TRANSITIONS: dict[str, tuple[frozenset[str], str]] = {
    "acknowledge": (frozenset({"open"}), "acknowledged"),
    "confirm_hold": (frozenset({"acknowledged"}), "hold_confirmed"),
    "dismiss": (frozenset({"open", "acknowledged"}), "dismissed"),
    "resolve": (frozenset({"acknowledged", "hold_confirmed"}), "resolved"),
}


@dataclass(frozen=True)
class TransitionResult:
    accepted: bool
    detail: str


def open_or_update(conn: sqlite3.Connection, trigger: Trigger, clock: Clock, config: Config) -> tuple[dict, Change]:
    now = clock.now_iso()
    severity = config.severity_of(trigger.rule_fired)
    category_rules = CATEGORIES[RULE_CATEGORY[trigger.rule_fired]]
    current = repo.get_active_incident(conn, trigger.sensor_id, category_rules)

    if current is None:
        # The cooldown is per category too: dismissing a process false alarm
        # must not silence a dropout on the same sensor.
        cooldown_since = clock.shift_iso(now, -config.dismissal_cooldown_ticks)
        if severity != "high" and repo.false_alarm_dismissed_since(conn, trigger.sensor_id, category_rules, cooldown_since):
            return {}, Change.NONE
        incident_id = f"INC-{repo.count_rows(conn, 'incidents') + 1:04d}"
        repo.insert_incident(
            conn, incident_id, trigger.tool_id, trigger.sensor_id, trigger.rule_fired, severity,
            trigger.onset_ts, now, config.version,
        )
        return dict(repo.get_incident(conn, incident_id)), Change.NEW

    # Every trigger refreshes updated_at (that is what "stale" and "quiet" are
    # measured from) and keeps the earliest onset seen.
    onset = min(current["onset_ts"], trigger.onset_ts)
    quiet = clock.ticks_between(current["updated_at"], now)
    if SEVERITY_RANK[severity] > SEVERITY_RANK[current["severity"]]:
        # An upgrade after a quiet spell is reported as UPGRADED: it re-notifies
        # either way, and "upgraded" also tells the person the severity rose.
        repo.update_incident_trigger(conn, current["incident_id"], trigger.rule_fired, severity, onset, now)
        change = Change.UPGRADED
    else:
        repo.update_incident_trigger(conn, current["incident_id"], current["rule_fired"], current["severity"], onset, now)
        change = Change.REACTIVATED if quiet >= config.reactivation_quiet_ticks else Change.NONE
    return dict(repo.get_incident(conn, current["incident_id"])), change


def expire_stale(conn: sqlite3.Connection, clock: Clock, config: Config) -> list[str]:
    """Open incidents with no new trigger for stale_after_ticks become expired."""
    now = clock.now_iso()
    ids = repo.stale_open_incident_ids(conn, clock.shift_iso(now, -config.stale_after_ticks))
    for incident_id in ids:
        repo.set_incident_status(conn, incident_id, "expired", now)
    return ids


def transition(
    conn: sqlite3.Connection,
    clock: Clock,
    incident_id: str,
    action: str,
    person_id: str,
    reason: str | None = None,
) -> TransitionResult:
    """Apply a person's action. Invalid transitions are rejected and logged."""
    incident = repo.get_incident(conn, incident_id)
    if incident is None:
        return _reject(incident_id, action, person_id, "unknown incident")
    if action not in TRANSITIONS:
        return _reject(incident_id, action, person_id, "unknown action")
    allowed_from, to_status = TRANSITIONS[action]
    if incident["status"] not in allowed_from:
        return _reject(incident_id, action, person_id, f"cannot {action} an incident that is {incident['status']}")

    now = clock.now_iso()
    repo.set_incident_status(conn, incident_id, to_status, now, dismiss_reason=reason if action == "dismiss" else None)
    if action == "acknowledge":
        repo.acknowledge_notifications(conn, incident_id, now)
    elif action == "confirm_hold":
        repo.hold_lots(conn, json.loads(incident["lots_at_risk"] or "[]"))
    log.info("incident %s: %s by %s -> %s", incident_id, action, person_id, to_status)
    return TransitionResult(True, to_status)


def _reject(incident_id: str, action: str, person_id: str, why: str) -> TransitionResult:
    log.warning("rejected %s on incident %s by %s: %s", action, incident_id, person_id, why)
    return TransitionResult(False, why)


def apply_decision(conn: sqlite3.Connection, incident: dict, decision, diagnosis) -> None:
    """Record triage's owner and hold recommendation and the latest diagnosis."""
    repo.apply_incident_decision(
        conn, incident["incident_id"], decision.owner_id, decision.recommend_hold, diagnosis.diagnosis_id,
    )
