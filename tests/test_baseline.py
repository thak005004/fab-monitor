"""Baseline lifecycle (§6): learn, freeze, extend, relearn, capability."""

from __future__ import annotations

import itertools

import pytest

from sim.faults import GradualDrift, RecipeChangeNoFault, RecipeChangeOutOfSpec
from state.baseline import capability_check, control_limits, cpk

from .conftest import WARMUP, make_system


def test_after_warmup_through_bus_every_sensor_is_active_with_limits(system):
    rows = system.conn.execute("SELECT * FROM sensor_state").fetchall()
    assert len(rows) == 15
    for r in rows:
        assert r["baseline_status"] == "active"
        assert r["control_mean"] is not None and r["control_stddev"] > 0
        # Learned from the first window, through the normal pipeline.
        assert r["baseline_activated_at"] == system.clock.iso_at(system.config.baseline_window_ticks)
        profile = system.world.sensor(r["sensor_id"])
        assert abs(r["control_mean"] - profile.healthy_mean) < profile.healthy_stddev


def test_limits_stay_frozen_while_signal_drifts(tmp_path):
    s = make_system(tmp_path / "f.db", faults=[GradualDrift("S-01-TEMP", start_tick=WARMUP + 5)])
    before = {r["sensor_id"]: (r["control_mean"], r["control_stddev"], r["baseline_activated_at"])
              for r in s.conn.execute("SELECT * FROM sensor_state")}
    s.run(150)
    after = {r["sensor_id"]: (r["control_mean"], r["control_stddev"], r["baseline_activated_at"])
             for r in s.conn.execute("SELECT * FROM sensor_state")}
    assert after == before


def test_window_extends_when_too_few_points(tmp_path):
    s = make_system(tmp_path / "w.db", warm_up=False)
    sid = "S-01-TEMP"
    # Drop this sensor's readings for most of the first window: 10 points < min 20.
    for tick in range(1, s.config.baseline_window_ticks + 1):
        records = s.sim.step()
        if tick > 10:
            records = [r for r in records if r.get("payload", {}).get("sensor_id") != sid]
        s.bus.publish_tick(records)
    st = s.sensor_state(sid)
    assert st["baseline_status"] == "learning"
    assert st["baseline_window_start"] == s.clock.iso_at(0)
    assert st["baseline_locked_until"] == s.clock.iso_at(2 * s.config.baseline_window_ticks)
    # Others activated on time.
    assert s.sensor_state("S-01-PRES")["baseline_status"] == "active"
    # The extended window (start unchanged) collects enough points and activates.
    s.run(s.config.baseline_window_ticks)
    st = s.sensor_state(sid)
    assert st["baseline_status"] == "active"
    assert st["baseline_activated_at"] == s.clock.iso_at(2 * s.config.baseline_window_ticks)


def test_stddev_floor_applies_to_a_flat_signal():
    mean, sd = control_limits([5.0] * 25, stddev_floor=0.01)
    assert mean == 5.0 and sd == 0.01
    mean, sd = control_limits([1.0, 3.0], stddev_floor=0.01)
    assert sd == pytest.approx(2 ** 0.5)


def test_capability_check():
    # Centered, spec at +/- 6 sigma: Cpk 2.0.
    assert cpk(10.0, 1.0, 4.0, 16.0) == pytest.approx(2.0)
    assert capability_check(10.0, 1.0, 4.0, 16.0, 1.33) == (pytest.approx(2.0), True)
    # Off-center: the nearer edge decides. 3 sigma from upper -> Cpk 1.0.
    c, ok = capability_check(13.0, 1.0, 4.0, 16.0, 1.33)
    assert c == pytest.approx(1.0) and ok is False
    # Exactly at the threshold passes.
    assert capability_check(10.0, 1.0, 6.01, 13.99, 1.33)[1] is True
    # Mean outside spec gives a negative Cpk.
    assert cpk(17.0, 1.0, 4.0, 16.0) < 0


