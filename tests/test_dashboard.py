"""Dashboard smoke tests, headless via Streamlit's AppTest. FakeClient is forced,
so these never call the real API."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agents.diagnosis.client import FailingClient, FakeClient

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")


@pytest.fixture
def at(monkeypatch):
    monkeypatch.setenv("FAB_MONITOR_LLM", "fake")
    app = AppTest.from_file(APP, default_timeout=120)
    app.run()
    assert not app.exception, app.exception
    return app


def click(app, label: str, where=None):
    button = next(b for b in (where or app).button if b.label == label)
    button.click().run()
    assert not app.exception, app.exception


def metric(app, label: str):
    return next(m.value for m in app.metric if m.label == label)


def system(app):
    return app.session_state.system


def test_loads_demo_with_banner_and_config_version(at):
    assert any("Simulated data" in e.value for e in at.info)
    assert metric(at, "Minute") == "150"  # warm-up done
    assert metric(at, "Settings version") == system(at).config.version
    faults = system(at).conn.execute("SELECT fault_type, sensor_id FROM fault_injections").fetchall()
    assert ("gradual_drift", "S-01-TEMP") in [tuple(r) for r in faults]


def test_advance_buttons(at):
    click(at, "+1", at.sidebar)
    click(at, "+10", at.sidebar)
    click(at, "+50", at.sidebar)
    assert metric(at, "Minute") == "211"


def test_malformed_event_is_quarantined(at):
    click(at, "Send bad data", at.sidebar)
    assert metric(at, "Bad data") == "1"
    assert any("Bad data rejected and set aside (dead letter)" in w.value for w in at.warning)


def test_recipe_change_starts_relearning(at):
    click(at, "Change recipe", at.sidebar)  # defaults to T-05
    states = {r["baseline_status"] for r in system(at).conn.execute(
        "SELECT ss.baseline_status FROM sensor_state ss JOIN sensors s USING (sensor_id) WHERE s.tool_id = 'T-05'")}
    assert states == {"relearning"}


def test_inject_fault_writes_ground_truth(at):
    at.sidebar.selectbox(key="f_type").set_value("step_shift").run()
    click(at, "Start this problem", at.sidebar)
    rows = system(at).conn.execute("SELECT fault_type FROM fault_injections").fetchall()
    assert "step_shift" in [r[0] for r in rows]


def test_kill_llm_toggle_and_availability(at):
    at.sidebar.toggle[0].set_value(True).run()
    assert isinstance(system(at).orchestrator.llm, FailingClient)
    at.sidebar.toggle[0].set_value(False).run()
    assert not isinstance(system(at).orchestrator.llm, FailingClient)

    click(at, "Mark unavailable", at.sidebar)
    first = system(at).conn.execute("SELECT available FROM people ORDER BY person_id LIMIT 1").fetchone()[0]
    assert first == 0


def test_drift_incident_detail_citations_and_acknowledge(at):
    for _ in range(3):
        click(at, "+50", at.sidebar)
    conn = system(at).conn
    inc = conn.execute("SELECT * FROM incidents WHERE sensor_id = 'S-01-TEMP' AND severity IN ('medium', 'high')"
                       " ORDER BY opened_at LIMIT 1").fetchone()
    assert inc is not None
    at.selectbox(key="incident_detail").set_value(inc["incident_id"]).run()
    planted = conn.execute("SELECT planted_cause_id FROM fault_injections WHERE fault_type = 'gradual_drift'").fetchone()[0]
    line = next(m.value for m in at.markdown if f"**{planted}**" in m.value)
    assert "heater" in line.lower() and "✓ checked against the records (verified)" in line

    status = inc["status"]
    click(at, "Acknowledge")
    after = conn.execute("SELECT status FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0]
    assert after == ("acknowledged" if status == "open" else status)
    # Inbox shows the owner's notification.
    owner = conn.execute("SELECT owner_id FROM incidents WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0]
    at.selectbox(key="inbox_person").set_value(owner).run()
    assert any(inc["incident_id"] in e.label for e in at.expander)
    assert json.loads(inc["lots_at_risk"])


# ----- per-session databases and hosted mode -------------------------------------

def test_each_session_gets_its_own_database_and_never_shares_state(at):
    other = AppTest.from_file(APP, default_timeout=120)
    other.run()
    assert not other.exception
    a_dir, b_dir = Path(at.session_state.db_dir), Path(other.session_state.db_dir)
    assert a_dir != b_dir
    assert str(a_dir).startswith(tempfile.gettempdir()) and str(b_dir).startswith(tempfile.gettempdir())
    assert (a_dir / "dashboard.db").exists() and (b_dir / "dashboard.db").exists()

    click(at, "+10", at.sidebar)
    click(at, "Send bad data", at.sidebar)
    other.run()
    assert metric(at, "Minute") == "160" and metric(at, "Bad data") == "1"
    assert metric(other, "Minute") == "150" and metric(other, "Bad data") == "0"
    assert other.session_state.system.conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0] == 0


def test_reloading_the_demo_replaces_this_sessions_database(at):
    old_dir = Path(at.session_state.db_dir)
    click(at, "+10", at.sidebar)
    click(at, "Load demo scenario", at.sidebar)
    assert metric(at, "Minute") == "150"
    assert not old_dir.exists()
    assert Path(at.session_state.db_dir).exists()


def test_hosted_mode_from_env_uses_the_fake_and_never_reads_an_api_key(monkeypatch):
    import os

    import agents.diagnosis.client as client_module

    monkeypatch.setenv("FAB_MONITOR_HOSTED", "1")
    monkeypatch.setenv("FAB_MONITOR_LLM", "anthropic")  # would pick Claude locally
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-not-a-real-key")
    looked_up = []
    real_get = os.environ.get
    monkeypatch.setattr(os.environ, "get", lambda key, default=None: looked_up.append(key) or real_get(key, default))

    def no_real_client(*args, **kwargs):
        raise AssertionError("AnthropicClient must not be built in hosted mode")
    monkeypatch.setattr(client_module, "AnthropicClient", no_real_client)

    app = AppTest.from_file(APP, default_timeout=120)
    app.run()
    assert not app.exception, app.exception
    assert any("Simulated data, scripted model responses" in e.value and "live model runs in the local version" in e.value
               for e in app.info)
    assert isinstance(app.session_state.llm, FakeClient)
    assert "ANTHROPIC_API_KEY" not in looked_up
    assert any("hosted demo" in c.value for c in app.sidebar.caption)


def test_hosted_mode_from_streamlit_secret(monkeypatch):
    monkeypatch.delenv("FAB_MONITOR_HOSTED", raising=False)
    monkeypatch.setenv("FAB_MONITOR_LLM", "fake")
    app = AppTest.from_file(APP, default_timeout=120)
    app.secrets["FAB_MONITOR_HOSTED"] = "true"
    app.run()
    assert not app.exception, app.exception
    assert any("scripted model responses" in e.value for e in app.info)


def test_local_banner_is_unchanged(at):
    assert any(e.value.startswith("**Simulated data.** Every tool, sensor, person, lot and reading") for e in at.info)
    assert not any("scripted model responses" in e.value for e in at.info)


def tabs_position(app) -> int:
    """Where the tabs block sits among the page's top-level elements."""
    return next(i for i, node in app.main.children.items()
                if any(type(c).__name__ == "Tab" for c in getattr(node, "children", {}).values()))


