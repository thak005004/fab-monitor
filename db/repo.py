"""All SQL queries live here (spec §18).

Only the writes needed by the seed and the simulator exist so far.
Callers wrap these in `connection.transaction()`; nothing here commits.
"""

from __future__ import annotations

import sqlite3


def insert_tool(conn: sqlite3.Connection, tool_id: str, name: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO tools (tool_id, name, kind) VALUES (?, ?, ?)",
        (tool_id, name, kind),
    )


def insert_sensor(
    conn: sqlite3.Connection,
    sensor_id: str,
    tool_id: str,
    sensor_type: str,
    unit: str,
    spec_lower: float,
    spec_upper: float,
) -> None:
    conn.execute(
        "INSERT INTO sensors (sensor_id, tool_id, sensor_type, unit, spec_lower, spec_upper)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (sensor_id, tool_id, sensor_type, unit, spec_lower, spec_upper),
    )


def insert_person(conn: sqlite3.Connection, person_id: str, name: str, available: bool = True) -> None:
    conn.execute(
        "INSERT INTO people (person_id, name, available) VALUES (?, ?, ?)",
        (person_id, name, int(available)),
    )


def insert_qualification(conn: sqlite3.Connection, tool_id: str, person_id: str) -> None:
    conn.execute(
        "INSERT INTO tool_qualifications (tool_id, person_id) VALUES (?, ?)",
        (tool_id, person_id),
    )


def insert_tool_state(conn: sqlite3.Connection, tool_id: str, status: str, current_recipe_id: str | None) -> None:
    conn.execute(
        "INSERT INTO tool_state (tool_id, status, current_recipe_id) VALUES (?, ?, ?)",
        (tool_id, status, current_recipe_id),
    )


def insert_sensor_state(
    conn: sqlite3.Connection,
    sensor_id: str,
    baseline_status: str,
    baseline_window_start: str,
    baseline_locked_until: str,
) -> None:
    conn.execute(
        "INSERT INTO sensor_state (sensor_id, baseline_status, baseline_window_start, baseline_locked_until)"
        " VALUES (?, ?, ?, ?)",
        (sensor_id, baseline_status, baseline_window_start, baseline_locked_until),
    )


def insert_lot(conn: sqlite3.Connection, lot_id: str, tool_id: str, start_ts: str, end_ts: str | None) -> None:
    conn.execute(
        "INSERT INTO lots (lot_id, tool_id, start_ts, end_ts) VALUES (?, ?, ?, ?)",
        (lot_id, tool_id, start_ts, end_ts),
    )


def insert_fault_injection(
    conn: sqlite3.Connection,
    fault_id: str,
    fault_type: str,
    tool_id: str,
    sensor_id: str | None,
    start_ts: str,
    planted_cause_id: str | None,
    expected_outcome: str,
) -> None:
    conn.execute(
        "INSERT INTO fault_injections"
        " (fault_id, fault_type, tool_id, sensor_id, start_ts, planted_cause_id, expected_outcome)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (fault_id, fault_type, tool_id, sensor_id, start_ts, planted_cause_id, expected_outcome),
    )


# ===== Events and dead letter (bus) ==========================================

def insert_event(conn, event_id: str, event_type: str, source: str, tool_id: str | None, payload_json: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO events (event_id, event_type, source, tool_id, payload, ts) VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, event_type, source, tool_id, payload_json, ts),
    )


def insert_dead_letter(conn, dl_id: str, source: str | None, raw_payload: str, error_reason: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO dead_letter (id, source, raw_payload, error_reason, ts) VALUES (?, ?, ?, ?, ?)",
        (dl_id, source, raw_payload, error_reason, ts),
    )


def count_rows(conn, table: str) -> int:
    if table not in {"events", "dead_letter", "incidents", "readings", "logged_firings"}:
        raise ValueError(table)
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ===== Reference data ========================================================

def sensor_tools(conn) -> dict[str, str]:
    return {r["sensor_id"]: r["tool_id"] for r in conn.execute("SELECT sensor_id, tool_id FROM sensors")}


def tool_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT tool_id FROM tools")}