def _relearn(system, sid, tool, value_for_tick):
    """Recipe change now, then one full relearning window with the given values."""
    system.step_with(extra=[system.recipe_change(tool)])
    change_ts = system.clock.now_iso()
    for _ in range(system.config.baseline_window_ticks):
        system.step_with(overrides={sid: value_for_tick(system.clock.tick + 1)})
    return change_ts


def test_recipe_change_relearns_with_control_rules_suppressed(system):
    sid, tool = "S-01-TEMP", "T-01"
    old = system.sensor_state(sid)
    shifted = old["control_mean"] + 4 * old["control_stddev"]  # new recipe: outside old 3-sigma, inside spec
    spec_upper = system.conn.execute("SELECT spec_upper FROM sensors WHERE sensor_id = ?", (sid,)).fetchone()[0]
    assert shifted < spec_upper

    system.step_with(extra=[system.recipe_change(tool)])
    st = system.sensor_state(sid)
    assert st["baseline_status"] == "relearning"
    assert st["control_mean"] == old["control_mean"]  # old limits kept until new ones are ready
    assert system.conn.execute("SELECT current_recipe_id FROM tool_state WHERE tool_id = ?", (tool,)).fetchone()[0] == "R-NEW"
    assert {r["baseline_status"] for r in system.conn.execute(
        "SELECT ss.baseline_status FROM sensor_state ss JOIN sensors s USING (sensor_id) WHERE s.tool_id = ?", (tool,))} == {"relearning"}

    since = system.clock.now_iso()
    noise = itertools.cycle([0.1, -0.1])
    for _ in range(system.config.baseline_window_ticks):
        system.step_with(overrides={sid: shifted + next(noise) * old["control_stddev"]})
    # No trigger during relearning: nothing opened, and no incident changed rule or severity.
    assert [i for i in system.incidents(sid) if i["opened_at"] > since] == []
    assert not [i for i in system.incidents(sid) if i["status"] == "open"]
    st = system.sensor_state(sid)
    assert st["baseline_status"] == "active"
    assert st["control_mean"] == pytest.approx(shifted, abs=old["control_stddev"])


def test_reading_outside_spec_during_relearning_still_fires(system):
    sid, tool = "S-01-TEMP", "T-01"
    spec_upper = system.conn.execute("SELECT spec_upper FROM sensors WHERE sensor_id = ?", (sid,)).fetchone()[0]
    system.step_with(extra=[system.recipe_change(tool)])
    since = system.clock.now_iso()
    system.step_with(overrides={sid: spec_upper + 1.0})
    assert system.sensor_state(sid)["baseline_status"] == "relearning"
    # Fires as beyond_spec (opening a new incident, or upgrading an open one).
    incs = [i for i in system.incidents(sid) if i["status"] == "open"]
    assert len(incs) == 1
    assert incs[0]["rule_fired"] == "beyond_spec" and incs[0]["severity"] == "high"
    assert incs[0]["updated_at"] == system.clock.now_iso()


def test_recipe_change_near_spec_limit_raises_capability_degraded(system):
    sid, tool = "S-02-PRES", "T-02"
    sensor = system.conn.execute("SELECT * FROM sensors WHERE sensor_id = ?", (sid,)).fetchone()
    sd = system.world.sensor(sid).healthy_stddev
    # Precondition: an open incident of equal severity would absorb the trigger.
    assert not [i for i in system.incidents(sid) if i["status"] == "open"]
    near_edge = sensor["spec_upper"] - 2 * sd  # inside spec, but Cpk ~ 0.67
    wiggle = [0.5, -0.5, 1.0, -1.0]
    change_ts = _relearn(system, sid, tool, lambda t: near_edge + wiggle[t % 4] * sd)

    incs = system.incidents(sid)
    assert len(incs) == 1
    inc = incs[0]
    assert inc["rule_fired"] == "capability_degraded"
    assert inc["onset_ts"] == change_ts
    assert inc["opened_at"] == system.clock.now_iso()
    # Every reading stayed inside spec.
    assert system.conn.execute(
        "SELECT COUNT(*) FROM readings WHERE sensor_id = ? AND value > ?", (sid, sensor["spec_upper"])
    ).fetchone()[0] == 0
    # Its at-risk lots span from the recipe change to now.
    assert inc["lots_at_risk"] != "[]"


