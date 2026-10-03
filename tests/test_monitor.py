"""Monitor agent (§7): check_reading rules and the sweep."""

from __future__ import annotations

from collections import Counter

from agents.monitor import check_reading, fired_rules, most_severe
from config_loader import load_config
from sim.faults import Dropout

from .conftest import WARMUP, make_system

CONFIG = load_config()
RUN = CONFIG.run_length
SENSOR = {"sensor_id": "S-X", "tool_id": "T-X", "spec_lower": 0.0, "spec_upper": 100.0}
ACTIVE = {"baseline_status": "active", "control_mean": 50.0, "control_stddev": 1.0}
LEARNING = {"baseline_status": "learning", "control_mean": None, "control_stddev": None}
ABOVE = [50.5, 50.4, 50.6, 50.3, 50.7, 50.2, 50.6, 50.5, 50.4, 50.6, 50.3]  # above mean, never 6 monotone


def rd(values, start=0):
    return [{"reading_id": f"RD-{start + i:06d}", "value": v, "ts": f"t{start + i:04d}"} for i, v in enumerate(values)]


def rules(values, state=ACTIVE, config=CONFIG):
    return {t.rule_fired for t in fired_rules(SENSOR, rd(values), state, config)}


def test_no_rule_on_in_control_data():
    assert check_reading(SENSOR, rd([50.2, 49.7, 50.1, 49.9, 50.3, 49.8, 50.0, 50.4, 49.6]), ACTIVE, CONFIG) is None


def test_beyond_3sigma_is_low_severity():
    r = check_reading(SENSOR, rd([50.0, 53.5]), ACTIVE, CONFIG)
    assert r.rule_fired == "beyond_3sigma" and r.onset_ts == "t0001" and r.triggering_reading_ids == ("RD-000001",)
    assert CONFIG.severity_of("beyond_3sigma") == "low"


def test_beyond_spec_fires_even_while_learning_and_control_rules_do_not():
    assert check_reading(SENSOR, rd([101.0]), LEARNING, CONFIG).rule_fired == "beyond_spec"
    relearning = {**ACTIVE, "baseline_status": "relearning"}
    assert check_reading(SENSOR, rd([60.0] * RUN), relearning, CONFIG) is None  # would be 3-sigma + run if active
    assert check_reading(SENSOR, rd([-1.0]), relearning, CONFIG).rule_fired == "beyond_spec"


def test_sustained_run_needs_run_length_points_and_onset_is_first_of_run():
    assert RUN == 9
    assert "sustained_run" not in rules([49.0] + ABOVE[:RUN - 1])
    r = check_reading(SENSOR, rd([49.0] + ABOVE[:RUN]), ACTIVE, CONFIG)
    assert r.rule_fired == "sustained_run"
    assert r.onset_ts == "t0001" and len(r.triggering_reading_ids) == RUN


def test_trending_is_evaluated_but_log_only():
    up = [49.0, 49.2, 49.4, 50.1, 50.3, 50.5]  # crosses the mean, so no sustained run
    assert rules(up) == {"trending"}
    assert check_reading(SENSOR, rd(up), ACTIVE, CONFIG) is None  # never opens an incident
    flag_off = CONFIG.model_copy(update={"trending_log_only": False})
    assert check_reading(SENSOR, rd(up), ACTIVE, flag_off).rule_fired == "trending"
    flat_step = [49.0, 49.2, 49.2, 50.1, 50.3, 50.5]
    assert rules(flat_step) == set()


def test_most_severe_rule_wins():
    vals = ABOVE[:RUN - 1] + [105.0]  # run + 3-sigma + spec
    assert {"beyond_spec", "beyond_3sigma", "sustained_run"} <= rules(vals)
    assert check_reading(SENSOR, rd(vals), ACTIVE, CONFIG).rule_fired == "beyond_spec"
    vals[-1] = 54.0  # 3-sigma (low) and run (medium): medium wins
    assert check_reading(SENSOR, rd(vals), ACTIVE, CONFIG).rule_fired == "sustained_run"


