"""Plain-language wording for the dashboard, for readers who aren't software
engineers. Presentation only: labels for the system's terms (each keeps the
technical term next to it) and a read-only activity feed built from tables
that already exist. Nothing here changes what the system does.
"""

from __future__ import annotations

import json
import re

from config_loader import Config
from db import repo
from sim.clock import Clock

INTRO = (
    "This page simulates sensors on chip-manufacturing machines (tools). "
    "The system learns each sensor's normal range and watches for readings that drift out of it. "
    "When it finds a problem, an AI suggests likely reasons, and the system checks every piece of evidence "
    "the AI cites against the records. "
    "Then it works out which product batches (lots) are affected and alerts a qualified person. "
    "All data here is simulated."
)

SEVERITY = {"low": "Watch only (no one paged)", "medium": "Alert", "high": "Urgent"}
SEVERITY_SHORT = {"low": "Watch only", "medium": "Alert", "high": "Urgent"}
STATUS = {
    "open": "Waiting for response",
    "acknowledged": "Someone is on it",
    "hold_confirmed": "Product on hold",
    "dismissed": "Dismissed",
    "resolved": "Resolved",
    "expired": "Closed automatically (no further signs)",
}
STATUS_SHORT = {**STATUS, "expired": "Closed automatically"}  # for narrow table columns
DIAGNOSIS = {
    "diagnosed": "AI suggested likely causes",
    "abstained": "AI: not enough evidence to explain this",
    "rejected": "AI answer rejected: it cited evidence that didn't check out",
    "unavailable": "AI unavailable: alert sent without it",
}
BASELINE = {"active": "normal range learned", "learning": "learning normal",
            "relearning": "learning the new normal after a recipe change"}
BASELINE_SHORT = {"active": "monitoring", "learning": "learning normal", "relearning": "learning new normal"}
NOTICE = {
    "new": "new alert", "upgraded": "got worse", "reactivated": "flared up again", "persistent": "still active",
    "escalated": "escalated (no response)", "reassigned": "reassigned",
}
FAULT_TYPES = {
    "gradual_drift": "Slow drift",
    "step_shift": "Sudden jump",
    "dropout": "Sensor goes silent",
    "drift_with_decoys": "Slow drift + misleading maintenance notes",
    "drift_no_cause": "Slow drift, no recorded cause",
    "prompt_injection": "Slow drift + a note with instructions aimed at the AI",
    "recipe_change_no_fault": "Recipe change (healthy)",
    "recipe_change_out_of_spec": "Recipe change + one out-of-range reading",
}
DISMISS_REASONS = {"false_alarm": "False alarm", "duplicate": "Duplicate", "other": "Other"}
SENSOR_TYPES = {"temperature": "temperature", "pressure": "pressure", "rf_power": "RF power"}


def rule(name: str, config: Config) -> str:
    """Plain label with the technical rule name kept in parentheses."""
    labels = {
        "beyond_spec": "Outside allowed range",
        "beyond_3sigma": "Single unusual reading",
        "sustained_run": f"Sustained shift: {config.run_length} readings in a row on one side of normal",
        "dropout": "Sensor stopped reporting",
        "capability_degraded": "New recipe too close to limits",
        "trending": "Steady trend (logged only)",
    }
    return f"{labels.get(name, name)} ({name})"


def rule_short(name: str) -> str:
    return {"beyond_spec": "Outside allowed range", "beyond_3sigma": "Single unusual reading",
            "sustained_run": "Sustained shift", "dropout": "Sensor stopped reporting",
            "capability_degraded": "Recipe too close to limits", "trending": "Steady trend"}.get(name, name)


def severity(s: str) -> str:
    return f"{SEVERITY.get(s, s)}"


def status(s: str) -> str:
    return f"{STATUS.get(s, s)} ({s})"


