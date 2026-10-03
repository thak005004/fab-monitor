"""Notifications, escalation, reassignment, persistence, and the orchestrator's
PERSON_AVAILABILITY and ALERT_ACTION branches (§12, §13), through the bus."""

from __future__ import annotations

import json

import pytest

from agents.diagnosis.client import FakeClient
from sim.faults import StepShift

from .conftest import make_system

SID, TOOL = "S-05-PRES", "T-05"


def force_medium(s, sid=SID):
    """Push a healthy sensor into a medium sustained_run incident (a false alarm)."""
    st = s.sensor_state(sid)
    for _ in range(s.config.run_length):
        s.step_with(overrides={sid: st["control_mean"] + 0.5 * st["control_stddev"]})
    return dict(next(i for i in s.incidents(sid) if i["status"] == "open"))


def quiet(s, n, sid=SID):
    st = s.sensor_state(sid)
    for k in range(n):
        s.step_with(overrides={sid: st["control_mean"] + (0.1 if k % 2 else -0.1) * st["control_stddev"]})


def notes(s, incident_id):
    return [dict(r) for r in s.conn.execute(
        "SELECT * FROM notifications WHERE incident_id = ? ORDER BY sent_at, notification_id", (incident_id,))]


@pytest.fixture
def s(tmp_path):
    system = make_system(tmp_path / "n.db", llm=FakeClient("abstain"))
    yield system
    system.conn.close()


def test_new_medium_incident_notifies_first_qualified_person(s):
    inc = force_medium(s)
    [n] = notes(s, inc["incident_id"])
    first = s.world.qualified_people(TOOL)[0]
    assert (n["person_id"], n["reason"], n["escalation_level"]) == (first, "new", 0)
    assert n["sent_at"] == inc["opened_at"] and n["acknowledged_at"] is None
    assert inc["owner_id"] == first and inc["latest_diagnosis_id"]
    assert "[MEDIUM] new: S-05-PRES on T-05" in n["message"]