def person_ids(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT person_id FROM people")}


def get_sensor(conn, sensor_id: str):
    return conn.execute("SELECT * FROM sensors WHERE sensor_id = ?", (sensor_id,)).fetchone()


# ===== World state (merge-only writes) =======================================

def insert_reading(conn, reading_id: str, sensor_id: str, tool_id: str, value: float, ts: str) -> None:
    conn.execute(
        "INSERT INTO readings (reading_id, sensor_id, tool_id, value, ts) VALUES (?, ?, ?, ?, ?)",
        (reading_id, sensor_id, tool_id, value, ts),
    )


def set_last_reading_ts(conn, sensor_id: str, ts: str) -> None:
    conn.execute("UPDATE sensor_state SET last_reading_ts = ? WHERE sensor_id = ?", (ts, sensor_id))


def insert_maintenance(conn, log_id: str, tool_id: str, description: str, technician: str | None, ts: str) -> None:
    conn.execute(
        "INSERT INTO maintenance_log (log_id, tool_id, description, technician, ts) VALUES (?, ?, ?, ?, ?)",
        (log_id, tool_id, description, technician, ts),
    )


TOOL_STATE_FIELDS = ("status", "current_recipe_id")


def merge_tool_state(conn, tool_id: str, fields: dict) -> None:
    """UPDATE only the columns given. Never rewrites the whole row (spec §5)."""
    unknown = set(fields) - set(TOOL_STATE_FIELDS)
    if unknown:
        raise ValueError(f"not tool_state fields: {sorted(unknown)}")
    if not fields:
        return
    cols = sorted(fields)
    conn.execute(
        f"UPDATE tool_state SET {', '.join(f'{c} = ?' for c in cols)} WHERE tool_id = ?",
        (*(fields[c] for c in cols), tool_id),
    )


def insert_recipe_change(conn, change_id: str, tool_id: str, recipe_id: str, ts: str) -> None:
    conn.execute(
        "INSERT INTO recipe_changes (change_id, tool_id, recipe_id, ts) VALUES (?, ?, ?, ?)",
        (change_id, tool_id, recipe_id, ts),
    )


def start_relearning(conn, tool_id: str, window_start: str, locked_until: str) -> None:
    """Every sensor on the tool relearns. Control limits are left in place."""
    conn.execute(
        "UPDATE sensor_state SET baseline_status = 'relearning', baseline_window_start = ?, baseline_locked_until = ?"
        " WHERE sensor_id IN (SELECT sensor_id FROM sensors WHERE tool_id = ?)",
        (window_start, locked_until, tool_id),
    )


def set_person_available(conn, person_id: str, available: bool) -> None:
    conn.execute("UPDATE people SET available = ? WHERE person_id = ?", (int(available), person_id))


# ===== Baseline ==============================================================

def get_sensor_state(conn, sensor_id: str):
    return conn.execute("SELECT * FROM sensor_state WHERE sensor_id = ?", (sensor_id,)).fetchone()


def all_sensor_states(conn) -> list:
    return conn.execute(
        "SELECT ss.*, s.tool_id, s.spec_lower, s.spec_upper FROM sensor_state ss"
        " JOIN sensors s ON s.sensor_id = ss.sensor_id ORDER BY ss.sensor_id"
    ).fetchall()


def due_baseline_windows(conn, now: str) -> list:
    return conn.execute(
        "SELECT ss.*, s.tool_id, s.spec_lower, s.spec_upper FROM sensor_state ss"
        " JOIN sensors s ON s.sensor_id = ss.sensor_id"
        " WHERE ss.baseline_status IN ('learning', 'relearning') AND ss.baseline_locked_until <= ?"
        " ORDER BY ss.sensor_id",
        (now,),
    ).fetchall()


def window_values(conn, sensor_id: str, after_ts: str, until_ts: str) -> list[float]:
    """Readings in the window (after_ts, until_ts]. The start is exclusive: readings
    stamped at a recipe change's own tick were produced under the old recipe."""
    return [
        r[0] for r in conn.execute(
            "SELECT value FROM readings WHERE sensor_id = ? AND ts > ? AND ts <= ? ORDER BY ts",
            (sensor_id, after_ts, until_ts),
        )
    ]


def activate_baseline(conn, sensor_id: str, mean: float, stddev: float, activated_at: str) -> None:
    conn.execute(
        "UPDATE sensor_state SET baseline_status = 'active', control_mean = ?, control_stddev = ?,"
        " baseline_activated_at = ? WHERE sensor_id = ?",
        (mean, stddev, activated_at, sensor_id),
    )


def record_baseline_window(conn, sensor_id: str, kind: str, window_start: str, activated_at: str) -> None:
    conn.execute(
        "INSERT INTO baseline_windows (sensor_id, kind, window_start, activated_at) VALUES (?, ?, ?, ?)",
        (sensor_id, kind, window_start, activated_at),
    )


def baseline_windows(conn, sensor_id: str, kind: str) -> list:
    return conn.execute(
        "SELECT * FROM baseline_windows WHERE sensor_id = ? AND kind = ? ORDER BY activated_at", (sensor_id, kind)
    ).fetchall()


def extend_baseline_window(conn, sensor_id: str, locked_until: str) -> None:
    conn.execute("UPDATE sensor_state SET baseline_locked_until = ? WHERE sensor_id = ?", (locked_until, sensor_id))


# ===== Monitor ===============================================================

def recent_readings(conn, sensor_id: str, after_ts: str | None, limit: int) -> list:
    """Most recent `limit` readings with ts > after_ts, returned oldest first."""
    rows = conn.execute(
        "SELECT reading_id, value, ts FROM readings WHERE sensor_id = ? AND ts > ?"
        " ORDER BY ts DESC, reading_id DESC LIMIT ?",
        (sensor_id, after_ts or "", limit),
    ).fetchall()
    return rows[::-1]


# ===== Incidents =============================================================

ACTIVE_INCIDENT_STATUSES = ("open", "acknowledged", "hold_confirmed")


def get_incident(conn, incident_id: str):
    return conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()


def _in(values) -> str:
    return f"({', '.join('?' * len(values))})"


def get_active_incident(conn, sensor_id: str, rules: tuple[str, ...]):
    """The active incident on this sensor whose rule is one of `rules` (one category)."""
    return conn.execute(
        f"SELECT * FROM incidents WHERE sensor_id = ? AND status IN {_in(ACTIVE_INCIDENT_STATUSES)}"
        f" AND rule_fired IN {_in(rules)} ORDER BY opened_at DESC, incident_id DESC LIMIT 1",
        (sensor_id, *ACTIVE_INCIDENT_STATUSES, *rules),
    ).fetchone()


def false_alarm_dismissed_since(conn, sensor_id: str, rules: tuple[str, ...], since_ts: str) -> bool:
    """Was an incident on this sensor, in this category, dismissed as a false alarm
    after since_ts? A dismissed incident's updated_at is its dismissal time."""
    return conn.execute(
        "SELECT 1 FROM incidents WHERE sensor_id = ? AND status = 'dismissed'"
        f" AND dismiss_reason = 'false_alarm' AND rule_fired IN {_in(rules)} AND updated_at > ? LIMIT 1",
        (sensor_id, *rules, since_ts),
    ).fetchone() is not None


def insert_incident(
    conn, incident_id: str, tool_id: str, sensor_id: str, rule_fired: str, severity: str,
    onset_ts: str, now: str, config_version: str, trigger_reading_ids_json: str = "[]",
) -> None:
    conn.execute(
        "INSERT INTO incidents (incident_id, tool_id, sensor_id, rule_fired, trigger_reading_ids, severity, onset_ts,"
        " opened_at, updated_at, status, config_version, lots_at_risk)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, '[]')",
        (incident_id, tool_id, sensor_id, rule_fired, trigger_reading_ids_json, severity, onset_ts, now, now,
         config_version),
    )


def update_incident_trigger(conn, incident_id: str, rule_fired: str, severity: str, onset_ts: str, now: str,
                            trigger_reading_ids_json: str | None = None) -> None:
    """trigger_reading_ids_json=None keeps the stored trigger readings."""
    conn.execute(
        "UPDATE incidents SET rule_fired = ?, severity = ?, onset_ts = ?, updated_at = ?,"
        " trigger_reading_ids = COALESCE(?, trigger_reading_ids) WHERE incident_id = ?",
        (rule_fired, severity, onset_ts, now, trigger_reading_ids_json, incident_id),
    )


def set_incident_status(conn, incident_id: str, status: str, now: str, dismiss_reason: str | None = None) -> None:
    if dismiss_reason is None:
        conn.execute("UPDATE incidents SET status = ?, updated_at = ? WHERE incident_id = ?", (status, now, incident_id))
    else:
        conn.execute(
            "UPDATE incidents SET status = ?, updated_at = ?, dismiss_reason = ? WHERE incident_id = ?",
            (status, now, dismiss_reason, incident_id),
        )


def stale_open_incident_ids(conn, last_trigger_at_or_before: str) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT incident_id FROM incidents WHERE status = 'open' AND updated_at <= ? ORDER BY incident_id",
            (last_trigger_at_or_before,),
        )
    ]