class Names:
    """Display names from the seeded data: 'Avery Lin (P-01)', 'Etch Tool 1 (T-01)'."""

    def __init__(self, conn):
        self.people = {p["person_id"]: p["name"] for p in repo.people(conn)}
        self.tools = {t["tool_id"]: t["name"] for t in repo.tool_overview(conn)}
        self.sensors = {s["sensor_id"]: s for s in repo.all_sensors(conn)}

    def person(self, pid: str | None) -> str:
        if not pid:
            return "-"
        return f"{self.people.get(pid, pid)} ({pid})"

    def tool(self, tid: str) -> str:
        return f"{self.tools.get(tid, tid)} ({tid})"

    def sensor_type(self, sid: str) -> str:
        s = self.sensors.get(sid)
        return SENSOR_TYPES.get(s["sensor_type"], s["sensor_type"]) if s else sid

    def sensor(self, sid: str) -> str:
        s = self.sensors.get(sid)
        return f"{self.tool(s['tool_id'])} {self.sensor_type(sid)} ({sid})" if s else sid

    def where(self, sid: str) -> str:
        """Short form for tables: 'T-01 temperature'."""
        s = self.sensors.get(sid)
        return f"{s['tool_id']} {self.sensor_type(sid)}" if s else sid


# ----- activity feed -------------------------------------------------------------------

_RULE_IN_MESSAGE = re.compile(r"Rule: (\w+)\.")


def _rule_from_message(message: str) -> str | None:
    m = _RULE_IN_MESSAGE.search(message or "")
    return m.group(1) if m else None


def _side(conn, sensor_id: str, ts: str) -> str:
    """'above' or 'below' normal for the reading at that moment, vs the sensor's learned average."""
    r = repo.reading_at(conn, sensor_id, ts)
    st = repo.get_sensor_state(conn, sensor_id)
    if r is None or st is None or st["control_mean"] is None:
        return "away from"
    return "above" if r["value"] > st["control_mean"] else "below"


def _what_happened(conn, rule_name: str, sensor_id: str, ts: str, names: Names, config: Config) -> str:
    where = f"{names.tool(names.sensors[sensor_id]['tool_id'])} {names.sensor_type(sensor_id)}"
    side = _side(conn, sensor_id, ts)
    return {
        "beyond_spec": f"{where} went {side} its allowed range",
        "beyond_3sigma": f"{where} had a single unusual reading, {side} normal",
        "sustained_run": f"{where} has been {side} normal for {config.run_length} readings in a row",
        "dropout": f"{where} sensor stopped reporting",
        "capability_degraded": f"{where}: the new recipe runs too close to the allowed limits",
        "trending": f"{where} has been moving steadily in one direction",
    }.get(rule_name, f"{where}: {rule_name}")