def test_low_incident_never_notifies(s):
    st = s.sensor_state(SID)
    s.step_with(overrides={SID: st["control_mean"] + 4 * st["control_stddev"]})
    inc = next(i for i in s.incidents(SID) if i["status"] == "open")
    assert inc["severity"] == "low" and notes(s, inc["incident_id"]) == []
    assert s.conn.execute("SELECT COUNT(*) FROM diagnoses WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == 0


def test_no_acknowledgment_escalates_to_next_person_then_on_call_then_stops(s):
    inc = force_medium(s)
    qualified = s.world.qualified_people(TOOL)
    st = s.sensor_state(SID)
    # Keep it triggering (so it doesn't expire) while nobody acknowledges.
    for _ in range(s.config.escalation_ticks * (len(qualified) + 2)):
        s.step_with(overrides={SID: st["control_mean"] + 0.5 * st["control_stddev"]})
    ns = notes(s, inc["incident_id"])
    assert [n["person_id"] for n in ns] == qualified + [s.config.oncall_person_id]  # then stops
    assert [n["reason"] for n in ns] == ["new"] + ["escalated"] * len(qualified)
    assert [n["escalation_level"] for n in ns] == list(range(len(qualified) + 1))
    gaps = [s.clock.tick_of(b["sent_at"]) - s.clock.tick_of(a["sent_at"]) for a, b in zip(ns, ns[1:])]
    assert all(g == s.config.escalation_ticks for g in gaps)
    final = dict(s.conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone())
    assert final["owner_id"] == s.config.oncall_person_id and final["escalation_level"] == len(qualified)
    assert ns[-1]["message"].startswith("[ON-CALL]")


def test_acknowledge_stops_escalation_and_marks_notification(s):
    inc = force_medium(s)
    owner = inc["owner_id"]
    s.step_with(extra=[s.alert_action(inc["incident_id"], "acknowledge", owner)])
    st = s.sensor_state(SID)
    for _ in range(3 * s.config.escalation_ticks):
        s.step_with(overrides={SID: st["control_mean"] + 0.5 * st["control_stddev"]})
    ns = notes(s, inc["incident_id"])
    assert [n["reason"] for n in ns if n["reason"] == "escalated"] == []
    assert ns[0]["acknowledged_at"] is not None


def test_person_marked_unavailable_gets_open_incidents_reassigned(s):
    inc = force_medium(s)
    owner = inc["owner_id"]
    s.step_with(extra=[s.availability(owner, False)])
    ns = notes(s, inc["incident_id"])
    assert ns[-1]["reason"] == "reassigned" and ns[-1]["person_id"] != owner
    assert ns[-1]["person_id"] == next(p for p in s.world.qualified_people(TOOL) if p != owner)
    now_owner = s.conn.execute("SELECT owner_id FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0]
    assert now_owner == ns[-1]["person_id"]
    assert s.conn.execute("SELECT available FROM people WHERE person_id = ?", (owner,)).fetchone()[0] == 0


def test_acknowledged_incidents_are_not_reassigned(s):
    inc = force_medium(s)
    s.step_with(extra=[s.alert_action(inc["incident_id"], "acknowledge", inc["owner_id"])])
    s.step_with(extra=[s.availability(inc["owner_id"], False)])
    assert "reassigned" not in [n["reason"] for n in notes(s, inc["incident_id"])]


def test_confirm_hold_through_the_bus_holds_the_at_risk_lots(s):
    s.step_with(overrides={SID: s.conn.execute(
        "SELECT spec_upper FROM sensors WHERE sensor_id = ?", (SID,)).fetchone()[0] + 1})  # beyond spec: high
    inc = dict(next(i for i in s.incidents(SID) if i["status"] == "open"))
    assert inc["severity"] == "high" and inc["recommend_hold"] == 1
    lots = json.loads(inc["lots_at_risk"])
    assert lots
    s.step_with(extra=[s.alert_action(inc["incident_id"], "acknowledge", inc["owner_id"])])
    s.step_with(extra=[s.alert_action(inc["incident_id"], "confirm_hold", inc["owner_id"])])
    held = {r[0] for r in s.conn.execute("SELECT status FROM lots WHERE lot_id IN (%s)" % ",".join("?" * len(lots)), lots)}
    assert held == {"held"}


def test_invalid_alert_action_through_the_bus_is_rejected_and_pipeline_continues(s):
    inc = force_medium(s)
    s.step_with(extra=[s.alert_action(inc["incident_id"], "resolve", inc["owner_id"])])  # open -> resolved: invalid
    assert s.conn.execute("SELECT status FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == "open"
    s.step_with(extra=[s.alert_action(inc["incident_id"], "acknowledge", inc["owner_id"])])
    assert s.conn.execute("SELECT status FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == "acknowledged"


def test_reactivated_medium_incident_is_rediagnosed_and_notified(s):
    inc = force_medium(s)
    s.step_with(extra=[s.alert_action(inc["incident_id"], "acknowledge", inc["owner_id"])])
    quiet(s, s.config.reactivation_quiet_ticks)
    st = s.sensor_state(SID)
    s.step_with(overrides={SID: st["control_mean"] + 4 * st["control_stddev"]})
    ns = notes(s, inc["incident_id"])
    assert ns[-1]["reason"] == "reactivated" and ns[-1]["person_id"] == inc["owner_id"]
    assert s.conn.execute("SELECT COUNT(*) FROM diagnoses WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == 2


def test_diagnosis_is_not_called_again_while_severity_stays_the_same(s):
    inc = force_medium(s)

    def diagnoses():
        return s.conn.execute("SELECT COUNT(*) FROM diagnoses WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0]

    assert diagnoses() == 1
    st = s.sensor_state(SID)
    for _ in range(5):  # keeps triggering sustained_run (medium): NONE every time
        s.step_with(overrides={SID: st["control_mean"] + 0.5 * st["control_stddev"]})
    assert diagnoses() == 1
    assert len(notes(s, inc["incident_id"])) == 1


def test_persistent_notice_for_fault_landing_on_an_acknowledged_false_alarm(s):
    """Seeds 18/20: a false alarm is notified and acknowledged just before a real
    step shift; the step keeps the incident triggering with no quiet gap, so it
    never reactivates. The owner must still hear about it ("persistent")."""
    inc = force_medium(s)
    new_tick = s.clock.tick
    start = s.clock.tick + 1
    s.sim.inject(StepShift(SID, start_tick=start))  # the real fault 2: 2 sigma, inside spec
    s.step_with(extra=[s.alert_action(inc["incident_id"], "acknowledge", inc["owner_id"])])
    s.run(s.config.persistence_ticks + 25)

    ns = notes(s, inc["incident_id"])
    assert [n["reason"] for n in ns] == ["new", "persistent"]  # at most once, no reactivation, no escalation
    persistent = ns[1]
    assert persistent["person_id"] == inc["owner_id"]
    assert s.clock.tick_of(persistent["sent_at"]) >= new_tick + s.config.persistence_ticks
    assert s.conn.execute("SELECT COUNT(*) FROM diagnoses WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == 2
    # Same incident throughout: the fault was not split off or lost.
    assert [i["incident_id"] for i in s.incidents(SID) if i["opened_at"] >= inc["opened_at"]] == [inc["incident_id"]]


def test_unacknowledged_incident_gets_escalation_not_persistence(s):
    inc = force_medium(s)
    s.sim.inject(StepShift(SID, start_tick=s.clock.tick + 1))
    s.run(s.config.persistence_ticks + 10)
    reasons = [n["reason"] for n in notes(s, inc["incident_id"])]
    assert "persistent" not in reasons and "escalated" in reasons


# ----- escalation rules (§12) ---------------------------------------------------

def test_quiet_unacknowledged_medium_false_alarm_does_not_escalate(s):
    inc = force_medium(s)
    quiet(s, s.config.escalation_ticks + 5)  # no triggers, still open (expires only after stale_after_ticks)
    assert s.conn.execute("SELECT status FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == "open"
    assert [n["reason"] for n in notes(s, inc["incident_id"])] == ["new"]


def test_unacknowledged_medium_incident_that_keeps_triggering_escalates(s):
    inc = force_medium(s)
    st = s.sensor_state(SID)
    for _ in range(s.config.escalation_ticks):
        s.step_with(overrides={SID: st["control_mean"] + 0.5 * st["control_stddev"]})
    assert [n["reason"] for n in notes(s, inc["incident_id"])] == ["new", "escalated"]


def test_unacknowledged_high_incident_escalates_even_if_quiet(s):
    spec_upper = s.conn.execute("SELECT spec_upper FROM sensors WHERE sensor_id = ?", (SID,)).fetchone()[0]
    s.step_with(overrides={SID: spec_upper + 1})
    inc = dict(next(i for i in s.incidents(SID) if i["status"] == "open"))
    assert inc["severity"] == "high"
    quiet(s, s.config.escalation_ticks)
    assert [n["reason"] for n in notes(s, inc["incident_id"])] == ["new", "escalated"]


# ----- lots during an ongoing incident (§9) ----------------------------------------

def test_lot_starting_during_an_active_incident_is_marked_within_one_tick_without_notifying(s):
    inc = force_medium(s)
    s.step_with(extra=[s.alert_action(inc["incident_id"], "acknowledge", inc["owner_id"])])  # no escalation
    upcoming = min((l for l in s.world.lots if l.tool_id == TOOL and l.start_tick > s.clock.tick + 1),
                   key=lambda l: l.start_tick)
    quiet(s, upcoming.start_tick - 1 - s.clock.tick)  # acknowledged incidents don't expire
    assert s.conn.execute("SELECT status FROM lots WHERE lot_id = ?", (upcoming.lot_id,)).fetchone()[0] == "normal"
    before = len(notes(s, inc["incident_id"]))

    quiet(s, 1)  # the tick the lot starts
    assert s.clock.tick == upcoming.start_tick
    assert s.conn.execute("SELECT status FROM lots WHERE lot_id = ?", (upcoming.lot_id,)).fetchone()[0] == "at_risk"
    stored = json.loads(s.conn.execute("SELECT lots_at_risk FROM incidents WHERE incident_id = ?",
                                       (inc["incident_id"],)).fetchone()[0])
    assert upcoming.lot_id in stored
    assert len(notes(s, inc["incident_id"])) == before
    assert s.conn.execute("SELECT status FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == "acknowledged"
