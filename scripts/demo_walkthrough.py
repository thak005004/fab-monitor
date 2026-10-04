"""Run the dashboard demo headless, pressing the same buttons as docs/DEMO_SCRIPT.md,
and record what appears after each step. ALL DATA IS SYNTHETIC.

    .venv/bin/python scripts/demo_walkthrough.py        # prints the facts as JSON

Uses Streamlit's AppTest with the scripted fake LLM client, so the numbers are
exact and repeatable. tests/test_demo_script.py checks them against the script.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

APP = str(ROOT / "dashboard" / "app.py")
DRIFT_SENSOR = "S-01-TEMP"
RECIPE_TOOL = "T-05"


def _click(at: AppTest, label: str | None = None, key: str | None = None, sidebar: bool = False) -> None:
    where = at.sidebar if sidebar else at
    button = where.button(key=key) if key else next(b for b in where.button if b.label == label)
    button.click().run()
    if at.exception:
        raise RuntimeError(at.exception)


def _metric(at: AppTest, label: str) -> str:
    return next(m.value for m in at.metric if m.label == label)


def _drift_incident(conn) -> dict | None:
    row = conn.execute(
        "SELECT * FROM incidents WHERE sensor_id = ? AND rule_fired != 'dropout' ORDER BY opened_at DESC LIMIT 1",
        (DRIFT_SENSOR,),
    ).fetchone()
    return dict(row) if row else None


def _incident_facts(system, inc: dict) -> dict:
    conn, clock = system.conn, system.clock
    notes = conn.execute(
        "SELECT person_id, reason, sent_at, escalation_level FROM notifications WHERE incident_id = ? ORDER BY sent_at, notification_id",
        (inc["incident_id"],),
    ).fetchall()
    dxs = conn.execute(
        "SELECT diagnosis_id, status, cited_evidence, rejection_reason, created_at FROM diagnoses WHERE incident_id = ?"
        " ORDER BY created_at, diagnosis_id", (inc["incident_id"],),
    ).fetchall()
    return {
        "incident_id": inc["incident_id"], "rule": inc["rule_fired"], "severity": inc["severity"],
        "status": inc["status"], "onset_tick": clock.tick_of(inc["onset_ts"]),
        "opened_tick": clock.tick_of(inc["opened_at"]), "owner": inc["owner_id"],
        "recommend_hold": bool(inc["recommend_hold"]), "lots_at_risk": json.loads(inc["lots_at_risk"] or "[]"),
        "notifications": [{"tick": clock.tick_of(n["sent_at"]), "person": n["person_id"], "reason": n["reason"],
                           "level": n["escalation_level"]} for n in notes],
        "diagnoses": [{"tick": clock.tick_of(d["created_at"]), "id": d["diagnosis_id"], "status": d["status"],
                       "cited": json.loads(d["cited_evidence"] or "[]"), "reason": d["rejection_reason"]} for d in dxs],
    }


def _other_incidents(system, since_tick: int, until_tick: int) -> list[dict]:
    conn, clock = system.conn, system.clock
    rows = conn.execute(
        "SELECT * FROM incidents WHERE sensor_id != ? AND opened_at > ? AND opened_at <= ? ORDER BY opened_at",
        (DRIFT_SENSOR, clock.iso_at(since_tick), clock.iso_at(until_tick)),
    ).fetchall()
    return [{"incident_id": r["incident_id"], "sensor": r["sensor_id"], "rule": r["rule_fired"],
             "severity": r["severity"], "opened_tick": clock.tick_of(r["opened_at"])} for r in rows]


def run_walkthrough() -> dict:
    """Press the demo buttons in order; return what the dashboard shows after each step."""
    previous = os.environ.get("FAB_MONITOR_LLM")
    os.environ["FAB_MONITOR_LLM"] = "fake"
    try:
        at = AppTest.from_file(APP, default_timeout=180)
        at.run()
        if at.exception:
            raise RuntimeError(at.exception)
        system = at.session_state.system
        conn, clock = system.conn, system.clock
        facts: dict = {}

        # Step 1: the page loads with the demo scenario.
        drift_fault = conn.execute("SELECT * FROM fault_injections WHERE fault_type = 'gradual_drift'").fetchone()
        planted = conn.execute("SELECT * FROM maintenance_log WHERE log_id = ?", (drift_fault["planted_cause_id"],)).fetchone()
        facts["load"] = {
            "tick": int(_metric(at, "Simulated minute")), "config": _metric(at, "Settings version"),
            "baselines": sorted({r[0] for r in conn.execute("SELECT baseline_status FROM sensor_state")}),
            "drift_start_tick": clock.tick_of(drift_fault["start_ts"]),
            "planted_id": planted["log_id"] if planted else drift_fault["planted_cause_id"],
            "planted_tick": clock.tick_of(drift_fault["start_ts"]) - 1,
            "dead_letters": int(_metric(at, "Rejected bad data")),
        }

        # Step 2: +50 -> the drift is caught and a person is notified.
        _click(at, "+50", sidebar=True)
        inc = _drift_incident(conn)
        facts["advance_1"] = {
            "tick": clock.tick, "drift_incident": _incident_facts(system, inc) if inc else None,
            "planted_text": conn.execute("SELECT description FROM maintenance_log WHERE log_id = ?",
                                         (facts["load"]["planted_id"],)).fetchone()[0],
            "other_incidents": _other_incidents(system, facts["load"]["tick"], clock.tick),
        }

        # Step 3: acknowledge the drift incident as its owner.
        at.selectbox(key="incident_detail").set_value(inc["incident_id"]).run()
        _click(at, key=f"acknowledge_{inc['incident_id']}")
        facts["acknowledge"] = {"status": _drift_incident(conn)["status"],
                                "acting_as": at.selectbox(key=f"actor_{inc['incident_id']}").value}

        # Step 4: live recipe change on T-05.
        at.sidebar.selectbox(key="r_tool").set_value(RECIPE_TOOL).run()
        recipe_id = at.sidebar.text_input(key="r_recipe").value
        _click(at, "Change recipe", sidebar=True)
        facts["recipe_change"] = {
            "tick": clock.tick, "tool": RECIPE_TOOL, "recipe_id": recipe_id,
            "statuses": sorted({r[0] for r in conn.execute(
                "SELECT ss.baseline_status FROM sensor_state ss JOIN sensors s USING (sensor_id) WHERE s.tool_id = ?",
                (RECIPE_TOOL,))}),
            "change_id": conn.execute("SELECT change_id FROM recipe_changes WHERE tool_id = ? ORDER BY ts DESC LIMIT 1",
                                      (RECIPE_TOOL,)).fetchone()[0],
        }

        # Step 5: a malformed event.
        _click(at, "Send bad data", sidebar=True)
        dl = conn.execute("SELECT * FROM dead_letter ORDER BY id DESC LIMIT 1").fetchone()
        facts["malformed"] = {"dead_letters": int(_metric(at, "Rejected bad data")), "id": dl["id"],
                              "reason": dl["error_reason"]}

        # Step 6: Kill LLM, then +50: the alert path without the model.
        at.sidebar.toggle(key="kill_llm").set_value(True).run()
        _click(at, "+50", sidebar=True)
        facts["advance_2"] = {"tick": clock.tick, "drift_incident": _incident_facts(system, _drift_incident(conn))}

        # Step 7: +1 four times: the drift goes out of spec at tick 254 (LLM still killed).
        for _ in range(4):
            _click(at, "+1", sidebar=True)
        facts["out_of_spec"] = {"tick": clock.tick, "drift_incident": _incident_facts(system, _drift_incident(conn))}

        # Step 8: confirm the hold.
        inc = _drift_incident(conn)
        at.selectbox(key="incident_detail").set_value(inc["incident_id"]).run()
        _click(at, key=f"confirm_hold_{inc['incident_id']}")
        held = json.loads(_drift_incident(conn)["lots_at_risk"])
        facts["confirm_hold"] = {
            "status": _drift_incident(conn)["status"], "lots_held": int(_metric(at, "Product batches on hold")),
            "held": sorted(r[0] for r in conn.execute(
                f"SELECT lot_id FROM lots WHERE status = 'held' AND lot_id IN ({','.join('?' * len(held))})", held)),
        }

        # Step 9: LLM back on, then +50 x3: T-05 finishes relearning.
        at.sidebar.toggle(key="kill_llm").set_value(False).run()
        for _ in range(3):
            _click(at, "+50", sidebar=True)
        from state.baseline import relearning_windows
        window = relearning_windows(conn, "S-05-TEMP", clock)
        facts["relearn_done"] = {
            "tick": clock.tick,
            "statuses": sorted({r[0] for r in conn.execute(
                "SELECT ss.baseline_status FROM sensor_state ss JOIN sensors s USING (sensor_id) WHERE s.tool_id = ?",
                (RECIPE_TOOL,))}),
            "band": [[clock.tick_of(a), clock.tick_of(b), ongoing] for a, b, ongoing in window],
            "t05_incidents_since_change": [
                {"incident_id": r["incident_id"], "sensor": r["sensor_id"], "rule": r["rule_fired"],
                 "severity": r["severity"], "opened_tick": clock.tick_of(r["opened_at"])}
                for r in conn.execute("SELECT * FROM incidents WHERE tool_id = ? AND opened_at > ? ORDER BY opened_at",
                                      (RECIPE_TOOL, clock.iso_at(facts["recipe_change"]["tick"])))],
            "drift_incident": _incident_facts(system, _drift_incident(conn)),
        }
        return facts
    finally:
        if previous is None:
            os.environ.pop("FAB_MONITOR_LLM", None)
        else:
            os.environ["FAB_MONITOR_LLM"] = previous


if __name__ == "__main__":
    print(json.dumps(run_walkthrough(), indent=2))