def activity_feed(conn, clock: Clock, config: Config, limit: int = 60) -> list[tuple[int, str]]:
    """Notable events as (minute, sentence), newest first. Routine readings are left out."""
    names = Names(conn)
    events: list[tuple[int, int, str]] = []  # (minute, order within the minute, sentence)

    def add(ts: str, order: int, text: str) -> None:
        events.append((clock.tick_of(ts), order, text))

    actions = [(e["ts"], json.loads(e["payload"])) for e in repo.events_of_type(conn, "alert_action")]
    notes_by_incident: dict[str, list] = {}
    for n in repo.all_notifications(conn):
        notes_by_incident.setdefault(n["incident_id"], []).append(n)

    for inc in repo.all_incidents(conn):
        iid, sid = inc["incident_id"], inc["sensor_id"]
        notes = notes_by_incident.get(iid, [])
        first = next((n for n in notes if n["reason"] == "new"), None)
        if first:  # opened at Alert/Urgent: the first alert's message names the rule at that moment
            opened_rule = _rule_from_message(first["message"]) or inc["rule_fired"]
            tail = f"Alert sent to {names.person(first['person_id'])}."
        else:  # opened on the watch list (the only low rule is a single unusual reading)
            opened_rule = "beyond_3sigma" if any(n["reason"] == "upgraded" for n in notes) else inc["rule_fired"]
            tail = "Added to the watch list; no one paged."
        add(inc["opened_at"], 1, f"{_what_happened(conn, opened_rule, sid, inc['opened_at'], names, config)}. {tail} ({iid})")
        if inc["status"] == "expired":
            add(inc["updated_at"], 5, f"{iid} on {names.where(sid)} closed automatically: no further signs.")
        if inc["status"] == "dismissed":
            add(inc["updated_at"], 4, f"{iid} on {names.where(sid)} was dismissed"
                                       + (f" ({DISMISS_REASONS.get(inc['dismiss_reason'], inc['dismiss_reason'])})."
                                          if inc["dismiss_reason"] else "."))
        if inc["status"] == "resolved":
            add(inc["updated_at"], 4, f"{iid} on {names.where(sid)} was resolved.")

        for n in notes:
            who = names.person(n["person_id"])
            if n["reason"] == "upgraded":
                r = _rule_from_message(n["message"]) or inc["rule_fired"]
                level = SEVERITY_SHORT.get(config.severity_of(r), "")
                add(n["sent_at"], 2, f"{iid} got worse ({level}): "
                                     f"{_what_happened(conn, r, sid, n['sent_at'], names, config)}. Alert sent to {who}.")
            elif n["reason"] == "reactivated":
                add(n["sent_at"], 2, f"{iid} on {names.where(sid)} flared up again after going quiet. Alert sent to {who}.")
            elif n["reason"] == "persistent":
                add(n["sent_at"], 2, f"{iid} on {names.where(sid)} is still showing problems after it was "
                                     f"acknowledged. Reminder sent to {who}.")
            elif n["reason"] == "escalated":
                add(n["sent_at"], 3, f"No one responded to {iid} in time, so it was escalated to {who}.")
            elif n["reason"] == "reassigned":
                add(n["sent_at"], 3, f"{iid} was reassigned to {who} because its owner became unavailable.")

        acks = sorted({n["acknowledged_at"] for n in notes if n["acknowledged_at"]})
        for ts in acks:
            actor = next((p["person_id"] for t, p in actions
                          if t == ts and p.get("incident_id") == iid and p.get("action") == "acknowledge"), None)
            add(ts, 4, f"{names.person(actor) if actor else 'Someone'} acknowledged {iid}: someone is on it.")

    # Holds: the latest confirm_hold for an incident that is now on hold (or resolved after it).
    holds: dict[str, dict] = {}
    for t, p in actions:
        if p.get("action") == "confirm_hold":
            holds[p["incident_id"]] = {"ts": t, "person": p.get("person_id")}
    for iid, h in holds.items():
        inc = repo.get_incident(conn, iid)
        if inc is None or inc["status"] not in ("hold_confirmed", "resolved"):
            continue
        held = [l["lot_id"] for l in repo.lots_by_ids(conn, json.loads(inc["lots_at_risk"] or "[]")) if l["status"] == "held"]
        add(h["ts"], 4, f"{names.person(h['person'])} put {len(held)} product batch{'es' if len(held) != 1 else ''} "
                        f"(lots) on hold for {iid}: {', '.join(held) or 'none'}.")

    for c in repo.all_recipe_changes(conn):
        add(c["ts"], 0, f"{names.tool(c['tool_id'])} switched to recipe {c['recipe_id']}. Its sensors are learning "
                        "the new normal (relearning); only the allowed-range check runs meanwhile.")
    for a in repo.relearning_activations(conn):
        add(a["activated_at"], 0, f"{names.tool(a['tool_id'])} finished learning the new normal after its recipe change.")
    for d in repo.all_dead_letters(conn):
        add(d["ts"], 0, f"Rejected bad data from the {d['source'] or 'unknown'} feed ({d['id']}): {d['error_reason']}. "
                        "Monitoring kept going.")
    for e in repo.events_of_type(conn, "person_availability"):
        p = json.loads(e["payload"])
        add(e["ts"], 0, f"{names.person(p['person_id'])} marked {'available' if p['available'] else 'unavailable'}.")

    events.sort(key=lambda x: (-x[0], -x[1]))
    return [(minute, text) for minute, _, text in events[:limit]]