def set_incident_lots(conn, incident_id: str, lots_json: str) -> None:
    conn.execute("UPDATE incidents SET lots_at_risk = ? WHERE incident_id = ?", (lots_json, incident_id))


def acknowledge_notifications(conn, incident_id: str, now: str) -> None:
    conn.execute(
        "UPDATE notifications SET acknowledged_at = ? WHERE incident_id = ? AND acknowledged_at IS NULL",
        (now, incident_id),
    )


# ===== Lots ==================================================================

def lots_overlapping(conn, tool_id: str, now: str, onset: str) -> list[str]:
    """Spec §9: every lot on the tool at any point between onset and now."""
    return [
        r[0] for r in conn.execute(
            "SELECT lot_id FROM lots WHERE tool_id = ? AND start_ts <= ? AND (end_ts IS NULL OR end_ts >= ?)"
            " ORDER BY lot_id",
            (tool_id, now, onset),
        )
    ]


def mark_lots_at_risk(conn, lot_ids: list[str]) -> None:
    """Never downgrades a held lot."""
    conn.executemany("UPDATE lots SET status = 'at_risk' WHERE lot_id = ? AND status != 'held'", [(i,) for i in lot_ids])


def hold_lots(conn, lot_ids: list[str]) -> None:
    conn.executemany("UPDATE lots SET status = 'held' WHERE lot_id = ?", [(i,) for i in lot_ids])


