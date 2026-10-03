from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from db.connection import connect, init_schema, transaction
from sim.clock import Clock
from sim.faults import ANY_CONTROL_CHART_RULE, Dropout, GradualDrift, NoisyHealthy, StepShift
from sim.seed import ONCALL_PERSON_ID, build_world, seed_database
from sim.simulator import Simulator

from .conftest import SEED, TOTAL_TICKS, WARMUP, make_run, readings

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _tick_of(record, clock: Clock) -> int:
    elapsed = datetime.fromisoformat(record["ts"]) - clock.start_time
    return int(elapsed / clock.tick_length)


def _fault(run, cls):
    return next((fid, f) for fid, f in run.sim.faults if isinstance(f, cls))


def _dump(conn, table, order_by):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}")]


# ----- required tests --------------------------------------------------------

def test_same_seed_twice_produces_identical_records_and_db(tmp_path):
    a = make_run(tmp_path / "a.db")
    b = make_run(tmp_path / "b.db")
    assert len(a.records) > 0
    assert a.records == b.records
    for table, key in [
        ("tools", "tool_id"), ("sensors", "sensor_id"), ("people", "person_id"),
        ("tool_qualifications", "tool_id, person_id"), ("tool_state", "tool_id"),
        ("sensor_state", "sensor_id"), ("lots", "lot_id"), ("fault_injections", "fault_id"),
    ]:
        assert _dump(a.conn, table, key) == _dump(b.conn, table, key), table


def test_different_seed_produces_different_records(tmp_path):
    a = make_run(tmp_path / "a.db", seed=SEED, ticks=20)
    b = make_run(tmp_path / "b.db", seed=SEED + 1, ticks=20)
    assert a.records != b.records


def test_dropout_produces_no_readings_during_window(run):
    _, fault = _fault(run, Dropout)
    by_tick = {_tick_of(r, run.clock) for r in readings(run.records, fault.sensor_id)}
    window = set(range(fault.start_tick, fault.end_tick))
    assert by_tick.isdisjoint(window)
    # ...and the sensor reports normally on either side of the window.
    assert fault.start_tick - 1 in by_tick
    assert fault.end_tick in by_tick


def test_gradual_drift_moves_away_from_healthy_mean(run):
    _, fault = _fault(run, GradualDrift)
    profile = run.world.sensor(fault.sensor_id)
    drift = [
        r["payload"]["value"] - profile.healthy_mean
        for r in readings(run.records, fault.sensor_id)
        if _tick_of(r, run.clock) >= fault.start_tick
    ]
    chunk = 20
    chunk_means = [
        fault.direction * sum(drift[i:i + chunk]) / chunk
        for i in range(0, len(drift) - chunk + 1, chunk)
    ]
    assert len(chunk_means) >= 4
    assert all(later > earlier for earlier, later in zip(chunk_means, chunk_means[1:]))
    assert chunk_means[-1] > 3 * profile.healthy_stddev


def test_planted_maintenance_appears_before_drift_starts(run):
    fault_id, fault = _fault(run, GradualDrift)
    planted = run.conn.execute(
        "SELECT planted_cause_id FROM fault_injections WHERE fault_id = ?", (fault_id,)
    ).fetchone()["planted_cause_id"]
    assert planted and planted.startswith("M-")

    maint_idx = next(
        i for i, r in enumerate(run.records)
        if r["event_type"] == "maintenance" and r["payload"]["log_id"] == planted
    )
    maint = run.records[maint_idx]
    assert maint["tool_id"] == run.world.sensor(fault.sensor_id).tool_id
    assert _tick_of(maint, run.clock) == fault.start_tick - 1

    first_drift_idx = next(
        i for i, r in enumerate(run.records)
        if r["event_type"] == "reading"
        and r["payload"]["sensor_id"] == fault.sensor_id
        and _tick_of(r, run.clock) >= fault.start_tick
    )
    assert maint_idx < first_drift_idx


def test_every_injected_fault_has_a_fault_injections_row(run):
    rows = {r["fault_id"]: r for r in run.conn.execute("SELECT * FROM fault_injections")}
    assert set(rows) == {fid for fid, _ in run.sim.faults}
    assert len(rows) == 4
    for fid, fault in run.sim.faults:
        row = rows[fid]
        assert row["fault_type"] == fault.fault_type
        assert row["sensor_id"] == fault.sensor_id
        assert row["tool_id"] == run.world.sensor(fault.sensor_id).tool_id
        assert row["start_ts"] == run.clock.iso_at(fault.start_tick)
        assert row["expected_outcome"] == fault.expected_outcome


