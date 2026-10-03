"""Faults 1-3 end to end through sim -> bus -> orchestrator (Friday checkpoint)."""

from __future__ import annotations

import json

import pytest

import incidents
from sim.faults import Dropout, GradualDrift, StepShift

from .conftest import WARMUP, make_system

CONTROL_RULES = {"beyond_3sigma", "sustained_run"}  # trending is log-only
DRIFT = GradualDrift("S-01-TEMP", start_tick=WARMUP + 40)
STEP = StepShift("S-02-PRES", start_tick=WARMUP + 90)
DROP = Dropout("S-03-RF", start_tick=WARMUP + 140, duration_ticks=20)
END_TICK = WARMUP + 270  # long enough for the drift to leave spec


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    """Run the default scenario, snapshotting each tick's incidents on the faulted sensors."""
    s = make_system(tmp_path_factory.mktemp("p") / "p.db", faults=[DRIFT, STEP, DROP])
    history = []  # (tick, incident rows for faulted sensors)
    while s.clock.tick < END_TICK:
        s.run(1)
        rows = s.conn.execute(
            "SELECT * FROM incidents WHERE sensor_id IN (?, ?, ?) AND opened_at >= ? ORDER BY incident_id",
            (DRIFT.sensor_id, STEP.sensor_id, DROP.sensor_id, s.clock.iso_at(WARMUP + 1)),
        ).fetchall()
        history.append((s.clock.tick, [dict(r) for r in rows]))
    yield s, history
    s.conn.close()


def _first_out_of_spec_tick(s, sensor_id, after_tick):
    row = s.conn.execute(
        "SELECT MIN(r.ts) FROM readings r JOIN sensors x USING (sensor_id)"
        " WHERE r.sensor_id = ? AND r.ts >= ? AND (r.value > x.spec_upper OR r.value < x.spec_lower)",
        (sensor_id, s.clock.iso_at(after_tick)),
    ).fetchone()[0]
    return None if row is None else s.clock.tick_of(row)


def _fault_incidents(history, sensor_id, start_tick, s):
    """Incidents on the sensor opened at or after the fault started."""
    final = history[-1][1]
    return [i for i in final if i["sensor_id"] == sensor_id and s.clock.tick_of(i["opened_at"]) >= start_tick]


def test_drift_opens_on_control_rule_then_upgrades_to_beyond_spec_as_one_incident(scenario):
    s, history = scenario
    incs = _fault_incidents(history, DRIFT.sensor_id, DRIFT.start_tick, s)
    assert len(incs) == 1, incs
    inc = incs[0]
    opened = s.clock.tick_of(inc["opened_at"])

    first_snapshot = next(rows for tick, rows in history if tick == opened)
    opening = next(r for r in first_snapshot if r["incident_id"] == inc["incident_id"])
    assert opening["rule_fired"] in CONTROL_RULES

    assert inc["rule_fired"] == "beyond_spec" and inc["severity"] == "high" and inc["status"] == "open"
    out_of_spec = _first_out_of_spec_tick(s, DRIFT.sensor_id, DRIFT.start_tick)
    assert out_of_spec is not None
    lead_time = out_of_spec - opened
    assert lead_time > 0, f"SPC should warn before the drift leaves spec (lead time {lead_time})"
    # ...and it reaches a person (medium or higher) before then too.
    medium_tick = next(tick for tick, rows in history
                       if any(r["incident_id"] == inc["incident_id"] and r["severity"] != "low" for r in rows))
    assert medium_tick < out_of_spec

    # Lots between onset and detection are at risk, including ones from the detection lag.
    lots = json.loads(inc["lots_at_risk"])
    onset = s.clock.tick_of(inc["onset_ts"])
    assert DRIFT.start_tick <= onset <= opened
    expected_at_open = {l.lot_id for l in s.world.lots
                        if l.tool_id == "T-01" and l.start_tick <= opened and l.end_tick >= onset}
    assert expected_at_open <= set(lots)


def test_step_shift_detected_before_any_out_of_spec_reading(scenario):
    s, history = scenario
    incs = _fault_incidents(history, STEP.sensor_id, STEP.start_tick, s)
    assert len(incs) == 1
    opened = s.clock.tick_of(incs[0]["opened_at"])
    first_snapshot = next(rows for tick, rows in history if tick == opened)
    assert next(r for r in first_snapshot if r["incident_id"] == incs[0]["incident_id"])["rule_fired"] in CONTROL_RULES
    out_of_spec = _first_out_of_spec_tick(s, STEP.sensor_id, STEP.start_tick)
    assert out_of_spec is None or opened < out_of_spec
    # And a person is notified before any out-of-spec reading.
    first_notice = next(t for t, _ in s.notifications(STEP.sensor_id) if t >= STEP.start_tick)
    assert out_of_spec is None or first_notice < out_of_spec


def test_dropout_opens_one_incident_without_lots(scenario):
    s, history = scenario
    incs = [i for i in _fault_incidents(history, DROP.sensor_id, DROP.start_tick, s) if i["rule_fired"] == "dropout"]
    assert len(incs) == 1
    assert s.clock.tick_of(incs[0]["onset_ts"]) == DROP.start_tick
    assert incs[0]["lots_at_risk"] == "[]"


