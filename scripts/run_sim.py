"""Run the simulator through the full pipeline and print the incidents.

ALL DATA IS SYNTHETIC.

    .venv/bin/python scripts/run_sim.py [--db sim.db] [--seed 42] [--ticks 420]
                                        [--config config.json] [--warmup 150]

Seeds a fresh database, runs the warm-up through the bus, injects faults 1-4,
runs long enough for the gradual drift to leave spec, then prints each
incident, the dead-letter count, and (from ground truth, which the system
under test never reads) when each injected fault was caught.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so `python scripts/run_sim.py` finds the packages

from config_loader import ACTIONABLE_SEVERITIES, DEFAULT_CONFIG_PATH, load_config  # noqa: E402
from incidents import CATEGORIES, RULE_CATEGORY  # noqa: E402
from db import repo  # noqa: E402
from db.connection import connect, init_schema  # noqa: E402
from events.adapters import default_adapters  # noqa: E402
from events.bus import Bus  # noqa: E402
from orchestrator import Orchestrator  # noqa: E402
from sim.clock import Clock  # noqa: E402
from sim.faults import Dropout, NoisyHealthy, default_faults  # noqa: E402
from sim.seed import build_world, seed_database  # noqa: E402
from sim.simulator import DEFAULT_WARMUP_TICKS, Simulator  # noqa: E402

# Drift starts at warm-up + 40 and leaves spec (5.5 sigma at 0.05 sigma/tick)
# about 110 ticks later; this leaves room after that.
EXTRA_TICKS_AFTER_WARMUP = 270


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="sim.db")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP_TICKS)
    ap.add_argument("--ticks", type=int, default=None, help="default: warm-up + 270")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = ap.parse_args()
    ticks = args.ticks or args.warmup + EXTRA_TICKS_AFTER_WARMUP

    config = load_config(args.config)
    if args.warmup <= config.baseline_window_ticks:
        sys.exit(f"--warmup {args.warmup} must exceed baseline_window_ticks {config.baseline_window_ticks}")

    db_path = Path(args.db)
    for p in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm")):
        p.unlink(missing_ok=True)  # fresh database every run

    conn = connect(db_path)
    init_schema(conn)
    clock = Clock()
    world = build_world(seed=args.seed, total_ticks=ticks)
    seed_database(conn, world, clock, baseline_window_ticks=config.baseline_window_ticks)

    faults = default_faults(world, args.warmup)
    # Fault 4's extra noise must be there from the first reading so the
    # baseline learns it; faults 1-3 are injected after the warm-up.
    sim = Simulator(conn, world, clock, faults=[f for f in faults if isinstance(f, NoisyHealthy)],
                    warmup_ticks=args.warmup)
    orch = Orchestrator(conn, clock, config)  # no LLM client: diagnoses are recorded as unavailable
    bus = Bus(conn, orch, clock, default_adapters(conn))

    while sim.in_warmup:
        bus.publish_tick(sim.step())
    statuses = {r["baseline_status"] for r in repo.all_sensor_states(conn)}
    if statuses != {"active"}:
        sys.exit(f"Warm-up ended at tick {clock.tick} with baselines {sorted(statuses)}; lengthen --warmup")
    print(f"Warm-up done at tick {clock.tick}: every baseline active (config {config.version})")

    injected = []
    for f in faults:
        if not isinstance(f, NoisyHealthy):
            injected.append((sim.inject(f), f))
            print(f"Injected {injected[-1][0]}: {f.fault_type} on {f.sensor_id} at tick {f.start_tick}")

    # First tick each incident was at medium or higher, plus per-tick snapshots
    # of active incidents on the faulted sensors (for the ground-truth report).
    first_actionable: dict[str, int] = {}
    snapshots: list[tuple[int, list]] = []
    fault_sensors = sorted({f.sensor_id for _, f in injected})
    while clock.tick < ticks:
        bus.publish_tick(sim.step())
        for inc in conn.execute("SELECT incident_id, severity FROM incidents WHERE status != 'expired'"):
            if inc["severity"] in ACTIONABLE_SEVERITIES:
                first_actionable.setdefault(inc["incident_id"], clock.tick)
        snapshots.append((clock.tick, [dict(r) for r in conn.execute(
            "SELECT * FROM incidents WHERE status IN ('open', 'acknowledged', 'hold_confirmed')"
            f" AND sensor_id IN ({','.join('?' * len(fault_sensors))})", fault_sensors)]))

    print(f"\nSYNTHETIC DATA. Ran {clock.tick} ticks, seed {args.seed}.\n")
    header = (f"{'incident':<9} {'sensor':<10} {'rule':<15} {'sev':<6} {'onset':>5} {'opened':>6} "
              f"{'med+':>5} {'status':<9} lots at risk")
    print(header)
    print("-" * len(header))
    for inc in conn.execute("SELECT * FROM incidents ORDER BY opened_at, incident_id"):
        lots = json.loads(inc["lots_at_risk"] or "[]")
        med = first_actionable.get(inc["incident_id"])
        print(
            f"{inc['incident_id']:<9} {inc['sensor_id']:<10} {inc['rule_fired']:<15} {inc['severity']:<6} "
            f"{clock.tick_of(inc['onset_ts']):>5} {clock.tick_of(inc['opened_at']):>6} {med if med else '-':>5} "
            f"{inc['status']:<9} {', '.join(lots) if lots else '-'}"
        )
    print(f"\nLogged (log-only) firings: {repo.count_rows(conn, 'logged_firings')}")
    print(f"Dead-letter records: {repo.count_rows(conn, 'dead_letter')}")

    print("\nGround truth: when each injected fault was caught (ticks after fault start in brackets)")
    for fid, f in injected:
        # Caught = the first tick, from the fault's start, on which an active incident in
        # the fault's category got a trigger. That may be an incident that was already
        # open (e.g. a false alarm), which the fault's triggers then merge into.
        rules = CATEGORIES[RULE_CATEGORY["dropout" if isinstance(f, Dropout) else "sustained_run"]]
        caught = next(
            ((t, r) for t, rows in snapshots if t >= f.start_tick for r in rows
             if r["sensor_id"] == f.sensor_id and r["rule_fired"] in rules and r["updated_at"] == clock.iso_at(t)),
            None,
        )
        oos = conn.execute(
            "SELECT MIN(r.ts) FROM readings r JOIN sensors s USING (sensor_id) WHERE r.sensor_id = ? AND r.ts >= ?"
            " AND (r.value > s.spec_upper OR r.value < s.spec_lower)",
            (f.sensor_id, clock.iso_at(f.start_tick)),
        ).fetchone()[0]
        if caught is None:
            print(f"  {fid} {f.fault_type:<14} start {f.start_tick}, not caught")
            continue
        opened, inc = caught
        med = next((t for t, rows in snapshots if t >= opened for r in rows
                    if r["incident_id"] == inc["incident_id"] and r["severity"] in ACTIONABLE_SEVERITIES), None)
        prior = clock.tick_of(inc["opened_at"]) < f.start_tick
        how = (f"merged into {inc['incident_id']} (open since tick {clock.tick_of(inc['opened_at'])})"
               if prior else f"new incident {inc['incident_id']}")
        line = (f"  {fid} {f.fault_type:<14} start {f.start_tick}, caught {opened} [+{opened - f.start_tick}] {how}"
                f", medium+ {f'{med} [+{med - f.start_tick}]' if med else 'never'}")
        row = conn.execute(
            "SELECT n.sent_at, n.reason FROM notifications n JOIN incidents i USING (incident_id)"
            " WHERE i.sensor_id = ? AND n.sent_at >= ?"
            " AND n.reason IN ('new', 'upgraded', 'reactivated', 'persistent')"  # §17: not escalations/reassignments
            " ORDER BY n.sent_at, n.notification_id LIMIT 1",
            (f.sensor_id, clock.iso_at(f.start_tick)),
        ).fetchone()
        notice = (clock.tick_of(row["sent_at"]), row["reason"]) if row else None
        line += (f", first notification {notice[0]} [+{notice[0] - f.start_tick}] ({notice[1]})"
                 if notice else ", never notified")
        if oos:
            o = clock.tick_of(oos)
            line += f", notified {'BEFORE' if notice and notice[0] < o else 'NOT before'} out-of-spec"
            line += f", first out-of-spec {o}: lead time {o - opened} from opening"
            line += f", {o - med} from medium+" if med else ""
        else:
            line += ", never out of spec"
        print(line)
    conn.close()


if __name__ == "__main__":
    main()