# ===== Log-only rule firings ==================================================

def insert_logged_firing(
    conn, firing_id: str, sensor_id: str, tool_id: str, rule_fired: str, onset_ts: str,
    reading_ids_json: str, ts: str,
) -> None:
    conn.execute(
        "INSERT INTO logged_firings (firing_id, sensor_id, tool_id, rule_fired, onset_ts, triggering_reading_ids, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (firing_id, sensor_id, tool_id, rule_fired, onset_ts, reading_ids_json, ts),
    )


# ===== Diagnoses ==============================================================

def insert_diagnosis(
    conn, diagnosis_id: str, incident_id: str, status: str, likely_factors_json: str | None,
    cited_evidence_json: str | None, confidence: str | None, rejection_reason: str | None,
    evidence_bundle_json: str, raw_response_json: str | None, prompt_version: str, model: str | None,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO diagnoses (diagnosis_id, incident_id, status, likely_factors, cited_evidence, confidence,"
        " rejection_reason, evidence_bundle, raw_response, prompt_version, model, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (diagnosis_id, incident_id, status, likely_factors_json, cited_evidence_json, confidence,
         rejection_reason, evidence_bundle_json, raw_response_json, prompt_version, model, created_at),
    )


def count_diagnoses(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0]


def count_llm_calls_at(conn, ts: str) -> int:
    """Diagnoses this tick that called the LLM (rate-limited rows have no model)."""
    return conn.execute(
        "SELECT COUNT(*) FROM diagnoses WHERE created_at = ? AND model IS NOT NULL", (ts,)
    ).fetchone()[0]


def get_diagnosis(conn, diagnosis_id: str):
    return conn.execute("SELECT * FROM diagnoses WHERE diagnosis_id = ?", (diagnosis_id,)).fetchone()


# ===== Evidence ===============================================================

def readings_before(conn, sensor_id: str, at_or_before: str, limit: int) -> list:
    """Up to `limit` readings at or before a time, oldest first."""
    rows = conn.execute(
        "SELECT reading_id, tool_id, value, ts FROM readings WHERE sensor_id = ? AND ts <= ?"
        " ORDER BY ts DESC, reading_id DESC LIMIT ?",
        (sensor_id, at_or_before, limit),
    ).fetchall()
    return rows[::-1]


