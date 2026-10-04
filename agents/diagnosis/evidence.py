"""Builds the evidence bundle the diagnosis model sees (spec §10). Code only.

Every item carries its ID, tool and timestamp, so verify.py can check each
citation against exactly what was sent.
"""

from __future__ import annotations

import json
import sqlite3

from config_loader import Config
from db import repo
from sim.clock import Clock


def build_bundle(conn: sqlite3.Connection, incident: dict, clock: Clock, config: Config) -> dict:
    now = clock.now_iso()
    since = clock.shift_iso(now, -config.diagnosis_lookback_ticks)
    onset = incident["onset_ts"]
    sensor = repo.get_sensor(conn, incident["sensor_id"])
    state = repo.get_sensor_state(conn, incident["sensor_id"])

    mean, sd = state["control_mean"], state["control_stddev"]
    limits = None
    if mean is not None and sd is not None:
        k = config.sigma_threshold
        limits = {"mean": mean, "stddev": sd, "lower": mean - k * sd, "upper": mean + k * sd}

    readings, trigger_ids = _readings(conn, incident, onset, now, config.max_evidence_readings)

    return {
        "incident": {
            "incident_id": incident["incident_id"],
            "tool_id": incident["tool_id"],
            "sensor_id": incident["sensor_id"],
            "sensor_type": sensor["sensor_type"],
            "unit": sensor["unit"],
            "rule_fired": incident["rule_fired"],
            "severity": incident["severity"],
            "onset_ts": onset,
            "now": now,
            "triggering_reading_ids": trigger_ids,
            "control_limits": limits,
            "spec_limits": {"lower": sensor["spec_lower"], "upper": sensor["spec_upper"]},
        },
        "readings": [
            {"id": r["reading_id"], "tool_id": r["tool_id"], "ts": r["ts"], "value": r["value"],
             "fired_current_rule": r["reading_id"] in trigger_ids}
            for r in readings
        ],
        "maintenance": [
            {"id": m["log_id"], "tool_id": m["tool_id"], "ts": m["ts"],
             "description": m["description"], "technician": m["technician"]}
            for m in repo.maintenance_between(conn, incident["tool_id"], since, now)
        ],
        "recipe_changes": [
            {"id": c["change_id"], "tool_id": c["tool_id"], "ts": c["ts"], "recipe_id": c["recipe_id"]}
            for c in repo.recipe_changes_between(conn, incident["tool_id"], since, now)
        ],
    }


def _readings(conn, incident: dict, onset: str, now: str, budget: int) -> tuple[list, list[str]]:
    """Two windows of the incident's sensor, at most `budget` readings in total:

    - latest-trigger window: the readings that fired the incident's current rule
      (e.g. the out-of-spec reading after an upgrade to beyond_spec) and the ones
      just before them; up to half the budget, more only if the trigger itself
      needs it.
    - onset window: the rest of the budget, half at or before the onset, the
      rest after it.
    Overlapping readings are counted once.
    """
    sensor_id = incident["sensor_id"]
    trigger_ids = json.loads(incident.get("trigger_reading_ids") or "[]")
    triggers = repo.readings_by_ids(conn, trigger_ids)

    latest: dict[str, sqlite3.Row] = {}
    if triggers:
        n_latest = min(budget, max(budget // 2, len(triggers)))
        for r in repo.readings_before(conn, sensor_id, triggers[-1]["ts"], n_latest):
            latest[r["reading_id"]] = r
        for r in triggers:
            latest[r["reading_id"]] = r
        while len(latest) > n_latest:  # keep the trigger readings, drop the oldest context
            oldest = min((r for r in latest.values() if r["reading_id"] not in trigger_ids), key=lambda r: r["ts"])
            del latest[oldest["reading_id"]]

    remaining = budget - len(latest)
    before = repo.readings_before(conn, sensor_id, onset, remaining // 2)
    after = repo.readings_after(conn, sensor_id, onset, now, remaining - len(before))
    merged = {r["reading_id"]: r for r in before + after}
    merged.update(latest)
    return sorted(merged.values(), key=lambda r: (r["ts"], r["reading_id"])), [r["reading_id"] for r in triggers]


def bundle_items(bundle: dict) -> dict[str, dict]:
    """Every citable item in the bundle, by ID, with its kind."""
    items = {}
    for kind in ("readings", "maintenance", "recipe_changes"):
        for item in bundle[kind]:
            items[item["id"]] = {**item, "kind": kind}
    return items


def render(bundle: dict) -> str:
    """JSON for the <evidence> block. '<' and '>' are escaped so free text (for
    example a maintenance note) can never close the evidence tag and pose as
    instructions outside it."""
    text = json.dumps(bundle, indent=1, sort_keys=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e")
