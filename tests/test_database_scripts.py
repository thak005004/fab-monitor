"""The three database scripts: event replay, integrity audit, index benchmark.
Fake client only; every database is a new temporary file."""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import index_benchmark  # noqa: E402
import integrity_audit  # noqa: E402
import replay_events  # noqa: E402


def test_a_short_run_replays_to_an_identical_state(tmp_path):
    # Minute 254 includes every dashboard event type: acknowledge, recipe change,
    # availability, confirm hold, a rejected action, and a malformed record.
    result = replay_events.run(seed=42, end_tick=254, workdir=tmp_path)
    assert result["match"], replay_events.report(result)
    assert result["rebuilt_events"] == result["counts"]["events"]
    assert {d.table for d in result["diffs"]} == set(replay_events.DERIVED)
    assert all(d.rows_original == d.rows_rebuilt > 0 for d in result["diffs"])


def test_replay_comparison_reports_a_difference(tmp_path):
    replay_events.run(seed=42, end_tick=200, workdir=tmp_path)
    a = sqlite3.connect(tmp_path / "original.db")
    b = sqlite3.connect(tmp_path / "rebuilt.db")
    a.row_factory = b.row_factory = sqlite3.Row
    b.execute("UPDATE incidents SET status = 'resolved' WHERE incident_id = 'INC-0008'")
    b.execute("DELETE FROM notifications WHERE notification_id = (SELECT MAX(notification_id) FROM notifications)")
    diffs = {d.table: d for d in replay_events.compare(a, b)}
    assert diffs["incidents"].changed == [(("INC-0008",), "status", "acknowledged", "resolved")]  # acknowledged at 200
    assert len(diffs["notifications"].only_original) == 1
    assert diffs["lots"].matches


@pytest.fixture(scope="module")
def demo_db(tmp_path_factory):
    path = tmp_path_factory.mktemp("audit") / "demo.db"
    integrity_audit.build_demo(path)
    return path


def test_a_normal_run_has_zero_violations(demo_db):
    conn = integrity_audit.open_read_only(demo_db)
    checks = integrity_audit.audit(conn)
    conn.close()
    assert len(checks) == 6
    assert {c.name: c.count for c in checks} == {c.name: 0 for c in checks}


def test_the_audit_catches_planted_violations(demo_db, tmp_path):
    bad = tmp_path / "bad.db"
    shutil.copy(demo_db, bad)
    conn = sqlite3.connect(bad)  # foreign keys are off on a plain connection, so orphans can be planted
    conn.execute("INSERT INTO readings VALUES ('RD-900001', 'S-99-TEMP', 'T-01', 1.0, '2026-01-05T09:00:00+00:00')")
    conn.execute("INSERT INTO notifications VALUES ('N-999999', 'INC-9999', 'P-01', 'new', 0, 'x', "
                 "'2026-01-05T09:00:00+00:00', NULL)")
    conn.execute("UPDATE incidents SET status = 'resolved' WHERE incident_id = 'INC-0008'")  # no resolve action logged
    conn.execute("UPDATE lots SET status = 'held' WHERE lot_id = 'LOT-0001'")
    conn.execute("UPDATE diagnoses SET cited_evidence = '[\"M-0777\"]' WHERE diagnosis_id = 'DX-00004'")
    conn.execute("DELETE FROM notifications WHERE incident_id = 'INC-0003'")
    conn.commit()
    conn.close()
    conn = integrity_audit.open_read_only(bad)
    counts = [c.count for c in integrity_audit.audit(conn)]
    conn.close()
    # refs, silent, citations, held lots, orphan notifications, status history
    assert counts == [1, 1, 1, 1, 1, 1]


def test_index_benchmark_plans(tmp_path):
    r = index_benchmark.run(ticks=400, repeats=50, workdir=tmp_path)
    assert r["readings"] == 400 * r["sensors"]
    assert any("USING INDEX idx_readings_sensor_ts" in p for p in r["with_index"]["plan"])
    assert any(p.startswith("SCAN readings") for p in r["without_index"]["plan"])