def test_equal_severity_tie_goes_to_earliest_onset():
    both_medium = CONFIG.model_copy(update={"severity_map": {**CONFIG.severity_map, "beyond_3sigma": "medium"}})
    vals = ABOVE[:RUN - 1] + [54.0]
    r = most_severe(fired_rules(SENSOR, rd(vals), ACTIVE, both_medium), both_medium)
    assert r.rule_fired == "sustained_run" and r.onset_ts == "t0000"


def test_rules_need_enough_points_since_activation():
    assert check_reading(SENSOR, rd([51.0] * (RUN - 1)), ACTIVE, CONFIG) is None


def test_silent_sensor_raises_dropout_on_tick(tmp_path):
    start, sid = WARMUP + 20, "S-03-RF"
    s = make_system(tmp_path / "d.db", faults=[Dropout(sid, start_tick=start, duration_ticks=15)])
    s.run_until(start + CONFIG.dropout_threshold_ticks - 1)
    assert [i for i in s.incidents(sid) if i["rule_fired"] == "dropout"] == []
    s.run(1)  # silent for more than dropout_threshold_ticks now
    inc = [i for i in s.incidents(sid) if i["rule_fired"] == "dropout"]
    assert len(inc) == 1
    assert inc[0]["onset_ts"] == s.clock.iso_at(start)
    assert inc[0]["opened_at"] == s.clock.iso_at(start + CONFIG.dropout_threshold_ticks)
    assert inc[0]["lots_at_risk"] == "[]"  # dropouts don't mark lots (§9)
    s.run(20)
    assert len([i for i in s.incidents(sid) if i["rule_fired"] == "dropout"]) == 1  # one incident, not one per tick


def test_dropout_is_not_masked_by_an_open_process_incident(tmp_path):
    """The case found on Friday: a sensor with an open beyond_3sigma incident went
    silent, and the equal-severity dropout merged into it and never showed."""
    sid = "S-03-RF"
    s = make_system(tmp_path / "m.db")
    st = s.sensor_state(sid)
    s.step_with(overrides={sid: st["control_mean"] + 4 * st["control_stddev"]})  # inside spec
    process = [i for i in s.incidents(sid) if i["status"] == "open"]
    assert len(process) == 1 and process[0]["rule_fired"] == "beyond_3sigma"

    start = s.clock.tick + 1
    s.sim.inject(Dropout(sid, start_tick=start, duration_ticks=15))
    s.run(CONFIG.dropout_threshold_ticks + 1)

    open_now = {i["rule_fired"]: i for i in s.incidents(sid) if i["status"] == "open"}
    assert set(open_now) == {"beyond_3sigma", "dropout"}
    assert open_now["beyond_3sigma"]["incident_id"] == process[0]["incident_id"]
    assert open_now["dropout"]["onset_ts"] == s.clock.iso_at(start)
    assert open_now["dropout"]["severity"] == "medium"


def test_trending_firings_are_recorded_without_incidents(tmp_path):
    s = make_system(tmp_path / "t.db")
    s.run(400)
    n = s.conn.execute("SELECT COUNT(*) FROM logged_firings WHERE rule_fired = 'trending'").fetchone()[0]
    assert n > 0
    row = s.conn.execute("SELECT * FROM logged_firings LIMIT 1").fetchone()
    assert row["firing_id"] == "LF-000001" and row["triggering_reading_ids"].startswith('["RD-')
    assert s.conn.execute("SELECT COUNT(*) FROM incidents WHERE rule_fired = 'trending'").fetchone()[0] == 0


def test_noisy_but_healthy_sensor_stays_near_expected_false_alarm_rate(tmp_path):
    ticks = 2000
    s = make_system(tmp_path / "n.db", total_ticks=ticks)
    s.run_until(ticks)
    per_sensor = Counter(r["sensor_id"] for r in s.incidents())
    noisy = next(x.sensor_id for x in s.world.sensors if x.noise_multiplier > 1)
    others = [per_sensor[x.sensor_id] for x in s.world.sensors if x.sensor_id != noisy]
    mean_other = sum(others) / len(others)
    # Its high noise was learned into its baseline, so it behaves like any other healthy sensor.
    assert per_sensor[noisy] <= max(others)
    assert per_sensor[noisy] <= 2 * mean_other
    # And nothing healthy goes beyond spec.
    assert not any(r["rule_fired"] == "beyond_spec" for r in s.incidents())
