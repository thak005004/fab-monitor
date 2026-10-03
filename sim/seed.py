"""Builds the default synthetic world and writes its reference data to the DB.

All data is synthetic.

Two steps, kept separate so the simulator can use the same world without
touching the DB:
  build_world(seed)          -> World (pure, deterministic)
  seed_database(conn, world) -> writes tools, sensors, people, qualifications,
                                initial tool_state / sensor_state, and lots.

Healthy means and noise levels live only on the World (the schema has no
column for them). The system under test has to learn them from readings.

The warm-up is not run here. Its ticks go through the event bus at startup so
baselines are learned through the normal pipeline (spec §6).
"""

from __future__ import annotations

import random
import sqlite3
from dataclasses import dataclass

from db import repo
from db.connection import transaction
from sim.clock import Clock

ONCALL_PERSON_ID = "P-ONCALL"

# Spec limits sit this many healthy stddevs from the healthy mean, using each
# sensor's own noise level (so the noisy-but-healthy sensor gets wider limits).
# At 5.5 sigma a healthy reading leaves spec with probability ~4e-8.
SPEC_HALF_WIDTH_SIGMAS = 5.5

# Fault 4 (noisy but healthy): sensor_id -> noise multiplier, built into the
# world so the noise is present from the first reading and learned in baseline.
DEFAULT_NOISY_SENSORS = {"S-04-TEMP": 2.0}

# sensor_type -> (unit, nominal mean, +/- spread of mean across tools, nominal healthy stddev)
SENSOR_TYPES: dict[str, tuple[str, float, float, float]] = {
    "temperature": ("C", 60.0, 10.0, 0.5),
    "pressure": ("mTorr", 50.0, 10.0, 0.4),
    "rf_power": ("W", 500.0, 100.0, 4.0),
}
SENSOR_ABBREV = {"temperature": "TEMP", "pressure": "PRES", "rf_power": "RF"}
TOOL_KINDS = ("etch", "deposition")
PERSON_NAMES = (
    "Avery Lin", "Jordan Okafor", "Sam Reyes", "Riley Novak",
    "Casey Iyer", "Morgan Haddad", "Quinn Larsen", "Drew Moreau",
)

LOT_MIN_TICKS, LOT_MAX_TICKS = 20, 45
LOT_MAX_GAP_TICKS = 5


@dataclass(frozen=True)
class ToolSpec:
    tool_id: str
    name: str
    kind: str
    initial_recipe_id: str


@dataclass(frozen=True)
class SensorProfile:
    sensor_id: str
    tool_id: str
    sensor_type: str
    unit: str
    healthy_mean: float
    healthy_stddev: float          # actual noise level, including noise_multiplier
    noise_multiplier: float        # > 1 only for a noisy-but-healthy sensor
    spec_lower: float
    spec_upper: float


@dataclass(frozen=True)
class PersonSpec:
    person_id: str
    name: str


@dataclass(frozen=True)
class LotSpec:
    lot_id: str
    tool_id: str
    start_tick: int
    end_tick: int


@dataclass(frozen=True)
class World:
    seed: int
    total_ticks: int
    tools: tuple[ToolSpec, ...]
    sensors: tuple[SensorProfile, ...]
    people: tuple[PersonSpec, ...]
    qualifications: tuple[tuple[str, str], ...]  # (tool_id, person_id)
    lots: tuple[LotSpec, ...]

    def sensor(self, sensor_id: str) -> SensorProfile:
        for s in self.sensors:
            if s.sensor_id == sensor_id:
                return s
        raise KeyError(sensor_id)

    def qualified_people(self, tool_id: str) -> list[str]:
        return sorted(p for t, p in self.qualifications if t == tool_id)


