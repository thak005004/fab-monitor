"""Measure the false-alarm rate on a long healthy run (no faults). Measures; does not tune.

ALL DATA IS SYNTHETIC.

    .venv/bin/python scripts/false_alarm_rate.py [--ticks 10000] [--seed 42] [--config config.json]

Reports, per 1,000 active sensor-ticks (ticks after a sensor's baseline activated):
  - rule firings: every reading where each rule's condition holds (rules evaluated
    independently, against the frozen limits), next to the chance rate for that rule
  - incidents opened, by the rule that opened them (after dedupe + expiry)
  - incidents by severity when opened, and incidents that ever reached medium or
    high (those are the ones that would notify a person and call the LLM)
  - log-only firings recorded (trending, when trending_log_only is on)
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import monitor  # noqa: E402
from config_loader import ACTIONABLE_SEVERITIES, DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from db import repo  # noqa: E402
from db.connection import connect, init_schema  # noqa: E402
from events.adapters import default_adapters  # noqa: E402
from events.bus import Bus  # noqa: E402
from incidents import Change  # noqa: E402
import incidents  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from sim.clock import Clock  # noqa: E402
from sim.seed import build_world, seed_database  # noqa: E402
from sim.simulator import Simulator  # noqa: E402

RULES = ("beyond_spec", "beyond_3sigma", "sustained_run", "trending")


def chance_rates(config) -> dict[str, float]:
    """Per-reading probability that each rule's condition holds, for i.i.d.
    normal data with the TRUE mean and sigma (no estimation error)."""
    from math import erfc, factorial, sqrt
    return {
        "beyond_spec": erfc(5.5 / sqrt(2)),                                  # spec at 5.5 sigma
        "beyond_3sigma": erfc(config.sigma_threshold / sqrt(2)),             # 0.27%
        "sustained_run": 2 * 0.5 ** config.run_length,                       # last n on one side
        "trending": 2 / factorial(config.trend_length),                      # last n strictly monotone
    }


def measure(ticks: int = 10_000, seed: int = 42, config_path: str | Path = DEFAULT_CONFIG_PATH) -> dict:
    """Run a healthy simulation (no faults) and return the counts; see main() for the report."""
    config = load_config(config_path)
    conn = connect(Path(tempfile.mkdtemp()) / "fa.db")
    init_schema(conn)
    clock = Clock()
    world = build_world(seed=seed, total_ticks=ticks)
    seed_database(conn, world, clock, baseline_window_ticks=config.baseline_window_ticks)
    sim = Simulator(conn, world, clock)  # no faults at all

    opened: Counter[str] = Counter()
    opened_severity: Counter[str] = Counter()
    orch = Orchestrator(conn, clock, config)  # no LLM client: diagnoses are recorded as unavailable
    original = incidents.open_or_update

    def counting_open_or_update(c, trigger, clk, cfg):
        inc, change = original(c, trigger, clk, cfg)
        if change is Change.NEW:
            opened[trigger.rule_fired] += 1
            opened_severity[cfg.severity_of(trigger.rule_fired)] += 1
        return inc, change

    incidents.open_or_update = counting_open_or_update  # instrumentation for this measurement only
    try:
        bus = Bus(conn, orch, clock, default_adapters(conn))
        while clock.tick < ticks:
            bus.publish_tick(sim.step())
    finally:
        incidents.open_or_update = original

    # Independent per-rule firing counts, replayed against the frozen limits.
    fired: Counter[str] = Counter()
    sensor_ticks = 0
    window = max(config.run_length, config.trend_length)
    for state in repo.all_sensor_states(conn):
        sensor = repo.get_sensor(conn, state["sensor_id"])
        rows = conn.execute(
            "SELECT reading_id, value, ts FROM readings WHERE sensor_id = ? AND ts > ? ORDER BY ts",
            (state["sensor_id"], state["baseline_activated_at"]),
        ).fetchall()
        sensor_ticks += len(rows)
        for i in range(len(rows)):
            for t in monitor.fired_rules(sensor, rows[max(0, i - window + 1): i + 1], state, config):
                fired[t.rule_fired] += 1

    reached = conn.execute(
        f"SELECT COUNT(*) FROM incidents WHERE severity IN ({','.join('?' * len(ACTIONABLE_SEVERITIES))})",
        tuple(ACTIONABLE_SEVERITIES),
    ).fetchone()[0]  # severity only ever goes up, so final severity = highest reached
    noisy = {s.sensor_id: conn.execute("SELECT COUNT(*) FROM incidents WHERE sensor_id = ?", (s.sensor_id,)).fetchone()[0]
             for s in world.sensors if s.noise_multiplier > 1}
    result = {
        "config_version": config.version, "seed": seed, "ticks": ticks, "sensors": len(world.sensors),
        "sensor_ticks": sensor_ticks, "fired": dict(fired), "chance": chance_rates(config),
        "opened_by_rule": dict(opened), "opened_by_severity": dict(opened_severity), "reached_medium_or_high": reached,
        "notifications": dict(Counter(r[0] for r in conn.execute("SELECT reason FROM notifications"))),
        "logged_firings": repo.count_rows(conn, "logged_firings"), "noisy_incidents": noisy,
    }
    conn.close()
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticks", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = ap.parse_args()
    m = measure(args.ticks, args.seed, args.config)

    per_k = 1000 / m["sensor_ticks"]
    fired, opened, chance = Counter(m["fired"]), Counter(m["opened_by_rule"]), m["chance"]
    print(f"SYNTHETIC DATA. Config {m['config_version']}. {m['sensors']} sensors x {m['ticks']} ticks, "
          f"seed {m['seed']}, no faults; {m['sensor_ticks']:,} active sensor-ticks.\n")
    print(f"{'rule':<15} {'fires/1k':>9} {'chance/1k':>10} {'incidents opened/1k':>20} {'(count)':>8}")
    for rule in RULES:
        print(f"{rule:<15} {fired[rule] * per_k:>9.2f} {chance[rule] * 1000:>10.2f} "
              f"{opened[rule] * per_k:>20.2f} {opened[rule]:>8}")
    total = sum(opened.values())
    print(f"{'all rules':<15} {'':>9} {'':>10} {total * per_k:>20.2f} {total:>8}")

    sev = Counter(m["opened_by_severity"])
    print(f"\n{'incidents':<34} {'per 1k':>7} {'count':>7}")
    for s in ("low", "medium", "high"):
        print(f"{'opened at ' + s:<34} {sev[s] * per_k:>7.2f} {sev[s]:>7}")
    reached = m["reached_medium_or_high"]
    print(f"{'reached medium or high (notify+LLM)':<34} {reached * per_k:>7.2f} {reached:>7}")
    notices = Counter(m["notifications"])
    for reason in ("new", "upgraded", "reactivated", "persistent", "escalated", "reassigned"):
        print(f"{'notifications: ' + reason:<34} {notices[reason] * per_k:>7.2f} {notices[reason]:>7}")
    total_n = sum(notices.values())
    print(f"{'notifications: all':<34} {total_n * per_k:>7.2f} {total_n:>7}")
    print(f"{'log-only firings recorded':<34} {m['logged_firings'] * per_k:>7.2f} {m['logged_firings']:>7}")
    for sid, n in m["noisy_incidents"].items():
        print(f"\nNoisy-but-healthy {sid}: {n} incidents vs {total / m['sensors']:.1f} average per sensor")


if __name__ == "__main__":
    main()
