"""Audit a finished simulation database for integrity violations. ALL DATA IS SYNTHETIC.

    .venv/bin/python scripts/integrity_audit.py --db path/to/sim.db     # audit an existing database
    .venv/bin/python scripts/integrity_audit.py --demo                  # build the demo scenario, then audit it
    .venv/bin/python scripts/integrity_audit.py --long 10000            # build a 10,000-tick run, then audit it

Read-only on the database it audits (opened with mode=ro). Each check reports a
count and up to five examples.

The schema keeps no status-history table, so an incident's status history is
rebuilt from the event log: every alert_action event for it, in order, through
the allowed transitions in incidents.TRANSITIONS (a request that isn't allowed
from the current status is a rejected request, not a status change), plus the
one automatic change, open -> expired, at the incident's last update. A
violation is a stored status that history can't reach.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import ACTIONABLE_SEVERITIES  # noqa: E402
from incidents import TRANSITIONS  # noqa: E402

EXAMPLES = 5


@dataclass
class Check:
    name: str
    examples: list = field(default_factory=list)
    count: int = 0
    note: str = ""

    def add(self, example) -> None:
        self.count += 1
        if len(self.examples) < EXAMPLES:
            self.examples.append(example)


def open_read_only(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def status_history(conn) -> dict[str, list[tuple[str, dict]]]:
    """incident_id -> [(ts, alert_action payload), ...] from the event log, in order."""
    actions: dict[str, list[tuple[str, dict]]] = {}
    for e in conn.execute("SELECT payload, ts FROM events WHERE event_type = 'alert_action' ORDER BY event_id"):
        p = json.loads(e["payload"])
        actions.setdefault(p["incident_id"], []).append((e["ts"], p))
    return actions


def replay_status(incident, actions: list[tuple[str, dict]]) -> tuple[str, list[str]]:
    """The status an incident's history leads to, and which confirm_hold requests were accepted (timestamps)."""
    status, holds = "open", []
    expired_at = incident["updated_at"] if incident["status"] == "expired" else None
    for ts, p in actions:
        if expired_at is not None and status == "open" and ts >= expired_at:
            # Expiry runs in the tick event, before any person's action in that minute.
            status = "expired"
        allowed_from, to_status = TRANSITIONS.get(p["action"], (frozenset(), None))
        if status in allowed_from:
            status = to_status
            if p["action"] == "confirm_hold":
                holds.append(ts)
    if expired_at is not None and status == "open":
        status = "expired"
    return status, holds


def audit(conn: sqlite3.Connection) -> list[Check]:
    sensors = {r["sensor_id"]: r["tool_id"] for r in conn.execute("SELECT sensor_id, tool_id FROM sensors")}
    tools = {r["tool_id"] for r in conn.execute("SELECT tool_id FROM tools")}
    people = {r["person_id"] for r in conn.execute("SELECT person_id FROM people")}
    incidents = {r["incident_id"]: r for r in conn.execute("SELECT * FROM incidents")}

    # 1. Unknown references in readings and events.
    refs = Check("Readings or events referring to unknown sensors, tools or people")
    for r in conn.execute("SELECT reading_id, sensor_id, tool_id FROM readings"):
        if r["sensor_id"] not in sensors:
            refs.add(f"{r['reading_id']}: unknown sensor {r['sensor_id']}")
        elif r["tool_id"] not in tools or sensors[r["sensor_id"]] != r["tool_id"]:
            refs.add(f"{r['reading_id']}: tool {r['tool_id']} doesn't own sensor {r['sensor_id']}")
    for e in conn.execute("SELECT event_id, event_type, tool_id, payload FROM events"):
        p = json.loads(e["payload"])
        if e["tool_id"] is not None and e["tool_id"] not in tools:
            refs.add(f"{e['event_id']}: unknown tool {e['tool_id']}")
        if "sensor_id" in p and p["sensor_id"] not in sensors:
            refs.add(f"{e['event_id']}: unknown sensor {p['sensor_id']}")
        if "person_id" in p and p["person_id"] not in people:
            refs.add(f"{e['event_id']}: unknown person {p['person_id']}")
        if e["event_type"] == "alert_action" and p["incident_id"] not in incidents:
            refs.add(f"{e['event_id']}: action on unknown incident {p['incident_id']}")

    # 2. Medium/high incidents nobody was told about.
    silent = Check("Incidents at medium or high with no notification")
    notified = {r[0] for r in conn.execute("SELECT DISTINCT incident_id FROM notifications")}
    for iid, inc in incidents.items():
        if inc["severity"] in ACTIONABLE_SEVERITIES and iid not in notified:
            silent.add(f"{iid}: {inc['severity']} {inc['rule_fired']} on {inc['sensor_id']}, status {inc['status']}")

    # 3. Accepted diagnoses citing evidence that isn't in their own stored bundle.
    citations = Check("Diagnoses citing an evidence ID not in their own stored bundle")
    caught = 0
    for d in conn.execute("SELECT diagnosis_id, status, cited_evidence, evidence_bundle FROM diagnoses"):
        bundle = json.loads(d["evidence_bundle"])
        ids = {item["id"] for kind in ("readings", "maintenance", "recipe_changes") for item in bundle.get(kind, [])}
        missing = [c for c in json.loads(d["cited_evidence"] or "[]") if c not in ids]
        if not missing:
            continue
        if d["status"] == "rejected":
            caught += 1  # the verifier caught it: this is the check working, not a violation
        else:
            citations.add(f"{d['diagnosis_id']} ({d['status']}): cites {', '.join(missing)}")
    citations.note = (f"{caught} rejected diagnoses cited missing IDs and were rejected by the checker, as designed; "
                      "they are not counted.")

    # 4/6. Status history, and held lots without a confirmed hold.
    history = status_history(conn)
    bad_history = Check("Incidents whose status history breaks the allowed transitions")
    confirmed_lots: set[str] = set()
    for iid, inc in incidents.items():
        reached, holds = replay_status(inc, history.get(iid, []))
        if reached != inc["status"]:
            bad_history.add(f"{iid}: stored {inc['status']}, but its actions lead to {reached}")
        if inc["status"] not in ("dismissed", "resolved", "expired", "acknowledged", "hold_confirmed", "open"):
            bad_history.add(f"{iid}: unknown status {inc['status']}")
        if holds:
            confirmed_lots.update(json.loads(inc["lots_at_risk"] or "[]"))
    held = Check("Held lots with no hold-confirmed incident")
    for r in conn.execute("SELECT lot_id, tool_id FROM lots WHERE status = 'held'"):
        if r["lot_id"] not in confirmed_lots:
            held.add(f"{r['lot_id']} on {r['tool_id']}")

    # 5. Notifications for incidents that don't exist.
    orphans = Check("Notifications for incidents that don't exist")
    for r in conn.execute("SELECT notification_id, incident_id FROM notifications"):
        if r["incident_id"] not in incidents:
            orphans.add(f"{r['notification_id']} -> {r['incident_id']}")

    return [refs, silent, citations, held, orphans, bad_history]


