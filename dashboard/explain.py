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


# ----- status colors and icons ----------------------------------------------------------
# One look per state, used everywhere (cards, tables, incident detail, inboxes, chart):
# green healthy, amber watch or alert, red urgent, gray closed.

CLOSED_STATUSES = ("dismissed", "resolved", "expired")
LOOK = {  # state: (Streamlit color name, hex for charts, icon, label)
    "healthy": ("green", "#1f8a4c", "🟢", "Healthy"),
    "watch": ("orange", "#c27c0e", "🟠", "Watch"),
    "alert": ("orange", "#c27c0e", "🟠", "Alert"),
    "urgent": ("red", "#c8323c", "🔴", "Urgent"),
    "closed": ("gray", "#7a808c", "⚪", "Closed"),
}
_STATE_OF_SEVERITY = {"low": "watch", "medium": "alert", "high": "urgent"}


def state_of(severity: str | None, status: str | None = None) -> str:
    """'healthy', 'watch', 'alert', 'urgent' or 'closed'."""
    if status in CLOSED_STATUSES:
        return "closed"
    return _STATE_OF_SEVERITY.get(severity or "", "healthy")


def icon_label(state: str, label: str | None = None) -> str:
    """Plain text, for tables and expander titles: '🟠 Alert'."""
    _, _, icon, default = LOOK[state]
    return f"{icon} {label or default}"


def badge(state: str, label: str | None = None) -> str:
    """Markdown: the icon and a colored label, '🟠 :orange[**Alert**]'."""
    color, _, icon, default = LOOK[state]
    return f"{icon} :{color}[**{label or default}**]"


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
        add(d["ts"], 0, f"Rejected bad data from the {d['source'] or 'unknown'} feed ({d['id']}): it failed the "
                        "format check, so it was set aside. Monitoring kept going.")
    for e in repo.events_of_type(conn, "person_availability"):
        p = json.loads(e["payload"])
        add(e["ts"], 0, f"{names.person(p['person_id'])} marked {'available' if p['available'] else 'unavailable'}.")

    events.sort(key=lambda x: (-x[0], -x[1]))
    return [(minute, text) for minute, _, text in events[:limit]]


# ----- one-sentence incident summary ---------------------------------------------------

def _minutes(n: int) -> str:
    return f"{n} minute{'s' if n != 1 else ''}"


def incident_summary(conn, inc, clock: Clock, config: Config, names: Names) -> str:
    """What happened, who is on it, and what product is affected, in plain words.
    Read-only: built from the incident row as it is."""
    sid = inc["sensor_id"]
    sensor = names.sensors[sid]
    what = f"{names.tools.get(sensor['tool_id'], sensor['tool_id'])}'s {names.sensor_type(sid)}"
    closed = inc["status"] in CLOSED_STATUSES
    end_tick = clock.tick_of(inc["updated_at"]) if closed else clock.tick
    since = _minutes(max(0, end_tick - clock.tick_of(inc["onset_ts"])))
    side = _side(conn, sid, inc["opened_at"])
    trend = {"above": "running high", "below": "running low"}.get(side, "running away from normal")
    first = {
        "sustained_run": f"{what} {'was' if closed else 'has been'} {trend} for {since}",
        "beyond_spec": f"{what} went {'outside' if side == 'away from' else side} its allowed range",
        "beyond_3sigma": f"{what} had a single unusual reading",
        "dropout": f"{what} sensor stopped reporting",
        "capability_degraded": f"{what}: the new recipe runs too close to the allowed limits",
    }.get(inc["rule_fired"], f"{what}: {rule_short(inc['rule_fired'])}")

    owner = names.people.get(inc["owner_id"], inc["owner_id"]) if inc["owner_id"] else None
    who = {
        "open": f"Waiting for {owner} to respond." if owner else "On the watch list; no one has been paged.",
        "acknowledged": f"{owner or 'Someone'} is on it.",
        "hold_confirmed": f"{owner or 'Someone'} put the affected product on hold.",
        "dismissed": "It was dismissed.",
        "resolved": "It was resolved.",
        "expired": "It closed automatically: no further signs.",
    }.get(inc["status"], "")

    lots = repo.lots_by_ids(conn, json.loads(inc["lots_at_risk"] or "[]"))
    held = sum(1 for l in lots if l["status"] == "held")
    n = len(lots)
    if not n:
        product = "No product batches affected."
    else:
        product = f"{n} product batch{'es' if n != 1 else ''} {'were' if n != 1 else 'was'} flagged as at risk" if closed \
            else f"{n} product batch{'es' if n != 1 else ''} may be affected"
        if held:
            product += f" ({held} on hold)."
        elif inc["recommend_hold"] and not closed:
            product += "; holding them is recommended."
        else:
            product += "."
    return f"{first}. {who} {product}"


