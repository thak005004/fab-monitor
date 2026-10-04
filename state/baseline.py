"""Per-sensor baseline lifecycle (spec §6).

learning --(window closes, enough points)--> active --(recipe change)--> relearning --> active

While active, control limits are FROZEN. They are never recalculated from
recent data: a rolling baseline would absorb a slow drift into "normal" and
gradual drift would never be detected.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass

from config_loader import Config
from db import repo
from sim.clock import Clock


@dataclass(frozen=True)
class WindowClosure:
    sensor_id: str
    tool_id: str
    activated: bool                  # False: too few points, window extended
    was_relearning: bool
    window_start: str                # for a relearn, the recipe change time
    mean: float | None = None
    stddev: float | None = None
    cpk: float | None = None         # set only after a relearn
    capability_ok: bool | None = None


def control_limits(values: list[float], stddev_floor: float) -> tuple[float, float]:
    """Mean and sample stddev, with the stddev floored (a flat signal would
    otherwise give stddev 0 and divide-by-zero in the sigma rules)."""
    return statistics.fmean(values), max(statistics.stdev(values), stddev_floor)


def cpk(mean: float, stddev: float, spec_lower: float, spec_upper: float) -> float:
    return min(spec_upper - mean, mean - spec_lower) / (3 * stddev)


def capability_check(mean: float, stddev: float, spec_lower: float, spec_upper: float, threshold: float) -> tuple[float, bool]:
    """Returns (Cpk, ok). Catches a recipe that stays inside spec but too close to an edge."""
    c = cpk(mean, stddev, spec_lower, spec_upper)
    return c, c >= threshold


def relearning_windows(conn: sqlite3.Connection, sensor_id: str, clock: Clock) -> list[tuple[str, str, bool]]:
    """(start, end, ongoing) for each relearning window this sensor went through:
    finished ones from the stored history (exact, including any extension for
    lack of points), plus the current one if it is still relearning."""
    windows = [(w["window_start"], w["activated_at"], False) for w in repo.baseline_windows(conn, sensor_id, "relearning")]
    state = repo.get_sensor_state(conn, sensor_id)
    if state["baseline_status"] == "relearning":
        windows.append((state["baseline_window_start"], clock.now_iso(), True))
    return windows


def close_due_windows(conn: sqlite3.Connection, clock: Clock, config: Config) -> list[WindowClosure]:
    """Called on each TICK. Closes every learning/relearning window that has ended."""
    now = clock.now_iso()
    closures = []
    for row in repo.due_baseline_windows(conn, now):
        sid = row["sensor_id"]
        relearn = row["baseline_status"] == "relearning"
        values = repo.window_values(conn, sid, row["baseline_window_start"], row["baseline_locked_until"])

        if len(values) < config.min_baseline_points:
            # Too few points: extend the same window (start unchanged) by another window length.
            repo.extend_baseline_window(conn, sid, clock.shift_iso(row["baseline_locked_until"], config.baseline_window_ticks))
            closures.append(WindowClosure(sid, row["tool_id"], False, relearn, row["baseline_window_start"]))
            continue

        mean, stddev = control_limits(values, config.stddev_floor)
        repo.activate_baseline(conn, sid, mean, stddev, now)
        repo.record_baseline_window(conn, sid, row["baseline_status"], row["baseline_window_start"], now)
        c = ok = None
        if relearn:
            c, ok = capability_check(mean, stddev, row["spec_lower"], row["spec_upper"], config.cpk_threshold)
        closures.append(WindowClosure(sid, row["tool_id"], True, relearn, row["baseline_window_start"], mean, stddev, c, ok))
    return closures
