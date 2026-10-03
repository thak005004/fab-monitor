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

    # Readings around the onset: up to half before (and at) it, the rest after it.
    before_n = config.max_evidence_readings // 2
    before = repo.readings_before(conn, incident["sensor_id"], onset, before_n)
    after = repo.readings_after(conn, incident["sensor_id"], onset, now, config.max_evidence_readings - len(before))

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
            "control_limits": limits,
            "spec_limits": {"lower": sensor["spec_lower"], "upper": sensor["spec_upper"]},
        },
        "readings": [
            {"id": r["reading_id"], "tool_id": r["tool_id"], "ts": r["ts"], "value": r["value"]} for r in before + after
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