def build_world(
    seed: int = 42,
    n_tools: int = 5,
    sensors_per_tool: int = 3,
    n_people: int = 6,
    total_ticks: int = 300,
    noisy_sensors: dict[str, float] | None = None,
) -> World:
    """noisy_sensors defaults to DEFAULT_NOISY_SENSORS when those sensors exist."""
    if not 1 <= sensors_per_tool <= len(SENSOR_TYPES):
        raise ValueError(f"sensors_per_tool must be 1..{len(SENSOR_TYPES)}")
    if not 2 <= n_people <= len(PERSON_NAMES):
        raise ValueError(f"n_people must be 2..{len(PERSON_NAMES)} so every tool has 2 qualified people")
    if n_tools < 1 or total_ticks < 1:
        raise ValueError("n_tools and total_ticks must be positive")

    rng = random.Random(seed)

    tools = tuple(
        ToolSpec(
            tool_id=f"T-{i:02d}",
            name=f"{TOOL_KINDS[(i - 1) % len(TOOL_KINDS)].title()} Tool {i}",
            kind=TOOL_KINDS[(i - 1) % len(TOOL_KINDS)],
            initial_recipe_id=f"R-{i:02d}-A",
        )
        for i in range(1, n_tools + 1)
    )

    sensor_types = list(SENSOR_TYPES)[:sensors_per_tool]
    all_ids = {f"S-{t.tool_id[2:]}-{SENSOR_ABBREV[st]}" for t in tools for st in sensor_types}
    if noisy_sensors is None:
        noisy_sensors = {k: v for k, v in DEFAULT_NOISY_SENSORS.items() if k in all_ids}
    unknown = set(noisy_sensors) - all_ids
    if unknown:
        raise ValueError(f"unknown noisy sensors: {sorted(unknown)}")
    if any(m <= 1 for m in noisy_sensors.values()):
        raise ValueError("noise multipliers must be > 1")

    sensors = []
    for tool in tools:
        for stype in sensor_types:
            unit, nominal, spread, nominal_stddev = SENSOR_TYPES[stype]
            sensor_id = f"S-{tool.tool_id[2:]}-{SENSOR_ABBREV[stype]}"
            mult = noisy_sensors.get(sensor_id, 1.0)
            stddev = nominal_stddev * mult
            mean = round(nominal + rng.uniform(-spread, spread), 2)
            half = SPEC_HALF_WIDTH_SIGMAS * stddev
            sensors.append(
                SensorProfile(
                    sensor_id=sensor_id,
                    tool_id=tool.tool_id,
                    sensor_type=stype,
                    unit=unit,
                    healthy_mean=mean,
                    healthy_stddev=stddev,
                    noise_multiplier=mult,
                    spec_lower=round(mean - half, 4),
                    spec_upper=round(mean + half, 4),
                )
            )

    people = tuple(PersonSpec(f"P-{i:02d}", PERSON_NAMES[i - 1]) for i in range(1, n_people + 1))
    oncall = PersonSpec(ONCALL_PERSON_ID, "On-Call Engineer")

    # Two fixed people per tool (rotating, so everyone is qualified somewhere),
    # plus sometimes a third at random. The on-call person is deliberately not
    # qualified on any tool: they are the fallback when nobody qualified is left.
    quals: set[tuple[str, str]] = set()
    for idx, tool in enumerate(tools):
        quals.add((tool.tool_id, people[idx % n_people].person_id))
        quals.add((tool.tool_id, people[(idx + 1) % n_people].person_id))
        if rng.random() < 0.5:
            quals.add((tool.tool_id, rng.choice(people).person_id))

    # Back-to-back lots with small idle gaps, covering the whole timeline.
    lots = []
    for tool in tools:
        tick = rng.randint(0, LOT_MAX_GAP_TICKS)
        while tick <= total_ticks:
            end = tick + rng.randint(LOT_MIN_TICKS, LOT_MAX_TICKS)
            lots.append((tool.tool_id, tick, end))
            tick = end + rng.randint(0, LOT_MAX_GAP_TICKS)
    lots.sort(key=lambda lot: (lot[1], lot[0]))
    lot_specs = tuple(
        LotSpec(f"LOT-{n:04d}", tool_id, start, end) for n, (tool_id, start, end) in enumerate(lots, start=1)
    )

    return World(
        seed=seed,
        total_ticks=total_ticks,
        tools=tools,
        sensors=tuple(sensors),
        people=people + (oncall,),
        qualifications=tuple(sorted(quals)),
        lots=lot_specs,
    )


def seed_database(
    conn: sqlite3.Connection,
    world: World,
    clock: Clock,
    baseline_window_ticks: int = 30,
) -> None:
    """Write the world's reference data, initial state rows, and lots in one transaction."""
    window_start = clock.now_iso()
    window_end = clock.iso_at(clock.tick + baseline_window_ticks)

    with transaction(conn):
        for tool in world.tools:
            repo.insert_tool(conn, tool.tool_id, tool.name, tool.kind)
            repo.insert_tool_state(conn, tool.tool_id, "healthy", tool.initial_recipe_id)
        for s in world.sensors:
            repo.insert_sensor(conn, s.sensor_id, s.tool_id, s.sensor_type, s.unit, s.spec_lower, s.spec_upper)
            repo.insert_sensor_state(conn, s.sensor_id, "learning", window_start, window_end)
        for p in world.people:
            repo.insert_person(conn, p.person_id, p.name)
        for tool_id, person_id in world.qualifications:
            repo.insert_qualification(conn, tool_id, person_id)
        for lot in world.lots:
            repo.insert_lot(conn, lot.lot_id, lot.tool_id, clock.iso_at(lot.start_tick), clock.iso_at(lot.end_tick))