def test_live_injection_also_writes_fault_injections_row(run):
    fid = run.sim.inject(StepShift(sensor_id="S-05-RF", start_tick=run.clock.tick + 5))
    row = run.conn.execute("SELECT * FROM fault_injections WHERE fault_id = ?", (fid,)).fetchone()
    assert row["fault_type"] == "step_shift"


def test_no_healthy_reading_falls_outside_spec(run):
    spec = {
        r["sensor_id"]: (r["spec_lower"], r["spec_upper"])
        for r in run.conn.execute("SELECT sensor_id, spec_lower, spec_upper FROM sensors")
    }
    checked = 0
    for r in readings(run.records):
        sid = r["payload"]["sensor_id"]
        if run.sim.is_healthy(sid, _tick_of(r, run.clock)):
            lo, hi = spec[sid]
            assert lo <= r["payload"]["value"] <= hi, r
            checked += 1
    assert checked > 0.9 * len(readings(run.records))


# ----- supporting checks -----------------------------------------------------

def test_records_ordered_readings_then_maintenance_then_tick(run):
    kinds_by_tick: dict[str, list[str]] = {}
    for r in run.records:
        kinds_by_tick.setdefault(r["ts"], []).append(r["event_type"])
    rank = {"reading": 0, "maintenance": 1, "tick": 2}
    for kinds in kinds_by_tick.values():
        assert kinds[-1] == "tick" and kinds.count("tick") == 1
        assert [rank[k] for k in kinds] == sorted(rank[k] for k in kinds)


def test_record_payloads_match_spec_fields(run):
    expected = {
        "reading": {"reading_id", "sensor_id", "value"},
        "maintenance": {"log_id", "description", "technician"},
        "tick": {"tick"},
    }
    for r in run.records:
        assert set(r) == {"event_type", "source", "tool_id", "ts", "payload"}
        assert set(r["payload"]) == expected[r["event_type"]]
    ids = [r["payload"]["reading_id"] for r in readings(run.records)]
    assert all(re.fullmatch(r"RD-\d{6}", i) for i in ids)
    assert len(ids) == len(set(ids))


def test_simulator_writes_only_fault_injections(run):
    for table in ("readings", "maintenance_log", "recipe_changes", "events"):
        assert run.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


def test_warmup_is_healthy_and_faults_cannot_start_in_it(run):
    for r in readings(run.records):
        t = _tick_of(r, run.clock)
        if t <= WARMUP:
            assert run.sim.is_healthy(r["payload"]["sensor_id"], t)
    with pytest.raises(ValueError):
        Simulator(run.conn, run.world, Clock(), faults=[StepShift("S-05-RF", start_tick=WARMUP)],
                  warmup_ticks=WARMUP)


def test_seed_world_shape(run):
    c = run.conn
    assert c.execute("SELECT COUNT(*) FROM tools").fetchone()[0] == 5
    assert c.execute("SELECT COUNT(*) FROM sensors").fetchone()[0] == 15
    assert c.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 7
    assert c.execute("SELECT 1 FROM people WHERE person_id = ?", (ONCALL_PERSON_ID,)).fetchone()
    per_tool = c.execute(
        "SELECT t.tool_id, COUNT(q.person_id) FROM tools t"
        " LEFT JOIN tool_qualifications q ON q.tool_id = t.tool_id GROUP BY t.tool_id"
    ).fetchall()
    assert all(n >= 2 for _, n in per_tool)
    assert c.execute("SELECT COUNT(*) FROM tool_state").fetchone()[0] == 5
    statuses = {r[0] for r in c.execute("SELECT baseline_status FROM sensor_state")}
    assert statuses == {"learning"}
    # Lots cover the whole timeline on every tool.
    end = run.clock.iso_at(TOTAL_TICKS)
    for tool in run.world.tools:
        last = c.execute("SELECT MAX(end_ts) FROM lots WHERE tool_id = ?", (tool.tool_id,)).fetchone()[0]
        assert last >= end
    # Spec limits sit ~5.5 sigma from the mean, using each sensor's own noise.
    for s in run.world.sensors:
        assert s.spec_upper - s.healthy_mean == pytest.approx(5.5 * s.healthy_stddev, abs=1e-3)
        assert s.healthy_mean - s.spec_lower == pytest.approx(5.5 * s.healthy_stddev, abs=1e-3)
    noisy = run.world.sensor("S-04-TEMP")
    normal = run.world.sensor("S-05-TEMP")
    assert noisy.healthy_stddev == 2 * normal.healthy_stddev
    assert (noisy.spec_upper - noisy.spec_lower) > 1.9 * (normal.spec_upper - normal.spec_lower)


