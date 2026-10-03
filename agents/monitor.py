"""Monitor agent (spec §7). No LLM.

check_reading: SPC rules on one new reading (pure function).
sweep:         on each TICK, dropout detection, closing baseline windows
               (with the capability check), and expiring stale incidents.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import incidents
from config_loader import SEVERITY_RANK, Config
from db import repo
from sim.clock import Clock, to_iso
from state import baseline

# Table order in §7, used only to break ties between equally severe rules.
RULE_ORDER = ("beyond_spec", "beyond_3sigma", "sustained_run", "trending")


@dataclass(frozen=True)
class Trigger:
    """One rule firing on one sensor. check_reading's MonitorResult (§7) plus
    the sensor and tool it belongs to; sweep produces the same shape."""

    sensor_id: str
    tool_id: str
    rule_fired: str
    onset_ts: str
    triggering_reading_ids: tuple[str, ...] = field(default_factory=tuple)


MonitorResult = Trigger


def fired_rules(sensor, recent_readings, sensor_state, config: Config) -> list[Trigger]:
    """Every rule that fires on the newest reading (the last of recent_readings).

    recent_readings: oldest first, only readings after the current baseline
    activated (for learning sensors, at least the newest reading).
    """
    if not recent_readings:
        return []
    newest = recent_readings[-1]
    sid, tool = sensor["sensor_id"], sensor["tool_id"]
    out = []

    # Spec limits are always checked, including while learning/relearning.
    if not sensor["spec_lower"] <= newest["value"] <= sensor["spec_upper"]:
        out.append(Trigger(sid, tool, "beyond_spec", newest["ts"], (newest["reading_id"],)))

    if sensor_state["baseline_status"] != "active":
        return out

    mean, sd = sensor_state["control_mean"], sensor_state["control_stddev"]
    if abs(newest["value"] - mean) > config.sigma_threshold * sd:
        out.append(Trigger(sid, tool, "beyond_3sigma", newest["ts"], (newest["reading_id"],)))

    run = recent_readings[-config.run_length:]
    if len(run) == config.run_length and (
        all(r["value"] > mean for r in run) or all(r["value"] < mean for r in run)
    ):
        out.append(Trigger(sid, tool, "sustained_run", run[0]["ts"], tuple(r["reading_id"] for r in run)))

    trend = recent_readings[-config.trend_length:]
    if len(trend) == config.trend_length:
        steps = [b["value"] - a["value"] for a, b in zip(trend, trend[1:])]
        if all(d > 0 for d in steps) or all(d < 0 for d in steps):
            out.append(Trigger(sid, tool, "trending", trend[0]["ts"], tuple(r["reading_id"] for r in trend)))
    return out


def most_severe(fired: list[Trigger], config: Config) -> Trigger | None:
    """The most severe incident-eligible trigger, or None. Log-only rules are
    excluded. Ties go to the earliest onset, then to §7 table order."""
    eligible = [t for t in fired if not config.is_log_only(t.rule_fired)]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda t: (-SEVERITY_RANK[config.severity_of(t.rule_fired)], t.onset_ts, RULE_ORDER.index(t.rule_fired)),
    )


def log_only(fired: list[Trigger], config: Config) -> list[Trigger]:
    """Firings to record for the dashboard without opening an incident."""
    return [t for t in fired if config.is_log_only(t.rule_fired)]


def check_reading(sensor, recent_readings, sensor_state, config: Config) -> MonitorResult | None:
    """The most severe incident-eligible rule that fires, or None."""
    return most_severe(fired_rules(sensor, recent_readings, sensor_state, config), config)


def sweep(conn: sqlite3.Connection, clock: Clock, config: Config) -> list[Trigger]:
    """Runs on each TICK. Returns dropout and capability_degraded triggers."""
    triggers = []
    now = clock.now_iso()

    # Dropout. Silence is not an event, so it can only be noticed on the clock.
    # A sensor that has never reported is measured from the clock's start.
    for row in repo.all_sensor_states(conn):
        last = row["last_reading_ts"] or to_iso(clock.start_time)
        if clock.ticks_between(last, now) > config.dropout_threshold_ticks:
            # Onset: the first tick with a missing reading.
            triggers.append(Trigger(row["sensor_id"], row["tool_id"], "dropout", clock.shift_iso(last, 1)))

    for c in baseline.close_due_windows(conn, clock, config):
        if c.capability_ok is False:
            # Onset is the recipe change time, i.e. the relearning window's start.
            triggers.append(Trigger(c.sensor_id, c.tool_id, "capability_degraded", c.window_start))

    incidents.expire_stale(conn, clock, config)
    return triggers
