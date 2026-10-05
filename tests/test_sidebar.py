"""Every sidebar control: it does what it says, and the person always gets feedback
(a pop-up toast, which shows even when the sidebar covers the page). Fake client only."""

from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agents.diagnosis.client import FailingClient
from dashboard.scenarios import SCENARIOS
from sim.faults import LIVE_FAULT_TYPES

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")


@pytest.fixture
def at(monkeypatch):
    monkeypatch.setenv("FAB_MONITOR_LLM", "fake")
    app = AppTest.from_file(APP, default_timeout=120)
    app.run()
    assert not app.exception, app.exception
    return app


def click(app, label: str):
    next(b for b in app.sidebar.button if b.label == label).click().run()
    assert not app.exception, app.exception


def toasts(app) -> list[str]:
    return [t.value for t in app.toast]


def minute(app) -> int:
    return int(next(m.value for m in app.metric if m.label == "Minute"))


def conn(app):
    return app.session_state.system.conn


def test_every_sidebar_button_and_control_is_covered(at):
    assert [b.label for b in at.sidebar.button] == [
        "Load demo scenario", "+1", "+10", "+50", *(sc.label for sc in SCENARIOS),
        "Start this problem", "Change recipe", "Send bad data", "Mark unavailable"]
    assert [t.key for t in at.sidebar.toggle] == ["kill_llm"]
    assert [s.key for s in at.sidebar.selectbox] == ["f_tool", "f_sensor", "f_type", "r_tool", "p_id"]
    assert [t.key for t in at.sidebar.text_input] == ["r_recipe"]


def test_page_load_says_the_demo_is_loaded(at):
    assert any(t.startswith("Demo loaded at minute 150.") for t in toasts(at))


def test_advance_buttons_move_time_and_say_what_was_found(at):
    click(at, "+1")
    assert minute(at) == 151 and toasts(at) == ["Moved forward 1 minute, to minute 151. No new problems."]
    click(at, "+10")
    assert minute(at) == 161 and toasts(at)[0].startswith("Moved forward 10 minutes, to minute 161.")
    click(at, "+50")
    assert minute(at) == 211
    found = conn(at).execute("SELECT COUNT(*) FROM incidents WHERE opened_at > '2026-01-05T08:41:00+00:00'").fetchone()[0]
    assert toasts(at) == [f"Moved forward 50 minutes, to minute 211. {found} new problems found: see Activity or Incidents."]
    assert not at.success  # pop-up only: no box above the tabs for time steps


def test_outage_toggle_switches_the_ai_and_says_so(at):
    at.sidebar.toggle(key="kill_llm").set_value(True).run()
    assert isinstance(at.session_state.system.orchestrator.llm, FailingClient)
    assert any(c.value == "AI: off (simulated outage)" for c in at.sidebar.caption)
    assert toasts(at)[0].startswith("AI outage on: new alerts will go out without an AI diagnosis.")
    at.sidebar.toggle(key="kill_llm").set_value(False).run()
    assert not isinstance(at.session_state.system.orchestrator.llm, FailingClient)
    assert toasts(at) == ["AI back on: new alerts get an AI diagnosis again."]


def test_load_demo_starts_over_including_the_ai_outage(at):
    click(at, "+50")
    at.sidebar.toggle(key="kill_llm").set_value(True).run()
    click(at, "Load demo scenario")
    assert minute(at) == 150
    assert at.sidebar.toggle(key="kill_llm").value is False
    assert not isinstance(at.session_state.system.orchestrator.llm, FailingClient)
    assert any(t.startswith("Demo loaded at minute 150.") for t in toasts(at))


def test_sensor_choices_follow_the_machine(at):
    at.sidebar.selectbox(key="f_tool").set_value("T-03").run()
    assert at.sidebar.selectbox(key="f_sensor").value == "S-03-TEMP"
    assert at.sidebar.selectbox(key="f_sensor").options == [
        "temperature (S-03-TEMP)", "pressure (S-03-PRES)", "RF power (S-03-RF)"]