def test_connection_wal_foreign_keys_and_rollback(tmp_path):
    conn = connect(tmp_path / "x.db")
    init_schema(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    with pytest.raises(Exception):
        with transaction(conn):
            conn.execute("INSERT INTO tools VALUES ('T-99', 'x', 'etch')")
            conn.execute("INSERT INTO sensors VALUES ('S-X', 'T-NOPE', 't', 'C', 0, 1)")  # FK violation
    assert conn.execute("SELECT COUNT(*) FROM tools").fetchone()[0] == 0


def test_clock():
    clock = Clock()
    t0 = clock.now()
    clock.advance(5)
    assert clock.tick == 5
    assert (clock.now() - t0).total_seconds() == 300
    assert clock.iso_at(0) < clock.iso_at(5) < clock.iso_at(100)


def test_no_wall_clock_calls_in_source():
    pattern = re.compile(r"datetime\.now\(|datetime\.utcnow\(|date\.today\(|time\.time\(")
    for path in PROJECT_ROOT.rglob("*.py"):
        if ".venv" in path.parts or path.parts[-2] == "tests":
            continue
        assert not pattern.search(path.read_text()), path


LONG_RUN_TICKS = 20_000


def test_no_healthy_reading_outside_spec_over_long_run(tmp_path):
    """300k healthy readings (incl. the noisy sensor). At 5.5 sigma the
    expected number of exceedances is ~0.01, so any hit means limits are wrong."""
    conn = connect(tmp_path / "long.db")
    init_schema(conn)
    world = build_world(seed=SEED, total_ticks=LONG_RUN_TICKS)
    clock = Clock()
    seed_database(conn, world, clock)
    sim = Simulator(conn, world, clock, faults=[NoisyHealthy("S-04-TEMP")], warmup_ticks=WARMUP)
    spec = {s.sensor_id: (s.spec_lower, s.spec_upper) for s in world.sensors}
    n = worst = 0
    for _ in range(LONG_RUN_TICKS):
        for r in sim.step():
            if r["event_type"] != "reading":
                continue
            sid, value = r["payload"]["sensor_id"], r["payload"]["value"]
            lo, hi = spec[sid]
            assert lo <= value <= hi, r
            profile = world.sensor(sid)
            worst = max(worst, abs(value - profile.healthy_mean) / profile.healthy_stddev)
            n += 1
    assert n == LONG_RUN_TICKS * len(world.sensors)
    assert worst < 5.5


def test_faults_1_and_2_expect_any_control_chart_rule(run):
    rows = dict(run.conn.execute("SELECT fault_type, expected_outcome FROM fault_injections"))
    assert rows["gradual_drift"] == ANY_CONTROL_CHART_RULE == "incident:beyond_3sigma|sustained_run"
    assert rows["step_shift"] == ANY_CONTROL_CHART_RULE


def test_noisy_healthy_must_match_world(run):
    with pytest.raises(ValueError):
        Simulator(run.conn, run.world, Clock(), faults=[NoisyHealthy("S-05-TEMP")], warmup_ticks=WARMUP)


def test_timestamps_have_one_fixed_iso_format(run):
    fmt = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00")
    assert all(fmt.fullmatch(r["ts"]) for r in run.records)
    for table, col in [("sensor_state", "baseline_window_start"), ("lots", "start_ts"),
                       ("lots", "end_ts"), ("fault_injections", "start_ts")]:
        assert all(fmt.fullmatch(v) for (v,) in run.conn.execute(f"SELECT {col} FROM {table}")), table
    with pytest.raises(ValueError):
        Clock(tick_length=timedelta(milliseconds=500))


def test_config_rejects_window_shorter_than_min_points():
    from pydantic import ValidationError

    from config_loader import Config, load_config
    good = load_config().model_dump()
    with pytest.raises(ValidationError):
        Config.model_validate({**good, "min_baseline_points": good["baseline_window_ticks"] + 1})
    with pytest.raises(ValidationError):
        Config.model_validate({k: v for k, v in good.items() if k != "trending_log_only"})