def readings_by_ids(conn, reading_ids: list[str]) -> list:
    if not reading_ids:
        return []
    return conn.execute(
        f"SELECT reading_id, tool_id, value, ts FROM readings WHERE reading_id IN {_in(reading_ids)} ORDER BY ts",
        tuple(reading_ids),
    ).fetchall()


def readings_after(conn, sensor_id: str, after: str, until: str, limit: int) -> list:
    return conn.execute(
        "SELECT reading_id, tool_id, value, ts FROM readings WHERE sensor_id = ? AND ts > ? AND ts <= ?"
        " ORDER BY ts, reading_id LIMIT ?",
        (sensor_id, after, until, limit),
    ).fetchall()


def maintenance_between(conn, tool_id: str, since: str, until: str) -> list:
    return conn.execute(
        "SELECT log_id, tool_id, description, technician, ts FROM maintenance_log"
        " WHERE tool_id = ? AND ts >= ? AND ts <= ? ORDER BY ts, log_id",
        (tool_id, since, until),
    ).fetchall()


def recipe_changes_between(conn, tool_id: str, since: str, until: str) -> list:
    return conn.execute(
        "SELECT change_id, tool_id, recipe_id, ts FROM recipe_changes"
        " WHERE tool_id = ? AND ts >= ? AND ts <= ? ORDER BY ts, change_id",
        (tool_id, since, until),
    ).fetchall()


# ===== Triage and notifications ===============================================

def qualified_available_people(conn, tool_id: str) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT p.person_id FROM tool_qualifications q JOIN people p USING (person_id)"
            " WHERE q.tool_id = ? AND p.available = 1 ORDER BY p.person_id",
            (tool_id,),
        )
    ]


def is_available(conn, person_id: str) -> bool:
    row = conn.execute("SELECT available FROM people WHERE person_id = ?", (person_id,)).fetchone()
    return bool(row and row[0])