def test_same_seed_gives_identical_incidents(tmp_path):
    def run(path):
        s = make_system(path, faults=[DRIFT, STEP, DROP])
        s.run_until(220)
        rows = [tuple(r) for r in s.conn.execute("SELECT * FROM incidents ORDER BY incident_id")]
        lots = [tuple(r) for r in s.conn.execute("SELECT * FROM lots ORDER BY lot_id")]
        s.conn.close()
        return rows, lots
    assert run(tmp_path / "a.db") == run(tmp_path / "b.db")


def test_low_incident_is_watch_list_until_it_upgrades(tmp_path):
    """beyond_3sigma alone opens a low (watch-list) incident: no lots marked.
    When it upgrades to medium, lots are marked (and, from Saturday, it is diagnosed and notified)."""
    s = make_system(tmp_path / "w.db")
    sid = "S-05-PRES"
    st = s.sensor_state(sid)
    mean, sd = st["control_mean"], st["control_stddev"]
    s.step_with(overrides={sid: mean + 4 * sd})
    low = [i for i in s.incidents(sid) if i["status"] == "open"]
    assert len(low) == 1 and low[0]["severity"] == "low" and low[0]["lots_at_risk"] == "[]"
    assert s.conn.execute("SELECT COUNT(*) FROM lots WHERE status = 'at_risk' AND tool_id = 'T-05'").fetchone()[0] == 0

    for _ in range(s.config.run_length):
        s.step_with(overrides={sid: mean + 0.5 * sd})
    up = dict(s.conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (low[0]["incident_id"],)).fetchone())
    assert up["severity"] == "medium" and up["rule_fired"] == "sustained_run"
    assert up["onset_ts"] == low[0]["onset_ts"]
    assert json.loads(up["lots_at_risk"])


def _force_medium_false_alarm(s, sid):
    """Push a healthy sensor into a medium sustained_run incident, then return it."""
    st = s.sensor_state(sid)
    for _ in range(s.config.run_length):
        s.step_with(overrides={sid: st["control_mean"] + 0.5 * st["control_stddev"]})
    open_now = [i for i in s.incidents(sid) if i["status"] == "open"]
    assert len(open_now) == 1 and open_now[0]["severity"] == "medium"
    return dict(open_now[0])


def _quiet(s, sid, n):
    """n ticks of in-control values that trip no rule (alternating just around the mean)."""
    st = s.sensor_state(sid)
    for k in range(n):
        s.step_with(overrides={sid: st["control_mean"] + (0.1 if k % 2 else -0.1) * st["control_stddev"]})


def test_real_fault_on_an_open_medium_false_alarm_is_notified_as_reactivated(tmp_path):
    s = make_system(tmp_path / "r.db")
    sid = "S-05-PRES"
    false_alarm = _force_medium_false_alarm(s, sid)
    _quiet(s, sid, s.config.reactivation_quiet_ticks)
    assert dict(s.conn.execute("SELECT status FROM incidents WHERE incident_id = ?",
                               (false_alarm["incident_id"],)).fetchone())["status"] == "open"
    notified_before = len(s.notifications(sid))

    start = s.clock.tick + 1
    # 3 sigma: triggers within a few ticks, still inside spec (5.5 sigma).
    s.sim.inject(StepShift(sid, start_tick=start, shift_sigma=3.0))
    s.run(5)

    after = [(t, n) for t, n in s.notifications(sid)[notified_before:] if n["reason"] != "escalated"]
    assert after, "the real fault was never notified"
    t, first = after[0]
    assert first["reason"] == "reactivated"
    assert first["incident_id"] == false_alarm["incident_id"] and first["severity"] == "medium"
    assert start <= t < start + 5


def test_quiet_spell_shorter_than_reactivation_window_does_not_renotify(tmp_path):
    s = make_system(tmp_path / "q.db")
    sid = "S-05-PRES"
    _force_medium_false_alarm(s, sid)
    _quiet(s, sid, s.config.reactivation_quiet_ticks - 2)
    n = len(s.notifications(sid))
    st = s.sensor_state(sid)
    s.step_with(overrides={sid: st["control_mean"] + 4 * st["control_stddev"]})  # low trigger, not quiet long enough
    assert len(s.notifications(sid)) == n


def test_real_fault_right_after_false_alarm_dismissal_is_notified_when_cooldown_ends(tmp_path):
    s = make_system(tmp_path / "c.db")
    sid = "S-05-PRES"
    false_alarm = _force_medium_false_alarm(s, sid)
    assert incidents.transition(s.conn, s.clock, false_alarm["incident_id"], "dismiss", "P-01", "false_alarm").accepted
    dismissed_at = s.clock.tick
    cooldown_ends = dismissed_at + s.config.dismissal_cooldown_ticks

    start = s.clock.tick + 1
    # The real fault 2: a 2-sigma step that stays inside spec, so nothing it
    # produces is high severity (high would bypass the cooldown).
    s.sim.inject(StepShift(sid, start_tick=start))
    s.run_until(cooldown_ends + 30)

    notices = [(t, o) for t, o in s.notifications(sid) if t >= start]
    assert notices, "the real fault was never notified"
    t, first = notices[0]
    assert t >= cooldown_ends, "notified during the cooldown"
    assert first["severity"] == "medium" and first["incident_id"] != false_alarm["incident_id"]
    assert first["reason"] in ("new", "upgraded")
    # Triggers during the cooldown were suppressed: no incident opened in between.
    opened_during = [i for i in s.incidents(sid)
                     if s.clock.iso_at(dismissed_at) < i["opened_at"] < s.clock.iso_at(cooldown_ends)]
    assert opened_during == []
