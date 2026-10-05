"""Time the "latest readings for one sensor" query with and without its index.
ALL DATA IS SYNTHETIC.

    .venv/bin/python scripts/index_benchmark.py [--ticks 10000] [--repeats 2000]

Builds a new temporary database with the project schema and the seeded
reference data, fills readings with one reading per sensor per minute (15
sensors x 10,000 minutes = 150,000 rows), then times the exact query the
orchestrator runs on every incoming reading (db.repo.recent_readings): the
sensor's most recent run_length readings since its baseline became active.
It is timed with idx_readings_sensor_ts on (sensor_id, ts), then with that index
dropped, and SQLite's EXPLAIN QUERY PLAN is shown for both.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_loader import load_config  # noqa: E402
from db.connection import connect, init_schema  # noqa: E402
from sim.clock import Clock  # noqa: E402
from sim.seed import build_world, seed_database  # noqa: E402

# Same SQL as db.repo.recent_readings.
QUERY = ("SELECT reading_id, value, ts FROM readings WHERE sensor_id = ? AND ts > ?"
         " ORDER BY ts DESC, reading_id DESC LIMIT ?")
INDEX = "idx_readings_sensor_ts"


def build(db_path: Path, ticks: int) -> tuple:
    conn = connect(db_path)
    init_schema(conn)
    clock = Clock()
    world = build_world(seed=42, total_ticks=ticks)
    seed_database(conn, world, clock, baseline_window_ticks=load_config().baseline_window_ticks)
    rng = random.Random(42)
    rows, n = [], 0
    for tick in range(1, ticks + 1):
        ts = clock.iso_at(tick)
        for s in world.sensors:
            n += 1
            rows.append((f"RD-{n:06d}", s.sensor_id, s.tool_id, round(rng.gauss(s.healthy_mean, s.healthy_stddev), 4), ts))
    with conn:
        conn.executemany("INSERT INTO readings (reading_id, sensor_id, tool_id, value, ts) VALUES (?, ?, ?, ?, ?)", rows)
    conn.execute("ANALYZE")
    return conn, clock, [s.sensor_id for s in world.sensors]


def plan(conn, params) -> list[str]:
    return [r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + QUERY, params)]


def time_query(conn, params_list, repeats: int) -> list[float]:
    """Milliseconds per query, one sample per call, after a warm-up pass."""
    for p in params_list[:50]:
        conn.execute(QUERY, p).fetchall()
    samples = []
    for i in range(repeats):
        p = params_list[i % len(params_list)]
        t0 = time.perf_counter()
        conn.execute(QUERY, p).fetchall()
        samples.append((time.perf_counter() - t0) * 1000)
    return samples


def stats(samples: list[float]) -> dict:
    q = statistics.quantiles(samples, n=20)
    return {"median_ms": statistics.median(samples), "p5_ms": q[0], "p95_ms": q[-1], "n": len(samples)}


def run(ticks: int = 10000, repeats: int = 2000, workdir: Path | None = None) -> dict:
    workdir = Path(workdir or tempfile.mkdtemp(prefix="index-bench-"))
    conn, clock, sensor_ids = build(workdir / "bench.db", ticks)
    count = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
    limit = load_config().run_length
    # As in the live system: since the baseline became active (minute 120 here), latest `limit` readings.
    after = clock.iso_at(120)
    params_list = [(sid, after, limit) for sid in sensor_ids]

    with_index = {"plan": plan(conn, params_list[0]), **stats(time_query(conn, params_list, repeats))}
    conn.execute(f"DROP INDEX {INDEX}")
    conn.execute("ANALYZE")
    without_repeats = max(50, repeats // 10)  # each unindexed query scans the table; fewer samples still give a stable median
    without_index = {"plan": plan(conn, params_list[0]), **stats(time_query(conn, params_list, without_repeats))}
    conn.close()
    return {"readings": count, "sensors": len(sensor_ids), "limit": limit,
            "with_index": with_index, "without_index": without_index,
            "speedup": without_index["median_ms"] / with_index["median_ms"]}


def report(r: dict) -> str:
    lines = [f"{r['readings']:,} readings across {r['sensors']} sensors. Query: one sensor's latest {r['limit']} readings."]
    for label, key in (("With the (sensor_id, ts) index", "with_index"), ("Index dropped", "without_index")):
        x = r[key]
        lines.append(f"{label}: median {x['median_ms']:.4f} ms (5th-95th percentile {x['p5_ms']:.4f}-{x['p95_ms']:.4f} ms, "
                     f"{x['n']} runs)")
        lines += [f"    EXPLAIN QUERY PLAN: {p}" for p in x["plan"]]
    lines.append(f"Without the index the query is {r['speedup']:,.0f}x slower (median).")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ticks", type=int, default=10000)
    ap.add_argument("--repeats", type=int, default=2000)
    args = ap.parse_args()
    print(report(run(args.ticks, args.repeats)))


if __name__ == "__main__":
    main()
