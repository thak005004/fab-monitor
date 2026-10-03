"""Bus, adapters, dead letter, and merge-only world state (§4, §5)."""

from __future__ import annotations

import json

from db import repo


def _tick_record(system, tick):
    return {"event_type": "tick", "source": "clock", "tool_id": None, "ts": system.clock.iso_at(tick), "payload": {"tick": tick}}


def test_malformed_record_goes_to_dead_letter_and_next_record_still_processes(system):
    records = system.sim.step()
    good = next(r for r in records if r["event_type"] == "reading")
    bad = json.loads(json.dumps(good))
    bad["payload"]["value"] = "not a number"
    bad["payload"]["reading_id"] = "RD-999999"
    records.insert(0, bad)  # malformed record comes first; everything after it must still land

    accepted = system.bus.publish_tick(records)

    dl = system.conn.execute("SELECT * FROM dead_letter").fetchall()
    assert len(dl) == 1
    assert dl[0]["source"] == "sensor" and "value" in dl[0]["error_reason"]
    assert json.loads(dl[0]["raw_payload"])["payload"]["reading_id"] == "RD-999999"
    assert len(accepted) == len(records) - 1
    stored = {r[0] for r in system.conn.execute("SELECT reading_id FROM readings")}
    assert good["payload"]["reading_id"] in stored and "RD-999999" not in stored


def test_bus_never_raises_on_assorted_bad_input(system):
    tick = system.clock.tick + 1
    ts = system.clock.iso_at(tick)
    reading = {"event_type": "reading", "source": "sensor", "tool_id": "T-01", "ts": ts,
               "payload": {"reading_id": "RD-900001", "sensor_id": "S-01-TEMP", "value": 60.0}}
    bad = [
        "not even a dict",
        {"source": "nowhere"},
        {**reading, "ts": "2026-01-05 06:00"},                               # wrong timestamp format
        {**reading, "tool_id": "T-02"},                                       # sensor belongs to T-01
        {**reading, "payload": {**reading["payload"], "sensor_id": "S-99"}},  # unknown sensor
        {**reading, "payload": {**reading["payload"], "extra": 1}},           # unknown payload field
        {**reading, "payload": {**reading["payload"], "value": float("nan")}},
        {**reading, "event_type": "tick"},                                    # type/adapter mismatch
        {**reading, "surprise": True},                                        # unknown envelope key
        {"event_type": "maintenance", "source": "maintenance", "tool_id": "T-01", "ts": ts,
         "payload": {"log_id": "M-0099", "description": "x", "current_recipe_id": "R-X"}},  # recipe via maintenance
        {"event_type": "tick", "source": "clock", "tool_id": None, "ts": ts, "payload": {"tick": True}},
    ]
    system.clock.advance()
    system.bus.publish_tick(bad + [_tick_record(system, tick)])
    assert repo.count_rows(system.conn, "dead_letter") == len(bad)
    assert system.conn.execute("SELECT COUNT(*) FROM events WHERE ts = ?", (ts,)).fetchone()[0] == 1  # the tick


def test_duplicate_reading_id_is_quarantined_without_losing_the_tick(system):
    records = system.sim.step()
    first = next(r for r in records if r["event_type"] == "reading")
    dup = json.loads(json.dumps(first))
    records.insert(1, dup)
    system.bus.publish_tick(records)
    dl = system.conn.execute("SELECT error_reason FROM dead_letter").fetchall()
    assert len(dl) == 1 and "integrity" in dl[0][0]
    n_readings = sum(1 for r in records if r["event_type"] == "reading") - 1
    ts = records[-1]["ts"]
    assert system.conn.execute("SELECT COUNT(*) FROM readings WHERE ts = ?", (ts,)).fetchone()[0] == n_readings


def test_bus_assigns_sequential_event_ids(system):
    ids = [r[0] for r in system.conn.execute("SELECT event_id FROM events ORDER BY rowid")]
    assert ids[:3] == ["EV-0000001", "EV-0000002", "EV-0000003"]
    assert len(ids) == len(set(ids))
    # Reading payloads keep their RD- ID; event IDs are separate.
    row = system.conn.execute("SELECT payload FROM events WHERE event_type = 'reading' LIMIT 1").fetchone()
    assert json.loads(row[0])["reading_id"].startswith("RD-")


def test_partial_maintenance_merges_without_wiping_other_fields(system):
    before_tool = dict(system.conn.execute("SELECT * FROM tool_state WHERE tool_id = 'T-01'").fetchone())
    before_sensor = dict(system.sensor_state("S-01-TEMP"))
    assert before_tool["current_recipe_id"] and before_sensor["control_mean"] is not None

    ts = system.clock.iso_at(system.clock.tick + 1)
    maint = {"event_type": "maintenance", "source": "maintenance", "tool_id": "T-01", "ts": ts,
             "payload": {"log_id": "M-0500", "description": "Chamber clean", "status": "degraded"}}
    system.step_with(extra=[maint])

    after_tool = dict(system.conn.execute("SELECT * FROM tool_state WHERE tool_id = 'T-01'").fetchone())
    assert after_tool["status"] == "degraded"
    assert after_tool["current_recipe_id"] == before_tool["current_recipe_id"]
    after_sensor = dict(system.sensor_state("S-01-TEMP"))
    for k in ("baseline_status", "control_mean", "control_stddev", "baseline_activated_at"):
        assert after_sensor[k] == before_sensor[k]
    log = system.conn.execute("SELECT * FROM maintenance_log WHERE log_id = 'M-0500'").fetchone()
    assert log["technician"] is None and log["tool_id"] == "T-01"

    # A maintenance event without `status` leaves status alone.
    maint2 = {**maint, "ts": system.clock.iso_at(system.clock.tick + 1),
              "payload": {"log_id": "M-0501", "description": "Visual inspection"}}
    system.step_with(extra=[maint2])
    assert system.conn.execute("SELECT status FROM tool_state WHERE tool_id = 'T-01'").fetchone()[0] == "degraded"
