"""Rebuild a database from its event log and check it matches. ALL DATA IS SYNTHETIC.

    .venv/bin/python scripts/replay_events.py [--seed 42] [--ticks 254]

1. Runs the demo scenario (the drift on T-01) with the scripted fake LLM client
   and a fixed seed, including every kind of event a person can send from the
   dashboard: acknowledge, recipe change, malformed record, availability change,
   confirm hold.
2. Replays that database's events table, in event_id order, into a brand-new
   empty database: same seed (so the same reference data and lots), same
   config, same fake client, no simulator. Each event goes through the bus and
   orchestrator exactly like a live one, with the clock moved to its timestamp.
3. Compares the derived tables row by row and reports any differences.

Read-only on the original database; the rebuilt one is a new temporary file.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
from dataclasses import dataclass, field
from itertools import groupby
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.diagnosis.client import FakeClient  # noqa: E402
from config_loader import Config, load_config  # noqa: E402
from db.connection import connect, init_schema  # noqa: E402
from events.adapters import default_adapters  # noqa: E402
from events.bus import Bus  # noqa: E402
from orchestrator import Orchestrator, build_system  # noqa: E402
from sim.clock import Clock  # noqa: E402
from sim.faults import demo_faults  # noqa: E402
from sim.seed import build_world, seed_database  # noqa: E402
from sim.simulator import DEFAULT_WARMUP_TICKS  # noqa: E402

# Tables built from events by the system (not reference data, not the event log itself).
DERIVED = {  # table: primary key columns
    "tool_state": ("tool_id",),
    "sensor_state": ("sensor_id",),
    "baseline_windows": ("sensor_id", "activated_at"),
    "incidents": ("incident_id",),
    "notifications": ("notification_id",),
    "lots": ("lot_id",),
}
TOTAL_TICKS = 3000  # how far ahead the seeded lots reach; must match between the two databases
FAKE_SCRIPT = "valid"


def run_original(db_path: Path, seed: int, end_tick: int, config: Config) -> sqlite3.Connection:
    """The demo scenario, driven to end_tick with every kind of dashboard event."""
    s = build_system(db_path, seed=seed, config=config, llm=FakeClient(FAKE_SCRIPT), total_ticks=TOTAL_TICKS,
                     faults_after_warmup=demo_faults(DEFAULT_WARMUP_TICKS))
    def advance_to(tick: int) -> None:
        if min(tick, end_tick) > s.clock.tick:
            s.advance(min(tick, end_tick) - s.clock.tick)

    advance_to(200)
    if s.clock.tick == 200:  # the demo's actions at minute 200
        s.alert_action("INC-0008", "acknowledge", "P-01")
        s.recipe_change("T-05", "R-05-B")
        s.send_malformed()  # goes to dead_letter, never into events
        s.set_availability("P-02", False)
    advance_to(254)
    if s.clock.tick == 254:
        s.alert_action("INC-0008", "confirm_hold", "P-01")
        s.alert_action("INC-0008", "acknowledge", "P-01")  # invalid on purpose: rejected, but still an event
    advance_to(end_tick)
    return s.conn


def replay(source: sqlite3.Connection, db_path: Path, seed: int, config: Config) -> sqlite3.Connection:
    """A fresh database with the same reference data, fed only the source's events."""
    conn = connect(db_path)
    init_schema(conn)
    clock = Clock()
    seed_database(conn, build_world(seed=seed, total_ticks=TOTAL_TICKS), clock,
                  baseline_window_ticks=config.baseline_window_ticks)
    bus = Bus(conn, Orchestrator(conn, clock, config, llm=FakeClient(FAKE_SCRIPT)), clock, default_adapters(conn))
    events = source.execute("SELECT event_type, source, tool_id, payload, ts FROM events ORDER BY event_id")
    # Events with the same timestamp share one transaction, like one live tick.
    for ts, group in groupby(events, key=lambda e: e["ts"]):
        tick = clock.tick_of(ts)
        if tick > clock.tick:
            clock.advance(tick - clock.tick)
        records = [{"event_type": e["event_type"], "source": e["source"], "tool_id": e["tool_id"],
                    "ts": e["ts"], "payload": json.loads(e["payload"])} for e in group]
        bus.publish_tick(records)
    return conn


@dataclass
class TableDiff:
    table: str
    rows_original: int
    rows_rebuilt: int
    only_original: list = field(default_factory=list)
    only_rebuilt: list = field(default_factory=list)
    changed: list = field(default_factory=list)  # (key, column, original, rebuilt)

    @property
    def matches(self) -> bool:
        return not (self.only_original or self.only_rebuilt or self.changed)


def compare(a: sqlite3.Connection, b: sqlite3.Connection) -> list[TableDiff]:
    diffs = []
    for table, key in DERIVED.items():
        rows_a = {tuple(r[k] for k in key): dict(r) for r in a.execute(f"SELECT * FROM {table}")}
        rows_b = {tuple(r[k] for k in key): dict(r) for r in b.execute(f"SELECT * FROM {table}")}
        d = TableDiff(table, len(rows_a), len(rows_b),
                      sorted(rows_a.keys() - rows_b.keys()), sorted(rows_b.keys() - rows_a.keys()))
        for k in sorted(rows_a.keys() & rows_b.keys()):
            for col, va in rows_a[k].items():
                if va != rows_b[k][col]:
                    d.changed.append((k, col, va, rows_b[k][col]))
        diffs.append(d)
    return diffs


def run(seed: int = 42, end_tick: int = 254, workdir: Path | None = None) -> dict:
    workdir = Path(workdir or tempfile.mkdtemp(prefix="replay-"))
    config = load_config()
    original = run_original(workdir / "original.db", seed, end_tick, config)
    rebuilt = replay(original, workdir / "rebuilt.db", seed, config)
    diffs = compare(original, rebuilt)
    counts = {t: original.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("events", "readings", "dead_letter", "diagnoses")}
    rebuilt_events = rebuilt.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    original.close()
    rebuilt.close()
    return {"seed": seed, "end_tick": end_tick, "counts": counts, "rebuilt_events": rebuilt_events,
            "diffs": diffs, "match": all(d.matches for d in diffs)}


def report(result: dict) -> str:
    lines = [f"Seed {result['seed']}, run to minute {result['end_tick']}: {result['counts']['events']} events "
             f"({result['counts']['readings']} readings, {result['counts']['dead_letter']} rejected records, "
             f"{result['counts']['diagnoses']} diagnoses). Replayed {result['rebuilt_events']} events."]
    for d in result["diffs"]:
        status = "match" if d.matches else "DIFFERENT"
        lines.append(f"  {d.table:17s} {d.rows_original:5d} rows original, {d.rows_rebuilt:5d} rebuilt: {status}")
        for k in d.only_original[:5]:
            lines.append(f"    only in original: {k}")
        for k in d.only_rebuilt[:5]:
            lines.append(f"    only in rebuilt: {k}")
        for k, col, va, vb in d.changed[:10]:
            lines.append(f"    {k} {col}: original {va!r}, rebuilt {vb!r}")
    lines.append("RESULT: identical" if result["match"] else "RESULT: differences found")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ticks", type=int, default=404, help="minute to run the original to (default 404)")
    args = ap.parse_args()
    result = run(args.seed, args.ticks)
    print(report(result))
    sys.exit(0 if result["match"] else 1)


if __name__ == "__main__":
    main()
