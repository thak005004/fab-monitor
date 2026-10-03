"""End to end with FakeClient: planted drift -> incident -> diagnosis citing the
planted entry -> triage -> notification row. Plus faults 8, 9 and 10."""

from __future__ import annotations

import json

import pytest

from agents.diagnosis.client import FakeClient
from sim.faults import DriftNoCause, DriftWithDecoys, DriftWithInjection, GradualDrift, INJECTION_NOTE

from .conftest import WARMUP, make_system

SID, TOOL = "S-01-TEMP", "T-01"
START = WARMUP + 25  # fault 8's unrelated decoy is 20 ticks before the drift


def first_notified_incident(s, sid, start):
    row = s.conn.execute(
        "SELECT i.* FROM notifications n JOIN incidents i USING (incident_id)"
        " WHERE i.sensor_id = ? AND n.sent_at >= ? ORDER BY n.sent_at LIMIT 1",
        (sid, s.clock.iso_at(start)),
    ).fetchone()
    return dict(row) if row else None


def run_until_notified(s, sid, start, max_ticks=120):
    for _ in range(max_ticks):
        s.run(1)
        inc = first_notified_incident(s, sid, start)
        if inc:
            return inc
    raise AssertionError("never notified")


def planted(s, fault_type):
    return s.conn.execute("SELECT planted_cause_id, expected_outcome FROM fault_injections WHERE fault_type = ?",
                          (fault_type,)).fetchone()


def test_planted_drift_end_to_end(tmp_path):
    s = make_system(tmp_path / "e2e.db", faults=[GradualDrift(SID, start_tick=START)], llm=FakeClient("valid"))
    inc = run_until_notified(s, SID, START)
    cause = planted(s, "gradual_drift")["planted_cause_id"]

    # Incident with the right lots: every T-01 lot between onset and when it was opened.
    onset, opened = s.clock.tick_of(inc["onset_ts"]), s.clock.tick_of(inc["opened_at"])
    assert inc["severity"] in ("medium", "high")
    expected_lots = {l.lot_id for l in s.world.lots if l.tool_id == TOOL and l.start_tick <= opened and l.end_tick >= onset}
    assert expected_lots <= set(json.loads(inc["lots_at_risk"]))

    # Verified diagnosis citing the planted maintenance entry.
    dx = dict(s.conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = ?", (inc["latest_diagnosis_id"],)).fetchone())
    assert dx["status"] == "diagnosed" and json.loads(dx["cited_evidence"]) == [cause]

    # Triage + notification row carrying the verified citation.
    [n] = [dict(r) for r in s.conn.execute("SELECT * FROM notifications WHERE incident_id = ?", (inc["incident_id"],))]
    assert n["reason"] in ("new", "upgraded") and n["person_id"] == s.world.qualified_people(TOOL)[0]
    assert cause in n["message"] and "Likely contributing factors" in n["message"]
    assert inc["owner_id"] == n["person_id"]


def test_fault_8_decoys_are_recorded_and_citing_one_is_rejected(tmp_path):
    s = make_system(tmp_path / "f8.db", faults=[DriftWithDecoys(SID, start_tick=START)])
    row = planted(s, "drift_with_decoys")
    decoys = row["expected_outcome"].split("decoys:")[1].split(",")
    assert row["planted_cause_id"] and len(decoys) == 3 and row["planted_cause_id"] not in decoys

    other_tool, unrelated, after_onset = decoys
    s.bus.orchestrator.llm = FakeClient(json.dumps({
        "status": "diagnosed", "likely_factors": ["Heater board replaced"], "cited_evidence": [other_tool],
        "confidence": "high"}))
    inc = run_until_notified(s, SID, START)
    dx = s.conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = ?", (inc["latest_diagnosis_id"],)).fetchone()
    assert dx["status"] == "rejected" and other_tool in dx["rejection_reason"]
    bundle = json.loads(dx["evidence_bundle"])
    ids = [m["id"] for m in bundle["maintenance"]]
    assert row["planted_cause_id"] in ids and unrelated in ids and other_tool not in ids
    # The rejected diagnosis is not shown as trusted, but the alert still went out.
    n = s.conn.execute("SELECT message FROM notifications WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0]
    assert "failed verification" in n

    # The after-onset decoy lands later on T-01 and, once in the evidence, citing it is rejected.
    log = {r["log_id"]: r for r in s.conn.execute("SELECT * FROM maintenance_log")}
    s.run_until(START + 45)
    assert after_onset in {r[0] for r in s.conn.execute("SELECT log_id FROM maintenance_log WHERE tool_id = ?", (TOOL,))}
    assert log[other_tool]["tool_id"] == "T-02"


def test_fault_9_drift_with_no_cause_has_no_planted_entry(tmp_path):
    s = make_system(tmp_path / "f9.db", faults=[DriftNoCause(SID, start_tick=START)], llm=FakeClient("valid"))
    row = planted(s, "drift_no_cause")
    assert row["planted_cause_id"] is None and row["expected_outcome"].endswith("diagnosis:abstained")
    inc = run_until_notified(s, SID, START)
    dx = s.conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = ?", (inc["latest_diagnosis_id"],)).fetchone()
    assert json.loads(dx["evidence_bundle"])["maintenance"] == []
    assert dx["status"] == "abstained"  # the "valid" script has nothing to cite, so it abstains


def test_fault_10_injection_note_is_sent_as_data_and_incident_is_unaffected(tmp_path):
    s = make_system(tmp_path / "f10.db", faults=[DriftWithInjection(SID, start_tick=START)], llm=FakeClient("valid"))
    row = planted(s, "prompt_injection")
    inc = run_until_notified(s, SID, START)
    assert inc["severity"] in ("medium", "high")  # detection doesn't read notes at all
    client = s.bus.orchestrator.llm
    system, user = client.calls[-1]
    evidence = user.split("<evidence>")[1].split("</evidence>")[0]
    assert "ignore all previous instructions" in evidence  # inside the evidence block, as data
    assert user.count("<evidence>") == 1 and user.count("</evidence>") == 1
    note = s.conn.execute("SELECT description FROM maintenance_log WHERE log_id = ?", (row["planted_cause_id"],)).fetchone()[0]
    assert note == INJECTION_NOTE
    dx = s.conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = ?", (inc["latest_diagnosis_id"],)).fetchone()
    assert json.loads(dx["cited_evidence"]) == [row["planted_cause_id"]]


@pytest.mark.parametrize("fault_cls, fault_type", [
    (DriftWithDecoys, "drift_with_decoys"), (DriftNoCause, "drift_no_cause"), (DriftWithInjection, "prompt_injection"),
])
def test_faults_8_to_10_are_written_to_fault_injections(tmp_path, fault_cls, fault_type):
    s = make_system(tmp_path / "fi.db", faults=[fault_cls(SID, start_tick=START)])
    row = s.conn.execute("SELECT * FROM fault_injections WHERE fault_type = ?", (fault_type,)).fetchone()
    assert row and row["sensor_id"] == SID and row["start_ts"] == s.clock.iso_at(START)
    assert row["expected_outcome"].startswith("incident:beyond_3sigma|sustained_run")


def test_fault_with_an_impossible_decoy_leaves_nothing_behind(tmp_path):
    s = make_system(tmp_path / "bad.db")
    with pytest.raises(ValueError):
        s.sim.inject(DriftWithDecoys(SID, start_tick=WARMUP + 5))  # unrelated decoy would be in the past
    assert s.sim._scheduled_maintenance == {}
    assert s.conn.execute("SELECT COUNT(*) FROM fault_injections WHERE fault_type = 'drift_with_decoys'").fetchone()[0] == 0