@pytest.mark.parametrize("fault_type", LIVE_FAULT_TYPES)
def test_start_this_problem_works_for_every_kind(at, fault_type):
    at.sidebar.selectbox(key="f_tool").set_value("T-03").run()
    at.sidebar.selectbox(key="f_sensor").set_value("S-03-PRES").run()
    at.sidebar.selectbox(key="f_type").set_value(fault_type).run()
    click(at, "Start this problem")
    row = conn(at).execute("SELECT fault_type, sensor_id FROM fault_injections ORDER BY fault_id DESC LIMIT 1").fetchone()
    assert (row["fault_type"], row["sensor_id"]) == (fault_type, "S-03-PRES")
    assert toasts(at)[0].startswith("Started ") and "S-03-PRES" in toasts(at)[0]


def test_recipe_name_follows_the_machine_and_is_applied_to_it(at):
    assert at.sidebar.text_input(key="r_recipe").value == "R-05-B"  # T-05 by default
    at.sidebar.selectbox(key="r_tool").set_value("T-02").run()
    assert at.sidebar.text_input(key="r_recipe").value == "R-02-B"
    click(at, "Change recipe")
    row = conn(at).execute("SELECT current_recipe_id FROM tool_state WHERE tool_id = 'T-02'").fetchone()
    assert row[0] == "R-02-B"
    states = {r[0] for r in conn(at).execute(
        "SELECT ss.baseline_status FROM sensor_state ss JOIN sensors s USING (sensor_id) WHERE s.tool_id = 'T-02'")}
    assert states == {"relearning"}
    assert toasts(at)[0].startswith("Deposition Tool 2 (T-02) switched to recipe R-02-B")


def test_recipe_change_refuses_an_empty_or_unchanged_name_without_side_effects(at):
    events = conn(at).execute("SELECT COUNT(*) FROM events").fetchone()[0]
    at.sidebar.text_input(key="r_recipe").set_value("   ").run()
    click(at, "Change recipe")
    assert toasts(at) == ["Type a name for the new recipe first."]
    at.sidebar.text_input(key="r_recipe").set_value("R-05-A").run()  # what T-05 already runs
    click(at, "Change recipe")
    assert toasts(at) == ["Etch Tool 5 (T-05) is already running recipe R-05-A. Type a different name."]
    assert conn(at).execute("SELECT COUNT(*) FROM events").fetchone()[0] == events  # nothing was sent
    assert conn(at).execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 0


def test_send_bad_data_is_rejected_and_counted(at):
    click(at, "Send bad data")
    assert conn(at).execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 1
    assert toasts(at) == ["Bad data rejected and set aside (dead letter); monitoring kept going."]


def test_mark_unavailable_then_available_and_say_what_happened_to_their_alerts(at):
    click(at, "+50")  # Avery Lin (P-01) now owns the unanswered INC-0008 alert
    click(at, "Mark unavailable")
    assert conn(at).execute("SELECT available FROM people WHERE person_id = 'P-01'").fetchone()[0] == 0
    new_owner = conn(at).execute("SELECT owner_id FROM incidents WHERE incident_id = 'INC-0008'").fetchone()[0]
    assert new_owner != "P-01"
    assert toasts(at) == ["Avery Lin (P-01) marked unavailable. Their unanswered alert INC-0008 was passed to the "
                          "next qualified person."]
    assert [b.label for b in at.sidebar.button][-1] == "Mark available"
    click(at, "Mark available")
    assert conn(at).execute("SELECT available FROM people WHERE person_id = 'P-01'").fetchone()[0] == 1
    assert toasts(at) == ["Avery Lin (P-01) marked available."]
    assert [b.label for b in at.sidebar.button][-1] == "Mark unavailable"


def test_mark_unavailable_with_no_alerts_says_so(at):
    at.sidebar.selectbox(key="p_id").set_value("P-06").run()
    click(at, "Mark unavailable")
    assert toasts(at)[0].endswith("They had no unanswered alerts to pass on.")


def test_status_box_shows_minute_ai_and_open_alerts(at):
    first = at.sidebar.markdown[0].value
    assert first == "**Minute 150** · AI on · 0 open alerts"
    assert any("Press **+50**" in c.value for c in at.sidebar.caption)
    click(at, "+50")
    assert at.sidebar.markdown[0].value == "**Minute 200** · AI on · 2 open alerts"
    at.sidebar.toggle(key="kill_llm").set_value(True).run()
    assert "AI off (outage)" in at.sidebar.markdown[0].value