# ----- faults 5 and 6 ------------------------------

def _since(system, sid, start_tick):
    return [i for i in system.incidents(sid) if i["opened_at"] >= system.clock.iso_at(start_tick)]


def test_fault_5_recipe_change_relearns_with_no_incident(system):
    sid, tool = "S-05-TEMP", "T-05"
    old = dict(system.sensor_state(sid))
    start = system.clock.tick + 1
    fid = system.sim.inject(RecipeChangeNoFault(sid, start_tick=start))

    records = system.step_with()  # the change tick: readings, then the recipe change, then the tick
    kinds = [r["event_type"] for r in records]
    assert kinds[-2:] == ["recipe_change", "tick"] and set(kinds[:-2]) == {"reading"}
    change_id = records[-2]["payload"]["change_id"]
    row = system.conn.execute("SELECT * FROM fault_injections WHERE fault_id = ?", (fid,)).fetchone()
    assert row["planted_cause_id"] == change_id and row["expected_outcome"] == "relearn;no_incident;capability_ok"
    assert {r["baseline_status"] for r in system.conn.execute(
        "SELECT ss.baseline_status FROM sensor_state ss JOIN sensors s USING (sensor_id) WHERE s.tool_id = ?", (tool,))} == {"relearning"}

    system.run(system.config.baseline_window_ticks)
    st = system.sensor_state(sid)
    assert st["baseline_status"] == "active"
    assert st["baseline_activated_at"] == system.clock.iso_at(start + system.config.baseline_window_ticks)
    sd = system.world.sensor(sid).healthy_stddev
    assert abs(st["control_mean"] - (old["control_mean"] + sd)) < 0.5 * sd  # learned the new operating point
    # No incident on the tool during relearning, and capability is fine.
    for s in [x.sensor_id for x in system.world.sensors if x.tool_id == tool]:
        assert _since(system, s, start) == [], s

    # Without relearning, the old limits would have fired: a run of run_length above the old mean.
    vals = [r[0] for r in system.conn.execute(
        "SELECT value FROM readings WHERE sensor_id = ? AND ts > ? ORDER BY ts", (sid, system.clock.iso_at(start)))]
    run, longest = 0, 0
    for v in vals:
        run = run + 1 if v > old["control_mean"] else 0
        longest = max(longest, run)
    assert longest >= system.config.run_length


def test_fault_6_out_of_spec_reading_during_relearning_raises_beyond_spec(system):
    sid = "S-05-TEMP"
    start = system.clock.tick + 1
    fault = RecipeChangeOutOfSpec(sid, start_tick=start)
    fid = system.sim.inject(fault)
    excursion = start + fault.excursion_offset_ticks
    system.run_until(excursion - 1)
    assert system.sensor_state(sid)["baseline_status"] == "relearning"
    assert _since(system, sid, start) == []
    system.run(1)
    assert system.sensor_state(sid)["baseline_status"] == "relearning"
    [inc] = _since(system, sid, start)
    assert (inc["rule_fired"], inc["severity"]) == ("beyond_spec", "high")
    assert inc["opened_at"] == system.clock.iso_at(excursion)
    assert system.conn.execute("SELECT COUNT(*) FROM notifications WHERE incident_id = ?", (inc["incident_id"],)).fetchone()[0] == 1
    assert system.conn.execute("SELECT expected_outcome FROM fault_injections WHERE fault_id = ?", (fid,)).fetchone()[0] == "incident:beyond_spec"
