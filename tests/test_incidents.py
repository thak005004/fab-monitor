"""Incidents (§8) and lots at risk (§9), driven directly with triggers."""

from __future__ import annotations

import json

import pytest

import incidents
import lots_at_risk
from agents.monitor import Trigger
from config_loader import load_config
from db import repo
from db.connection import connect, init_schema
from incidents import Change
from sim.clock import Clock
from sim.seed import build_world, seed_database

CONFIG = load_config()
SID, TOOL = "S-01-TEMP", "T-01"


@pytest.fixture
def db(tmp_path):
    conn = connect(tmp_path / "i.db")
    init_schema(conn)
    clock = Clock()
    world = build_world(seed=7, total_ticks=500)
    seed_database(conn, world, clock)
    yield conn, clock, world
    conn.close()


def trig(clock, rule, onset_tick=None, sid=SID, tool=TOOL):
    onset = clock.iso_at(clock.tick if onset_tick is None else onset_tick)
    return Trigger(sid, tool, rule, onset)


def test_several_rules_on_one_drift_give_one_upgraded_incident(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, ch = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert ch is Change.NEW and inc["severity"] == "low"  # watch list
    clock.advance()
    inc2, ch = incidents.open_or_update(conn, trig(clock, "sustained_run", 92), clock, CONFIG)
    assert ch is Change.UPGRADED and inc2["incident_id"] == inc["incident_id"] and inc2["severity"] == "medium"
    assert inc2["rule_fired"] == "sustained_run" and inc2["onset_ts"] == clock.iso_at(92)  # earliest onset kept
    clock.advance()
    inc3, ch = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert ch is Change.NONE and inc3["rule_fired"] == "sustained_run"  # less severe: no change
    assert inc3["updated_at"] == clock.now_iso()
    clock.advance()
    inc4, ch = incidents.open_or_update(conn, trig(clock, "beyond_spec"), clock, CONFIG)
    assert ch is Change.UPGRADED and inc4["severity"] == "high" and inc4["onset_ts"] == clock.iso_at(92)
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1


def test_lots_overlapping_onset_to_now_are_marked_and_earlier_lots_are_not(db):
    conn, clock, world = db
    clock.advance(200)
    onset_tick = 150
    inc, _ = incidents.open_or_update(conn, trig(clock, "sustained_run", onset_tick), clock, CONFIG)
    marked = lots_at_risk.mark(conn, inc, clock)

    tool_lots = [l for l in world.lots if l.tool_id == TOOL]
    expected = sorted(l.lot_id for l in tool_lots if l.start_tick <= 200 and l.end_tick >= onset_tick)
    assert marked == expected and len(expected) >= 2
    ended_before = [l.lot_id for l in tool_lots if l.end_tick < onset_tick]
    future = [l.lot_id for l in tool_lots if l.start_tick > 200]
    assert ended_before and future
    status = dict(conn.execute("SELECT lot_id, status FROM lots"))
    assert all(status[i] == "at_risk" for i in expected)
    assert all(status[i] == "normal" for i in ended_before + future)
    assert not any(status[l.lot_id] != "normal" for l in world.lots if l.tool_id != TOOL)  # other tools untouched
    assert json.loads(conn.execute("SELECT lots_at_risk FROM incidents").fetchone()[0]) == expected


def test_held_lot_is_not_downgraded(db):
    conn, clock, _ = db
    clock.advance(200)
    inc, _ = incidents.open_or_update(conn, trig(clock, "sustained_run", 150), clock, CONFIG)
    lots = lots_at_risk.mark(conn, inc, clock)
    conn.execute("UPDATE lots SET status = 'held' WHERE lot_id = ?", (lots[0],))
    lots_at_risk.mark(conn, dict(conn.execute("SELECT * FROM incidents").fetchone()), clock)
    assert conn.execute("SELECT status FROM lots WHERE lot_id = ?", (lots[0],)).fetchone()[0] == "held"


def test_false_alarm_dismissal_starts_cooldown_for_non_high_triggers(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, _ = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert incidents.transition(conn, clock, inc["incident_id"], "dismiss", "P-01", "false_alarm").accepted

    clock.advance(CONFIG.dismissal_cooldown_ticks - 1)
    _, ch = incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)
    assert ch is Change.NONE
    assert conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
    # A high-severity trigger is never suppressed.
    high, ch = incidents.open_or_update(conn, trig(clock, "beyond_spec"), clock, CONFIG)
    assert ch is Change.NEW and high["severity"] == "high"


def test_cooldown_ends_after_dismissal_cooldown_ticks(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, _ = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    incidents.transition(conn, clock, inc["incident_id"], "dismiss", "P-01", "false_alarm")
    clock.advance(CONFIG.dismissal_cooldown_ticks)
    _, ch = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert ch is Change.NEW


def test_dismissal_for_another_reason_has_no_cooldown(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, _ = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    incidents.transition(conn, clock, inc["incident_id"], "dismiss", "P-01", "duplicate")
    clock.advance()
    _, ch = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert ch is Change.NEW


@pytest.mark.parametrize(
    "path, bad_action",
    [
        ([], "resolve"),                        # open -> resolved skips acknowledge
        ([], "confirm_hold"),                   # open -> hold_confirmed
        (["acknowledge"], "acknowledge"),       # already acknowledged
        (["acknowledge", "confirm_hold"], "dismiss"),
        (["dismiss"], "acknowledge"),           # dismissed is final
        (["acknowledge", "resolve"], "acknowledge"),
        ([], "explode"),                        # unknown action
    ],
)
def test_invalid_status_transition_is_rejected(db, path, bad_action):
    conn, clock, _ = db
    clock.advance(10)
    inc, _ = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    for action in path:
        assert incidents.transition(conn, clock, inc["incident_id"], action, "P-01").accepted
    before = conn.execute("SELECT status FROM incidents").fetchone()[0]
    result = incidents.transition(conn, clock, inc["incident_id"], bad_action, "P-01")
    assert not result.accepted and result.detail
    assert conn.execute("SELECT status FROM incidents").fetchone()[0] == before


def test_unknown_incident_is_rejected(db):
    conn, clock, _ = db
    assert not incidents.transition(conn, clock, "INC-9999", "acknowledge", "P-01").accepted


def test_valid_path_and_confirm_hold_holds_lots(db):
    conn, clock, _ = db
    clock.advance(200)
    inc, _ = incidents.open_or_update(conn, trig(clock, "beyond_spec", 150), clock, CONFIG)
    lots = lots_at_risk.mark(conn, inc, clock)
    iid = inc["incident_id"]
    for action, status in [("acknowledge", "acknowledged"), ("confirm_hold", "hold_confirmed"), ("resolve", "resolved")]:
        r = incidents.transition(conn, clock, iid, action, "P-01")
        assert r.accepted and conn.execute("SELECT status FROM incidents").fetchone()[0] == status
        if action == "confirm_hold":
            assert {s for (s,) in conn.execute(
                f"SELECT status FROM lots WHERE lot_id IN ({','.join('?' * len(lots))})", lots)} == {"held"}


def test_stale_open_incident_expires_and_later_trigger_opens_new_one(db):
    conn, clock, _ = db
    clock.advance(100)
    first, _ = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    clock.advance(CONFIG.stale_after_ticks - 1)
    assert incidents.expire_stale(conn, clock, CONFIG) == []
    clock.advance()
    assert incidents.expire_stale(conn, clock, CONFIG) == [first["incident_id"]]
    row = conn.execute("SELECT status, updated_at FROM incidents").fetchone()
    assert row["status"] == "expired" and row["updated_at"] == clock.now_iso()

    clock.advance()
    second, ch = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert ch is Change.NEW and second["incident_id"] != first["incident_id"]
    # Expired is final: a person can't act on it, and it is not "resolved".
    assert not incidents.transition(conn, clock, first["incident_id"], "resolve", "P-01").accepted


def test_new_triggers_keep_an_incident_from_expiring(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, _ = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    for _ in range(3):
        clock.advance(CONFIG.stale_after_ticks - 1)
        incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)  # NONE, but refreshes
        assert incidents.expire_stale(conn, clock, CONFIG) == []


def test_acknowledged_incident_does_not_expire(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, _ = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    incidents.transition(conn, clock, inc["incident_id"], "acknowledge", "P-01")
    clock.advance(10 * CONFIG.stale_after_ticks)
    assert incidents.expire_stale(conn, clock, CONFIG) == []


# ----- categories: dedupe is per sensor AND per category (process vs data) ----

def test_dropout_opens_its_own_incident_while_a_process_incident_is_open(db):
    conn, clock, _ = db
    clock.advance(100)
    proc, _ = incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)  # medium
    clock.advance(6)
    drop, ch = incidents.open_or_update(conn, trig(clock, "dropout", 101), clock, CONFIG)  # also medium
    assert ch is Change.NEW and drop["incident_id"] != proc["incident_id"]
    assert drop["rule_fired"] == "dropout"
    # The process incident is untouched by the data trigger.
    assert dict(repo.get_incident(conn, proc["incident_id"]))["updated_at"] == clock.iso_at(100)
    # And a process trigger now updates the process incident, not the dropout one.
    clock.advance()
    again, ch = incidents.open_or_update(conn, trig(clock, "beyond_spec"), clock, CONFIG)
    assert ch is Change.UPGRADED and again["incident_id"] == proc["incident_id"]
    assert dict(repo.get_incident(conn, drop["incident_id"]))["rule_fired"] == "dropout"


def test_false_alarm_cooldown_is_per_category(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, _ = incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)
    incidents.transition(conn, clock, inc["incident_id"], "dismiss", "P-01", "false_alarm")
    clock.advance()
    _, ch = incidents.open_or_update(conn, trig(clock, "dropout", 95), clock, CONFIG)
    assert ch is Change.NEW  # a dismissed process false alarm doesn't silence a dropout
    _, ch = incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)
    assert ch is Change.NONE  # but process triggers are still in cooldown


# ----- reactivation ------------------------------------------------------------

def test_trigger_after_quiet_spell_reactivates(db):
    conn, clock, _ = db
    clock.advance(100)
    inc, _ = incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)
    clock.advance(CONFIG.reactivation_quiet_ticks - 1)
    _, ch = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert ch is Change.NONE  # not quiet long enough (and this resets the quiet clock)
    clock.advance(CONFIG.reactivation_quiet_ticks)
    again, ch = incidents.open_or_update(conn, trig(clock, "beyond_3sigma"), clock, CONFIG)
    assert ch is Change.REACTIVATED and again["incident_id"] == inc["incident_id"]
    assert again["severity"] == "medium" and again["rule_fired"] == "sustained_run"
    assert again["updated_at"] == clock.now_iso()


def test_upgrade_after_quiet_spell_is_reported_as_upgraded(db):
    conn, clock, _ = db
    clock.advance(100)
    incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)
    clock.advance(CONFIG.reactivation_quiet_ticks + 2)
    _, ch = incidents.open_or_update(conn, trig(clock, "beyond_spec"), clock, CONFIG)
    assert ch is Change.UPGRADED


def test_quiet_past_stale_expires_instead_of_reactivating(db):
    conn, clock, _ = db
    clock.advance(100)
    first, _ = incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)
    clock.advance(CONFIG.stale_after_ticks)
    assert incidents.expire_stale(conn, clock, CONFIG) == [first["incident_id"]]
    clock.advance()
    second, ch = incidents.open_or_update(conn, trig(clock, "sustained_run"), clock, CONFIG)
    assert ch is Change.NEW and second["incident_id"] != first["incident_id"]