def test_action_buttons_keep_the_incidents_tab_and_the_selected_incident(at):
    """After an action button the page shows a flash message. The tabs must stay in
    the same place in the page, or the browser rebuilds them and jumps back to the
    first tab, hiding the incident the person was working on."""
    click(at, "+50", at.sidebar)
    assert not at.success  # no flash message on this rerun
    position = tabs_position(at)
    at.selectbox(key="incident_detail").set_value("INC-0008").run()

    click(at, "Acknowledge")
    assert any("INC-0008: Waiting for response → Someone is on it (open → acknowledged)" in m.value
               for m in at.success)  # flash shown...
    assert tabs_position(at) == position  # ...without moving the tabs
    assert at.selectbox(key="incident_detail").value == "INC-0008"

    click(at, "Confirm hold")
    assert any("acknowledged → hold_confirmed" in m.value for m in at.success)
    assert tabs_position(at) == position
    assert at.selectbox(key="incident_detail").value == "INC-0008"


def test_hosted_demo_shows_the_scripted_self_correction(monkeypatch):
    monkeypatch.setenv("FAB_MONITOR_HOSTED", "1")
    app = AppTest.from_file(APP, default_timeout=120)
    app.run()
    next(b for b in app.sidebar.button if b.label == "+50").click().run()
    app.selectbox(key="incident_detail").set_value("INC-0008").run()
    assert not app.exception, app.exception
    note = next(i.value for i in app.info if "AI revised its answer after the checker rejected it." in i.value)
    assert "Scripted demo" in note
    text = " ".join(m.value for m in app.markdown)
    assert "First answer (rejected):** cited M-9999" in text and "not in the evidence that was sent" in text
    assert "Revised answer:** AI suggested likely causes (diagnosed), citing M-0001" in text
