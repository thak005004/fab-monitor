"""Plain-language layer of the dashboard: labels keep the technical term, and the
activity feed tells the demo's story at the right minutes (read-only)."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from agents.diagnosis.client import FakeClient
from config_loader import load_config
from dashboard import explain
from orchestrator import build_system
from sim.faults import demo_faults
from sim.simulator import DEFAULT_WARMUP_TICKS

APP = str(Path(__file__).resolve().parents[1] / "dashboard" / "app.py")


def test_labels_keep_the_technical_term():
    config = load_config()
    assert explain.rule("sustained_run", config) == \
        "Sustained shift: 9 readings in a row on one side of normal (sustained_run)"
    assert explain.rule("beyond_spec", config) == "Outside allowed range (beyond_spec)"
    assert explain.status("expired") == "Closed automatically (no further signs) (expired)"
    assert explain.SEVERITY == {"low": "Watch only (no one paged)", "medium": "Alert", "high": "Urgent"}
    assert set(explain.DIAGNOSIS) == {"diagnosed", "abstained", "rejected", "unavailable"}


@pytest.fixture(scope="module")
def feed():
    """The demo scenario, driven the way DEMO_SCRIPT.md does it, then the feed at minute 404."""
    s = build_system(Path(tempfile.mkdtemp()) / "f.db", seed=42, llm=FakeClient("valid"),
                     faults_after_warmup=demo_faults(DEFAULT_WARMUP_TICKS))
    s.advance(50)
    s.alert_action("INC-0008", "acknowledge", "P-01")
    s.recipe_change("T-05", "R-05-B")
    s.send_malformed()
    s.advance(54)
    s.alert_action("INC-0008", "confirm_hold", "P-01")
    s.advance(150)
    entries = explain.activity_feed(s.conn, s.clock, s.config, limit=500)
    s.conn.close()
    return entries


def has(feed, minute, *parts):
    return any(m == minute and all(p in text for p in parts) for m, text in feed)


def test_feed_tells_the_demo_story(feed):
    assert has(feed, 198, "Etch Tool 1 (T-01) temperature has been above normal for 9 readings in a row",
               "Alert sent to Avery Lin (P-01)", "(INC-0008)")
    assert has(feed, 200, "Avery Lin (P-01) acknowledged INC-0008")
    assert has(feed, 200, "Etch Tool 5 (T-05) switched to recipe R-05-B", "learning the new normal (relearning)")
    assert has(feed, 200, "Rejected bad data", "DL-000001")
    assert has(feed, 213, "INC-0008", "Reminder sent to Avery Lin (P-01)")
    assert has(feed, 254, "INC-0008 got worse (Urgent)", "went above its allowed range")
    assert has(feed, 254, "Avery Lin (P-01) put 2 product batches (lots) on hold for INC-0008: LOT-0030, LOT-0037")
    assert has(feed, 320, "Etch Tool 5 (T-05) finished learning the new normal")
    assert has(feed, 337, "Etch Tool 5 (T-05) pressure had a single unusual reading", "watch list", "(INC-0012)")
    assert has(feed, 357, "INC-0012", "closed automatically")


def test_feed_is_newest_first_and_skips_routine_readings(feed):
    minutes = [m for m, _ in feed]
    assert minutes == sorted(minutes, reverse=True)
    assert not any("RD-" in text for _, text in feed)


def test_page_opens_with_the_explanation_and_plain_labels(monkeypatch):
    monkeypatch.setenv("FAB_MONITOR_LLM", "fake")
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    intro = next(e for e in at.expander if e.label == "What you're looking at")
    assert intro.proto.expanded and any("All data here is simulated." in m.value for m in intro.markdown)
    assert all(m.help for m in at.metric)  # every metric has hover help
    assert all(b.help for b in at.sidebar.button)  # and every sidebar control
    next(b for b in at.sidebar.button if b.label == "+50").click().run()
    at.selectbox(key="incident_detail").set_value("INC-0008").run()
    text = " ".join(m.value for m in at.markdown)
    assert "Sustained shift: 9 readings in a row on one side of normal (sustained_run)" in text
    assert "Waiting for response (open)" in text and "Alert" in text
    assert "Etch Tool 1 (T-01) temperature (S-01-TEMP)" in text
    assert any("Avery Lin (P-01)" in c.value for c in at.caption)
    feed_lines = [m.value for m in at.markdown if m.value.startswith("**Minute ")]
    assert feed_lines[0].startswith("**Minute 200:**")  # newest first
    assert any(line.startswith("**Minute 198:** Etch Tool 1 (T-01) temperature") and "(INC-0008)" in line
               for line in feed_lines)


def test_status_look_is_one_color_and_icon_per_state():
    assert explain.state_of("low", "open") == "watch" and explain.state_of("medium", "acknowledged") == "alert"
    assert explain.state_of("high", "hold_confirmed") == "urgent" and explain.state_of("high", "expired") == "closed"
    assert explain.state_of(None) == "healthy"
    assert explain.badge("urgent") == "🔴 :red[**Urgent**]" and explain.icon_label("closed") == "⚪ Closed"
    assert explain.LOOK["watch"][:3] == explain.LOOK["alert"][:3]  # amber for both


def test_incident_summary_tells_the_story_in_one_line():
    s = build_system(Path(tempfile.mkdtemp()) / "s.db", seed=42, llm=FakeClient("valid"),
                     faults_after_warmup=demo_faults(DEFAULT_WARMUP_TICKS))

    def summary(iid):
        return explain.incident_summary(s.conn, s.conn.execute("SELECT * FROM incidents WHERE incident_id = ?",
                                                               (iid,)).fetchone(), s.clock, s.config, explain.Names(s.conn))

    s.advance(50)
    assert summary("INC-0008") == ("Etch Tool 1's temperature has been running high for 10 minutes. "
                                   "Waiting for Avery Lin to respond. 1 product batch may be affected.")
    s.alert_action("INC-0008", "acknowledge", "P-01")
    s.advance(54)
    assert summary("INC-0008") == ("Etch Tool 1's temperature went above its allowed range. Avery Lin is on it. "
                                   "2 product batches may be affected; holding them is recommended.")
    s.alert_action("INC-0008", "confirm_hold", "P-01")
    assert "Avery Lin put the affected product on hold. 2 product batches may be affected (2 on hold)." in summary("INC-0008")
    s.conn.close()


def test_technical_details_are_tucked_away(monkeypatch):
    monkeypatch.setenv("FAB_MONITOR_LLM", "fake")
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    next(b for b in at.sidebar.button if b.label == "+50").click().run()
    at.selectbox(key="incident_detail").set_value("INC-0008").run()
    tech = [e for e in at.expander if e.label == "Technical details"]
    assert tech and not any(e.proto.expanded for e in tech)  # collapsed
    assert any(m.label == "Settings version" for e in tech for m in e.metric)
    inner = " ".join(m.value for e in tech for m in e.markdown)
    assert "DX-00004" in inner and "**M-0001**" in inner and "escalation level 0" in inner
    assert any("##### Etch Tool 1's temperature has been running high" in m.value for m in at.markdown)
    assert not at.exception