def notified_people(conn, incident_id: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT person_id FROM notifications WHERE incident_id = ?", (incident_id,))}


def latest_notification(conn, incident_id: str):
    return conn.execute(
        "SELECT * FROM notifications WHERE incident_id = ? ORDER BY sent_at DESC, notification_id DESC LIMIT 1",
        (incident_id,),
    ).fetchone()


def has_notification_with_reason(conn, incident_id: str, reason: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM notifications WHERE incident_id = ? AND reason = ? LIMIT 1", (incident_id, reason)
    ).fetchone() is not None


def insert_notification(
    conn, notification_id: str, incident_id: str, person_id: str, reason: str,
    escalation_level: int, message: str, sent_at: str,
) -> None:
    conn.execute(
        "INSERT INTO notifications (notification_id, incident_id, person_id, reason, escalation_level, message, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (notification_id, incident_id, person_id, reason, escalation_level, message, sent_at),
    )


def count_notifications(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0]


def apply_incident_decision(conn, incident_id: str, owner_id: str, recommend_hold: bool,
                            latest_diagnosis_id: str | None) -> None:
    """Owner, hold recommendation and latest diagnosis. Leaves updated_at alone:
    it measures time since the last trigger (stale/quiet), not since any edit."""
    conn.execute(
        "UPDATE incidents SET owner_id = ?, recommend_hold = ?,"
        " latest_diagnosis_id = COALESCE(?, latest_diagnosis_id) WHERE incident_id = ?",
        (owner_id, int(recommend_hold), latest_diagnosis_id, incident_id),
    )


def set_incident_owner(conn, incident_id: str, owner_id: str, escalation_level: int) -> None:
    conn.execute(
        "UPDATE incidents SET owner_id = ?, escalation_level = ? WHERE incident_id = ?",
        (owner_id, escalation_level, incident_id),
    )


def open_notified_incidents(conn) -> list:
    """Open (unacknowledged) medium/high incidents that have been notified at least once."""
    return conn.execute(
        "SELECT i.* FROM incidents i WHERE i.status = 'open' AND i.severity IN ('medium', 'high')"
        " AND EXISTS (SELECT 1 FROM notifications n WHERE n.incident_id = i.incident_id) ORDER BY i.incident_id"
    ).fetchall()


def open_incidents_owned_by(conn, person_id: str) -> list:
    return conn.execute(
        "SELECT * FROM incidents WHERE status = 'open' AND owner_id = ? ORDER BY incident_id", (person_id,)
    ).fetchall()


def active_process_incidents_to_refresh(conn) -> list:
    """Active (open/acknowledged/hold_confirmed) medium/high incidents that mark lots, i.e. not dropout."""
    return conn.execute(
        f"SELECT * FROM incidents WHERE status IN {_in(ACTIVE_INCIDENT_STATUSES)}"
        " AND severity IN ('medium', 'high') AND rule_fired != 'dropout' ORDER BY incident_id",
        ACTIVE_INCIDENT_STATUSES,
    ).fetchall()


# ===== Dashboard views (read-only) =============================================

def all_sensors(conn) -> list:
    return conn.execute("SELECT * FROM sensors ORDER BY sensor_id").fetchall()


def people(conn) -> list:
    return conn.execute("SELECT * FROM people ORDER BY person_id").fetchall()


def tool_overview(conn) -> list:
    """Each tool with its state and its active incidents by severity."""
    return conn.execute(
        "SELECT t.tool_id, t.name, t.kind, ts.status, ts.current_recipe_id,"
        " SUM(i.severity = 'high') AS high, SUM(i.severity = 'medium') AS medium, SUM(i.severity = 'low') AS low"
        " FROM tools t JOIN tool_state ts USING (tool_id)"
        f" LEFT JOIN incidents i ON i.tool_id = t.tool_id AND i.status IN {_in(ACTIVE_INCIDENT_STATUSES)}"
        " GROUP BY t.tool_id ORDER BY t.tool_id",
        ACTIVE_INCIDENT_STATUSES,
    ).fetchall()


def readings_since(conn, sensor_id: str, since: str) -> list:
    return conn.execute(
        "SELECT reading_id, value, ts FROM readings WHERE sensor_id = ? AND ts >= ? ORDER BY ts", (sensor_id, since)
    ).fetchall()


def incidents_on_sensor(conn, sensor_id: str) -> list:
    return conn.execute("SELECT * FROM incidents WHERE sensor_id = ? ORDER BY opened_at", (sensor_id,)).fetchall()


def incidents_by_tier(conn, actionable: bool, active_only: bool) -> list:
    """Medium/high (notified) or low (watch list) incidents, newest first."""
    sev = ("medium", "high") if actionable else ("low",)
    q = f"SELECT * FROM incidents WHERE severity IN {_in(sev)}"
    args: tuple = sev
    if active_only:
        q += f" AND status IN {_in(ACTIVE_INCIDENT_STATUSES)}"
        args += ACTIVE_INCIDENT_STATUSES
    return conn.execute(q + " ORDER BY opened_at DESC, incident_id DESC", args).fetchall()


def diagnoses_for_incident(conn, incident_id: str) -> list:
    return conn.execute(
        "SELECT * FROM diagnoses WHERE incident_id = ? ORDER BY created_at, diagnosis_id", (incident_id,)
    ).fetchall()


def lots_by_ids(conn, lot_ids: list[str]) -> list:
    if not lot_ids:
        return []
    return conn.execute(f"SELECT * FROM lots WHERE lot_id IN {_in(lot_ids)} ORDER BY lot_id", tuple(lot_ids)).fetchall()


def notifications_for_person(conn, person_id: str) -> list:
    return conn.execute(
        "SELECT n.*, i.sensor_id, i.tool_id, i.severity, i.status AS incident_status FROM notifications n"
        " JOIN incidents i USING (incident_id) WHERE n.person_id = ? ORDER BY n.sent_at DESC, n.notification_id DESC",
        (person_id,),
    ).fetchall()


def recent_dead_letters(conn, limit: int = 5) -> list:
    return conn.execute("SELECT * FROM dead_letter ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def count_by_status(conn, table: str, column: str, value: str) -> int:
    if (table, column) not in {("incidents", "status"), ("lots", "status")}:
        raise ValueError((table, column))
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column} = ?", (value,)).fetchone()[0]
