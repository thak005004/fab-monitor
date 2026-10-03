from __future__ import annotations

from dataclasses import dataclass

import pytest

from config_loader import load_config
from db.connection import connect, init_schema
from events.adapters import default_adapters
from events.bus import Bus
from orchestrator import Orchestrator
from sim.clock import Clock
from sim.faults import NoisyHealthy, default_faults
from sim.seed import World, build_world, seed_database
from sim.simulator import Simulator

SEED = 1234
TOTAL_TICKS = 450
WARMUP = 150  # > baseline_window_ticks (120), so every baseline activates


@dataclass
class Run:
    conn: object
    world: World
    clock: Clock
    sim: Simulator
    records: list[dict]


def make_run(db_path, seed: int = SEED, ticks: int = TOTAL_TICKS) -> Run:
    conn = connect(db_path)
    init_schema(conn)
    world = build_world(seed=seed, total_ticks=TOTAL_TICKS)
    clock = Clock()
    seed_database(conn, world, clock)
    sim = Simulator(conn, world, clock, faults=default_faults(world, WARMUP), warmup_ticks=WARMUP)
    records = sim.run(ticks)
    return Run(conn, world, clock, sim, records)


@pytest.fixture
def run(tmp_path) -> Run:
    r = make_run(tmp_path / "fab.db")
    yield r
    r.conn.close()


def readings(records, sensor_id=None):
    return [
        r for r in records
        if r["event_type"] == "reading" and (sensor_id is None or r["payload"]["sensor_id"] == sensor_id)
    ]


# ----- full pipeline (sim -> bus -> orchestrator) ----------------------------

@dataclass
class System:
    conn: object
    world: World
    clock: Clock
    sim: Simulator
    bus: Bus
    config: object
    _custom_seq: int = 0

    def run(self, n_ticks: int) -> None:
        for _ in range(n_ticks):
            self.bus.publish_tick(self.sim.step())

    def run_until(self, tick: int) -> None:
        self.run(tick - self.clock.tick)

    def step_with(self, overrides: dict[str, float] | None = None, extra: list[dict] | None = None) -> list[dict]:
        """One simulator tick with some readings' values replaced and extra
        records inserted before the tick record. Returns the records sent."""
        records = self.sim.step()
        for r in records:
            if r["event_type"] == "reading" and r["payload"]["sensor_id"] in (overrides or {}):
                r["payload"]["value"] = overrides[r["payload"]["sensor_id"]]
        records[-1:-1] = extra or []
        self.bus.publish_tick(records)
        return records

    def recipe_change(self, tool_id: str, recipe_id: str = "R-NEW") -> dict:
        self._custom_seq += 1
        return {
            "event_type": "recipe_change", "source": "recipe", "tool_id": tool_id,
            "ts": self.clock.iso_at(self.clock.tick + 1),
            "payload": {"change_id": f"RC-{self._custom_seq:04d}", "recipe_id": recipe_id},
        }

    def notifications(self, sensor_id: str) -> list[tuple[int, dict]]:
        """(tick, notification row + incident fields) for every notification on this sensor, oldest first."""
        rows = self.conn.execute(
            "SELECT n.*, i.sensor_id, i.severity, i.rule_fired FROM notifications n"
            " JOIN incidents i USING (incident_id) WHERE i.sensor_id = ? ORDER BY n.sent_at, n.notification_id",
            (sensor_id,),
        ).fetchall()
        return [(self.clock.tick_of(r["sent_at"]), dict(r)) for r in rows]

    def alert_action(self, incident_id: str, action: str, person_id: str, reason: str | None = None) -> dict:
        """A dashboard ALERT_ACTION record for the next tick."""
        payload = {"incident_id": incident_id, "action": action, "person_id": person_id}
        if reason:
            payload["reason"] = reason
        return {"event_type": "alert_action", "source": "dashboard", "tool_id": None,
                "ts": self.clock.iso_at(self.clock.tick + 1), "payload": payload}

    def availability(self, person_id: str, available: bool) -> dict:
        """A PERSON_AVAILABILITY record for the next tick."""
        return {"event_type": "person_availability", "source": "people", "tool_id": None,
                "ts": self.clock.iso_at(self.clock.tick + 1),
                "payload": {"person_id": person_id, "available": available}}

    def maintenance(self, tool_id: str, log_id: str, description: str, ticks_ahead: int = 1) -> dict:
        return {"event_type": "maintenance", "source": "maintenance", "tool_id": tool_id,
                "ts": self.clock.iso_at(self.clock.tick + ticks_ahead),
                "payload": {"log_id": log_id, "description": description}}

    def sensor_state(self, sensor_id: str):
        return self.conn.execute("SELECT * FROM sensor_state WHERE sensor_id = ?", (sensor_id,)).fetchone()

    def incidents(self, sensor_id: str | None = None) -> list:
        q, args = "SELECT * FROM incidents", ()
        if sensor_id:
            q, args = q + " WHERE sensor_id = ?", (sensor_id,)
        return self.conn.execute(q + " ORDER BY incident_id", args).fetchall()


def make_system(db_path, seed: int = SEED, total_ticks: int = 500, warm_up: bool = True, faults=(), llm=None) -> System:
    """Seeded world + pipeline. Fault 4's noisy sensor is labelled from tick 0;
    `faults` are injected after the warm-up (when warm_up is True)."""
    config = load_config()
    conn = connect(db_path)
    init_schema(conn)
    world = build_world(seed=seed, total_ticks=total_ticks)
    clock = Clock()
    seed_database(conn, world, clock, baseline_window_ticks=config.baseline_window_ticks)
    noisy = [NoisyHealthy(s.sensor_id) for s in world.sensors if s.noise_multiplier > 1]
    sim = Simulator(conn, world, clock, faults=noisy, warmup_ticks=WARMUP)
    bus = Bus(conn, Orchestrator(conn, clock, config, llm=llm), clock, default_adapters(conn))
    system = System(conn, world, clock, sim, bus, config)
    if warm_up:
        system.run_until(WARMUP)
    for f in faults:
        sim.inject(f)
    return system


@pytest.fixture
def system(tmp_path) -> System:
    s = make_system(tmp_path / "sys.db")
    yield s
    s.conn.close()