# What each scenario must show on the sensor it's about, run from the demo's start (minute 150).
SCENARIO_EXPECTS = {
    "sudden_jump": ("S-03-PRES", "sustained_run", None),
    "sensor_silent": ("S-02-RF", "dropout", None),
    "misleading_notes": ("S-03-TEMP", "sustained_run", "diagnosed"),
    "trick_note": ("S-02-PRES", "sustained_run", "diagnosed"),
    "no_cause": ("S-04-PRES", "sustained_run", "abstained"),
    "recipe_bad_reading": ("S-05-TEMP", "beyond_spec", None),
}


@pytest.mark.parametrize("key", list(SCENARIO_EXPECTS))
def test_each_fault_scenario_produces_its_incident_and_reports_it(at, key):
    sensor, rule, dx_status = SCENARIO_EXPECTS[key]
    sc = next(s for s in SCENARIOS if s.key == key)
    start = minute(at)
    click(at, sc.label)
    assert not at.exception
    inc = conn(at).execute("SELECT * FROM incidents WHERE sensor_id = ? AND rule_fired = ? ORDER BY opened_at DESC",
                           (sensor, rule)).fetchone()
    assert inc is not None, f"{key}: no {rule} incident on {sensor}"
    report = toasts(at)[0]
    assert report.startswith(f"**{sc.label}**: minute {start} → {minute(at)}.")
    assert inc["incident_id"] in report and f"Look at: {sc.look}" in report
    assert any(inc["incident_id"] in i.value for i in at.info)  # also in the box above the tabs
    if dx_status:
        dx = conn(at).execute("SELECT status, cited_evidence FROM diagnoses WHERE incident_id = ? ORDER BY diagnosis_id",
                              (inc["incident_id"],)).fetchone()
        assert dx["status"] == dx_status
        if key == "misleading_notes":  # cites the real cause, never a decoy
            planted = conn(at).execute("SELECT planted_cause_id, expected_outcome FROM fault_injections "
                                       "WHERE fault_type = 'drift_with_decoys'").fetchone()
            assert dx["cited_evidence"] == f'["{planted["planted_cause_id"]}"]'
            assert planted["planted_cause_id"] not in planted["expected_outcome"].split("decoys:")[1]


def test_someone_calls_out_scenario_reassigns_an_unanswered_alert(at):
    click(at, "Someone calls out mid-alert")
    reassigned = conn(at).execute("SELECT incident_id, person_id FROM notifications WHERE reason = 'reassigned'").fetchall()
    assert len(reassigned) == 1
    unavailable = [r[0] for r in conn(at).execute("SELECT person_id FROM people WHERE available = 0")]
    assert len(unavailable) == 1 and reassigned[0]["person_id"] != unavailable[0]
    assert "marked unavailable while owning " + reassigned[0]["incident_id"] in toasts(at)[0]


def test_bad_data_burst_scenario_rejects_four_kinds(at):
    click(at, "Burst of bad data")
    reasons = [r[0] for r in conn(at).execute("SELECT error_reason FROM dead_letter ORDER BY id")]
    assert len(reasons) == 4
    assert any("valid number" in r for r in reasons) and any("unknown sensor_id" in r for r in reasons)
    assert any("Field required" in r for r in reasons) and any("unknown tool_id" in r for r in reasons)
    assert conn(at).execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0
    assert "4 garbled records sent; 4 rejected" in toasts(at)[0]


def test_scenarios_combine_and_load_demo_starts_over(at):
    fresh = conn(at).execute("SELECT COUNT(*) FROM fault_injections").fetchone()[0]
    click(at, "Sudden jump")
    click(at, "Sensor goes silent")
    assert minute(at) == 150 + 30 + 12
    click(at, "Load demo scenario")
    assert minute(at) == 150
    assert conn(at).execute("SELECT COUNT(*) FROM fault_injections").fetchone()[0] == fresh  # the demo's own faults only