# ----- behind the scenes: the database -------------------------------------------------

DB_GROUPS = [  # (group, what the group is, [(table, one-line description)])
    ("Reference data", "Fixed facts about the factory, seeded once.", [
        ("tools", "The machines (tools)."),
        ("sensors", "Each machine's sensors, with their fixed allowed range (spec limits)."),
        ("people", "Engineers who can be alerted, and whether they're available."),
        ("tool_qualifications", "Who is qualified to handle which machine."),
    ]),
    ("Live state", "What's true right now; updated in place.", [
        ("tool_state", "Each machine's current status and recipe."),
        ("sensor_state", "Each sensor's learned normal range and whether it's learning."),
        ("lots", "Product batches (lots), marked at risk or on hold as things happen."),
    ]),
    ("Append-only history", "Everything that happened, only ever added to.", [
        ("events", "The event log: every accepted record, in order. State can be rebuilt from it."),
        ("readings", "Every sensor reading."),
        ("maintenance_log", "Maintenance notes (free text; treated as data, never as instructions)."),
        ("recipe_changes", "Every recipe switch."),
        ("baseline_windows", "Each completed learning period for a sensor's normal range."),
        ("logged_firings", "Steady trends, recorded but never alerted on (log-only rules)."),
        ("dead_letter", "Rejected bad data, kept for inspection (dead letter)."),
    ]),
    ("Outputs and audit", "What the system decided, and why.", [
        ("incidents", "Problems found: what, where, how serious, and who owns them."),
        ("diagnoses", "Every AI answer, what it was shown, and whether its citations checked out."),
        ("notifications", "Every alert sent to a person, with its fixed (non-AI) message."),
        ("config_versions", "Saved settings versions. Unused: settings reloading was left out of this build."),
    ]),
    ("Ground truth", "The answer key: written only by the simulator, read only by the evaluation.", [
        ("fault_injections", "Which problems were planted, where and when; the system never reads it."),
    ]),
]

EVENT_TYPES = {"reading": "Sensor reading", "maintenance": "Maintenance note", "recipe_change": "Recipe change",
               "person_availability": "Availability change", "alert_action": "Person's action", "tick": "Clock tick"}

# Summary of eval_results/database_report.md (tests check these numbers against the report).
DB_REPORT_URL = "https://github.com/thak005004/fab-monitor/blob/main/eval_results/database_report.md"
DB_REPORT = {
    "replay_events": 6470,
    "replay_tables": 6,
    "violations": 0,
    "audit_ticks": 10000,
    "index_ms": "0.041",
    "no_index_ms": "25.6",
    "speedup": "about 550–620×",
}


def event_details(event_type: str, payload: dict) -> str:
    """One event's payload in plain words."""
    p = payload
    if event_type == "reading":
        return f"{p.get('sensor_id')} read {p.get('value')} ({p.get('reading_id')})"
    if event_type == "maintenance":
        return f"{p.get('log_id')}: \u201c{p.get('description', '')}\u201d"
    if event_type == "recipe_change":
        return f"switched to recipe {p.get('recipe_id')} ({p.get('change_id')})"
    if event_type == "person_availability":
        return f"{p.get('person_id')} marked {'available' if p.get('available') else 'unavailable'}"
    if event_type == "alert_action":
        return f"{p.get('person_id')}: {p.get('action', '').replace('_', ' ')} {p.get('incident_id')}"
    if event_type == "tick":
        return f"minute {p.get('tick')} ends; time-based checks run"
    return json.dumps(p, sort_keys=True)
