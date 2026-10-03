"""Frozen vs rolling baseline on a gradual drift (backs the claim in spec §6).

ALL DATA IS SYNTHETIC.

    .venv/bin/python scripts/rolling_vs_frozen.py [--runs 200] [--rate 0.05] [--config config.json]

Each run: a baseline_window_ticks-point healthy baseline (unit noise), then a
linear drift of `rate` sigma per tick. "frozen" learns limits once from the
baseline; "rolling" recomputes them every tick from the previous
baseline_window_ticks readings. Reports the share of runs detected within
300 ticks and the median detection delay, using the monitor's own rules.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.monitor import fired_rules  # noqa: E402
from config_loader import ACTIONABLE_SEVERITIES, DEFAULT_CONFIG_PATH, load_config  # noqa: E402

MAX_TICKS = 300
SENSOR = {"sensor_id": "S", "tool_id": "T", "spec_lower": -1e9, "spec_upper": 1e9}  # control rules only


def first_detection(config, rate, rolling, seed):
    """(first incident-opening control rule tick, first medium+ tick), or None for each."""
    rng = random.Random(seed)
    n = config.baseline_window_ticks
    history = [rng.gauss(0, 1) for _ in range(n)]
    frozen = (statistics.fmean(history), statistics.stdev(history))
    window = max(config.run_length, config.trend_length)
    recent, any_tick, med_tick = [], None, None
    for k in range(1, MAX_TICKS + 1):
        mean, sd = (statistics.fmean(history[-n:]), statistics.stdev(history[-n:])) if rolling else frozen
        v = rate * k + rng.gauss(0, 1)
        history.append(v)
        recent = (recent + [{"reading_id": str(k), "value": v, "ts": f"{k:05d}"}])[-window:]
        state = {"baseline_status": "active", "control_mean": mean, "control_stddev": sd}
        fired = [t for t in fired_rules(SENSOR, recent, state, config) if not config.is_log_only(t.rule_fired)]
        if fired and any_tick is None:
            any_tick = k
        if med_tick is None and any(config.severity_of(t.rule_fired) in ACTIONABLE_SEVERITIES for t in fired):
            med_tick = k
        if any_tick and med_tick:
            break
    return any_tick, med_tick


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=200)
    ap.add_argument("--rate", type=float, default=0.05)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = ap.parse_args()
    config = load_config(args.config)
    print(f"SYNTHETIC DATA. Config {config.version}: {config.baseline_window_ticks}-point baseline, "
          f"run_length {config.run_length}, drift {args.rate} sigma/tick, {args.runs} runs.\n")
    print(f"{'baseline':<9} {'detection':<28} {'detected':>9} {'median delay':>13}")
    for rolling in (False, True):
        results = [first_detection(config, args.rate, rolling, s) for s in range(args.runs)]
        for i, label in enumerate(("any incident (low or higher)", "medium or higher")):
            hits = [r[i] for r in results if r[i] is not None]
            med = f"{statistics.median(hits):.0f} ticks" if hits else "-"
            print(f"{'rolling' if rolling else 'frozen':<9} {label:<28} {100 * len(hits) / args.runs:>8.0f}% {med:>13}")


if __name__ == "__main__":
    main()