def summary_counts(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("events", "readings", "incidents", "notifications", "diagnoses", "lots")}


def report(title: str, conn) -> tuple[str, int]:
    checks = audit(conn)
    total = sum(c.count for c in checks)
    counts = summary_counts(conn)
    lines = [f"{title}: " + ", ".join(f"{v} {k}" for k, v in counts.items())]
    for c in checks:
        lines.append(f"  {c.count:4d}  {c.name}")
        lines += [f"          e.g. {ex}" for ex in c.examples]
        if c.note:
            lines.append(f"          ({c.note})")
    lines.append(f"  TOTAL VIOLATIONS: {total}")
    return "\n".join(lines), total


# ----- runs to audit -------------------------------------------------------------

def build_demo(db_path: Path) -> None:
    """The dashboard demo (docs/DEMO_SCRIPT.md), steps 1-9, with the scripted fake client."""
    from agents.diagnosis.client import FakeClient
    from orchestrator import build_system
    from sim.faults import demo_faults
    from sim.simulator import DEFAULT_WARMUP_TICKS

    s = build_system(db_path, seed=42, llm=FakeClient("valid"), faults_after_warmup=demo_faults(DEFAULT_WARMUP_TICKS))
    s.advance(50)
    s.alert_action("INC-0008", "acknowledge", "P-01")
    s.recipe_change("T-05", "R-05-B")
    s.send_malformed()
    s.advance(54)
    s.alert_action("INC-0008", "confirm_hold", "P-01")
    s.advance(150)
    s.conn.close()


def build_long(db_path: Path, ticks: int) -> None:
    """A long unattended run: faults 1-4 after warm-up, scripted fake client, nobody acknowledges."""
    from agents.diagnosis.client import FakeClient
    from orchestrator import build_system
    from sim.faults import NoisyHealthy, default_faults
    from sim.simulator import DEFAULT_WARMUP_TICKS

    s = build_system(db_path, seed=42, llm=FakeClient("valid"), total_ticks=ticks + 10)
    for f in default_faults(s.world, DEFAULT_WARMUP_TICKS):
        if not isinstance(f, NoisyHealthy):  # build_system already added the noisy-healthy sensors
            s.sim.inject(f)
    s.advance(ticks - s.clock.tick)
    s.conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--db")
    group.add_argument("--demo", action="store_true")
    group.add_argument("--long", type=int, metavar="TICKS")
    args = ap.parse_args()
    if args.db:
        path, title = Path(args.db), args.db
    else:
        path = Path(tempfile.mkdtemp(prefix="audit-")) / "audit.db"
        if args.demo:
            build_demo(path)
            title = "Demo scenario (minute 404)"
        else:
            build_long(path, args.long)
            title = f"{args.long:,}-tick run"
    conn = open_read_only(path)
    text, total = report(title, conn)
    conn.close()
    print(text)
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
